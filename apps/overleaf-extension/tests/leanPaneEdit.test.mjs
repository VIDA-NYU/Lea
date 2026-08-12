import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { handleLeanPaneEditSave, handleLeanPaneEditStart, handleLeanPaneManifest } from "../companion/server.mjs";
import { formatDependentOutcome } from "../extension/leanPaneView.mjs";

async function makeLeaRepo() {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "lea-edit-repo-"));
  await fs.mkdir(path.join(dir, "workspace", "proofs"), { recursive: true });
  // handleLeanPaneManifest validates the repo (validateLeaRepo) before
  // enrichment; without these it degrades every item to status "unknown"
  // rather than actually computing a status, which would mask this file's
  // manifest-level regression test rather than exercise it.
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

function makeState({ leaRepo, jobs = {}, fetchImpl }) {
  return {
    settings: { leaRepoPath: leaRepo, leaApiBaseUrl: "http://127.0.0.1:8001" },
    jobs,
    jobsPath: path.join(leaRepo, "jobs.json"),
    env: {},
    fetchImpl
  };
}

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, async text() { return JSON.stringify(body); } };
}

// Routes GET/POST /api/sessions/{id}[/file|/lean-check] against per-session
// fixtures, and records every call for assertions on exactly what the
// cascade logic did/didn't touch.
//
// It also emulates the adapter's LEDGER (PLAN 4.4): the real adapter records
// every manual write and every check/rebuild verdict as code_steps and serves
// them back per-declaration through GET .../target-status — which is what the
// status engine (and therefore every pane chip) reads. The maps below track
// the same facts from the calls this stub serves, so a save that posts an
// "error" verdict is reflected in the next manifest exactly like production.
function makeEditFetch(calls, { sessionDetails = {}, writeResponses = {}, checkResponses = {}, rebuildResponses = {} } = {}) {
  const ledgerFiles = new Map(); // repo-relative path -> newest written content
  const ledgerChecks = new Map(); // repo-relative path -> { status, detail }
  const declarationPattern = (name) => new RegExp(`\\b(?:theorem|lemma|def|abbrev)\\s+${name}\\b`);
  return async (url, requestOptions = {}) => {
    const method = requestOptions.method || "GET";
    const body = requestOptions.body ? JSON.parse(requestOptions.body) : null;
    if (String(url).includes("/target-status")) {
      const requested = decodeURIComponent(String(url).split("declarations=")[1] || "").split(",").filter(Boolean);
      const targets = requested.map((name) => {
        // A declaration's recorded file: the newest written content that
        // contains it (covers renames), else its conventional "<name>.lean"
        // when a write or verdict touched that path. Nothing known → the
        // index has never seen it, same as a fresh adapter.
        let recordedPath = null;
        for (const [candidate, content] of ledgerFiles) {
          if (declarationPattern(name).test(content)) recordedPath = candidate;
        }
        if (!recordedPath) {
          const conventional = `${name}.lean`;
          if (ledgerFiles.has(conventional) || ledgerChecks.has(conventional)) recordedPath = conventional;
        }
        if (!recordedPath) return { declaration_name: name, recorded: false };
        const content = ledgerFiles.get(recordedPath) ?? "";
        const check = ledgerChecks.get(recordedPath) || null;
        return {
          declaration_name: name,
          recorded: true,
          path: recordedPath,
          module_name: `${NAMESPACE}.${recordedPath.replace(/\.lean$/, "")}`,
          kind: "proof",
          exists: true,
          declaration_present: true,
          has_sorry: /\b(?:sorry|admit)\b/.test(content),
          check_status: check ? check.status : "ok",
          check_detail: check ? check.detail : null,
          check_author: "user",
          content
        };
      });
      return jsonResponse(200, { project_id: "adapter-project-1", slug: "project-1", targets });
    }
    calls.push({ url: String(url), method, body });
    const match = String(url).match(/\/api\/sessions\/([^/]+)(?:\/(file|lean-check|rebuild))?$/);
    if (!match) return jsonResponse(404, { detail: "unmapped url in test fetch stub" });
    const sessionId = decodeURIComponent(match[1]);
    const kind = match[2] || "detail";
    if (kind === "detail") {
      return jsonResponse(200, sessionDetails[sessionId] || { code_steps: [] });
    }
    if (kind === "file") {
      if (body?.path) ledgerFiles.set(body.path, String(body.content ?? ""));
      const response = writeResponses[sessionId];
      return jsonResponse(200, typeof response === "function" ? response(body) : (response || { unchanged: false, code_step: null, note: null }));
    }
    if (kind === "lean-check") {
      const response = checkResponses[sessionId];
      const resolved = typeof response === "function" ? response(body) : (response || { path: body?.path, status: "ok", detail: null });
      const verdictPath = resolved?.path || body?.path;
      if (verdictPath) ledgerChecks.set(verdictPath, { status: resolved.status, detail: resolved.detail ?? null });
      return jsonResponse(200, resolved);
    }
    if (kind === "rebuild") {
      // Defaults to a successful rebuild so every existing test (none of which
      // configure rebuildResponses) sees the cascade proceed exactly as before
      // this step was introduced -- only tests that care about a *failed*
      // rebuild need to override it.
      const response = rebuildResponses[sessionId];
      const resolved = typeof response === "function" ? response(body) : (response || { path: body?.path, status: "ok", detail: null });
      const verdictPath = resolved?.path || body?.path;
      if (verdictPath) ledgerChecks.set(verdictPath, { status: resolved.status, detail: resolved.detail ?? null });
      return jsonResponse(200, resolved);
    }
    return jsonResponse(404, { detail: "unmapped kind" });
  };
}

const NAMESPACE = "Lea.Project1";

function editedJob(overrides = {}) {
  return {
    jobKey: "project-1:theorem:compactness_criterion",
    status: "formalized",
    leaSessionId: "sess-a",
    projectSlug: "project-1",
    projectNamespace: NAMESPACE,
    ...overrides
  };
}

function dependentJob(overrides = {}) {
  return {
    jobKey: "project-1:theorem:compactness_corollary",
    status: "formalized",
    leaSessionId: "sess-b",
    projectSlug: "project-1",
    projectNamespace: NAMESPACE,
    ...overrides
  };
}

const EDITED_SESSION_DETAIL = {
  project_namespace: NAMESPACE,
  code_steps: [
    { path: "compactness_criterion.lean", seq: 1, code: "theorem compactness_criterion : True := by\n  sorry\n" }
  ]
};

