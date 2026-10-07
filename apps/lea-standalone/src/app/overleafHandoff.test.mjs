import assert from 'node:assert/strict';
import test from 'node:test';
import { createChatSubmission, createCheckpointSubmission } from './lib/overleafHandoff.mjs';

const forms = [{ id: 'target', origin: 'overleaf' }, { id: 'local', origin: 'ui' }];
const input = { message: 'Explain this proof', sessionId: 'chat', model: 'test', scope: 'target', formalizations: forms };

test('only the specifically selected Overleaf target acquires source', async () => {
  const prepared = [], requests = [];
  const submit = createChatSubmission({
    prepare: async (id) => { prepared.push(id); return { source_context_ref: 'fresh' }; },
    create: async (...args) => { requests.push(args[3]); return {}; }, makeId: () => 'id'
  });
  for (const scope of ['project', 'new', 'local']) await submit({ ...input, scope });
  await submit({ ...input, general: true });
  assert.deepEqual(prepared, []);
  await submit(input);
  assert.deepEqual(prepared, ['target']);
  assert.equal(requests.at(-1).source_context_ref, 'fresh');
  assert.equal(requests.at(-1).focus_formalization_id, 'target');
  assert.equal(requests.at(-2).source_context_ref, undefined);
});

test('acquisition failure admits nothing and does not mutate the draft', async () => {
  let admitted = 0;
  const submit = createChatSubmission({ prepare: async () => { throw new Error('Disconnected'); },
    create: async () => admitted++ });
  await assert.rejects(submit(input), /Disconnected/);
  assert.equal(admitted, 0);
  assert.equal(input.message, 'Explain this proof');
});

test('lost admission response reuses the exact snapshot and request identity', async () => {
  let prepared = 0;
  const requests = [];
  const submit = createChatSubmission({
    prepare: async () => ({ source_context_ref: `source-${++prepared}` }), makeId: () => 'request-id',
    create: async (...args) => { requests.push(args); if (requests.length === 1) throw new TypeError('Network lost'); return { run_id: 'one-run' }; }
  });
  await assert.rejects(submit(input), /Network lost/);
  assert.equal((await submit(input)).run_id, 'one-run');
  assert.equal(prepared, 1);
  assert.deepEqual(requests[0], requests[1]);
});

test('definite stale-source rejection reacquires instead of replaying rejected evidence', async () => {
  let prepared = 0, attempts = 0;
  const submit = createChatSubmission({
    prepare: async () => ({ source_context_ref: `source-${++prepared}` }),
    create: async () => { if (++attempts === 1) throw Object.assign(new Error('Stale'), { status: 409 }); return {}; }
  });
  await assert.rejects(submit(input), /Stale/);
  await submit(input);
  assert.equal(prepared, 2);
});

test('navigation during acquisition prevents admission into the old conversation', async () => {
  let current = true, admitted = 0;
  const submit = createChatSubmission({
    prepare: async () => { current = false; return { source_context_ref: 'fresh' }; },
    create: async () => admitted++
  });
  await assert.rejects(submit({ ...input, isCurrent: () => current }), /chat or target changed/);
  assert.equal(admitted, 0);
});

test('checkpoint retries also preserve their exact prepared source and identity', async () => {
  let prepared = 0;
  const requests = [];
  const resume = createCheckpointSubmission({
    prepare: async () => ({ source_context_ref: `source-${++prepared}` }), makeId: () => 'continue-once',
    create: async (...args) => { requests.push(args); if (requests.length === 1) throw new TypeError('Network lost'); return {}; }
  });
  const request = { runId: 'paused', targetId: 'target' };
  await assert.rejects(resume(request), /Network lost/);
  await resume(request);
  assert.equal(prepared, 1);
  assert.deepEqual(requests, [['paused', 'source-1', 'continue-once'], ['paused', 'source-1', 'continue-once']]);
});
