// Workflow state is independent of a solver segment's confidence and verdict.
export function dependencyWait(value = {}) {
  if (value.dependencyWait) return value.dependencyWait;
  const reason = value.stopReason || value.leaStatus?.activity?.stop_reason || value.leanCheck?.stopReason;
  const ids = value.waitingForDependencies?.length ? value.waitingForDependencies
    : value.leaStatus?.waiting_dependencies || [];
  if (reason !== "dependency_wait" && !ids.length) return null;
  const dependencies = value.dependencies || [];
  const required = ids.length ? ids.map(id => dependencies.find(dep => dep.id === id)) : dependencies;
  const names = required.filter(Boolean).map(dep => dep.specification?.name || dep.declaration_name).filter(Boolean);
  const title = names.length ? names.slice(0, 2).join(", ") + (names.length > 2 ? ` and ${names.length - 2} more` : "") : "a supporting result";
  const autoResume = value.autoResumeEnabled !== false && !value.dependencyCancelRequested;
  if (!autoResume) return { state: "stopped", autoResume: false,
    message: "Automatic continuation is stopped. Continue when ready to wait for verified supporting results again." };
  if (value.dependencyContinuationBlocker) return { state: "blocked", autoResume: true,
    message: value.dependencyContinuationBlocker, continuationBlocked: true };
  if (!required.length || required.some(dep => !dep || dep.current === false || dep.state === "unavailable")) {
    return { state: "unavailable", autoResume: true, message: "Supporting-result status could not be checked. Refresh status to reconnect." };
  }
  const blocked = required.find(dep => !dep.ready && ["failed", "blocked", "stale", "needs_review", "interrupted"].includes(dep.state));
  if (blocked) {
    const name = blocked.specification?.name || blocked.declaration_name || "A supporting result";
    return { state: "blocked", autoResume: true, dependencyId: blocked.id,
      message: `${name} ${blocked.state === "interrupted" ? "was interrupted" : "needs attention"}. ${blocked.blocker?.detail || "Inspect its attempt and resolve or retry the supporting result."}` };
  }
  if (required.every(dep => dep.ready)) return { state: "ready", autoResume: true,
    message: "Supporting results verified; checking saved progress before continuing automatically…" };
  return { state: "waiting", autoResume: true,
    message: `Waiting for supporting result: ${title}. Formalization will continue automatically when verified.` };
}

export function dependencyWaitAction(wait) {
  if (!wait) return null;
  if (wait.state === "stopped") return { id: "dependency-arm", label: "Continue when ready" };
  if (wait.state === "unavailable" || wait.continuationBlocked) return { id: "dependency-refresh", label: "Refresh status" };
  return { id: "dependency-view", label: "View supporting result" };
}
