import test from "node:test";
import assert from "node:assert/strict";
import {
  fetchAdapterModelCatalog,
  fetchAdapterModelRequirements,
  previewGithubImportBySlug,
  confirmGithubImportBySlug,
  getGithubImportBySlug,
  syncProjectFormalizationTargetsBySlug,
  parseSseFrame,
  runApiProofJob,
} from "../companion/leaApiClient.mjs";

const encoder = new TextEncoder();

function jsonResponse(obj, ok = true, status = 200) {
  return { ok, status, async text() { return JSON.stringify(obj); } };
}

// A streaming SSE response whose body is an async iterable of byte chunks.
function sseResponse(frames, { chunkSize = null } = {}) {
  const text = frames.map((f) => `${f}\n\n`).join("");
  async function* body() {
    if (chunkSize) {
      for (let i = 0; i < text.length; i += chunkSize) {
        yield encoder.encode(text.slice(i, i + chunkSize));
      }
    } else {
      yield encoder.encode(text);
    }
  }
  return { ok: true, status: 200, body: body(), async text() { return ""; } };
}

function frame(type, payload) {
  return `event: ${type}\ndata: ${JSON.stringify(payload)}`;
}

test("parseSseFrame reads the event name off the event: line, not the data body", () => {
  const { type, data } = parseSseFrame("event: code_step\ndata: {\"id\":\"s1\",\"turn\":2}");
  assert.equal(type, "code_step");
  assert.deepEqual(data, { id: "s1", turn: 2 });
});

test("parseSseFrame tolerates a missing/blank data line", () => {
  const { type, data } = parseSseFrame("event: done");
  assert.equal(type, "done");
  assert.equal(data, null);
});

test("adapter model helpers call the catalog and encoded requirements endpoints", async () => {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url: String(url), method: options.method });
    if (String(url).endsWith("/api/models")) {
      return jsonResponse({ models: [{ value: "mistral/large", provider: "mistral" }] });
    }
    return jsonResponse({
      model: "custom/model id",
      provider: "custom",
      required_keys: [{ env: "CUSTOM_API_KEY", configured: false }],
      satisfied: false
    });
  };

  const catalog = await fetchAdapterModelCatalog({ fetchImpl, baseUrl: "http://adapter" });
  const requirements = await fetchAdapterModelRequirements({
    fetchImpl,
    baseUrl: "http://adapter",
    model: "custom/model id"
  });

  assert.equal(catalog.body.models[0].value, "mistral/large");
  assert.equal(requirements.body.required_keys[0].env, "CUSTOM_API_KEY");
  assert.equal(calls[0].url, "http://adapter/api/models");
  assert.equal(calls[1].url, "http://adapter/api/models/requirements?model=custom%2Fmodel%20id");
});

test("GitHub import helpers use by-slug adapter routes and preserve targets", async () => {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url: String(url), options, body: options.body ? JSON.parse(options.body) : null });
    return jsonResponse({ id: "import-1", status: "checking" }, true, options.method === "POST" ? 202 : 200);
  };
  const context = { fetchImpl, baseUrl: "http://adapter", slug: "paper-one" };
  const targets = [{ origin_key: "paper-one:theorem:t", declaration_name: "t" }];

  await previewGithubImportBySlug({
    ...context,
    repositoryUrl: "https://github.com/owner/repo",
    targets,
    projectName: "Paper One",
    namespace: "Lea.PaperOne",
  });
  await confirmGithubImportBySlug({ ...context, previewId: "preview-1" });
  await getGithubImportBySlug({ ...context, importId: "import/1" });
  await syncProjectFormalizationTargetsBySlug({ ...context, targets });

  assert.equal(calls[0].url, "http://adapter/api/projects/by-slug/paper-one/github-imports/preview");
  assert.equal(calls[0].body.repository_url, "https://github.com/owner/repo");
  assert.deepEqual(calls[0].body.targets, targets);
  assert.equal(calls[1].body.preview_id, "preview-1");
  assert.ok(calls[2].url.endsWith("/github-imports/import%2F1"));
  assert.ok(calls[3].url.endsWith("/formalizations/sync"));
});

