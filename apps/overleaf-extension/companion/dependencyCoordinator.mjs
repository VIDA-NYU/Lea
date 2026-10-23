// A persistent registry owns attempts. This coordinator chooses ready work and
// awaits other owners; it never creates another proof for a recorded failure.
import { requestFailure } from './workflowPolicy.mjs';
export async function resolveSharedDependencies(ids, {
  getDependency, startDependency, observeRun, getRun, continueRun, onUpdate = async () => {},
  isCanceled = () => false, wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
}) {
  const resolved = new Map();
  async function resolve(id, ancestors = [], initiatingRunId = null) {
    if (resolved.has(id)) return resolved.get(id);
    if (ancestors.includes(id)) return { ok: false, dependencyId: id, reason: "dependency_cycle" };
    if (isCanceled()) return { ok: false, canceled: true, dependencyId: id };
    const stack = [...ancestors, id];
    let startedHere = false;
    let knownRunId = null;
    let generation;
    const projectionPending = dep => dep?.unavailable || (dep?.projection && dep.projection.state !== 'observed');
    for (;;) {
      if (isCanceled()) return { ok: false, canceled: true, dependencyId: id };
      const dep = await getDependency(id);
      if (!dep) return { ok: false, dependencyId: id, reason: "dependency_unavailable" };
      // A missing/invalidated SQL projection is uncertainty, not a failed proof.
      // In particular, the SQL fallback labels a published dependency "stale"
      // until its new observation is available.
      if (projectionPending(dep)) { await wait(1000); continue; }
      if (generation !== dep.generation) {
        generation = dep.generation;
        knownRunId = null;
        startedHere = false;
      }
      await onUpdate({ ...dep, reused: dep.ready === true && !startedHere });
      if (dep.ready) {
        const result = { ok: true, dependencyId: id, reused: !startedHere };
        resolved.set(id, result);
        return result;
      }
      if (["failed", "needs_review", "blocked", "stale", "interrupted"].includes(dep.state)) {
        return { ok: false, dependencyId: id, reason: dep.blocker?.detail || dep.state, dependency: dep };
      }
      let runId = dep.owner_run_id || knownRunId;
      let start;
      if (dep.state === "pending" && !runId) {
        start = await startDependency(id, initiatingRunId);
        if (start?.ready) continue;
        if (!start?.run_id) {
          // Another coordinator can win admission; an unknown transport outcome
          // is retried with the registry's deterministic request identity.
          const current = await getDependency(id);
          if (current?.owner_run_id || current?.ready) continue;
          if (!requestFailure(start).retry) {
            return { ok: false, dependencyId: id, reason: start.error || "dependency_admission_failed" };
          }
          await wait(1000);
          continue;
        }
        startedHere = true;
        runId = start.run_id;
      }
      if (!runId) { await wait(500); continue; }
      knownRunId = runId;
      let row = await getRun(runId);
      if (!row || ["pending", "running"].includes(row.status)) {
        if (!startedHere && !start) { await wait(500); continue; }
        await onUpdate({ ...dep, state: "running", active_run_id: runId, started_here: startedHere });
        // The observer preserves transport/restart behavior from ordinary runs.
        await observeRun(runId, start);
        row = await getRun(runId);
      }
      if (!row) { await wait(1000); continue; }
      if (row.stop_reason === "dependency_wait") {
        const children = row.waiting_dependencies || JSON.parse(row.waiting_dependencies_json || "[]");
        if (!children.length) return { ok: false, dependencyId: id, reason: "missing_dependency_checkpoint" };
        for (const child of children) {
          const outcome = await resolve(child, stack, runId);
          if (!outcome.ok) return outcome;
        }
        if (isCanceled()) return { ok: false, canceled: true, dependencyId: id };
        const resumed = await continueRun(runId);
        if (!resumed?.run_id) return { ok: false, dependencyId: id, reason: resumed?.error || "incompatible_checkpoint" };
        await onUpdate({ ...dep, state: "running", active_run_id: resumed.run_id, started_here: true });
        knownRunId = resumed.run_id;
        await observeRun(resumed.run_id, resumed);
        continue;
      }
      const final = await getDependency(id);
      if (projectionPending(final)) { await wait(1000); continue; }
      if (final?.ready) continue;
      if (!final) return { ok: false, dependencyId: id, reason: "dependency_unavailable" };
      if (["failed", "needs_review", "blocked", "stale", "interrupted"].includes(final.state)) {
        return { ok: false, dependencyId: id, reason: final.blocker?.detail || final.state, dependency: final };
      }
      // A passing run can settle before dependency publication/observation.
      // Keep its identity while polling so clearing owner_run_id cannot admit
      // another execution. Only the registry's ready verdict releases consumers.
      if (row.status === "proved") { await wait(500); continue; }
      if (row.status && !["pending", "running"].includes(row.status)) {
        await onUpdate({ ...final, state: final?.state === "running" ? "needs_review" : final?.state });
        return { ok: false, dependencyId: id, reason: final?.blocker?.detail || row.result_detail || row.stop_reason || "dependency_unverified" };
      }
      await wait(500);
    }
  }
  for (const id of ids) {
    const result = await resolve(id);
    if (!result.ok) return result;
  }
  return { ok: true, dependencies: [...resolved.values()] };
}

export function mergeDependencyProgress(previous = [], update) {
  if (!update?.id) return previous;
  const records = new Map(previous.map((item) => [item.id, item]));
  const old = records.get(update.id);
  // A result proved here remains "new" after subsequent ready polling.
  records.set(update.id, { ...old, ...update,
    reused: old?.started_here || old?.reused === false ? false : update.reused,
    started_here: old?.started_here || update.started_here || false });
  return [...records.values()];
}