test("edit start returns no_session when the item has never been formalized", async () => {
  const leaRepo = await makeLeaRepo();
  const state = makeState({ leaRepo, fetchImpl: makeEditFetch([]) });

  const res = await handleLeanPaneEditStart(
    { overleafProjectId: "project-1", targetKind: "theorem", targetLabel: "compactness_criterion" },
    state
  );

  assert.equal(res.statusCode, 404);
  assert.equal(res.body.error, "no_session");
});

test("edit start rejects an invalid payload before touching the adapter", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = makeState({ leaRepo, fetchImpl: makeEditFetch(calls) });

  const res = await handleLeanPaneEditStart({ overleafProjectId: "", targetKind: "theorem", targetLabel: "foo" }, state);

  assert.equal(res.statusCode, 400);
  assert.equal(res.body.error, "missing_project_id");
  assert.deepEqual(calls, []);
});

test("edit start resolves the session's current content and pre-save dependents", async () => {
  const leaRepo = await makeLeaRepo();
  await writeProof(leaRepo, "Lea/Project1/compactness_criterion.lean", "theorem compactness_criterion : True := by\n  sorry\n");
  await writeProof(
    leaRepo,
    "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  sorry\n"
  );
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: { a: editedJob() },
    fetchImpl: makeEditFetch(calls, { sessionDetails: { "sess-a": EDITED_SESSION_DETAIL } })
  });

  const res = await handleLeanPaneEditStart(
    { overleafProjectId: "project-1", targetKind: "theorem", targetLabel: "compactness_criterion" },
    state
  );

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.leaSessionId, "sess-a");
  assert.equal(res.body.path, "compactness_criterion.lean");
  assert.match(res.body.content, /theorem compactness_criterion : True/);
  assert.deepEqual(res.body.dependents.map((d) => d.targetLabel), ["compactness_corollary"]);
});

test("project namespace rename makes the pane and editor read the migrated working file", async () => {
  const leaRepo = await makeLeaRepo();
  const oldContent = "namespace Lea.Project1\n\ntheorem compactness_criterion : True := by\n  sorry\n\nend Lea.Project1\n";
  const newContent = "namespace Lea.RenamedProject\n\ntheorem compactness_criterion : True := by\n  trivial\n\nend Lea.RenamedProject\n";
  await writeProof(leaRepo, "Lea/RenamedProject/compactness_criterion.lean", newContent);
  const state = makeState({
    leaRepo,
    jobs: {
      a: editedJob({
        declarationName: "compactness_criterion",
        projectNamespace: "Lea.Project1",
        recordedProofPath: "workspace/proofs/Lea/Project1/compactness_criterion.lean",
        moduleName: "Lea.Project1.compactness_criterion",
        finishedAt: "2026-08-01T00:00:00.000Z"
      })
    },
    fetchImpl: async (url) => {
      const value = String(url);
      if (value.endsWith("/api/projects/by-slug/project-1/identity")) {
        return jsonResponse(200, {
          projectId: "adapter-project-1",
          slug: "project-1",
          projectName: "Renamed Project",
          namespace: "Lea.RenamedProject",
          repoPath: "proofs/Lea/RenamedProject",
          exists: true
        });
      }
      if (value.includes("/api/projects/by-slug/project-1/target-status")) {
        return jsonResponse(200, {
          project_id: "adapter-project-1",
          slug: "project-1",
          targets: [{
            declaration_name: "compactness_criterion",
            recorded: true,
            path: "compactness_criterion.lean",
            module_name: "Lea.RenamedProject.compactness_criterion",
            kind: "proof",
            exists: true,
            declaration_present: true,
            has_sorry: false,
            check_status: "ok",
            content: newContent
          }]
        });
      }
      if (value.endsWith("/api/sessions/sess-a")) {
        return jsonResponse(200, {
          project_namespace: "Lea.RenamedProject",
          code_steps: [{ path: "compactness_criterion.lean", seq: 1, code: oldContent }]
        });
      }
      return jsonResponse(404, { detail: "unmapped url" });
    }
  });
  const files = [{
    path: "main.tex",
    content: [
      "\\begin{theorem}\\label{thm:x}",
      "% lea: formalize label=compactness_criterion",
      "Every open cover has a finite subcover.",
      "\\end{theorem}"
    ].join("\n")
  }];

  const manifest = await handleLeanPaneManifest({ overleafProjectId: "project-1", files }, state);
  assert.equal(manifest.statusCode, 200);
  assert.equal(manifest.body.items[0].leanArtifactContent, newContent);
  assert.equal(
    manifest.body.items[0].leanArtifactPath,
    "workspace/proofs/Lea/RenamedProject/compactness_criterion.lean"
  );
  assert.doesNotMatch(manifest.body.items[0].leanArtifactContent, /namespace Lea\.Project1/);

  const edit = await handleLeanPaneEditStart(
    { overleafProjectId: "project-1", targetKind: "theorem", targetLabel: "compactness_criterion" },
    state
  );
  assert.equal(edit.statusCode, 200);
  assert.equal(edit.body.content, newContent);
});

test("edit save: a proof-body-only edit never triggers a cascade re-check", async () => {
  const leaRepo = await makeLeaRepo();
  await writeProof(
    leaRepo,
    "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  sorry\n"
  );
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: { a: editedJob(), b: dependentJob() },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-2" }, note: null } },
      checkResponses: { "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null } }
    })
  });

  const res = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      // same header ("theorem compactness_criterion : True"), different proof body
      content: "theorem compactness_criterion : True := by\n  trivial\n"
    },
    state
  );

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.ownResult.classification.kind, "proof-only");
  assert.deepEqual(res.body.dependentsImpact, []);
  // no lean-check call was ever made against the dependent's session
  assert.ok(!calls.some((c) => c.url.includes("/sess-b/")));
});

