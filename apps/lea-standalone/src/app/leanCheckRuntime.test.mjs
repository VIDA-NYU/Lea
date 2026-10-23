import test from 'node:test';
import assert from 'node:assert/strict';
import { runtimeObservation, runtimeNeedsPolling, runtimeStatusMessage, checkExecutionMessage } from './lib/leanCheckRuntime.mjs';

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
  assert.equal(runtimeNeedsPolling({ state: 'unavailable' }), true);
  assert.equal(runtimeStatusMessage({ state: 'unavailable' }), 'Lean check runtime unavailable');
  assert.equal(checkExecutionMessage({ backend: 'cold', timings_ms: { total: 1200 } }, 'ok'), 'Lean check passed · full compilation · 1.2s');
});

test('pending and failed status observations remain distinct from compiler health', () => {
  const pending = { state: 'unavailable', observation: { state: 'pending' } };
  const disconnected = { state: 'unavailable', observation: { state: 'unavailable' } };
  assert.equal(runtimeNeedsPolling(pending), true);
  assert.equal(runtimeStatusMessage(pending), 'Waiting for Lean runtime status');
  assert.equal(runtimeStatusMessage(disconnected), 'Lean runtime status unavailable · retrying');
  assert.equal(runtimeStatusMessage({ state: 'idle' }), '');
  assert.equal(runtimeNeedsPolling({ state: 'idle' }), false);
});

test('missing and expired projections cannot be reported as a compiler outage', () => {
  const pending = runtimeObservation({ status: 503, body: { detail: {
    error: 'projection_unavailable', message: 'Not published yet',
  } } });
  assert.equal(runtimeStatusMessage(pending), 'Waiting for Lean runtime status');
  assert.equal(pending.observation.status, 503);
  const expired = runtimeObservation({ ok: true, body: { state: 'unavailable', projection: { state: 'observation_expired' } } });
  assert.equal(expired.observation.state, 'unavailable');
  const current = { state: 'unavailable', last_failure: { message: 'Real runtime failure' } };
  assert.equal(runtimeObservation({ ok: true, body: current }), current);
  assert.equal(runtimeStatusMessage(current), 'Lean check runtime unavailable');
});
