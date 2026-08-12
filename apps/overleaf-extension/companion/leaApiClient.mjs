// Lea standalone adapter client for the Overleaf companion.
//
// The adapter (apps/lea-standalone/adapter, :8001) drives the prover in-process.
// A run is started with POST /api/runs, then driven and observed by opening its
// SSE event stream. Per-run config lives in config/lea.local.toml; the companion
// passes the prompt, provenance, and optional session/project metadata.

const TOOL_APPROVAL_DECISION = "always_session";

// done.status values the adapter emits (see bridge._FINISH_STATUS + run_lea):
//   proved | disproved | needs_review | answered | max_turns | cancelled | failed
// For a *formalization* job, "proved" and "disproved" are completed checked work;
// "answered" finished cleanly but proved nothing. The old "success" alias (kept
// for pre-vocabulary test doubles) was removed once the integration harness
// (tests/integration/) began asserting the real wire vocabulary.
const SUCCESS_DONE_STATUS = new Set(["proved", "disproved"]);

function toNonNegativeNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

function decodeChunk(chunk) {
  if (typeof chunk === "string") return chunk;
  if (chunk instanceof Uint8Array) return new TextDecoder().decode(chunk);
  return String(chunk);
}

async function* iterateResponseBody(body) {
  if (!body) return;
  if (body[Symbol.asyncIterator]) {
    yield* body;
    return;
  }
  if (!body.getReader) return;
  const reader = body.getReader();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) return;
      yield value;
    }
  } finally {
    reader.releaseLock?.();
  }
}

// Parse one SSE frame ("event: <type>\ndata: <json>") into { type, data }.
// The adapter encodes the event name on the `event:` line and the payload on
// `data:` lines.
export function parseSseFrame(frame) {
  let type = "message";
  const dataLines = [];
  for (const rawLine of frame.split(/\r?\n/)) {
    if (rawLine.startsWith("event:")) {
      type = rawLine.slice("event:".length).trim();
    } else if (rawLine.startsWith("data:")) {
      dataLines.push(rawLine.slice("data:".length).replace(/^ /, ""));
    }
  }
  const dataText = dataLines.join("\n").trim();
  if (!dataText) return { type, data: null };
  try {
    return { type, data: JSON.parse(dataText) };
  } catch {
    return { type, data: null };
  }
}

function buildHeaders(apiKey, extra = {}) {
  const headers = { ...extra };
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
  return headers;
}

async function fetchJson(fetchImpl, url, options) {
  let response;
  try {
    response = await fetchImpl(url, options);
  } catch (error) {
    return { ok: false, error: `Request to ${url} failed: ${error instanceof Error ? error.message : String(error)}` };
  }
  let body = null;
  const text = await response.text().catch(() => "");
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!response.ok) {
    const rawDetail = body?.detail ?? body?.error ?? text ?? `HTTP ${response.status}`;
    const detail =
      rawDetail && typeof rawDetail === "object"
        ? rawDetail.message || JSON.stringify(rawDetail)
        : rawDetail;
    return { ok: false, status: response.status, error: `HTTP ${response.status}: ${detail}`, body };
  }
  return { ok: true, status: response.status, body };
}

// --- Shared settings delegation -------------------------------------------
// The adapter's `config/lea.local.toml` (served by GET/PUT /api/settings) is the
// single source of truth for the settings shared with the lea-standalone UI:
// `model`, `max_turns`, `max_spend_usd`, and provider `api_keys`. The companion
// reads them here and writes them via `putAdapterSettings`, instead of keeping
// its own divergent copies, so a change in either UI shows up in both.

export function fetchAdapterSettings({ fetchImpl, baseUrl }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/settings`, {
    method: "GET",
    headers: buildHeaders(null),
  });
}

export function fetchAdapterModelCatalog({ fetchImpl, baseUrl }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/models`, {
    method: "GET",
    headers: buildHeaders(null),
  });
}

export function fetchAdapterModelRequirements({ fetchImpl, baseUrl, model }) {
  return fetchJson(
    fetchImpl,
    `${baseUrl}/api/models/requirements?model=${encodeURIComponent(String(model || ""))}`,
    {
      method: "GET",
      headers: buildHeaders(null),
    }
  );
}