test("edit save: a signature edit cascades and reports a broken dependent, attributed to the upstream edit", async () => {
  const leaRepo = await makeLeaRepo();
  await writeProof(
    leaRepo,
    "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  sorry\n"
  );
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: { a: editedJob(), b: dependentJob() },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-2" }, note: null } },
      checkResponses: {
        "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null }
      },
      // The dependent's verdict comes from a REAL `lake build` of its module
      // (live testing showed its warm lean-check can spuriously pass against
      // a stale compiled import) -- so breakage is signaled here, not via
      // checkResponses.
      rebuildResponses: {
        "sess-b": (body) => ({ path: body.path, status: "error", detail: "unknown identifier: compactness_criterion" })
      }
    })
  });

  const res = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      // hypothesis added -- header changes, so this is a signature edit
      content: "theorem compactness_criterion (h : True) : True := by\n  sorry\n"
    },
    state
  );

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.ownResult.classification.kind, "signature");
  assert.equal(res.body.dependentsImpact.length, 1);
  const impact = res.body.dependentsImpact[0];
  assert.equal(impact.targetLabel, "compactness_corollary");
  assert.equal(impact.status, "invalid");
  assert.equal(impact.attributed, true);
  // via/editedAt: attribution for the self-repair offer (feature spec Part 2)
  assert.equal(impact.brokenByUpstream.targetLabel, "compactness_criterion");
  assert.equal(impact.brokenByUpstream.renamed, false);
  assert.equal(impact.brokenByUpstream.via, "edit");
  assert.ok(impact.brokenByUpstream.editedAt);

  // the cascade check was recorded against the DEPENDENT's own session, with
  // author=cascade, not against the edited target's session
  const cascadeCall = calls.find((c) => c.url.endsWith("/sess-b/lean-check"));
  assert.ok(cascadeCall);
  assert.equal(cascadeCall.body.author, "cascade");
  assert.equal(cascadeCall.body.path, "compactness_corollary.lean");

  // the edited module (sess-a) was rebuilt for real -- once, and before the
  // dependent's check -- so the dependent's "still valid"/"invalid" verdict
  // above is against A's *current* signature, not a stale compiled artifact.
  const rebuildCalls = calls.filter((c) => c.url.endsWith("/sess-a/rebuild"));
  assert.equal(rebuildCalls.length, 1);
  assert.equal(rebuildCalls[0].body.path, "compactness_criterion.lean");
  assert.ok(calls.indexOf(rebuildCalls[0]) < calls.indexOf(cascadeCall));

  // both the edited item's own job and the broken dependent's job carry the
  // fresh verdict, so getTheoremStatus's override picks it up for both on the
  // next manifest refresh (not just the edited item)
  assert.equal(state.jobs.a.lastEditCheckStatus, "ok");
  assert.equal(state.jobs.b.lastEditCheckStatus, "error");
  assert.match(state.jobs.b.lastEditCheckDetail, /unknown identifier/);

  // a same-name signature edit (not a rename) must not touch declarationName --
  // only classification.kind === "renamed" should
  assert.equal(state.jobs.a.declarationName, undefined);
});

test("edit save: a rename updates the cached declarationName on EVERY job under the item's key", async () => {
  // Regression test for a bug found live: the pane's item identity
  // (targetLabel) correctly stays pinned to the LaTeX marker's label=...
  // forever, but job.declarationName is a *cache* of "what Lean symbol
  // this session's file currently defines," taken once at formalize time.
  // Nothing refreshed it on a manual rename, so readLeanPaneArtifactFromSession
  // (which selects the latest code_step whose content still contains
  // declarationName) kept falling back to the pre-rename step -- the edited
  // item's own displayed code block silently stayed stale even though the
  // rename itself, and the cascade break on its dependent, were both detected
  // and reported correctly. See docs/FEATURE-overleaf-lean-pane-manual-edit.md.
  //
  // Second live round of the same bug: updating only the NEWEST session-linked
  // job left older records under the same key stale. After the item had been
  // repaired, the rename landed on the repair job while getTheoremStatus's
  // newest-terminal branch resolved through the original formalize job -- so
  // the artifact lookup went hunting for the old name again. All jobs under
  // the key describe the same one working file; they must all agree.
  const leaRepo = await makeLeaRepo();
  await writeProof(
    leaRepo,
    "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  sorry\n"
  );
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: {
      // the older formalize run for the same item -- must be renamed too
      a0: editedJob({ declarationName: "compactness_criterion", finishedAt: "2026-01-01T00:00:00.000Z" }),
      a: editedJob({ declarationName: "compactness_criterion", finishedAt: "2026-01-02T00:00:00.000Z" }),
      b: dependentJob()
    },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-2" }, note: null } },
      checkResponses: {
        "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null }
      },
      rebuildResponses: {
        "sess-b": (body) => ({ path: body.path, status: "error", detail: "unknown identifier: compactness_criterion" })
      }
    })
  });

  const res = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      // same signature shape, different name -- classifyEdit's "renamed" case
      content: "theorem compactness_thm : True := by\n  sorry\n"
    },
    state
  );

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.ownResult.classification.kind, "renamed");
  assert.equal(res.body.ownResult.classification.from, "compactness_criterion");
  assert.equal(res.body.ownResult.classification.to, "compactness_thm");

  // the actual fix: the cached identity now matches the file's real, current
  // declaration -- not what it was when first formalized -- on every job
  // record for this item, not just the newest session-linked one
  assert.equal(state.jobs.a.declarationName, "compactness_thm");
  assert.equal(state.jobs.a0.declarationName, "compactness_thm");
  // the dependent's own cache is a different item's file -- untouched
  assert.equal(state.jobs.b.declarationName, undefined);

  // unaffected by the fix: the pane's own identity for this item stays pinned
  // to the LaTeX label, so the cascade impact on the dependent is still
  // reported by that stable name, not the new Lean symbol
  assert.equal(res.body.dependentsImpact[0].brokenByUpstream.targetLabel, "compactness_criterion");
  assert.equal(res.body.dependentsImpact[0].brokenByUpstream.renamed, true);
  assert.equal(res.body.dependentsImpact[0].brokenByUpstream.via, "edit");
});

test("edit save: a proof-only edit never triggers a rebuild (no dependents to verify)", async () => {
  const leaRepo = await makeLeaRepo();
  await writeProof(
    leaRepo,
    "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  sorry\n"
  );
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: { a: editedJob(), b: dependentJob() },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-2" }, note: null } },
      checkResponses: { "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null } }
    })
  });

  const res = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      content: "theorem compactness_criterion : True := by\n  trivial\n"
    },
    state
  );

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.ownResult.classification.kind, "proof-only");
  assert.ok(!calls.some((c) => c.url.endsWith("/rebuild")));
});