test("runApiProofJob: proved done → ok with usage read back from the run row", async () => {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url, method: options.method || "GET" });
    if (url.endsWith("/api/runs") && options.method === "POST") {
      return jsonResponse({ session_id: "sess-1", run_id: "run-1" });
    }
    if (url.includes("/api/runs/run-1/events")) {
      return sseResponse([
        frame("status", { status: "tool_call", message: "Running write_file", turn: 1 }),
        frame("code_step", { id: "cs1", turn: 1 }),
        frame("done", { status: "proved" }),
      ], { chunkSize: 7 });
    }
    if (url.includes("/api/sessions/sess-1")) {
      return jsonResponse({ runs: [{ id: "run-1", input_tokens: 100, output_tokens: 40, cost_usd: 0.012 }] });
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  const progress = [];
  const result = await runApiProofJob({
    fetchImpl,
    baseUrl: "http://127.0.0.1:8001",
    message: "Formalize foo",
    timeoutMs: 5000,
    onProgressUpdated: async (p) => progress.push(p),
  });

  assert.equal(result.ok, true);
  assert.equal(result.timedOut, false);
  assert.equal(result.apiRunId, "run-1");
  assert.equal(result.sessionId, "sess-1");
  assert.equal(result.doneStatus, "proved");
  assert.deepEqual(result.usage, { inputTokens: 100, outputTokens: 40, totalTokens: 140 });
  assert.equal(result.costUsd, 0.012);
  assert.ok(progress.some((p) => p.currentTurn === 1));
});

