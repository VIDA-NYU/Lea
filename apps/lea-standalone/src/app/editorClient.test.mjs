import assert from 'node:assert/strict';
import test from 'node:test';
import { EditorClient } from './lib/editorClient.mjs';
const response = (body, status = 200) => ({ ok: status < 400, json: async () => body, status });
function fixture() {
  let environment = 'env', base = 'base', calls = [], lost = false;
  const buffers = new Map(), requests = new Map();
  const client = new EditorClient('s', 'Main.lean', { fetch: async (url, init) => {
    const value = init.body && JSON.parse(init.body); calls.push({ url, value });
    if (url.includes('/editor/clients')) return response({ credential: 'owner' });
    if (url.includes('/s/editor')) return response({ path: 'Main.lean', content: 'initial', environment_revision: environment, file_revision: base });
    if (url === '/api/editor/buffers') {
      const buffer = { id: `buffer-${value.version}`, ...value, expires_at: new Date(Date.now() + 10000).toISOString() };
      buffers.set(buffer.id, buffer); return response(buffer);
    }
    if (url === '/api/editor/requests') {
      const result = { id: value.idempotency_key, state: 'succeeded', current: true, buffer: buffers.get(value.buffer_id), result: { value: { rendered: 'goal', goals: ['goal'] } } };
      requests.set(value.idempotency_key, result);
      if (lost) { lost = false; throw new TypeError('lost acknowledgement'); }
      return response(result);
    }
    if (url.startsWith('/api/editor/receipts')) return response(requests.get(new URL(url, 'http://test').searchParams.get('idempotency_key')));
    throw new Error(`Unexpected ${url}`);
  } });
  return { client, calls, setEnvironment(value) { environment = value; }, setBase(value) { base = value; }, loseAck() { lost = true; } };
}
test('typed requests reuse immutable bytes and recover a lost acknowledgement', async () => {
  const x = fixture(); await x.client.open(); x.loseAck();
  assert.equal((await x.client.request('editor.goals', { line: 0, character: 0 })).rendered, 'goal');
  await x.client.request('editor.hover', { line: 0, character: 1 });
  assert.equal(x.calls.filter(c => c.url === '/api/editor/buffers').length, 1);
  assert.equal(x.calls.filter(c => c.url === '/api/editor/requests').length, 2);
  assert.ok(x.calls.some(c => c.url.startsWith('/api/editor/receipts')));
});
test('environment or saved base changes publish a new binding without altering the local draft', async () => {
  const x = fixture(); await x.client.open(); x.client.setContent('local draft');
  await x.client.request('editor.analyze'); x.setEnvironment('env2'); x.setBase('base2');
  await x.client.request('editor.analyze');
  const publications = x.calls.filter(c => c.url === '/api/editor/buffers');
  assert.equal(publications.length, 2); assert.equal(publications[1].value.content, 'local draft');
  assert.equal(x.client.content, 'local draft');
});
test('a buffer change during publication discards the obsolete request before execution', async () => {
  const x = fixture(); await x.client.open();
  const request = x.client.request('editor.analyze'); x.client.setContent('next');
  await assert.rejects(request, error => error.state === 'superseded');
  assert.equal(x.calls.filter(c => c.url === '/api/editor/requests').length, 0);
});
test('scope disposal ignores all later results', async () => {
  const x = fixture(); await x.client.open(); x.client.dispose();
  await assert.rejects(x.client.request('editor.analyze'), error => error.state === 'superseded');
});
test('a later cursor request wins even if an earlier acknowledgement arrives last', async () => {
  const x = fixture(); await x.client.open();
  const original = x.client.fetch;
  let release, entered;
  const ready = new Promise(resolve => { entered = resolve; });
  x.client.fetch = async (url, init) => {
    const result = await original(url, init);
    if (url === '/api/editor/requests' && JSON.parse(init.body).sequence === 1) {
      entered(); await new Promise(resolve => { release = resolve; });
    }
    return result;
  };
  const old = x.client.request('editor.goals', { line: 0, character: 0 });
  await ready;
  const current = await x.client.request('editor.goals', { line: 0, character: 1 });
  assert.equal(current.rendered, 'goal'); release();
  await assert.rejects(old, error => error.state === 'superseded');
});
test('SQL freshness polling invalidates old output and recovers after a disconnected observation', async () => {
  const x = fixture(); await x.client.open(); await x.client.request('editor.analyze');
  const revision = x.client.revision;
  x.client.invalidate(); assert.ok(x.client.revision > revision);
  assert.equal(await x.client.refresh(), true);
  await x.client.request('editor.analyze');
  assert.equal(x.client.content, 'initial');
  x.setEnvironment('new'); assert.equal(await x.client.refresh(), true);
  assert.equal(x.client.buffer, null);
});
test('transient quota failure retries after an idle SQL metadata poll', async () => {
  const x = fixture(); await x.client.open();
  const original = x.client.fetch; let blocked = true;
  x.client.fetch = (url, init) => url === '/api/editor/requests' && blocked
    ? Promise.resolve(response({ detail: { error: 'queue_full' } }, 429)) : original(url, init);
  await assert.rejects(x.client.request('editor.goals', { line: 0, character: 0 }), error => error.state === 'queue_full');
  assert.equal(x.client.activeRequests, 0); blocked = false;
  assert.equal(await x.client.refresh(), true);
  assert.equal((await x.client.request('editor.goals', { line: 0, character: 0 })).rendered, 'goal');
  assert.equal(await x.client.refresh(), false);
});