test("edit save: fixing a previously-broken file re-verifies dependents even though the fix itself is proof-only", async () => {
  // Regression test for a real bug: classifyEdit/cascadeRequired only reason
  // about whether THIS edit could newly break a dependent -- a proof-only fix
  // is correctly exempt from that (proof irrelevance). But if the file was
  // previously broken and a dependent got left holding a stale "unconfirmed"
  // verdict (via the rebuild-failure fail-closed path), fixing the proof back
  // up must still re-verify that dependent, or its chip is stuck forever.
  const leaRepo = await makeLeaRepo();
  await writeProof(
    leaRepo,
    "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  sorry\n"
  );
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: {
      // Both previously left broken/unconfirmed by an earlier bad edit.
      a: editedJob({ lastEditCheckStatus: "error", lastEditCheckDetail: "unsolved goals" }),
      b: dependentJob({ lastEditCheckStatus: "error", lastEditCheckDetail: "Not re-verified: rebuilding compactness_criterion failed, so this dependent's status is unconfirmed." })
    },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-3" }, note: null } },
      checkResponses: {
        "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null },
        "sess-b": { path: "compactness_corollary.lean", status: "ok", detail: null }
      }
    })
  });

  const res = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      // same header as EDITED_SESSION_DETAIL's -- proof-only fix
      content: "theorem compactness_criterion : True := by\n  trivial\n"
    },
    state
  );

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.ownResult.classification.kind, "proof-only");

  // A proof-only classification alone would normally skip the cascade -- but
  // this file just recovered from a failing state, so it must run anyway.
  const rebuildCalls = calls.filter((c) => c.url.endsWith("/sess-a/rebuild"));
  assert.equal(rebuildCalls.length, 1);
  const cascadeCall = calls.find((c) => c.url.endsWith("/sess-b/lean-check"));
  assert.ok(cascadeCall);

  assert.equal(res.body.dependentsImpact.length, 1);
  assert.equal(res.body.dependentsImpact[0].status, "reverified");

  // the dependent's stale "unconfirmed" chip is cleared back to ok, not left
  // stuck on the old verdict forever
  assert.equal(state.jobs.b.lastEditCheckStatus, "ok");
});

test("edit save: a proof-only edit to an already-healthy file still skips the cascade", async () => {
  // Companion regression guard: the recovery trigger must not turn EVERY
  // proof-only edit into a cascade -- only ones that actually recover from a
  // prior failure. Same content shape as the always-skip test above, but
  // explicit about why: lastEditCheckStatus was never "error" to begin with.
  const leaRepo = await makeLeaRepo();
  await writeProof(
    leaRepo,
    "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  sorry\n"
  );
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: { a: editedJob(), b: dependentJob() }, // no lastEditCheckStatus at all
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-2" }, note: null } },
      checkResponses: { "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null } }
    })
  });

  const res = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      content: "theorem compactness_criterion : True := by\n  trivial\n"
    },
    state
  );

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.ownResult.classification.kind, "proof-only");
  assert.deepEqual(res.body.dependentsImpact, []);
  assert.ok(!calls.some((c) => c.url.includes("/sess-b/")));
});

test("edit save: a rebuild failure marks dependents unknown instead of trusting a stale recheck", async () => {
  const leaRepo = await makeLeaRepo();
  await writeProof(
    leaRepo,
    "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  sorry\n"
  );
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: { a: editedJob(), b: dependentJob() },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-2" }, note: null } },
      checkResponses: { "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null } },
      // The fast own-check can pass (e.g. via sorry-recovery) even when a real
      // build of the same module fails outright.
      rebuildResponses: { "sess-a": { path: "compactness_criterion.lean", status: "error", detail: "compactness_criterion.lean:1:0: error: unexpected token" } }
    })
  });

  const res = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      content: "theorem compactness_criterion (h : True) : True := by\n  sorry\n"
    },
    state
  );

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.dependentsImpact.length, 1);
  const impact = res.body.dependentsImpact[0];
  assert.equal(impact.targetLabel, "compactness_corollary");
  assert.equal(impact.status, "unknown");
  assert.match(impact.checkDetail, /unexpected token/);

  // never trusted a lean-check against the dependent once its own rebuild
  // failed -- no stale-olean-backed "still valid" gets reported
  assert.ok(!calls.some((c) => c.url.endsWith("/sess-b/lean-check")));

  // The dependent's own status CHIP (not just this impact message) must also
  // stop reading "valid" -- fail closed, same as a confirmed break, since
  // there's no separate "unconfirmed" chip state today. Regression guard: a
  // wrong chip is a different bug from a wrong message and needs its own check.
  assert.equal(state.jobs.b.lastEditCheckStatus, "error");
  assert.match(state.jobs.b.lastEditCheckDetail, /unconfirmed/);

  // End-to-end regression guard for the real bug: the data shape alone isn't
  // enough -- confirm what the pane actually RENDERS for this outcome is not
  // the same text as a genuine successful recheck. formatDependentOutcome had
  // no branch for status "unknown" and silently fell through to "re-checked,
  // still valid", which is exactly what was observed live even though the
  // rebuild correctly failed and the cascade correctly skipped the check.
  const rendered = formatDependentOutcome(impact);
  assert.doesNotMatch(rendered, /still valid/);
  assert.match(rendered, /could not be verified/);
});

test("edit save: a def body edit cascades even with an unchanged signature", async () => {
  const leaRepo = await makeLeaRepo();
  const defSessionDetail = {
    project_namespace: NAMESPACE,
    code_steps: [{ path: "locally_finite_family.lean", seq: 1, code: "def locally_finite_family : Prop := True\n" }]
  };
  await writeProof(
    leaRepo,
    "Lea/Project1/uses_it.lean",
    "import Lea.Project1.locally_finite_family\ntheorem uses_it : True := by\n  sorry\n"
  );
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: {
      a: { jobKey: "project-1:definition:locally_finite_family", status: "formalized", leaSessionId: "sess-def", projectSlug: "project-1", projectNamespace: NAMESPACE },
      b: { jobKey: "project-1:theorem:uses_it", status: "formalized", leaSessionId: "sess-uses", projectSlug: "project-1", projectNamespace: NAMESPACE }
    },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-def": defSessionDetail },
      writeResponses: { "sess-def": { unchanged: false, code_step: { id: "step-x" }, note: null } },
      checkResponses: {
        "sess-def": { path: "locally_finite_family.lean", status: "ok", detail: null },
        "sess-uses": { path: "uses_it.lean", status: "ok", detail: null }
      }
    })
  });

  const res = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "definition",
      targetLabel: "locally_finite_family",
      // same signature ("def locally_finite_family : Prop"), different value
      content: "def locally_finite_family : Prop := False\n"
    },
    state
  );

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.ownResult.classification.kind, "definition-body");
  assert.equal(res.body.dependentsImpact.length, 1);
  assert.equal(res.body.dependentsImpact[0].status, "reverified");
  assert.equal(res.body.dependentsImpact[0].brokenByUpstream, null);
});

