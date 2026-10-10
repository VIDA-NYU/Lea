import test from "node:test";
import assert from "node:assert/strict";
import { resolveSharedDependencies, mergeDependencyProgress } from "../companion/dependencyCoordinator.mjs";

function harness(initial = { L: { state: "pending" } }) {
  const deps = new Map(Object.entries(initial).map(([id, value]) => [id, { id, ready: false, ...value }]));
  const rows = new Map();
  const attempts = [];
  const events = [];
  const callbacks = {
    getDependency: async (id) => ({ ...deps.get(id) }),
    startDependency: async (id) => {
      attempts.push(id);
      const run_id = `${id}:1`;
      Object.assign(deps.get(id), { state: "running", owner_run_id: run_id });
      rows.set(run_id, { status: "running" });
      return { run_id };
    },
    observeRun: async (id) => {
      const dep = [...deps.values()].find((d) => d.owner_run_id === id);
      Object.assign(dep, { state: "ready", ready: true });
      rows.set(id, { status: "proved" });
    },
    getRun: async (id) => rows.get(id),
    continueRun: async () => { throw Error("unexpected continuation"); },
    wait: async () => {},
    onUpdate: async (dep) => events.push(dep),
  };
  return { deps, rows, attempts, events, callbacks };
}

test("A waits for L, resumes, and B consumes L with one attempt", async () => {
  const h = harness();
  const consumers = [];
  for (const target of ["A", "B"]) {
    const result = await resolveSharedDependencies(["L"], h.callbacks);
    assert.equal(result.ok, true);
    consumers.push({ target, reused: result.dependencies[0].reused });
  }
  assert.deepEqual(h.attempts, ["L"]);
  assert.deepEqual(consumers, [{ target: "A", reused: false }, { target: "B", reused: true }]);
});

test("a shared failure is cached across batches while C remains independent", async () => {
  const h = harness({ L: { state: "failed", blocker: { detail: "precise source gap" } }, C: { state: "pending" } });
  for (let batch = 0; batch < 2; batch++) {
    const failed = await resolveSharedDependencies(["L"], h.callbacks);
    assert.equal(failed.reason, "precise source gap");
    assert.equal((await resolveSharedDependencies(["C"], h.callbacks)).ok, true);
  }
  assert.deepEqual(h.attempts, ["C"]);
  h.deps.set("L", { id: "L", state: "pending", generation: 2 });
  assert.equal((await resolveSharedDependencies(["L"], h.callbacks)).ok, true);
  assert.deepEqual(h.attempts, ["C", "L"]);
});

test("nested dependencies execute first and continue the same saved parent", async () => {
  const h = harness({ L: { state: "waiting", owner_run_id: "L:1" }, K: { state: "pending" } });
  h.rows.set("L:1", { status: "cancelled", stop_reason: "dependency_wait", waiting_dependencies: ["K"] });
  const admission = h.callbacks.startDependency;
  h.callbacks.startDependency = async (id, initiator) => {
    assert.equal(initiator, "L:1");
    return admission(id);
  };
  h.callbacks.continueRun = async (id) => {
    assert.equal(id, "L:1");
    assert.equal(h.deps.get("K").ready, true);
    h.deps.get("L").owner_run_id = "L:2";
    return { run_id: "L:2" };
  };
  assert.equal((await resolveSharedDependencies(["L"], h.callbacks)).ok, true);
  assert.deepEqual(h.attempts, ["K"]);
});

test("cycles and cancellation never launch duplicate work", async () => {
  const h = harness({ L: { state: "waiting", owner_run_id: "L:1" }, K: { state: "waiting", owner_run_id: "K:1" } });
  h.rows.set("L:1", { status: "cancelled", stop_reason: "dependency_wait", waiting_dependencies: ["K"] });
  h.rows.set("K:1", { status: "cancelled", stop_reason: "dependency_wait", waiting_dependencies: ["L"] });
  assert.equal((await resolveSharedDependencies(["L"], h.callbacks)).reason, "dependency_cycle");
  assert.equal((await resolveSharedDependencies(["L"], { ...h.callbacks, isCanceled: () => true })).canceled, true);
  assert.deepEqual(h.attempts, []);
});

test("another owner is awaited and shared counts never become target counts", async () => {
  const h = harness({ L: { state: "running", owner_run_id: "other" } });
  h.rows.set("other", { status: "running" });
  h.callbacks.wait = async () => { h.deps.get("L").ready = true; };
  h.callbacks.observeRun = async () => { throw Error("must not own another workflow"); };
  assert.equal((await resolveSharedDependencies(["L"], h.callbacks)).dependencies[0].reused, true);
  assert.deepEqual(h.attempts, []);
  let facts = mergeDependencyProgress([], { id: "L", started_here: true, reused: false });
  facts = mergeDependencyProgress(facts, { id: "L", ready: true, reused: true });
  assert.equal(facts.length, 1);
  assert.equal(facts[0].reused, false);
});
