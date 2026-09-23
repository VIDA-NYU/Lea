import test from 'node:test';
import assert from 'node:assert/strict';
import { applyLeaStatusEvent, normalizeLeaStatus, leaStatusLabel } from '../shared/leaStatus.mjs';
import { acceptStatusEvent } from '../companion/leaStatus.mjs';
import { startApiRun } from '../companion/leaApiClient.mjs';
import { handleLeaCheckRetry } from '../companion/server.mjs';
const event = (changes = {}) => ({ formalization_id: 'form', run_id: 'run', run_generation: 1,
  sequence: 1, assessment: { confidence: 'high', summary: 'Main argument checked', scope: 'partial_artifact' },
  attention: 'source_issue', ...changes });
const response = body => ({ ok: true, status: 200, text: async () => JSON.stringify(body) });

test('confidence, source attention, compiler verdict and freshness are independent', () => {
  const status = normalizeLeaStatus(event({ freshness: 'current', leanCheck: 'failed' }));
  assert.equal(leaStatusLabel(status), 'High confidence · Source issue');
  assert.equal(normalizeLeaStatus({ assessment: { confidence: 'approved' } }).confidence, 'unassessed');
  assert.match(leaStatusLabel({ ...status, freshness: 'source_changed' }), /Last assessed version/);
});

test('late, repeated, malformed and cross-target events cannot overwrite current status', () => {
  const current = applyLeaStatusEvent(null, event({ run_generation: 2, sequence: 3 }));
  for (const stale of [event({ sequence: 100 }), event({ run_generation: 2, sequence: 3 }),
    event({ formalization_id: 'other', run_generation: 3, sequence: 4 }), event({ run_generation: undefined })]) {
    assert.equal(applyLeaStatusEvent(current, stale), current);
  }
  const next = applyLeaStatusEvent(current, event({ run_generation: 2, sequence: 4, freshness: 'current' }));
  assert.equal(next.sequence, 4);
  assert.equal(next.freshness, 'unknown');
});

test('companion updates a matching active job and deduplicates replay', () => {
  const state = {};
  const job = { apiRunId: 'run', overleafProjectId: 'project' };
  assert.equal(acceptStatusEvent(state, event(), job), true);
  assert.equal(job.leaStatus.sequence, 1);
  assert.equal(acceptStatusEvent(state, event(), job), false);
});

test('reporting run requires capability and complete source admission; no terminal fallback', async () => {
  for (const capability of [null, { version: 0, admission_enabled: true }, { version: 1, admission_enabled: false }, { version: 1, admission_enabled: true }]) {
    const urls = [];
    const result = await startApiRun({ baseUrl: 'http://localhost:8001', message: 'formalize', purpose: 'overleaf_solver',
      sourceBundle: { bundleHash: 'hash' }, fetchImpl: async url => { urls.push(url); return response({ capabilities: { lea_status: capability } }); } });
    assert.equal(result.status, 409);
    assert.equal(urls.length, 1);
  }
  const calls = [];
  const source = { bundleHash: 'hash', proof: 'Author supplied proof' };
  await startApiRun({ baseUrl: 'http://localhost:8001', message: 'formalize', purpose: 'overleaf_solver', sourceBundle: source,
    fetchImpl: async (url, options) => { calls.push([url, options]); return response(url.endsWith('/health')
      ? { capabilities: { lea_status: { version: 1, admission_enabled: true, source_pause_policy: 1 } } } : { run_id: 'run' }); } });
  const posted = JSON.parse(calls[1][1].body);
  assert.equal(posted.lea_status_version, 1);
  assert.deepEqual(posted.source_bundle, source);
  assert.equal((await handleLeaCheckRetry({}, {})).statusCode, 410);
});