test("edit save: no-op content short-circuits with no lean-check or cascade calls", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: { a: editedJob() },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: true, code_step: null, note: null } }
    })
  });

  const res = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      content: EDITED_SESSION_DETAIL.code_steps[0].code
    },
    state
  );

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.unchanged, true);
  assert.ok(!calls.some((c) => c.url.endsWith("/lean-check")));
});

test("edit save is blocked with 409 while a run is already active for the item", async () => {
  const leaRepo = await makeLeaRepo();
  const state = makeState({
    leaRepo,
    jobs: { a: editedJob({ status: "in_progress" }) },
    fetchImpl: makeEditFetch([])
  });

  const res = await handleLeanPaneEditSave(
    { overleafProjectId: "project-1", targetKind: "theorem", targetLabel: "compactness_criterion", content: "theorem compactness_criterion : True := by\n  sorry\n" },
    state
  );

  assert.equal(res.statusCode, 409);
  assert.equal(res.body.error, "run_in_progress");
});

test("edit save: a dependent with its own active run is skipped, not raced", async () => {
  const leaRepo = await makeLeaRepo();
  await writeProof(
    leaRepo,
    "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  sorry\n"
  );
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: { a: editedJob(), b: dependentJob({ status: "in_progress" }) },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-2" }, note: null } },
      checkResponses: { "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null } }
    })
  });

  const res = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      content: "theorem compactness_criterion (h : True) : True := by\n  sorry\n"
    },
    state
  );

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.dependentsImpact.length, 1);
  assert.equal(res.body.dependentsImpact[0].busy, true);
  assert.ok(!calls.some((c) => c.url.endsWith("/sess-b/lean-check")));
});

// Regression test for the bug report: editing a proof to be broken must flip
// the pane's own status chip to invalid, not just surface a small-print note
// alongside a chip that still says valid. Exercises the real path a user
// hits: handleLeanPaneManifest (which computes the chip's status) before and
// after handleLeanPaneEditSave.
test("edit save flips the item's own status chip to invalid on the next manifest refresh (not just a side note)", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: { a: editedJob() }, // status: "formalized" -- what the pane read as "valid" before the bugfix
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-2" }, note: null } },
      checkResponses: {
        "sess-a": { path: "compactness_criterion.lean", status: "error", detail: "unknown identifier: foo" }
      }
    })
  });

  const tex = [
    "\\begin{theorem}\\label{thm:x}",
    "% lea: formalize label=compactness_criterion",
    "Every open cover has a finite subcover.",
    "\\end{theorem}"
  ].join("\n");
  const files = [{ path: "main.tex", content: tex }];

  const before = await handleLeanPaneManifest({ overleafProjectId: "project-1", files }, state);
  assert.equal(before.body.items[0].status, "valid");

  const save = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      content: "theorem compactness_criterion : True := by\n  exact foo\n"
    },
    state
  );
  assert.equal(save.statusCode, 200);
  assert.equal(save.body.ownResult.checkStatus, "error");

  const after = await handleLeanPaneManifest({ overleafProjectId: "project-1", files }, state);
  assert.equal(after.body.items[0].status, "invalid");
  assert.match(after.body.items[0].message, /unknown identifier: foo/);

  // and the override clears itself once a later edit compiles again
  state.fetchImpl = makeEditFetch(calls, {
    sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
    writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-3" }, note: null } },
    checkResponses: { "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null } }
  });
  await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      content: "theorem compactness_criterion : True := by\n  trivial\n"
    },
    state
  );
  const fixed = await handleLeanPaneManifest({ overleafProjectId: "project-1", files }, state);
  assert.equal(fixed.body.items[0].status, "valid");
});

// Regression for the other half of the rename fix: updating
// linkedJob.declarationName fixed readLeanPaneArtifactFromSession (the pane's
// DISPLAY of the artifact), but loadEditableSessionFile -- the EDIT path's
// own step lookup -- still searched by the LaTeX label. After a rename, the
// newest steps contain only the new name, so the next "Edit" served the last
// pre-rename snapshot into the editor (and a save would have written that
// stale content back over the renamed file). Both handlers now search, parse,
// and classify by the job's current declarationName.
test("edit start after a rename serves the post-rename content, and a second rename still classifies as a rename", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const sessionDetails = {
    "sess-a": {
      project_namespace: NAMESPACE,
      code_steps: [
        { path: "compactness_criterion.lean", seq: 1, code: "theorem compactness_criterion : True := by\n  sorry\n" }
      ]
    }
  };
  const state = makeState({
    leaRepo,
    jobs: { a: editedJob({ declarationName: "compactness_criterion" }) },
    fetchImpl: makeEditFetch(calls, { sessionDetails })
  });

  // First rename: criterion -> criterion_v2.
  const firstSave = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      content: "theorem compactness_criterion_v2 : True := by\n  sorry\n"
    },
    state
  );
  assert.equal(firstSave.statusCode, 200);
  assert.equal(firstSave.body.ownResult.classification.kind, "renamed");
  assert.equal(state.jobs.a.declarationName, "compactness_criterion_v2");

  // The adapter records the write as a new code_step; the old step (still
  // containing the OLD name) remains in the session history -- exactly the
  // shape that used to trap the label-based lookup.
  sessionDetails["sess-a"].code_steps.push({
    path: "compactness_criterion.lean",
    seq: 2,
    code: "theorem compactness_criterion_v2 : True := by\n  sorry\n"
  });

  const start = await handleLeanPaneEditStart(
    { overleafProjectId: "project-1", targetKind: "theorem", targetLabel: "compactness_criterion" },
    state
  );
  assert.equal(start.statusCode, 200);
  // the post-rename content, NOT the seq-1 pre-rename snapshot
  assert.match(start.body.content, /compactness_criterion_v2/);

  // Second rename: v2 -> v3. expectedName must be the CURRENT name (v2) for
  // classifyEdit to see this as a rename; searching/classifying by the
  // original label made it come out as "signature" and skipped the
  // declarationName refresh.
  const secondSave = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      content: "theorem compactness_criterion_v3 : True := by\n  sorry\n"
    },
    state
  );
  assert.equal(secondSave.statusCode, 200);
  assert.equal(secondSave.body.ownResult.classification.kind, "renamed");
  assert.equal(secondSave.body.ownResult.classification.from, "compactness_criterion_v2");
  assert.equal(secondSave.body.ownResult.classification.to, "compactness_criterion_v3");
  assert.equal(state.jobs.a.declarationName, "compactness_criterion_v3");
});

