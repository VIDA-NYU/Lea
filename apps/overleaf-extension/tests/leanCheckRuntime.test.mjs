import test from 'node:test';
import assert from 'node:assert/strict';
import { fetchApiLeanCheckRuntime } from '../companion/leaApiClient.mjs';
import { toChatSessionResponse } from '../companion/chatPrompt.mjs';
import { handleLeanCheckRuntime } from '../companion/server.mjs';

test('runtime client queries the adapter session without starting a check', async () => {
  let request;
  const result = await fetchApiLeanCheckRuntime({ baseUrl: 'http://localhost:8001', sessionId: 's/1',
    fetchImpl: async (url, options) => { request = { url, options }; return { ok: true, text: async () => JSON.stringify({ state: 'ready' }) }; } });
  assert.equal(request.url, 'http://localhost:8001/api/sessions/s%2F1/lean-check-runtime');
  assert.equal(request.options.method, 'GET');
  assert.equal(result.body.state, 'ready');
});

test('chat reload retains history but reports recovery from authoritative runtime', () => {
  const diagnostic = { code: 'lean.lsp_cold_fallback', message: 'old outage' };
  const response = toChatSessionResponse({ diagnostics: [diagnostic], lean_check_runtime: {
    state: 'ready', last_failure: { kind: 'transport', message: 'old pipe closed' }
  } });
  assert.equal(response.leanCheckRuntimeMessage, 'Lean server recovered');
  assert.equal(response.leanCheckRuntimePolling, false);
  assert.deepEqual(response.leanCheckDiagnostics, [{ ...diagnostic, executionMessage: "" }]);
});

test('an older adapter yields unavailable, never inferred server health', async () => {
  const result = await handleLeanCheckRuntime('session', { settings: { leaApiBaseUrl: 'http://localhost:8001' },
    fetchImpl: async () => ({ ok: false, status: 404, text: async () => '{}' }) });
  assert.equal(result.body.runtime.state, 'unavailable');
});