export function putAdapterSettings({ fetchImpl, baseUrl, body }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/settings`, {
    method: "PUT",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify(body || {}),
  });
}

// The adapter's GET /api/stats (store.usage_stats) is the single source of truth
// for usage the lea-standalone Stats page renders: `global` all-time rollups and
// per-`sessions` rows (each carrying `project_slug`). The companion reads it here
// so the Overleaf popover shows the exact same numbers instead of its own
// in-memory job tally.
export function fetchAdapterUsageStats({ fetchImpl, baseUrl }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/stats`, {
    method: "GET",
    headers: buildHeaders(null),
  });
}

// Mirror the Overleaf project's .tex/.sty/.cls sources into the matching adapter project's
// `.lea/files/overleaf/` (resolved by slug, get-or-create — same slug the run uses).
// The adapter reconciles synchronously and defers the git commit, so this returns
// quickly; `files` is `[{ path, content }]`. Best-effort: a transport
// failure surfaces as `{ ok:false }` and the caller logs/ignores it.
export function mirrorProjectTexFiles({ fetchImpl, baseUrl, slug, files, mode = "reconcile" }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/mirror`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    // mode "upsert" (PLAN 3.2) writes only the provided files — the active-
    // buffer tier; "reconcile" treats the payload as the full truth set.
    body: JSON.stringify({ source: "overleaf", mode, files: files || [] }),
  });
}

// --- Export & GitHub sharing (D34, by-slug) ---------------------------------
// The adapter's by-slug routes mirror the Lea UI's by-id export/share surface but
// are keyed by the same document slug the mirror/run paths use. None of them ever
// create a project — an unknown slug is a 404 ("nothing to export yet").

// Structured artifact index (PLAN-system-hardening 4.1/4.2): which declaration
// lives in which file, as recorded by the adapter's run finalizer. The primary
// artifact-identification source; registry-markdown diffing is the fallback.
export function fetchProjectArtifactsBySlug({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/artifacts`, {
    method: "GET",
    headers: buildHeaders(null),
  });
}

// Blueprint dependency graph with live-derived status (FEATURE-overleaf-blueprint-view):
// the parsed `.lea/blueprint.md` nodes + `uses` edges, each node enriched with its
// status/verified/session attribution. The read-only viewer in the Lean pane reads this.
export function fetchProjectGraphBySlug({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/graph`, {
    method: "GET",
    headers: buildHeaders(null),
  });
}

// Populate the blueprint from the project's formalized artifacts (the "Generate from
// formalized theorems" button). Additive + idempotent on the adapter side. Returns
// { added, skipped, warnings, graph }.
export function generateProjectBlueprintBySlug({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/blueprint/generate`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: "{}",
  });
}

// Ledger-side target evidence (PLAN-system-hardening 4.4): per-declaration
// file existence / sorry scan / newest check verdict, straight from the
// adapter's own records. One of the two sources the ledger status engine
// merges (the other is the companion's job overlay).
export function fetchProjectTargetStatusBySlug({ fetchImpl, baseUrl, slug, declarations }) {
  const query = encodeURIComponent((declarations || []).join(","));
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/target-status?declarations=${query}`, {
    method: "GET",
    headers: buildHeaders(null),
  });
}

// Single-writer retirement (PLAN-system-hardening 4.5): a retry asks the adapter
// to delete the previous proof and record that change. If the retry does not
// verify, the adapter restores its SQL-owned verified snapshot (falling back to
// project Git history for legacy/manual files).
export function retireProjectArtifactBySlug({ fetchImpl, baseUrl, slug, path }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/artifacts/retire`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({ path }),
  });
}

export function restoreProjectArtifactBySlug({ fetchImpl, baseUrl, slug, path, retireCommit }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/artifacts/restore`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({ path, retire_commit: retireCommit }),
  });
}

export function fetchProjectShareStatus({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/share`, {
    method: "GET",
    headers: buildHeaders(null),
  });
}

export function fetchProjectIdentityBySlug({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/identity`, {
    method: "GET",
    headers: buildHeaders(null),
  });
}

export function previewProjectNamespace({ fetchImpl, baseUrl, projectName, namespace = null, excludeProjectId = null }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/namespace-preview`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({
      project_name: projectName,
      namespace,
      exclude_project_id: excludeProjectId
    }),
  });
}