// Regression for cascade attribution after a DEPENDENT was itself renamed:
// the reverse index hands back the dependent file's parsed declaration name,
// but jobs stay keyed by the LaTeX label forever. Once the dependent's
// declarationName diverged from its key (via its own rename), the label-keyed
// lookup missed and the dependent was reported "unknown/unattributed" -- its
// chip never received cascade verdicts. resolveDependentSession now falls
// back to scanning jobs by their current declarationName.
test("edit save: a cascade still attributes a dependent that was previously renamed", async () => {
  const leaRepo = await makeLeaRepo();
  // The dependent's FILE declares the renamed symbol...
  await writeProof(
    leaRepo,
    "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem renamed_corollary : True := by\n  sorry\n"
  );
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: {
      a: editedJob({ declarationName: "compactness_criterion" }),
      // ...while its job stays keyed under the original LaTeX label, with the
      // rename branch's refreshed declarationName cache.
      b: dependentJob({ declarationName: "renamed_corollary" })
    },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-2" }, note: null } },
      checkResponses: {
        "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null }
      },
      rebuildResponses: {
        "sess-b": (body) => ({ path: body.path, status: "error", detail: "unknown identifier: compactness_criterion" })
      }
    })
  });

  const res = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      // signature change (binder added) -- forces the cascade
      content: "theorem compactness_criterion (n : Nat) : True := by\n  sorry\n"
    },
    state
  );

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.dependentsImpact.length, 1);
  const impact = res.body.dependentsImpact[0];
  assert.equal(impact.targetLabel, "renamed_corollary");
  // before the fix: attributed false, no sess-b call, no chip verdict
  assert.equal(impact.attributed, true);
  assert.equal(impact.status, "invalid");
  assert.ok(calls.some((c) => c.url.endsWith("/sess-b/lean-check")));
  assert.equal(state.jobs.b.lastEditCheckStatus, "error");
  assert.match(state.jobs.b.lastEditCheckDetail, /unknown identifier/);
});

// Regression for the live bug report: rename compactness_criterion ->
// compactness_thm via manual edit (works), then try to edit the item again
// to rename it back -- and get blocked with "No Lea session is recorded for
// this item yet. Formalize it first." Root cause: the extension identifies
// an item by its CURRENT declaration name (paneItemToEditTarget:
// leanDeclarationName || label), which after the rename is the NEW name --
// but jobs stay keyed by the LaTeX label, so resolveEditSession's key lookup
// missed. It now bridges via the jobs' declarationName cache.
test("edit start/save still resolve the session when the pane sends the post-rename declaration name", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = makeState({
    leaRepo,
    // post-rename state: job keyed under the LaTeX label, declarationName
    // refreshed to the new symbol by the first rename's save
    jobs: { a: editedJob({ declarationName: "compactness_thm" }) },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: {
        "sess-a": {
          project_namespace: NAMESPACE,
          code_steps: [
            { path: "compactness_criterion.lean", seq: 1, code: "theorem compactness_criterion : True := by\n  sorry\n" },
            { path: "compactness_criterion.lean", seq: 2, code: "theorem compactness_thm : True := by\n  sorry\n" }
          ]
        }
      },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-3" }, note: null } },
      checkResponses: { "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null } }
    })
  });

  // What the extension sends after the rename: the NEW name as targetLabel.
  const start = await handleLeanPaneEditStart(
    { overleafProjectId: "project-1", targetKind: "theorem", targetLabel: "compactness_thm" },
    state
  );
  // was: 404 no_session ("Formalize it first.")
  assert.equal(start.statusCode, 200);
  assert.equal(start.body.leaSessionId, "sess-a");
  assert.match(start.body.content, /compactness_thm/);

  // ...and renaming it BACK to the original name round-trips.
  const save = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_thm",
      content: "theorem compactness_criterion : True := by\n  sorry\n"
    },
    state
  );
  assert.equal(save.statusCode, 200);
  assert.equal(save.body.ownResult.classification.kind, "renamed");
  assert.equal(save.body.ownResult.classification.from, "compactness_thm");
  assert.equal(save.body.ownResult.classification.to, "compactness_criterion");
  assert.equal(state.jobs.a.declarationName, "compactness_criterion");

  // a definition item must not hijack a theorem-keyed job via the fallback
  const wrongKind = await handleLeanPaneEditStart(
    { overleafProjectId: "project-1", targetKind: "definition", targetLabel: "compactness_thm" },
    state
  );
  assert.equal(wrongKind.statusCode, 404);
  assert.equal(wrongKind.body.error, "no_session");
});

