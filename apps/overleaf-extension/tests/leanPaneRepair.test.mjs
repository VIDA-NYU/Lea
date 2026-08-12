// Self-repair Phases 3-4: the repair run (dispatch, verification, statement
// guard) and batch repair (topological ordering, skip-downstream-of-failed).
// docs/FEATURE-overleaf-self-repair.md / docs/PLAN-overleaf-self-repair.md.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  handleLeanPaneRepairAll,
  handleLeanPaneRepairAllContinue,
  handleLeanPaneRepairStart,
  handleLeanPaneRepairStatus
} from "../companion/server.mjs";
import { parseLeanImports, topologicalRepairOrder } from "../companion/leanDependencyGraph.mjs";

const NAMESPACE = "Lea.Project1";

async function makeLeaRepo() {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "lea-repair-repo-"));
  await fs.mkdir(path.join(dir, "workspace", "proofs"), { recursive: true });
  await fs.writeFile(path.join(dir, "pyproject.toml"), "[project]\nname = \"lea-prover\"\n", "utf8");
  await fs.writeFile(path.join(dir, "workspace", "lean-toolchain"), "leanprover/lean4:stable\n", "utf8");
  await fs.writeFile(path.join(dir, "workspace", "lakefile.lean"), "import Lake\nopen Lake DSL\n", "utf8");
  return dir;
}

async function writeProof(leaRepo, relativePath, content) {
  const absolute = path.join(leaRepo, "workspace", "proofs", relativePath);
  await fs.mkdir(path.dirname(absolute), { recursive: true });
  await fs.writeFile(absolute, content, "utf8");
}

async function makeState({ leaRepo, jobs = {}, fetchImpl }) {
  const appDir = await fs.mkdtemp(path.join(os.tmpdir(), "lea-repair-state-"));
  return {
    settingsPath: path.join(appDir, "settings.json"),
    jobsPath: path.join(appDir, "jobs.json"),
    settings: {
      leaRepoPath: leaRepo,
      leaApiBaseUrl: "http://127.0.0.1:8001",
      leaProvider: "openai",
      leaModel: "o4-mini",
      leaProviderApiKeys: {},
      leaMaxTurns: 20,
      leaMaxSpendUsd: null
    },
    jobs,
    env: { OPENAI_API_KEY: "test-key" },
    fetchImpl
  };
}

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, async text() { return JSON.stringify(body); } };
}