export function updateProjectIdentityBySlug({
  fetchImpl,
  baseUrl,
  slug,
  projectName,
  mode,
  namespace = null,
  expectedNamespace = null,
  createIfMissing = false,
}) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/identity`, {
    method: "PUT",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({
      project_name: projectName,
      mode,
      namespace,
      expected_namespace: expectedNamespace,
      create_if_missing: Boolean(createIfMissing)
    }),
  });
}

export function setProjectRemoteBySlug({ fetchImpl, baseUrl, slug, remoteUrl }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/git/remote`, {
    method: "PUT",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({ remote_url: remoteUrl }),
  });
}

export function pushProjectBySlug({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/git/push`, {
    method: "POST",
    headers: buildHeaders(null),
  });
}

// Additive GitHub project import (inverse of Share): analyze, confirm, then poll.
export function previewGithubImportBySlug({
  fetchImpl,
  baseUrl,
  slug,
  repositoryUrl,
  targets = [],
  projectName = null,
  namespace = null,
}) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/github-imports/preview`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({
      repository_url: repositoryUrl,
      targets,
      project_name: projectName,
      namespace,
    }),
  });
}

export function confirmGithubImportBySlug({ fetchImpl, baseUrl, slug, previewId }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/github-imports`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({ preview_id: previewId }),
  });
}

export function getGithubImportBySlug({ fetchImpl, baseUrl, slug, importId }) {
  return fetchJson(
    fetchImpl,
    `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/github-imports/${encodeURIComponent(importId)}`,
    { method: "GET", headers: buildHeaders(null) }
  );
}

export function syncProjectFormalizationTargetsBySlug({ fetchImpl, baseUrl, slug, targets = [] }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/formalizations/sync`, {
    method: "POST",
    headers: buildHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({ targets }),
  });
}

// Pull `filename="…"` out of a Content-Disposition header (the adapter always
// quotes it). Exported for tests.
export function filenameFromContentDisposition(header) {
  const match = /filename="([^"]+)"/.exec(String(header || ""));
  return match ? match[1] : null;
}

// Binary download — the one client call that can't go through fetchJson. Returns
// `{ ok, status, bytes: Uint8Array, filename, contentType }` on success and the
// familiar `{ ok:false, status?, error }` shape on failure (the error body is the
// adapter's JSON `detail` when present).
export async function exportProjectZipBySlug({ fetchImpl, baseUrl, slug }) {
  const url = `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/export`;
  let response;
  try {
    response = await fetchImpl(url, { method: "GET", headers: buildHeaders(null) });
  } catch (error) {
    return { ok: false, error: `Request to ${url} failed: ${error instanceof Error ? error.message : String(error)}` };
  }
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    const text = await response.text().catch(() => "");
    if (text) {
      try {
        detail = JSON.parse(text)?.detail || detail;
      } catch {
        /* non-JSON error body — keep the status line */
      }
    }
    return { ok: false, status: response.status, error: detail };
  }
  const buf = await response.arrayBuffer();
  return {
    ok: true,
    status: response.status,
    bytes: new Uint8Array(buf),
    filename: filenameFromContentDisposition(response.headers?.get?.("content-disposition")) || `${slug}.zip`,
    contentType: response.headers?.get?.("content-type") || "application/zip",
  };
}