// Recipe 4 end-to-end (live report: "downstream theorems did not change their
// status"). Two layers to this:
//   1. The DIRECT dependent must flip: its cascade lean-check fails, the
//      verdict lands on its job, and the next manifest renders its chip
//      invalid with the compiler's own message.
//   2. The TRANSITIVE dependent (import chain reaches the edit only through
//      the broken middle module) is where a real hole lived: its warm
//      cascade check resolves imports against compiled .oleans, and only the
//      EDITED module was rebuilt -- the broken middle module's stale .olean
//      still exports its old, working self, so the check spuriously passed
//      and the item kept reading "valid". Compilation is transitive, so
//      breakage now propagates down the import graph regardless.
test("recipe 4: a signature edit flips the broken dependent AND propagates to transitive dependents", async () => {
  const leaRepo = await makeLeaRepo();
  await writeProof(
    leaRepo,
    "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  exact compactness_criterion\n"
  );
  await writeProof(
    leaRepo,
    "Lea/Project1/heine_borel_application.lean",
    "import Lea.Project1.compactness_corollary\ntheorem heine_borel_application : True := by\n  exact compactness_corollary\n"
  );
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: {
      a: editedJob({ declarationName: "compactness_criterion" }),
      b: dependentJob({ declarationName: "compactness_corollary" }),
      c: dependentJob({
        jobKey: "project-1:theorem:heine_borel_application",
        leaSessionId: "sess-c",
        declarationName: "heine_borel_application"
      })
    },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-2" }, note: null } },
      checkResponses: {
        "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null }
      },
      rebuildResponses: {
        // verdicts now come from real per-dependent `lake build`s
        "sess-b": (body) => ({ path: body.path, status: "error", detail: "type mismatch: compactness_criterion now expects (h : True)" }),
        // even if the transitive dependent's own build SPURIOUSLY passes
        // (e.g. an adapter-side caching artifact), the companion must not
        // believe it -- import-graph propagation overrules it
        "sess-c": (body) => ({ path: body.path, status: "ok", detail: null })
      }
    })
  });

  const save = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      // recipe 4: add a hypothesis -- same name, changed signature
      content: "theorem compactness_criterion (h : True) : True := by\n  trivial\n"
    },
    state
  );

  assert.equal(save.statusCode, 200);
  assert.equal(save.body.ownResult.classification.kind, "signature");
  const impactByLabel = Object.fromEntries(save.body.dependentsImpact.map((d) => [d.targetLabel, d]));

  // direct dependent: genuinely re-checked, genuinely failed
  assert.equal(impactByLabel.compactness_corollary.status, "invalid");
  assert.equal(impactByLabel.compactness_corollary.brokenByUpstream.targetLabel, "compactness_criterion");
  assert.equal(state.jobs.b.lastEditCheckStatus, "error");
  assert.match(state.jobs.b.lastEditCheckDetail, /type mismatch/);

  // transitive dependent: its spurious "ok" was overruled by propagation
  assert.equal(impactByLabel.heine_borel_application.status, "invalid");
  assert.equal(impactByLabel.heine_borel_application.brokenByUpstream.viaModule, "Lea.Project1.compactness_corollary");
  assert.equal(state.jobs.c.lastEditCheckStatus, "error");
  assert.match(state.jobs.c.lastEditCheckDetail, /compactness_corollary no longer compiles/);

  // ...and the pane chips agree on the next manifest refresh
  const files = [{
    path: "main.tex",
    content: [
      "\\begin{theorem}\\label{thm:criterion}",
      "% lea: formalize label=compactness_criterion",
      "S.",
      "\\end{theorem}",
      "\\begin{theorem}\\label{thm:corollary}",
      "% lea: formalize label=compactness_corollary uses={compactness_criterion}",
      "S.",
      "\\end{theorem}",
      "\\begin{corollary}\\label{thm:application}",
      "% lea: formalize label=heine_borel_application uses={compactness_corollary}",
      "S.",
      "\\end{corollary}"
    ].join("\n")
  }];
  const manifest = await handleLeanPaneManifest({ overleafProjectId: "project-1", files }, state);
  const byLabel = Object.fromEntries(manifest.body.items.map((item) => [item.leanDeclarationName, item]));
  assert.equal(byLabel.compactness_criterion.status, "valid"); // its own edit compiles
  assert.equal(byLabel.compactness_corollary.status, "invalid");
  assert.match(byLabel.compactness_corollary.message, /type mismatch/);
  assert.equal(byLabel.heine_borel_application.status, "invalid");
  assert.match(byLabel.heine_borel_application.message, /compactness_corollary no longer compiles/);
});

// Live bug report reproduction (2026-07-08): formalize A (compactness_criterion)
// and B (compactness_corollary, which does `exact compactness_criterion`), then
// RENAME A's declaration to compactness_crt via a manual pane edit. B now
// references a symbol that no longer exists and cannot compile, yet its chip
// stayed "valid". Mirrors the exact on-disk state found in the workspace
// (jobs.json showed B carried NO lastEditCheckStatus -- the cascade never wrote
// to it). The discriminator vs. "recipe 4" above is the TRIGGER: a pure rename
// (classification "renamed"), not a signature change.
test("recipe: a pure RENAME of an upstream flips a dependent that references the old name", async () => {
  const leaRepo = await makeLeaRepo();
  await writeProof(
    leaRepo,
    "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  exact compactness_criterion\n"
  );
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: {
      a: editedJob({ declarationName: "compactness_criterion" }),
      b: dependentJob({ declarationName: "compactness_corollary" })
    },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-2" }, note: null } },
      // the renamed upstream compiles fine on its own...
      checkResponses: { "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null } },
      rebuildResponses: {
        "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null },
        // ...but the dependent's `lake build` fails: it still says `exact
        // compactness_criterion`, which no longer exists.
        "sess-b": (body) => ({ path: body.path, status: "error", detail: "unknown identifier 'compactness_criterion'" })
      }
    })
  });

  const save = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      // pure rename: criterion -> crt, nothing else changed
      content: "theorem compactness_crt : True := by\n  trivial\n"
    },
    state
  );

  assert.equal(save.statusCode, 200);
  assert.equal(save.body.ownResult.classification.kind, "renamed");
  assert.equal(save.body.ownResult.classification.to, "compactness_crt");
  assert.equal(state.jobs.a.declarationName, "compactness_crt");

  // The heart of the report: B must be re-checked and flipped to invalid.
  assert.equal(save.body.dependentsImpact.length, 1);
  const impact = save.body.dependentsImpact[0];
  assert.equal(impact.targetLabel, "compactness_corollary");
  assert.equal(impact.status, "invalid");
  assert.equal(impact.brokenByUpstream.renamed, true);
  assert.equal(state.jobs.b.lastEditCheckStatus, "error");
  assert.match(state.jobs.b.lastEditCheckDetail, /unknown identifier/);
});

// --- Self-repair Phase 2: persisted breakage attribution -------------------
// docs/FEATURE-overleaf-self-repair.md / docs/PLAN-overleaf-self-repair.md.
// The repair offer must be re-derivable after a manifest refresh or a
// companion restart, so the upstream attribution -- not just the failing
// verdict -- must persist on the dependent's job and flow to the pane item.