function sseResponse(frames) {
  return {
    ok: true,
    status: 200,
    body: (async function* () {
      for (const { type, data } of frames) {
        yield new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`);
      }
    })()
  };
}

// Full-route stub: the repair run flow (POST /api/runs -> SSE events) plus the
// session detail / rebuild / lean-check calls verification and cascade make.
// `runSessions` maps each started run (in call order) to the session id the
// adapter reports back.
function makeRepairFetch(calls, { sessionDetails = {}, rebuildResponses = {}, onRunStarted = null } = {}) {
  let runCounter = 0;
  return async (url, requestOptions = {}) => {
    const u = String(url);
    if (u.endsWith("/api/settings")) return jsonResponse(404, { detail: "not found" });
    const body = requestOptions.body ? JSON.parse(requestOptions.body) : null;
    calls.push({ url: u, method: requestOptions.method || "GET", body });
    if (u.endsWith("/api/runs") && requestOptions.method === "POST") {
      runCounter += 1;
      // lets a test mutate its session-detail fixture the way a real run
      // mutates a real session: only AFTER the run started
      if (onRunStarted) await onRunStarted(body);
      return jsonResponse(200, {
        run_id: `api-run-${runCounter}`,
        session_id: body?.session_id || "sess-unexpected-new",
        status: "running",
        project_namespace: NAMESPACE
      });
    }
    if (u.includes("/api/runs/") && u.endsWith("/events")) {
      return sseResponse([
        { type: "status", data: { status: "tool_call", turn: 1 } },
        { type: "done", data: { status: "proved" } }
      ]);
    }
    const match = u.match(/\/api\/sessions\/([^/]+)(?:\/(file|lean-check|rebuild))?$/);
    if (match) {
      const sessionId = decodeURIComponent(match[1]);
      const kind = match[2] || "detail";
      if (kind === "detail") {
        const detail = sessionDetails[sessionId];
        return jsonResponse(200, typeof detail === "function" ? detail() : (detail || { code_steps: [] }));
      }
      if (kind === "rebuild") {
        const response = rebuildResponses[sessionId];
        return jsonResponse(200, typeof response === "function" ? response(body) : (response || { path: body?.path, status: "ok", detail: null }));
      }
      if (kind === "lean-check") return jsonResponse(200, { path: body?.path, status: "ok", detail: null });
    }
    return jsonResponse(404, { detail: `unmapped url in repair test stub: ${u}` });
  };
}

async function waitFor(predicate) {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > 3000) throw new Error("Timed out waiting for condition.");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

const BREAKAGE = {
  upstreamLabel: "compactness_criterion",
  upstreamDeclarationName: "compactness_thm",
  classificationKind: "renamed",
  renamedFrom: "compactness_criterion",
  renamedTo: "compactness_thm",
  via: "chat",
  editedAt: "2026-07-04T12:00:00.000Z",
  beforeHeader: "theorem compactness_criterion : True",
  afterHeader: "theorem compactness_thm : True"
};

// A dependent broken by the upstream rename: its recorded file still imports/
// references the OLD name.
function brokenDependentJob(overrides = {}) {
  return {
    jobId: "job-dependent",
    jobKey: "project-1:theorem:compactness_corollary",
    status: "formalized",
    targetKind: "theorem",
    targetLabel: "compactness_corollary",
    declarationName: "compactness_corollary",
    leaSessionId: "sess-b",
    projectSlug: "project-1",
    projectNamespace: NAMESPACE,
    startedAt: "2026-01-01T00:00:00.000Z",
    finishedAt: "2026-01-01T00:01:00.000Z",
    lastEditCheckStatus: "error",
    lastEditCheckDetail: "unknown constant 'Lea.Project1.compactness_criterion'",
    lastEditBreakage: { ...BREAKAGE },
    ...overrides
  };
}

const DEPENDENT_SESSION_DETAIL = {
  project_namespace: NAMESPACE,
  code_steps: [
    {
      path: "compactness_corollary.lean",
      seq: 1,
      code: "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  exact compactness_criterion\n"
    }
  ]
};

// The step a repair run records when it fixes only the proof/references
// (header unchanged). Appended to the session AFTER the run starts, the way a
// real run would.
const REPAIRED_STEP = {
  path: "compactness_corollary.lean",
  seq: 2,
  code: "import Lea.Project1.compactness_thm\ntheorem compactness_corollary : True := by\n  exact compactness_thm\n",
  check_status: "ok"
};

test("repair start: no session recorded -> 404 no_session", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepo, fetchImpl: makeRepairFetch([]) });
  const res = await handleLeanPaneRepairStart(
    { overleafProjectId: "project-1", targetKind: "theorem", targetLabel: "compactness_corollary" },
    state
  );
  assert.equal(res.statusCode, 404);
  assert.equal(res.body.error, "no_session");
});

test("repair start: an item that compiles again is a no-op (alreadyFixed), never a run", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepo,
    jobs: { b: brokenDependentJob({ lastEditCheckStatus: "ok", lastEditBreakage: undefined }) },
    fetchImpl: makeRepairFetch(calls)
  });
  const res = await handleLeanPaneRepairStart(
    { overleafProjectId: "project-1", targetKind: "theorem", targetLabel: "compactness_corollary" },
    state
  );
  assert.equal(res.statusCode, 200);
  assert.equal(res.body.alreadyFixed, true);
  assert.ok(!calls.some((c) => c.url.endsWith("/api/runs")));
});

test("repair start: a live run on the item -> 409 run_in_progress", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepo,
    jobs: {
      b: brokenDependentJob(),
      live: { jobId: "live", jobKey: "project-1:theorem:compactness_corollary", status: "in_progress", leaSessionId: "sess-b" }
    },
    fetchImpl: makeRepairFetch([])
  });
  const res = await handleLeanPaneRepairStart(
    { overleafProjectId: "project-1", targetKind: "theorem", targetLabel: "compactness_corollary" },
    state
  );
  assert.equal(res.statusCode, 409);
  assert.equal(res.body.error, "run_in_progress");
});

test("repair start: suppressed while the upstream item's own file is broken", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({
    leaRepo,
    jobs: {
      b: brokenDependentJob(),
      upstream: {
        jobId: "job-upstream",
        jobKey: "project-1:theorem:compactness_criterion",
        status: "formalized",
        targetLabel: "compactness_criterion",
        declarationName: "compactness_criterion",
        leaSessionId: "sess-a",
        projectSlug: "project-1",
        lastEditCheckStatus: "error",
        startedAt: "2026-01-01T00:00:00.000Z"
      }
    },
    fetchImpl: makeRepairFetch([])
  });
  const res = await handleLeanPaneRepairStart(
    { overleafProjectId: "project-1", targetKind: "theorem", targetLabel: "compactness_corollary" },
    state
  );
  assert.equal(res.statusCode, 409);
  assert.equal(res.body.error, "repair_suppressed");
  assert.match(res.body.message, /compactness_criterion/);
});

test("repair run: dispatches on the item's own session with the repair prompt, verifies by rebuild, clears breakage on success", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const steps = [...DEPENDENT_SESSION_DETAIL.code_steps];
  const state = await makeState({
    leaRepo,
    jobs: { b: brokenDependentJob() },
    fetchImpl: makeRepairFetch(calls, {
      sessionDetails: { "sess-b": () => ({ project_namespace: NAMESPACE, code_steps: steps.slice() }) },
      rebuildResponses: { "sess-b": { status: "ok", detail: null } },
      onRunStarted: () => { steps.push(REPAIRED_STEP); }
    })
  });

  const res = await handleLeanPaneRepairStart(
    { overleafProjectId: "project-1", targetKind: "theorem", targetLabel: "compactness_corollary" },
    state
  );
  assert.equal(res.statusCode, 200);
  assert.equal(res.body.status, "in_progress");
  const repairJobId = res.body.jobId;
  assert.match(repairJobId, /^repair-/);

  await waitFor(() => state.jobs[repairJobId]?.finalStatus === "repaired");
  const job = state.jobs[repairJobId];

  // ordinary autonomous run against the EXISTING session, repair prompt attached
  const runCall = calls.find((c) => c.url.endsWith("/api/runs"));
  assert.equal(runCall.body.session_id, "sess-b");
  assert.equal(runCall.body.autonomous, true);
  assert.match(runCall.body.message, /You are repairing a broken Lean formalization/);
  assert.match(runCall.body.message, /Lean namespace: Lea\.Project1/);
  assert.match(runCall.body.message, /do not derive a namespace from the display name/);
  assert.match(runCall.body.message, /RENAMED to `compactness_thm`/);
  assert.match(runCall.body.message, /unknown constant 'Lea.Project1.compactness_criterion'/);
  assert.equal(job.mode, "repair");
  assert.equal(job.repairOf.upstreamLabel, "compactness_criterion");
  // verified by a real rebuild of the item's module, not by run completion
  assert.ok(calls.some((c) => c.url.endsWith("/sess-b/rebuild")));
  // the authoritative pass cleared the persisted breakage via the verdict choke point
  assert.equal(job.lastEditCheckStatus, "ok");
  assert.equal(job.lastEditBreakage, undefined);
  // proof/reference-only repair: header unchanged -> no review flag
  assert.equal(job.lastRepair.state, "repaired");
});

test("repair run: a repair that still fails to build ends repair_failed with the agent's explanation, breakage kept", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = await makeState({
    leaRepo,
    jobs: { b: brokenDependentJob() },
    fetchImpl: makeRepairFetch(calls, {
      sessionDetails: {
        "sess-b": {
          ...DEPENDENT_SESSION_DETAIL,
          messages: [
            { id: "m1", role: "assistant", seq: 9, content: "The statement is unprovable as stated: the removed hypothesis was essential." }
          ]
        }
      },
      rebuildResponses: { "sess-b": { status: "error", detail: "still does not compile" } }
    })
  });

  const res = await handleLeanPaneRepairStart(
    { overleafProjectId: "project-1", targetKind: "theorem", targetLabel: "compactness_corollary" },
    state
  );
  const repairJobId = res.body.jobId;
  await waitFor(() => state.jobs[repairJobId]?.finalStatus === "repair_failed");
  const job = state.jobs[repairJobId];
  // "repair_failed" is a status findLatestJob(key, "failed") can never match:
  // a failed repair must not masquerade as a failed formalization.
  assert.notEqual(job.status, "failed");
  assert.equal(job.lastRepair.state, "failed");
  assert.match(job.lastRepair.failureReason, /unprovable as stated/);
  assert.equal(job.lastEditCheckStatus, "error");
  assert.equal(job.lastEditBreakage.upstreamLabel, "compactness_criterion");
});

test("repair run: statement guard flags a compiling repair whose header changed beyond the sanctioned rename", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const steps = [...DEPENDENT_SESSION_DETAIL.code_steps];
  const state = await makeState({
    leaRepo,
    jobs: { b: brokenDependentJob() },
    fetchImpl: makeRepairFetch(calls, {
      sessionDetails: { "sess-b": () => ({ project_namespace: NAMESPACE, code_steps: steps.slice() }) },
      rebuildResponses: { "sess-b": { status: "ok", detail: null } },
      onRunStarted: () => {
        steps.push({
          path: "compactness_corollary.lean",
          seq: 2,
          // the agent "fixed" it by adding a hypothesis -- statement changed
          code: "import Lea.Project1.compactness_thm\ntheorem compactness_corollary (h : True) : True := by\n  exact h\n",
          check_status: "ok"
        });
      }
    })
  });

  const res = await handleLeanPaneRepairStart(
    { overleafProjectId: "project-1", targetKind: "theorem", targetLabel: "compactness_corollary" },
    state
  );
  const repairJobId = res.body.jobId;
  await waitFor(() => state.jobs[repairJobId]?.finalStatus === "repaired");
  assert.equal(state.jobs[repairJobId].lastRepair.state, "needs_review");
  assert.match(state.jobs[repairJobId].lastRepair.failureReason, /header changed/);
});

// --- topological ordering (pure) --------------------------------------------

test("topologicalRepairOrder: an importer repairs after its import, including through non-batch modules", () => {
  const importsByModule = new Map([
    [`${NAMESPACE}.c`, new Set([`${NAMESPACE}.middle`])],
    [`${NAMESPACE}.middle`, new Set([`${NAMESPACE}.b`])], // middle is NOT in the batch
    [`${NAMESPACE}.b`, new Set([`${NAMESPACE}.a`])]
  ]);
  const items = [
    { targetLabel: "c", moduleName: `${NAMESPACE}.c` },
    { targetLabel: "b", moduleName: `${NAMESPACE}.b` }
  ];
  const { ordered, cyclic } = topologicalRepairOrder(items, importsByModule);
  assert.equal(cyclic, false);
  assert.deepEqual(ordered.map((i) => i.targetLabel), ["b", "c"]);
});

test("topologicalRepairOrder: unattributable items sort last; a cycle falls back to given order with a flag", () => {
  const items = [
    { targetLabel: "unknown", moduleName: null },
    { targetLabel: "a", moduleName: "M.a" }
  ];
  const { ordered } = topologicalRepairOrder(items, new Map());
  assert.deepEqual(ordered.map((i) => i.targetLabel), ["a", "unknown"]);

  const cycleImports = new Map([
    ["M.a", new Set(["M.b"])],
    ["M.b", new Set(["M.a"])]
  ]);
  const cycleItems = [
    { targetLabel: "a", moduleName: "M.a" },
    { targetLabel: "b", moduleName: "M.b" }
  ];
  const result = topologicalRepairOrder(cycleItems, cycleImports);
  assert.equal(result.cyclic, true);
  assert.deepEqual(result.ordered.map((i) => i.targetLabel), ["a", "b"]);
});

// --- batch repair ------------------------------------------------------------

test("repair all: repairs in import order, skips transitive importers of a failed repair, and pauses for continue", async () => {
  const leaRepo = await makeLeaRepo();
  // On-disk graph: corollary imports criterion; application imports corollary;
  // unrelated imports nothing. corollary + application + unrelated form the batch.
  await writeProof(leaRepo, "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  sorry\n");
  await writeProof(leaRepo, "Lea/Project1/heine_borel_application.lean",
    "import Lea.Project1.compactness_corollary\ntheorem heine_borel_application : True := by\n  sorry\n");
  await writeProof(leaRepo, "Lea/Project1/unrelated_lemma.lean",
    "theorem unrelated_lemma : True := by\n  sorry\n");

  const jobs = {
    b: brokenDependentJob({ moduleName: `${NAMESPACE}.compactness_corollary` }),
    c: brokenDependentJob({
      jobId: "job-application",
      jobKey: "project-1:theorem:heine_borel_application",
      targetLabel: "heine_borel_application",
      declarationName: "heine_borel_application",
      leaSessionId: "sess-c",
      moduleName: `${NAMESPACE}.heine_borel_application`
    }),
    d: brokenDependentJob({
      jobId: "job-unrelated",
      jobKey: "project-1:theorem:unrelated_lemma",
      targetLabel: "unrelated_lemma",
      declarationName: "unrelated_lemma",
      leaSessionId: "sess-d",
      moduleName: `${NAMESPACE}.unrelated_lemma`
    })
  };
  const calls = [];
  const state = await makeState({
    leaRepo,
    jobs,
    fetchImpl: makeRepairFetch(calls, {
      sessionDetails: {
        "sess-b": DEPENDENT_SESSION_DETAIL, // no fix recorded -> stays broken
        "sess-c": { project_namespace: NAMESPACE, code_steps: [{ path: "heine_borel_application.lean", seq: 1, code: "theorem heine_borel_application : True := by\n  sorry\n" }] },
        "sess-d": { project_namespace: NAMESPACE, code_steps: [{ path: "unrelated_lemma.lean", seq: 1, code: "theorem unrelated_lemma : True := by\n  sorry\n", check_status: "ok" }, { path: "unrelated_lemma.lean", seq: 2, code: "theorem unrelated_lemma : True := by\n  trivial\n", check_status: "ok" }] }
      },
      rebuildResponses: {
        "sess-b": { status: "error", detail: "still broken" }, // corollary repair fails
        "sess-d": { status: "ok", detail: null }
      }
    })
  });

  // Deliberately submit in the WRONG order: application before corollary.
  const res = await handleLeanPaneRepairAll({
    overleafProjectId: "project-1",
    items: [
      { targetKind: "theorem", targetLabel: "heine_borel_application" },
      { targetKind: "theorem", targetLabel: "unrelated_lemma" },
      { targetKind: "theorem", targetLabel: "compactness_corollary" }
    ]
  }, state);
  assert.equal(res.statusCode, 200);
  const batchId = res.body.batchId;
  // topological order: corollary before application (unrelated is independent)
  const orderedLabels = res.body.items.map((i) => i.targetLabel);
  assert.ok(orderedLabels.indexOf("compactness_corollary") < orderedLabels.indexOf("heine_borel_application"));

  await waitFor(() => {
    const status = state.repairBatches[batchId];
    return status.pausedOn && !status.running;
  });

  const paused = await handleLeanPaneRepairStatus({ batchId }, state);
  const byLabel = Object.fromEntries(paused.body.items.map((i) => [i.targetLabel, i]));
  assert.equal(byLabel.compactness_corollary.state, "failed");
  assert.equal(byLabel.heine_borel_application.state, "skipped");
  assert.match(byLabel.heine_borel_application.reason, /depends_on_failed:compactness_corollary/);
  assert.equal(byLabel.unrelated_lemma.state, "pending"); // independent, awaiting continue
  assert.equal(paused.body.pausedOn.targetLabel, "compactness_corollary");
  assert.equal(paused.body.pausedOn.reason, "repair_failed");

  // continue: the independent item still runs (and succeeds)
  const cont = await handleLeanPaneRepairAllContinue({ batchId }, state);
  assert.equal(cont.statusCode, 200);
  await waitFor(() => {
    const status = state.repairBatches[batchId];
    return status.done && !status.running;
  });
  const final = await handleLeanPaneRepairStatus({ batchId }, state);
  const finalByLabel = Object.fromEntries(final.body.items.map((i) => [i.targetLabel, i]));
  assert.equal(finalByLabel.unrelated_lemma.state, "repaired");
  assert.equal(final.body.done, true);
});

test("repair all: already-fixed items are skipped up front without a run", async () => {
  const leaRepo = await makeLeaRepo();
  await writeProof(leaRepo, "Lea/Project1/compactness_corollary.lean",
    "theorem compactness_corollary : True := by\n  trivial\n");
  const calls = [];
  const state = await makeState({
    leaRepo,
    jobs: { b: brokenDependentJob({ lastEditCheckStatus: "ok", lastEditBreakage: undefined }) },
    fetchImpl: makeRepairFetch(calls)
  });

  const res = await handleLeanPaneRepairAll({
    overleafProjectId: "project-1",
    items: [{ targetKind: "theorem", targetLabel: "compactness_corollary" }]
  }, state);
  assert.equal(res.statusCode, 200);
  const batchId = res.body.batchId;
  await waitFor(() => state.repairBatches[batchId].done);
  const status = await handleLeanPaneRepairStatus({ batchId }, state);
  assert.equal(status.body.items[0].state, "skipped");
  assert.equal(status.body.items[0].reason, "already_fixed");
  assert.ok(!calls.some((c) => c.url.endsWith("/api/runs")));
});

test("repair status: unknown batch id -> 404 (batches do not survive restarts)", async () => {
  const leaRepo = await makeLeaRepo();
  const state = await makeState({ leaRepo, fetchImpl: makeRepairFetch([]) });
  const res = await handleLeanPaneRepairStatus({ batchId: "nope" }, state);
  assert.equal(res.statusCode, 404);
  assert.equal(res.body.error, "unknown_batch");
});

// parseLeanImports sanity for the batch graph: Set membership by module name.
test("parseLeanImports feeds the batch graph with exact module names", () => {
  const imports = parseLeanImports("import Lea.Project1.compactness_criterion\nimport Mathlib.Topology.Basic\n");
  assert.ok(imports.has("Lea.Project1.compactness_criterion"));
});

// --- Round 2: upstream renamed AGAIN while the repair ran -------------------
// (docs/PLAN-self-repair-stale-offers.md, round-2 addendum)

test("a repair that compiled clean but fails verification after a mid-run upstream rename reports the real reason and refreshes attribution", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const steps = [...DEPENDENT_SESSION_DETAIL.code_steps];
  const state = await makeState({
    leaRepo,
    jobs: {
      b: brokenDependentJob(),
      // the upstream item's linked job; its declarationName is the live
      // "current name" the attribution refresh reads
      upstream: {
        jobId: "job-upstream",
        jobKey: "project-1:theorem:compactness_criterion",
        status: "formalized",
        targetKind: "theorem",
        targetLabel: "compactness_criterion",
        declarationName: "compactness_thm", // rename 1, what the repair targets
        leaSessionId: "sess-a",
        projectSlug: "project-1",
        projectNamespace: NAMESPACE,
        startedAt: "2026-01-01T00:00:00.000Z",
        finishedAt: "2026-01-01T00:01:00.000Z"
      }
    },
    fetchImpl: makeRepairFetch(calls, {
      sessionDetails: {
        "sess-b": () => ({
          project_namespace: NAMESPACE,
          code_steps: steps.slice(),
          messages: [
            // the agent's SUCCESS report -- must NOT become the failure reason
            { id: "m1", role: "assistant", seq: 9, content: "Acknowledged and repaired. The file compiles with no errors." }
          ]
        })
      },
      rebuildResponses: {
        "sess-b": { status: "error", detail: "unknown constant 'Lea.Project1.compactness_thm'" }
      },
      onRunStarted: () => {
        // the repair adopts rename 1 and its own check passes...
        steps.push({ ...REPAIRED_STEP });
        // ...but the upstream is renamed AGAIN while the repair runs (rename
        // bookkeeping keeps declarationName current on every path)
        state.jobs.upstream.declarationName = "compactness_crt";
      }
    })
  });

  const res = await handleLeanPaneRepairStart(
    { overleafProjectId: "project-1", targetKind: "theorem", targetLabel: "compactness_corollary" },
    state
  );
  const repairJobId = res.body.jobId;
  await waitFor(() => state.jobs[repairJobId]?.finalStatus === "repair_failed");
  const job = state.jobs[repairJobId];

  // the reason says what actually happened, not the agent's success prose
  assert.match(job.lastRepair.failureReason, /verification against the current project failed/);
  assert.match(job.lastRepair.failureReason, /changed again while the repair ran/);
  assert.match(job.lastRepair.failureReason, /compactness_crt/);
  assert.doesNotMatch(job.lastRepair.failureReason, /Acknowledged and repaired/);

  // attribution refreshed to the CURRENT upstream identity, so the pane's
  // copy and the next repair prompt carry the right rename mapping
  assert.equal(job.lastEditBreakage.classificationKind, "renamed");
  assert.equal(job.lastEditBreakage.renamedFrom, "compactness_thm");
  assert.equal(job.lastEditBreakage.renamedTo, "compactness_crt");
  assert.equal(job.lastEditBreakage.upstreamDeclarationName, "compactness_crt");
});