export async function startApiRun({
  fetchImpl,
  baseUrl,
  apiKey,
  message,
  sessionId = null,
  autonomous = true,
  projectSlug = null,
  projectTitle = null,
  projectNamespace = null,
  origin = null,
  originUrl = null,
  focusFormalizationId = null,
  focusSourceHash = null,
  newFormalization = null,
}) {
  // `autonomous: true` tells the adapter to run with no per-tool approval gate and
  // the non-interactive `default` prompt variant, so the Overleaf job formalizes
  // end-to-end with zero human interaction. (The client also auto-resolves any
  // approval events below as a belt-and-suspenders, but an autonomous run emits
  // none.) Defaults true because this client is the autonomous Overleaf path.
  //
  // `projectSlug` is the Overleaf document namespace (slugProjectId). When present
  // the adapter tags the session+run with a project of that slug, so the popover's
  // "This project" usage can be summed per document. Omitted for the interactive
  // UI path, which stays project-less.
  //
  // `origin` / `originUrl` record session providence: 'overleaf' + the canonical
  // Overleaf document URL, so the Lea UI can show an origin indicator and open/focus
  // the source document. Independent of the project usage-namespace above.
  const body = { message, autonomous };
  if (sessionId) body.session_id = sessionId;
  if (projectSlug) {
    body.project_slug = projectSlug;
    body.project_title = projectTitle || projectSlug;
    if (projectNamespace) body.project_namespace = projectNamespace;
  }
  if (origin) body.origin = origin;
  if (originUrl) body.origin_url = originUrl;
  if (focusFormalizationId) body.focus_formalization_id = focusFormalizationId;
  if (focusSourceHash) body.focus_source_hash = focusSourceHash;
  if (newFormalization) body.new_formalization = newFormalization;
  return fetchJson(fetchImpl, `${baseUrl}/api/runs`, {
    method: "POST",
    headers: buildHeaders(apiKey, { "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
}

export function resolveApiApproval({ fetchImpl, baseUrl, apiKey, runId, approvalId, decision = TOOL_APPROVAL_DECISION }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`, {
    method: "POST",
    headers: buildHeaders(apiKey, { "Content-Type": "application/json" }),
    body: JSON.stringify({ decision }),
  });
}

export function interruptApiRun({ fetchImpl, baseUrl, apiKey, runId }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/runs/${encodeURIComponent(runId)}/interrupt`, {
    method: "POST",
    headers: buildHeaders(apiKey),
  });
}

// Pull this run's usage/cost back off the persisted run row (no usage events on
// the wire). Best-effort: any shape mismatch yields zeroes.
export async function fetchApiRunUsage({ fetchImpl, baseUrl, apiKey, sessionId, runId }) {
  const detail = await fetchJson(fetchImpl, `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: "GET",
    headers: buildHeaders(apiKey),
  });
  const empty = { usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 }, costUsd: 0 };
  if (!detail.ok || !detail.body) return empty;
  const runs = Array.isArray(detail.body.runs) ? detail.body.runs : [];
  const run = runs.find((r) => r && r.id === runId) || null;
  const source = run || detail.body.usage || {};
  const inputTokens = toNonNegativeNumber(source.input_tokens ?? source.inputTokens);
  const outputTokens = toNonNegativeNumber(source.output_tokens ?? source.outputTokens);
  const costUsd = toNonNegativeNumber(source.cost_usd ?? source.costUsd ?? run?.cost_usd);
  return {
    usage: { inputTokens, outputTokens, totalTokens: inputTokens + outputTokens },
    costUsd,
  };
}

export function fetchApiSessionDetail({ fetchImpl, baseUrl, apiKey, sessionId }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: "GET",
    headers: buildHeaders(apiKey),
  });
}

// D9: write a manual edit to a session's working file as a first-class,
// run-less step (POST /api/sessions/{id}/file). The adapter commits it
// `author=user`, records a code_step, and returns `{unchanged:true}` for a
// no-op save. Used by the Overleaf lean pane's manual-edit surface
// (docs/FEATURE-overleaf-lean-pane-manual-edit.md) -- the same primitive the
// standalone canvas already uses, just called from a second client.
export function writeApiSessionFile({
  fetchImpl, baseUrl, apiKey, sessionId, path, content, note,
  formalizationId = null,
}) {
  const body = { path, content };
  if (note) body.note = note;
  if (formalizationId) body.formalization_id = formalizationId;
  return fetchJson(fetchImpl, `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/file`, {
    method: "POST",
    headers: buildHeaders(apiKey, { "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
}

// Standalone LSP-backed `lean_check` on a session's working file (D2), no
// run required. Without `author`, back-fills the verdict onto the file's
// existing latest code_step (the original behavior, used for the edited
// file's own check). With `author` (e.g. `"cascade"`), the adapter instead
// records a *new* code_step attributed to that author -- used for
// re-verifying a project dependent that the edit itself didn't touch. See
// docs/PLAN-overleaf-lean-pane-manual-edit.md Phase 1/2.
export function runApiSessionLeanCheck({
  fetchImpl, baseUrl, apiKey, sessionId, path, author, summary,
  formalizationId = null,
}) {
  const body = {};
  if (path) body.path = path;
  if (author) body.author = author;
  if (summary) body.summary = summary;
  if (formalizationId) body.formalization_id = formalizationId;
  return fetchJson(fetchImpl, `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/lean-check`, {
    method: "POST",
    headers: buildHeaders(apiKey, { "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
}

// Force a real `lake build` of a session's working file's module (D2-adjacent,
// no run). `runApiSessionLeanCheck`'s LSP fast path never updates the compiled
// `.olean` another file's `import` resolves against, so before trusting a
// cascade re-check of a *dependent* file, the edited module needs this to have
// run once first. See docs/FEATURE-overleaf-lean-pane-manual-edit.md ("Cascade
// verification") and the matching adapter route's docstring
// (routes/sessions.py `rebuild_session_module`).
export function rebuildApiSessionModule({ fetchImpl, baseUrl, apiKey, sessionId, path }) {
  const body = {};
  if (path) body.path = path;
  return fetchJson(fetchImpl, `${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/rebuild`, {
    method: "POST",
    headers: buildHeaders(apiKey, { "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
}

function deriveTurnProgress(payload, defaultMaxTurns) {
  if (!payload || typeof payload !== "object") return null;
  const turn = Number(payload.turn);
  if (!Number.isFinite(turn) || turn <= 0) return null;
  return { currentTurn: turn, maxTurns: defaultMaxTurns ?? null };
}

// Drive + observe one run to terminal `done`. Opening the stream is what runs the
// job adapter-side; auto-approving keeps it autonomous. Returns the terminal
// outcome (NOT usage — caller reads that back via fetchApiRunUsage).
export async function streamApiRun({
  fetchImpl,
  baseUrl,
  apiKey,
  runId,
  maxTurns = null,
  autoApprove = true,
  onEvent = null,
  onProgressUpdated = null,
  signal = null,
}) {
  let response;
  try {
    response = await fetchImpl(`${baseUrl}/api/runs/${encodeURIComponent(runId)}/events`, {
      method: "GET",
      headers: buildHeaders(apiKey),
      signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") return { ok: false, aborted: true, error: "Run stream aborted." };
    return { ok: false, error: `Could not open run event stream: ${error instanceof Error ? error.message : String(error)}` };
  }
  if (!response?.ok || !response.body) {
    // httpStatus lets the caller tell an HTTP rejection (real failure — the
    // Phase 2 adapter has no busy/finished 409s) apart from a dropped
    // transport (httpStatus null), which runApiProofJob retries.
    return { ok: false, httpStatus: response?.status ?? null, error: `Run event stream returned HTTP ${response?.status ?? "?"}.` };
  }

  let doneStatus = null;
  let resultKind = null;
  let resultDetail = null;
  let runError = null;
  let buffer = "";

  const handleFrame = async (frame) => {
    const { type, data } = parseSseFrame(frame);
    if (!type) return;
    if (onEvent) await onEvent(type, data);
    if (onProgressUpdated) {
      const progress = deriveTurnProgress(data, maxTurns);
      if (progress) await onProgressUpdated(progress);
    }
    if (type === "approval_requested" && autoApprove && data?.approval_id) {
      await resolveApiApproval({ fetchImpl, baseUrl, apiKey, runId, approvalId: data.approval_id });
    } else if (type === "run_error") {
      runError = data?.message || "Lea run error.";
    } else if (type === "done") {
      doneStatus = String(data?.status || "").toLowerCase();
      resultKind = String(data?.result_kind || doneStatus || "").toLowerCase() || null;
      resultDetail = typeof data?.result_detail === "string" ? data.result_detail : null;
    }
  };

  try {
    for await (const chunk of iterateResponseBody(response.body)) {
      buffer += decodeChunk(chunk);
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() || "";
      for (const frame of frames) {
        if (frame.trim()) await handleFrame(frame);
      }
      if (doneStatus !== null) break;
    }
    if (doneStatus === null && buffer.trim()) await handleFrame(buffer);
  } catch (error) {
    if (error?.name === "AbortError") return { ok: false, aborted: true, error: runError || "Run stream aborted." };
    return { ok: false, error: `Run event stream failed: ${error instanceof Error ? error.message : String(error)}`, doneStatus, runError };
  }

  const ok = doneStatus !== null && SUCCESS_DONE_STATUS.has(doneStatus);
  return {
    ok,
    doneStatus,
    resultKind,
    resultDetail,
    error: ok ? null : (runError || (doneStatus ? `Lea run ended with status: ${doneStatus}` : "Lea run ended without a terminal status.")),
  };
}

// Consecutive failures to read the run row while the stream is also failing —
// past this, the adapter is genuinely unreachable and the client gives up
// (best-effort interrupting the run it may be orphaning) instead of spinning
// until the job timeout.
const MAX_RUN_ROW_MISSES = 5;

// De-synchronize concurrent re-attachers after a transport drop (e.g. the
// adapter restarting mid-run) so they don't hammer it in lockstep.
function retryDelayWithJitter(baseMs) {
  return baseMs + Math.floor(Math.random() * baseMs * 0.5);
}

// Resolve to true after ms, or false immediately if/when the signal aborts —
// so a queued run reacts to its timeout mid-wait instead of after the delay.
function waitBeforeRetry(ms, signal) {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve(false);
    // Deliberately NOT unref'd: this delay gates forward progress (the next
    // attach attempt), so it must keep the event loop alive to fire.
    const timer = setTimeout(() => {
      signal?.removeEventListener?.("abort", onAbort);
      resolve(true);
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      resolve(false);
    };
    signal?.addEventListener?.("abort", onAbort, { once: true });
  });
}

// Read one run's { id, status, result_kind, result_detail } — the cheap poll the
// busy-wait / stream-drop retry loop makes every ~3s. Hits the dedicated
// GET /api/runs/{run_id} (v2.3 item 16), which reads a single run row instead of
// the full session detail (messages + code_steps + status_events + usage) the old
// path paid on every tick — a self-inflicted DB-contention source under concurrency.
// A 404 for that route on an older adapter falls back to the session-detail scan so
// a companion pointed at a pre-item-16 backend still works. Best-effort: null when
// the adapter or the row can't be reached.
async function fetchApiRunRow({ fetchImpl, baseUrl, apiKey, sessionId, runId }) {
  if (!runId) return null;
  const res = await fetchJson(fetchImpl, `${baseUrl}/api/runs/${encodeURIComponent(runId)}`, {
    method: "GET",
    headers: buildHeaders(apiKey),
  });
  if (res.ok && res.body) return res.body;
  // 404 here is ambiguous: an unknown run id (nothing to fall back to), OR an old
  // adapter that lacks the route. Only try the legacy scan when we have a session
  // to scan; a bare unknown-run 404 stays null.
  if (res.status === 404 && sessionId) {
    const detail = await fetchApiSessionDetail({ fetchImpl, baseUrl, apiKey, sessionId });
    if (!detail.ok || !detail.body) return null;
    const runs = Array.isArray(detail.body.runs) ? detail.body.runs : [];
    return runs.find((r) => r && r.id === runId) || null;
  }
  return null;
}

// High-level: start a run and drive it to completion, returning the shape the
// companion orchestration consumes:
// { ok, timedOut, apiRunId, error, usage, costUsd }.
export async function runApiProofJob({
  fetchImpl,
  baseUrl,
  apiKey,
  message,
  sessionId = null,
  maxTurns = null,
  timeoutMs = 900000,
  busyRetryDelayMs = 3000,
  autoApprove = true,
  autonomous = true,
  projectSlug = null,
  projectTitle = null,
  projectNamespace = null,
  origin = null,
  originUrl = null,
  focusFormalizationId = null,
  focusSourceHash = null,
  newFormalization = null,
  appendLog = null,
  logPath = null,
  onEvent = null,
  onRunStarted = null,
  onProgressUpdated = null,
}) {
  const log = async (line) => {
    if (appendLog && logPath) await appendLog(logPath, line);
  };

  const start = await startApiRun({
    fetchImpl,
    baseUrl,
    apiKey,
    message,
    sessionId,
    autonomous,
    projectSlug,
    projectTitle,
    projectNamespace,
    origin,
    originUrl,
    focusFormalizationId,
    focusSourceHash,
    newFormalization,
  });
  if (!start.ok) return { ok: false, timedOut: false, error: start.error };

  const runId = start.body?.run_id;
  const newSessionId = start.body?.session_id || sessionId;
  if (!runId) return { ok: false, timedOut: false, error: "Lea adapter did not return a run_id." };
  await log(`[backend] Lea adapter run started: ${runId} (session ${newSessionId})\n`);
  if (onRunStarted) await onRunStarted(runId, newSessionId, start.body || {});

  const abort = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    abort.abort();
    interruptApiRun({ fetchImpl, baseUrl, apiKey, runId }).catch(() => {});
  }, Math.max(1, timeoutMs));
  if (typeof timer.unref === "function") timer.unref();

  // Phase 2 contract (PLAN-system-hardening): the adapter queues runs
  // server-side and the events endpoint is a pure observer — attach is
  // idempotent, a queued run streams `queued` frames, and a finished run
  // replays to a terminal `done`. The old 409 busy/finished disambiguation is
  // gone; any HTTP rejection of the attach is a real failure.
  //
  // What remains client-side is transport robustness: a DROPPED stream (open
  // error, mid-stream disconnect, or close without a `done` frame) does not
  // mean the run failed — it may still be executing adapter-side, so failing
  // the job here would abandon a live, billing run and mislabel its outcome.
  // Consult the run row: re-attach while it reads pending/running, adopt its
  // outcome once terminal, and give up (with a best-effort interrupt) only
  // when the adapter is unreachable past MAX_RUN_ROW_MISSES.
  let loggedQueued = false;
  const observeEvent = async (type, data) => {
    if (type === "queued" && !loggedQueued) {
      loggedQueued = true;
      await log(`[backend] Lea adapter queued this run (position ${Number.isFinite(data?.position) ? data.position : "?"}).\n`);
    }
    if (onEvent) await onEvent(type, data);
  };

  let outcome;
  let loggedStreamDrop = false;
  let rowMisses = 0;
  try {
    for (;;) {
      outcome = await streamApiRun({
        fetchImpl, baseUrl, apiKey, runId, maxTurns, autoApprove,
        signal: abort.signal, onEvent: observeEvent, onProgressUpdated,
      });
      if (outcome.ok || outcome.aborted) break;
      const streamDropped = outcome.httpStatus == null && !outcome.doneStatus;
      if (!streamDropped) break;

      const row = await fetchApiRunRow({ fetchImpl, baseUrl, apiKey, sessionId: newSessionId, runId });
      const rowStatus = String(row?.status || "").toLowerCase();
      if (rowStatus && rowStatus !== "pending" && rowStatus !== "running") {
        // Terminal without us attached: adopt the run row's outcome as if it
        // had arrived on a `done` event.
        const ok = SUCCESS_DONE_STATUS.has(rowStatus);
        outcome = {
          ok,
          doneStatus: rowStatus,
          resultKind: String(row.result_kind || rowStatus).toLowerCase() || null,
          resultDetail: typeof row.result_detail === "string" ? row.result_detail : null,
          error: ok ? null : `Lea run ended with status: ${rowStatus}`,
        };
        break;
      }
      if (!rowStatus) {
        // A missing row and a malformed row with no status are equally useless
        // for deciding whether the detached run is still live. Bound both cases;
        // otherwise an older/incompatible adapter can keep this loop alive forever.
        rowMisses += 1;
        if (rowMisses >= MAX_RUN_ROW_MISSES) {
          await log("[backend] Lea run status is unavailable; giving up on this run and requesting an interrupt.\n");
          interruptApiRun({ fetchImpl, baseUrl, apiKey, runId }).catch(() => {});
          break;
        }
      } else {
        rowMisses = 0;
      }

      if (!loggedStreamDrop) {
        loggedStreamDrop = true;
        await log("[backend] Run event stream dropped while the run is still live; re-attaching...\n");
      }
      if (!(await waitBeforeRetry(retryDelayWithJitter(busyRetryDelayMs), abort.signal))) {
        outcome = { ok: false, aborted: true, error: "Run stream aborted." };
        break;
      }
    }
  } finally {
    clearTimeout(timer);
  }

  const usage = await fetchApiRunUsage({ fetchImpl, baseUrl, apiKey, sessionId: newSessionId, runId });

  if (timedOut) {
    return {
      ok: false,
      timedOut: true,
      apiRunId: runId,
      sessionId: newSessionId,
      doneStatus: outcome?.doneStatus || null,
      resultKind: outcome?.resultKind || null,
      resultDetail: outcome?.resultDetail || null,
      formalizationId: start.body?.focus_formalization_id || start.body?.formalization?.id || null,
      error: "Lea adapter run timed out.",
      ...usage,
    };
  }
  return {
    ok: outcome.ok,
    timedOut: false,
    apiRunId: runId,
    sessionId: newSessionId,
    doneStatus: outcome.doneStatus,
    resultKind: outcome.resultKind || null,
    resultDetail: outcome.resultDetail || null,
    formalizationId: start.body?.focus_formalization_id || start.body?.formalization?.id || null,
    error: outcome.ok ? undefined : outcome.error,
    ...usage,
  };
}