test("runApiProofJob: disproved done → ok with disproof result kind", async () => {
  const fetchImpl = async (url, options = {}) => {
    if (url.endsWith("/api/runs") && options.method === "POST") {
      return jsonResponse({ session_id: "sess-d", run_id: "run-d" });
    }
    if (url.includes("/api/runs/run-d/events")) {
      return sseResponse([frame("done", { status: "disproved", result_kind: "disproved", result_detail: "DISPROVED" })]);
    }
    if (url.includes("/api/sessions/sess-d")) {
      return jsonResponse({ runs: [{ id: "run-d", input_tokens: 1, output_tokens: 2, cost_usd: 0.003 }] });
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  const result = await runApiProofJob({
    fetchImpl,
    baseUrl: "http://127.0.0.1:8001",
    message: "Find a counterexample",
    timeoutMs: 5000,
  });

  assert.equal(result.ok, true);
  assert.equal(result.doneStatus, "disproved");
  assert.equal(result.resultKind, "disproved");
  assert.equal(result.resultDetail, "DISPROVED");
});

test("runApiProofJob: forwards origin/origin_url + project tags to POST /api/runs", async () => {
  let postBody = null;
  const fetchImpl = async (url, options = {}) => {
    if (url.endsWith("/api/runs") && options.method === "POST") {
      postBody = JSON.parse(options.body);
      return jsonResponse({ session_id: "sess-o", run_id: "run-o" });
    }
    if (url.includes("/api/runs/run-o/events")) {
      return sseResponse([frame("done", { status: "proved" })]);
    }
    if (url.includes("/api/sessions/sess-o")) {
      return jsonResponse({ runs: [{ id: "run-o", input_tokens: 0, output_tokens: 0, cost_usd: 0 }] });
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  await runApiProofJob({
    fetchImpl,
    baseUrl: "http://127.0.0.1:8001",
    message: "Formalize foo",
    timeoutMs: 5000,
    projectSlug: "doc-a",
    projectTitle: "Doc A",
    origin: "overleaf",
    originUrl: "https://www.overleaf.com/project/doc-a",
  });

  assert.equal(postBody.origin, "overleaf");
  assert.equal(postBody.origin_url, "https://www.overleaf.com/project/doc-a");
  assert.equal(postBody.project_slug, "doc-a");
});

test("runApiProofJob: forwards stable formalization identity and source provenance", async () => {
  let postBody = null;
  const fetchImpl = async (url, options = {}) => {
    if (url.endsWith("/api/runs") && options.method === "POST") {
      postBody = JSON.parse(options.body);
      return jsonResponse({
        session_id: "sess-f",
        run_id: "run-f",
        focus_formalization_id: "form-f",
      });
    }
    if (url.includes("/api/runs/run-f/events")) {
      return sseResponse([frame("done", { status: "proved" })]);
    }
    if (url.includes("/api/sessions/sess-f")) {
      return jsonResponse({ runs: [{ id: "run-f" }] });
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  const result = await runApiProofJob({
    fetchImpl,
    baseUrl: "http://127.0.0.1:8001",
    message: "Formalize stable target",
    focusFormalizationId: "form-f",
    focusSourceHash: "source-v2",
    timeoutMs: 5000,
  });

  assert.equal(postBody.focus_formalization_id, "form-f");
  assert.equal(postBody.focus_source_hash, "source-v2");
  assert.equal(result.formalizationId, "form-f");
});

test("runApiProofJob: creates an external formalization with an origin key", async () => {
  let postBody = null;
  const fetchImpl = async (url, options = {}) => {
    if (url.endsWith("/api/runs") && options.method === "POST") {
      postBody = JSON.parse(options.body);
      return jsonResponse({
        session_id: "sess-new",
        run_id: "run-new",
        formalization: { id: "form-new" },
      });
    }
    if (url.includes("/api/runs/run-new/events")) {
      return sseResponse([frame("done", { status: "proved" })]);
    }
    if (url.includes("/api/sessions/sess-new")) {
      return jsonResponse({ runs: [{ id: "run-new" }] });
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  const result = await runApiProofJob({
    fetchImpl,
    baseUrl: "http://127.0.0.1:8001",
    message: "Formalize new target",
    newFormalization: {
      display_title: "compact_image",
      declaration_name: "compact_image",
      origin: "overleaf",
      origin_key: "doc:theorem:compact_image",
      source_hash: "source-v1",
    },
    timeoutMs: 5000,
  });

  assert.equal(postBody.new_formalization.origin_key, "doc:theorem:compact_image");
  assert.equal(result.formalizationId, "form-new");
});

test("runApiProofJob: omits origin fields when not provided (interactive parity)", async () => {
  let postBody = null;
  const fetchImpl = async (url, options = {}) => {
    if (url.endsWith("/api/runs") && options.method === "POST") {
      postBody = JSON.parse(options.body);
      return jsonResponse({ session_id: "sess-n", run_id: "run-n" });
    }
    if (url.includes("/api/runs/run-n/events")) {
      return sseResponse([frame("done", { status: "proved" })]);
    }
    if (url.includes("/api/sessions/sess-n")) {
      return jsonResponse({ runs: [] });
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  await runApiProofJob({ fetchImpl, baseUrl: "http://127.0.0.1:8001", message: "go", timeoutMs: 5000 });

  assert.equal("origin" in postBody, false);
  assert.equal("origin_url" in postBody, false);
});

test("runApiProofJob: auto-approves a gated tool call so the run stays autonomous", async () => {
  const approvalCalls = [];
  const fetchImpl = async (url, options = {}) => {
    if (url.endsWith("/api/runs") && options.method === "POST") {
      return jsonResponse({ session_id: "sess-2", run_id: "run-2" });
    }
    if (url.includes("/api/runs/run-2/events")) {
      return sseResponse([
        frame("approval_requested", { approval_id: "appr-1", tool_name: "bash", args: {} }),
        frame("approval_resolved", { approval_id: "appr-1", decision: "always_session" }),
        frame("done", { status: "proved" }),
      ]);
    }
    if (url.includes("/api/runs/run-2/approvals/appr-1") && options.method === "POST") {
      approvalCalls.push(JSON.parse(options.body));
      return jsonResponse({ status: "resolved" });
    }
    if (url.includes("/api/sessions/sess-2")) {
      return jsonResponse({ runs: [{ id: "run-2", input_tokens: 0, output_tokens: 0, cost_usd: 0 }] });
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  const result = await runApiProofJob({ fetchImpl, baseUrl: "http://127.0.0.1:8001", message: "go", timeoutMs: 5000 });
  assert.equal(result.ok, true);
  assert.deepEqual(approvalCalls, [{ decision: "always_session" }]);
});

test("runApiProofJob: non-completed terminal status → not ok with a descriptive error", async () => {
  const fetchImpl = async (url, options = {}) => {
    if (url.endsWith("/api/runs") && options.method === "POST") {
      return jsonResponse({ session_id: "sess-3", run_id: "run-3" });
    }
    if (url.includes("/api/runs/run-3/events")) {
      return sseResponse([frame("done", { status: "max_turns" })]);
    }
    if (url.includes("/api/sessions/sess-3")) {
      return jsonResponse({ runs: [] });
    }
    throw new Error(`unexpected fetch ${url}`);
  };
  const result = await runApiProofJob({ fetchImpl, baseUrl: "http://127.0.0.1:8001", message: "go", timeoutMs: 5000 });
  assert.equal(result.ok, false);
  assert.match(result.error, /max_turns/);
});

test("runApiProofJob: a run_error frame surfaces as the failure reason", async () => {
  const fetchImpl = async (url, options = {}) => {
    if (url.endsWith("/api/runs") && options.method === "POST") {
      return jsonResponse({ session_id: "sess-4", run_id: "run-4" });
    }
    if (url.includes("/api/runs/run-4/events")) {
      return sseResponse([
        frame("run_error", { message: "Another Lea run is already active." }),
        frame("done", { status: "failed" }),
      ]);
    }
    if (url.includes("/api/sessions/sess-4")) {
      return jsonResponse({ runs: [] });
    }
    throw new Error(`unexpected fetch ${url}`);
  };
  const result = await runApiProofJob({ fetchImpl, baseUrl: "http://127.0.0.1:8001", message: "go", timeoutMs: 5000 });
  assert.equal(result.ok, false);
  assert.match(result.error, /already active/);
});

// --- server-side queue (Phase 2) ----------------------------------------------
// The adapter queues runs FIFO and the events endpoint is a pure observer: a
// queued run streams `queued` frames (with its position), and attach never
// races a slot. The client just watches; the only retries left are transport
// drops (next section).

test("runApiProofJob: queued frames stream through and are logged before the run starts", async () => {
  const logLines = [];
  const observed = [];
  const fetchImpl = async (url, options = {}) => {
    if (url.endsWith("/api/runs") && options.method === "POST") {
      return jsonResponse({ session_id: "sess-q", run_id: "run-q", queue_position: 1 });
    }
    if (url.includes("/api/runs/run-q/events")) {
      return sseResponse([
        frame("queued", { run_id: "run-q", position: 1 }),
        frame("status", { status: "tool_call", turn: 1 }),
        frame("done", { status: "proved" }),
      ]);
    }
    // The busy-wait now polls the cheap run-row endpoint (item 16), not session detail.
    if (url.endsWith("/api/runs/run-q")) {
      return jsonResponse({ id: "run-q", status: "pending", result_kind: null, result_detail: null });
    }
    if (url.includes("/api/sessions/sess-q")) {
      return jsonResponse({ runs: [{ id: "run-q", status: "proved", input_tokens: 5, output_tokens: 3, cost_usd: 0.001 }] });
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  const result = await runApiProofJob({
    fetchImpl,
    baseUrl: "http://127.0.0.1:8001",
    message: "Formalize queued",
    timeoutMs: 5000,
    busyRetryDelayMs: 5,
    appendLog: async (_path, line) => logLines.push(line),
    logPath: "/dev/null",
    onEvent: async (type) => observed.push(type),
  });

  assert.equal(result.ok, true);
  assert.equal(result.doneStatus, "proved");
  assert.ok(observed.includes("queued"), "queued frames reach onEvent");
  assert.ok(logLines.some((l) => l.includes("queued this run (position 1)")));
});

test("runApiProofJob: still queued at the deadline → times out and interrupts the pending run", async () => {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url, method: options.method || "GET" });
    if (url.endsWith("/api/runs") && options.method === "POST") {
      return jsonResponse({ session_id: "sess-w", run_id: "run-w" });
    }
    if (url.includes("/api/runs/run-w/interrupt")) {
      return jsonResponse({ status: "interrupted" });
    }
    if (url.includes("/api/runs/run-w/events")) {
      // The stream announces the queue position, then the connection recycles
      // without a `done` — the run is still waiting its turn.
      return sseResponse([frame("queued", { run_id: "run-w", position: 2 })]);
    }
    if (url.endsWith("/api/runs/run-w")) {
      return jsonResponse({ id: "run-w", status: "pending", result_kind: null, result_detail: null });
    }
    if (url.includes("/api/sessions/sess-w")) {
      return jsonResponse({ runs: [{ id: "run-w", status: "pending" }] });
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  const result = await runApiProofJob({
    fetchImpl,
    baseUrl: "http://127.0.0.1:8001",
    message: "Formalize starved",
    timeoutMs: 40,
    busyRetryDelayMs: 5,
  });

  assert.equal(result.ok, false);
  assert.equal(result.timedOut, true);
  assert.ok(calls.some((c) => c.url.includes("/api/runs/run-w/interrupt") && c.method === "POST"));
});

test("runApiProofJob: an HTTP rejection of the attach fails immediately (no retry)", async () => {
  let eventsCalls = 0;
  const fetchImpl = async (url, options = {}) => {
    if (url.endsWith("/api/runs") && options.method === "POST") {
      return jsonResponse({ session_id: "sess-e", run_id: "run-e" });
    }
    if (url.includes("/api/runs/run-e/events")) {
      eventsCalls += 1;
      return jsonResponse({ detail: "boom" }, false, 500);
    }
    if (url.includes("/api/sessions/sess-e")) {
      return jsonResponse({ runs: [{ id: "run-e", status: "pending" }] });
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  const result = await runApiProofJob({
    fetchImpl,
    baseUrl: "http://127.0.0.1:8001",
    message: "Formalize broken",
    timeoutMs: 5000,
    busyRetryDelayMs: 5,
  });

  assert.equal(eventsCalls, 1);
  assert.equal(result.ok, false);
  assert.match(result.error, /HTTP 500/);
});

// --- mid-stream drop re-attach (AUDIT H4) ------------------------------------
// An already-attached stream that ends WITHOUT a terminal `done` frame (a
// transport drop / adapter hiccup) does not mean the run failed — the run may
// still be executing. The client consults the run row: re-attaches while it's
// pending/running, adopts its outcome once terminal, and only gives up (and
// interrupts) when the adapter is genuinely unreachable.

test("runApiProofJob: stream drops with no done frame, run row is terminal → adopts it (no false failure)", async () => {
  let eventsCalls = 0;
  const fetchImpl = async (url, options = {}) => {
    if (url.endsWith("/api/runs") && options.method === "POST") {
      return jsonResponse({ session_id: "sess-d", run_id: "run-d" });
    }
    if (url.includes("/api/runs/run-d/events")) {
      eventsCalls += 1;
      // A stream that yields a progress frame then ends — never a `done`.
      return sseResponse([frame("status", { status: "tool_call", turn: 1 })]);
    }
    // The cheap run-row poll (item 16) sees the run finished while we were detached.
    if (url.endsWith("/api/runs/run-d")) {
      return jsonResponse({ id: "run-d", status: "proved", result_kind: "proved", result_detail: null });
    }
    if (url.includes("/api/sessions/sess-d")) {
      // Usage read-back after the run resolved.
      return jsonResponse({ runs: [{ id: "run-d", status: "proved", result_kind: "proved", input_tokens: 10, output_tokens: 4, cost_usd: 0.002 }] });
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  const result = await runApiProofJob({
    fetchImpl,
    baseUrl: "http://127.0.0.1:8001",
    message: "Formalize dropped",
    timeoutMs: 5000,
    busyRetryDelayMs: 5,
  });

  assert.equal(eventsCalls, 1);
  assert.equal(result.ok, true);
  assert.equal(result.timedOut, false);
  assert.equal(result.doneStatus, "proved");
  assert.equal(result.costUsd, 0.002);
});

test("runApiProofJob: stream drops and the adapter is unreachable → gives up after the miss cap and interrupts", async () => {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url, method: options.method || "GET" });
    if (url.endsWith("/api/runs") && options.method === "POST") {
      return jsonResponse({ session_id: "sess-u", run_id: "run-u" });
    }
    if (url.includes("/api/runs/run-u/interrupt")) {
      return jsonResponse({ status: "interrupted" });
    }
    if (url.includes("/api/runs/run-u/events")) {
      // Open itself fails — a dropped/unreachable stream with no HTTP status.
      throw new Error("socket hang up");
    }
    if (url.endsWith("/api/runs/run-u")) {
      // Run-row read also fails: the adapter is genuinely unreachable.
      throw new Error("ECONNREFUSED");
    }
    if (url.includes("/api/sessions/sess-u")) {
      // Usage read-back path is unreachable too.
      throw new Error("ECONNREFUSED");
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  const result = await runApiProofJob({
    fetchImpl,
    baseUrl: "http://127.0.0.1:8001",
    message: "Formalize unreachable",
    timeoutMs: 5000,
    busyRetryDelayMs: 3,
  });

  assert.equal(result.ok, false);
  // It stopped on the miss cap, not the timeout.
  assert.equal(result.timedOut, false);
  assert.ok(
    calls.some((c) => c.url.includes("/api/runs/run-u/interrupt") && c.method === "POST"),
    "should best-effort interrupt the run it is abandoning"
  );
  // Bounded: one attach + one row read per miss, capped at 5 misses.
  const eventAttempts = calls.filter((c) => c.url.includes("/events")).length;
  assert.ok(eventAttempts <= 5, `expected <= 5 attach attempts, got ${eventAttempts}`);
});

test("runApiProofJob: a malformed run row without status is bounded like a failed status read", async () => {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url, method: options.method || "GET" });
    if (url.endsWith("/api/runs") && options.method === "POST") {
      return jsonResponse({ session_id: "sess-m", run_id: "run-m" });
    }
    if (url.includes("/api/runs/run-m/interrupt")) {
      return jsonResponse({ status: "interrupted" });
    }
    if (url.includes("/api/runs/run-m/events")) {
      return sseResponse([frame("status", { status: "tool_call", turn: 1 })]);
    }
    if (url.endsWith("/api/runs/run-m")) {
      return jsonResponse({ id: "run-m" }); // incompatible row: no status
    }
    if (url.includes("/api/sessions/sess-m")) {
      return jsonResponse({ runs: [] });
    }
    throw new Error(`unexpected fetch ${url}`);
  };

  const result = await runApiProofJob({
    fetchImpl,
    baseUrl: "http://127.0.0.1:8001",
    message: "Formalize malformed row",
    timeoutMs: 5000,
    busyRetryDelayMs: 3,
  });

  assert.equal(result.ok, false);
  assert.equal(result.timedOut, false);
  assert.ok(calls.some((c) => c.url.includes("/interrupt")));
  assert.ok(calls.filter((c) => c.url.includes("/events")).length <= 5);
});