test("a breaking signature edit persists breakage attribution on the dependent's job (restart-surviving) and surfaces item.breakage", async () => {
  const leaRepo = await makeLeaRepo();
  await writeProof(
    leaRepo,
    "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  sorry\n"
  );
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: { a: editedJob(), b: dependentJob() },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-2" }, note: null } },
      checkResponses: { "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null } },
      rebuildResponses: {
        "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null },
        "sess-b": { path: "compactness_corollary.lean", status: "error", detail: "type mismatch" }
      }
    })
  });

  const save = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      content: "theorem compactness_criterion (h : True) : True := by\n  sorry\n"
    },
    state
  );
  assert.equal(save.statusCode, 200);

  // Persisted attribution on the dependent's job -- with the headers the
  // repair prompt needs.
  const breakage = state.jobs.b.lastEditBreakage;
  assert.equal(breakage.upstreamLabel, "compactness_criterion");
  assert.equal(breakage.classificationKind, "signature");
  assert.equal(breakage.via, "edit");
  assert.equal(breakage.beforeHeader, "theorem compactness_criterion : True");
  assert.equal(breakage.afterHeader, "theorem compactness_criterion (h : True) : True");
  // ...and it reached disk (jobs.json), so it survives a companion restart.
  const persisted = JSON.parse(await fs.readFile(state.jobsPath, "utf8"));
  assert.equal(persisted.b.lastEditBreakage.upstreamLabel, "compactness_criterion");

  // The manifest surfaces it as item.breakage: not self-broken, not
  // suppressed (the upstream item itself still compiles).
  const files = [{
    path: "main.tex",
    content: [
      "\\begin{theorem}\\label{thm:criterion}",
      "% lea: formalize label=compactness_criterion",
      "S.",
      "\\end{theorem}",
      "\\begin{theorem}\\label{thm:corollary}",
      "% lea: formalize label=compactness_corollary uses={compactness_criterion}",
      "S.",
      "\\end{theorem}"
    ].join("\n")
  }];
  const manifest = await handleLeanPaneManifest({ overleafProjectId: "project-1", files }, state);
  const byLabel = Object.fromEntries(manifest.body.items.map((item) => [item.leanDeclarationName, item]));
  assert.equal(byLabel.compactness_criterion.breakage, undefined); // upstream compiles; no breakage
  assert.equal(byLabel.compactness_corollary.status, "invalid");
  assert.equal(byLabel.compactness_corollary.breakage.upstreamLabel, "compactness_criterion");
  assert.equal(byLabel.compactness_corollary.breakage.selfBroken, false);
  assert.equal(byLabel.compactness_corollary.breakage.repairSuppressed, undefined);
});

test("an edit that breaks the item ITSELF records self-attribution, and dependents' repair offers are suppressed while upstream is broken", async () => {
  const leaRepo = await makeLeaRepo();
  await writeProof(
    leaRepo,
    "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  sorry\n"
  );
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: { a: editedJob(), b: dependentJob() },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-2" }, note: null } },
      checkResponses: { "sess-a": { path: "compactness_criterion.lean", status: "error", detail: "unexpected token" } },
      rebuildResponses: {
        // own compile is broken, so the pre-cascade rebuild of the edited
        // module fails too -> fail-closed marks the dependent unconfirmed
        "sess-a": { path: "compactness_criterion.lean", status: "error", detail: "unexpected token" }
      }
    })
  });

  const save = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      content: "theorem compactness_criterion : True := by\n  exact\n"
    },
    state
  );
  assert.equal(save.statusCode, 200);
  assert.equal(save.body.ownResult.classification.kind, "own-check-failed");

  // Self-attribution on the edited item's own job.
  assert.equal(state.jobs.a.lastEditBreakage.upstreamLabel, "compactness_criterion");
  assert.equal(state.jobs.a.lastEditBreakage.classificationKind, "own-check-failed");
  // Fail-closed attribution on the dependent too.
  assert.equal(state.jobs.b.lastEditBreakage.upstreamLabel, "compactness_criterion");

  const files = [{
    path: "main.tex",
    content: [
      "\\begin{theorem}\\label{thm:criterion}",
      "% lea: formalize label=compactness_criterion",
      "S.",
      "\\end{theorem}",
      "\\begin{theorem}\\label{thm:corollary}",
      "% lea: formalize label=compactness_corollary uses={compactness_criterion}",
      "S.",
      "\\end{theorem}"
    ].join("\n")
  }];
  const manifest = await handleLeanPaneManifest({ overleafProjectId: "project-1", files }, state);
  const byLabel = Object.fromEntries(manifest.body.items.map((item) => [item.leanDeclarationName, item]));
  // The edited item: self-broken, repair offered on IT.
  assert.equal(byLabel.compactness_criterion.status, "invalid");
  assert.equal(byLabel.compactness_criterion.breakage.selfBroken, true);
  assert.equal(byLabel.compactness_criterion.breakage.repairSuppressed, undefined);
  // The dependent: attributed but suppressed until upstream compiles again.
  assert.equal(byLabel.compactness_corollary.breakage.selfBroken, false);
  assert.equal(byLabel.compactness_corollary.breakage.repairSuppressed, "upstream_broken");
});

test("a recovery edit clears persisted breakage on the item and on re-verified dependents", async () => {
  const leaRepo = await makeLeaRepo();
  await writeProof(
    leaRepo,
    "Lea/Project1/compactness_corollary.lean",
    "import Lea.Project1.compactness_criterion\ntheorem compactness_corollary : True := by\n  sorry\n"
  );
  const staleBreakage = {
    upstreamLabel: "compactness_criterion", classificationKind: "signature", via: "edit", editedAt: "old"
  };
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: {
      a: editedJob({ lastEditCheckStatus: "error", lastEditBreakage: { ...staleBreakage } }),
      b: dependentJob({ lastEditCheckStatus: "error", lastEditBreakage: { ...staleBreakage } })
    },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-3" }, note: null } },
      checkResponses: { "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null } }
      // rebuilds default to ok: the recovery cascade re-verifies the dependent clean
    })
  });

  const save = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      content: "theorem compactness_criterion : True := by\n  trivial\n"
    },
    state
  );
  assert.equal(save.statusCode, 200);
  // proof-only edit, but recovery forces the cascade; both attributions clear
  assert.equal(state.jobs.a.lastEditBreakage, undefined);
  assert.equal(state.jobs.b.lastEditBreakage, undefined);
  assert.equal(state.jobs.b.lastEditCheckStatus, "ok");
});

test("a manual edit save clears a standing lastRepair outcome (needs_review does not outlive the human's own edit)", async () => {
  const leaRepo = await makeLeaRepo();
  const calls = [];
  const state = makeState({
    leaRepo,
    jobs: { a: editedJob({ lastRepair: { state: "needs_review", finishedAt: "t" } }) },
    fetchImpl: makeEditFetch(calls, {
      sessionDetails: { "sess-a": EDITED_SESSION_DETAIL },
      writeResponses: { "sess-a": { unchanged: false, code_step: { id: "step-2" }, note: null } },
      checkResponses: { "sess-a": { path: "compactness_criterion.lean", status: "ok", detail: null } }
    })
  });

  const res = await handleLeanPaneEditSave(
    {
      overleafProjectId: "project-1",
      targetKind: "theorem",
      targetLabel: "compactness_criterion",
      content: "theorem compactness_criterion : True := by\n  trivial\n"
    },
    state
  );
  assert.equal(res.statusCode, 200);
  assert.equal(state.jobs.a.lastRepair, undefined);
  // ...and it reached disk with the flag gone
  const persisted = JSON.parse(await fs.readFile(state.jobsPath, "utf8"));
  assert.equal(persisted.a.lastRepair, undefined);
});
