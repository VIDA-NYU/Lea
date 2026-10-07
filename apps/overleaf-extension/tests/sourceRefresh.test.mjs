import assert from 'node:assert/strict';
import test from 'node:test';
import { createEventBus } from '../companion/eventBus.mjs';
import { registerSourceClient, pendingSourceRequests, pollSourceRequests, respondSourceRequest } from '../companion/sourceRefresh.mjs';
import { createSourceRefreshResponder, selectSourceTarget } from '../extension/sourceRefreshClient.mjs';
import { createEventsClient } from '../extension/eventsClient.mjs';

const request = { request_id: 'refresh', formalization_id: 'form', overleafProjectId: 'doc', targetKind: 'theorem', targetLabel: 'target' };
const source = { targetKey: 'target', targetKind: 'theorem' };
function transport() {
  const posts = [], events = [];
  const state = { eventBus: createEventBus(), fetchImpl: async (url, options) => {
    if (options.method === 'POST') posts.push(JSON.parse(options.body));
    return { ok: true, json: async () => options.method === 'POST' ? { ok: true } : { requests: [request, { ...request, request_id: 'other', overleafProjectId: 'other-doc' }] } };
  } };
  state.eventBus.subscribe((event) => events.push(event));
  return { state, posts, events };
}

test('refresh transport reaches only the connected document and supports reconnect pull', async () => {
  const { state, events, posts } = transport();
  const disconnect = registerSourceClient(state, 'doc', 'tab');
  await pollSourceRequests(state, 'http://adapter');
  assert.deepEqual(events.map((event) => event.request_id), ['refresh']);
  assert.deepEqual(await pendingSourceRequests(state, 'http://adapter', 'doc'), [request]);
  await respondSourceRequest(state, 'http://adapter', { ...request, clientId: 'tab', sourceContext: { sourceBundle: source } });
  assert.deepEqual(posts, [{ document_id: 'doc', source_bundle: source, mirrored_files: null, error: null }]);
  disconnect();
  await assert.rejects(respondSourceRequest(state, 'http://adapter', { ...request, clientId: 'tab' }), /Reconnect/);
});

test('multiple tabs fail explicitly while duplicate sockets from one tab are safe', async () => {
  const { state, events, posts } = transport();
  const a = registerSourceClient(state, 'doc', 'tab-a');
  const duplicate = registerSourceClient(state, 'doc', 'tab-a');
  await pollSourceRequests(state, 'http://adapter');
  assert.equal(events.length, 1);
  const b = registerSourceClient(state, 'doc', 'tab-b');
  await pollSourceRequests(state, 'http://adapter');
  assert.match(posts[0].error, /multiple Overleaf tabs/);
  a(); duplicate(); b();
  assert.equal(state.sourceClients.size, 0);
});

test('wrong document, target, and changed mirror never reach the adapter', async () => {
  const { state, posts } = transport();
  registerSourceClient(state, 'doc', 'tab');
  const payload = { ...request, clientId: 'tab', sourceContext: { sourceBundle: source } };
  await assert.rejects(respondSourceRequest(state, 'http://adapter', { ...payload, overleafProjectId: 'wrong' }), /different document/);
  await assert.rejects(respondSourceRequest(state, 'http://adapter', { ...payload, sourceContext: { sourceBundle: { ...source, targetKey: 'wrong' } } }), /different theorem/);
  await assert.rejects(respondSourceRequest(state, 'http://adapter', { ...payload, sourceContext: { sourceBundle: { ...source, mirror: { verified: true, revision: 'old' } } } }), /mirror changed/);
  assert.equal(posts.length, 0);
});

test('extension deduplicates pushes and reconnect pulls, and can retry a lost acknowledgement', async () => {
  let reads = 0, replies = 0;
  const handle = createSourceRefreshResponder({ documentId: () => 'doc',
    readSource: async () => { reads++; return { sourceBundle: source }; },
    respond: async () => { if (++replies === 1) throw new Error('Disconnected'); }
  });
  await assert.rejects(handle(request), /Disconnected/);
  await Promise.all([handle(request), handle(request)]);
  await handle(request);
  await handle({ ...request, overleafProjectId: 'wrong' });
  assert.equal(reads, 2);
  assert.equal(replies, 2);
});

test('target deletion and duplicate labels are reported instead of selecting a different item', async () => {
  assert.throws(() => selectSourceTarget([], request), /no longer exists/);
  const item = { label: 'target', leanKind: 'theorem' };
  assert.throws(() => selectSourceTarget([item, item], request), /ambiguous/);
  assert.equal(selectSourceTarget([item, { label: 'target', leanKind: 'def' }], request), item);
  const replies = [];
  const handle = createSourceRefreshResponder({ documentId: () => 'doc',
    readSource: async () => { throw new Error('The target was deleted.'); }, respond: async (payload) => replies.push(payload) });
  await handle(request);
  assert.equal(replies[0].sourceContext, undefined);
  assert.match(replies[0].error, /deleted/);
});

test('source requests are forwarded by the real events client', () => {
  let source, received;
  class EventSource {
    listeners = new Map();
    constructor() { source = this; }
    addEventListener(name, callback) { this.listeners.set(name, callback); }
    close() {}
  }
  const client = createEventsClient({ url: () => 'http://companion/events', EventSourceImpl: EventSource,
    onEvent: (type, data) => { received = { type, data }; } });
  client.start();
  source.listeners.get('source-refresh-requested')({ data: JSON.stringify(request) });
  assert.deepEqual(received, { type: 'source-refresh-requested', data: request });
  client.stop();
});
