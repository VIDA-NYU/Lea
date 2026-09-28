import test from 'node:test';
import assert from 'node:assert/strict';
import { runtimeNeedsPolling, runtimeStatusMessage, checkExecutionMessage } from './lib/leanCheckRuntime.mjs';

test('recovery clears degradation and stops polling even with retained historical failure', () => {
  const failed = { state: 'degraded', retry_after_ms: 30000, last_failure: { message: 'pipe closed' } };
  assert.equal(runtimeNeedsPolling(failed), true);
  assert.match(runtimeStatusMessage(failed), /30s/);
  const recovered = { ...failed, state: 'ready', retry_after_ms: 0 };
  assert.equal(runtimeNeedsPolling(recovered), false);
  assert.equal(runtimeStatusMessage(recovered), 'Lean server recovered');
});

test('runtime backend and proof verdict stay separate', () => {
  assert.equal(runtimeStatusMessage({ state: 'ready', active_cold_checks: 1 }), 'Checking with full compilation');
  assert.equal(runtimeStatusMessage({ state: 'recovering' }), 'Recovering Lean server');
  assert.equal(runtimeNeedsPolling({ state: 'idle' }, true), true);
  assert.equal(runtimeNeedsPolling({ state: 'unavailable' }), false);
  assert.equal(runtimeStatusMessage({ state: 'unavailable' }), 'Lean check runtime unavailable');
  assert.equal(checkExecutionMessage({ backend: 'cold', timings_ms: { total: 1200 } }, 'ok'), 'Lean check passed · full compilation · 1.2s');
});
