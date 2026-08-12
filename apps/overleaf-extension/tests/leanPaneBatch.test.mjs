// Batch stub / formalize ("Stub all" / "Formalize all"): the project-level
// launchers that reuse the repair-batch machinery over a set of full target
// payloads. Here we cover the request-validation guards and the dependency
// ordering; the shared batch loop (snapshot, pause, continue) is exercised by
// leanPaneRepair.test.mjs and the per-item run pipeline by companion.test.mjs.
import assert from "node:assert/strict";
import test from "node:test";

import {
  handleStubAll,
  handleFormalizeAll,
  handleBatchCancel,
  orderTargetsByUses,
  settleCanceledBatchEntry
} from "../companion/server.mjs";

const PROJECT = "project-1";

function theoremItem(label, extra = {}) {
  return {
    targetKind: "theorem",
    targetLabel: label,
    targetText: `Statement of ${label}.`,
    ...extra
  };
}

test("stub all / formalize all reject an empty or missing item set", async () => {
  for (const handler of [handleStubAll, handleFormalizeAll]) {
    const missing = await handler({ overleafProjectId: PROJECT }, {});
    assert.equal(missing.statusCode, 400);
    assert.equal(missing.body.error, "missing_items");

    const empty = await handler({ overleafProjectId: PROJECT, items: [] }, {});
    assert.equal(empty.statusCode, 400);
    assert.equal(empty.body.error, "missing_items");
  }
});

test("stub all / formalize all require an overleaf project id", async () => {
  for (const handler of [handleStubAll, handleFormalizeAll]) {
    const res = await handler({ items: [theoremItem("t")] }, {});
    assert.equal(res.statusCode, 400);
    assert.equal(res.body.error, "missing_project_id");
  }
});

test("batch validation rejects a malformed target before any run starts", async () => {
  const res = await handleFormalizeAll({
    overleafProjectId: PROJECT,
    items: [theoremItem("ok_one"), theoremItem("has a space")]
  }, {});
  assert.equal(res.statusCode, 400);
  assert.equal(res.body.error, "invalid_label");
});

test("stub all rejects a definition target (definitions have no stub path)", async () => {
  const res = await handleStubAll({
    overleafProjectId: PROJECT,
    items: [theoremItem("a_theorem"), theoremItem("a_definition", { targetKind: "definition" })]
  }, {});
  assert.equal(res.statusCode, 400);
  assert.equal(res.body.error, "unsupported_stub_target");
});

test("orderTargetsByUses places a target's dependencies before it", () => {
  // corollary uses lemma; lemma uses base. Given in reverse, ordering must put
  // base, then lemma, then corollary.
  const entries = [
    { targetLabel: "corollary", payload: { targetUses: ["lemma"] } },
    { targetLabel: "lemma", payload: { targetUses: ["base"] } },
    { targetLabel: "base", payload: { targetUses: [] } }
  ];
  const { ordered, usesByLabel } = orderTargetsByUses(entries);
  const labels = ordered.map((e) => e.targetLabel);
  assert.ok(labels.indexOf("base") < labels.indexOf("lemma"));
  assert.ok(labels.indexOf("lemma") < labels.indexOf("corollary"));
  // The uses graph is keyed by label for the transitive depends-on-failed skip.
  assert.deepEqual(usesByLabel.get("lemma"), ["base"]);
});

test("cancel on an unknown batch is a 404", async () => {
  const res = await handleBatchCancel({ batchId: "nope" }, { repairBatches: {} });
  assert.equal(res.statusCode, 404);
  assert.equal(res.body.error, "unknown_batch");
});

test("cancel settles an idle/paused batch: unsettled items become canceled and it reads done", async () => {
  const batch = {
    batchId: "stub-batch-cancel",
    operation: "stub",
    overleafProjectId: PROJECT,
    items: [
      { targetKind: "theorem", targetLabel: "a", state: "stubbed", reason: null, runJobId: "job-a" },
      { targetKind: "theorem", targetLabel: "b", state: "pending", reason: null, runJobId: null },
      { targetKind: "theorem", targetLabel: "c", state: "pending", reason: null, runJobId: null }
    ],
    running: false,
    done: false,
    pausedOn: { targetLabel: "b", reason: "run_failed" }
  };
  const state = { repairBatches: { [batch.batchId]: batch } };

  const res = await handleBatchCancel({ batchId: batch.batchId }, state);
  assert.equal(res.statusCode, 200);
  assert.equal(res.body.canceled, true);
  assert.equal(res.body.done, true);
  assert.equal(res.body.stopping, false);
  const byLabel = Object.fromEntries(res.body.items.map((i) => [i.targetLabel, i.state]));
  assert.equal(byLabel.a, "stubbed"); // an already-settled item is untouched
  assert.equal(byLabel.b, "canceled");
  assert.equal(byLabel.c, "canceled");
});

test("cancel of a still-running batch defers settlement to its loop (reports stopping)", async () => {
  // batch.running true => no live loop to observe from here; handleBatchCancel
  // sets the flag and reports `stopping`, leaving finalization to the loop.
  const batch = {
    batchId: "formalize-batch-cancel",
    operation: "formalize",
    overleafProjectId: PROJECT,
    items: [{ targetKind: "theorem", targetLabel: "a", state: "running", reason: null, runJobId: null }],
    running: true,
    done: false,
    pausedOn: null
  };
  const state = { repairBatches: { [batch.batchId]: batch } };

  const res = await handleBatchCancel({ batchId: batch.batchId }, state);
  assert.equal(res.statusCode, 200);
  assert.equal(res.body.canceled, false);
  assert.equal(res.body.done, false);
  assert.equal(res.body.stopping, true);
  assert.equal(batch.cancelRequested, true);
});

test("a successful active run that wins the stop race remains completed", () => {
  const entry = { targetLabel: "winner", state: "running", reason: null, runJobId: null };
  settleCanceledBatchEntry(entry, { ok: true, state: "formalized", jobId: "job-winner" });
  assert.deepEqual(entry, {
    targetLabel: "winner",
    state: "formalized",
    reason: null,
    runJobId: "job-winner"
  });
});

test("an interrupted active run becomes canceled", () => {
  const entry = { targetLabel: "interrupted", state: "running", reason: null, runJobId: "job-interrupted" };
  settleCanceledBatchEntry(entry, { ok: false, jobId: "job-interrupted" });
  assert.equal(entry.state, "canceled");
  assert.equal(entry.reason, "canceled");
  assert.equal(entry.runJobId, "job-interrupted");
});

test("orderTargetsByUses ignores uses that are not part of the batch", () => {
  // `external` is used but not itself a batch item -- it must not appear, and
  // the two batch items keep a valid relative order.
  const entries = [
    { targetLabel: "thm", payload: { targetUses: ["external", "helper"] } },
    { targetLabel: "helper", payload: { targetUses: ["external"] } }
  ];
  const { ordered } = orderTargetsByUses(entries);
  const labels = ordered.map((e) => e.targetLabel);
  assert.deepEqual([...labels].sort(), ["helper", "thm"]);
  assert.ok(labels.indexOf("helper") < labels.indexOf("thm"));
});
