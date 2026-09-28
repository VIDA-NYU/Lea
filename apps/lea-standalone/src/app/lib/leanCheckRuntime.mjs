// Runtime health is independent of the theorem/proof verdict.
export function runtimeNeedsPolling(runtime, active = false) {
  return active || Number(runtime?.active_cold_checks) > 0
    || ['starting', 'recovering', 'degraded'].includes(runtime?.state);
}

export function runtimeStatusMessage(runtime) {
  if (!runtime) return '';
  if (Number(runtime.active_cold_checks) > 0) return 'Checking with full compilation';
  if (runtime.state === 'starting') return 'Starting Lean server';
  if (runtime.state === 'recovering') return 'Recovering Lean server';
  if (runtime.state === 'degraded') {
    const seconds = Math.ceil(Number(runtime.retry_after_ms || 0) / 1000);
    return seconds > 0 ? `Lean server unavailable · retry eligible in ${seconds}s`
      : 'Lean server unavailable · the next check will retry';
  }
  if (runtime.state === 'ready' && runtime.last_failure) return 'Lean server recovered';
  if (runtime.state === 'disabled') return 'Lean server disabled · full compilation enabled';
  if (runtime.state === 'unavailable') return 'Lean check runtime unavailable';
  return '';
}

export function checkExecutionMessage(execution, status) {
  if (!execution) return '';
  const backend = { lsp: 'Lean server', cold: 'full compilation', none: 'no compiler result' }[execution.backend] || 'unknown backend';
  const elapsed = Number(execution.timings_ms?.total);
  const duration = Number.isFinite(elapsed) ? ` · ${(elapsed / 1000).toFixed(1)}s` : '';
  const verdict = status === 'ok' ? 'Lean check passed' : 'Lean check failed';
  return `${verdict} · ${backend}${duration}`;
}
