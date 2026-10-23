import assert from 'node:assert/strict';
import test from 'node:test';

import {
  pickInitialSession,
  readDeepLinkSessionId,
  readDeepLinkFormalizationId,
  restoreInitialSession,
  stripNavigationParams,
  stripSessionParam,
} from './sessionDeepLink.mjs';

test('readDeepLinkSessionId extracts the session param', () => {
  assert.equal(readDeepLinkSessionId('?session=abc123'), 'abc123');
  assert.equal(readDeepLinkSessionId('?view=stats&session=abc123'), 'abc123');
  assert.equal(readDeepLinkSessionId('?session=%20'), null);
  assert.equal(readDeepLinkSessionId('?view=stats'), null);
  assert.equal(readDeepLinkSessionId(''), null);
});

test('formalization deep links are parsed and navigation params can be stripped together', () => {
  assert.equal(readDeepLinkFormalizationId('?formalization=f-1'), 'f-1');
  assert.equal(readDeepLinkFormalizationId('?formalization=%20'), null);
  assert.equal(
    stripNavigationParams('?view=stats&session=s-1&formalization=f-1'),
    '?view=stats',
  );
});

test('stripSessionParam removes session but keeps other params', () => {
  assert.equal(stripSessionParam('?session=abc'), '');
  assert.equal(stripSessionParam('?view=stats&session=abc'), '?view=stats');
  assert.equal(stripSessionParam('?session=abc&view=stats'), '?view=stats');
  assert.equal(stripSessionParam(''), '');
});

test('deep-link session wins over the saved session', () => {
  const result = pickInitialSession({
    search: '?session=deep',
    savedId: 'saved',
    sessions: [{ id: 'saved' }],
  });
  assert.deepEqual(result, { sessionId: 'deep', source: 'deep-link' });
});

test('deep-link id is honored even when not yet in the session list', () => {
  const result = pickInitialSession({
    search: '?session=brand-new',
    savedId: null,
    sessions: [{ id: 'other' }],
  });
  assert.deepEqual(result, { sessionId: 'brand-new', source: 'deep-link' });
});

test('falls back to saved session only when it still exists', () => {
  assert.deepEqual(
    pickInitialSession({ search: '', savedId: 'saved', sessions: [{ id: 'saved' }] }),
    { sessionId: 'saved', source: 'saved' },
  );
  assert.deepEqual(
    pickInitialSession({ search: '', savedId: 'gone', sessions: [{ id: 'saved' }] }),
    { sessionId: null, source: 'none' },
  );
});

test('returns none when there is nothing to restore', () => {
  assert.deepEqual(
    pickInitialSession({ search: '', savedId: null, sessions: [] }),
    { sessionId: null, source: 'none' },
  );
});

function restoration(search, overrides = {}) {
  const calls = [];
  const location = { search };
  const options = {
    search, savedId: 'saved', sessions: [{ id: 'saved' }],
    loadSession: async (id) => { calls.push(['session', id]); return { id }; },
    loadFormalization: async (id) => ({ id, sessions: [{ id: 'target-session' }] }),
    selectFormalization: (id) => calls.push(['scope', id]),
    openProject: async (id) => { calls.push(['project', id]); return { id }; },
    onRestored: () => { location.search = stripNavigationParams(location.search); },
    ...overrides,
  };
  return { calls, location, restore: () => restoreInitialSession(options) };
}

test('a session link stays available until its requested session has loaded', async () => {
  let finish;
  const x = restoration('?session=target&view=chat', {
    loadSession: () => new Promise((resolve) => { finish = resolve; }),
  });
  const pending = x.restore();
  assert.equal(x.location.search, '?session=target&view=chat');
  finish({ id: 'target' });
  await pending;
  assert.equal(x.location.search, '?view=chat');
});

for (const message of ['Session not found', 'HTTP 503', 'Failed to fetch']) {
  test(`a failed session link surfaces ${message} and can be retried with the same URL`, async () => {
    let failing = true;
    const x = restoration('?session=target', {
      loadSession: async (id) => {
        x.calls.push(['session', id]);
        if (failing) throw new Error(message);
        return { id };
      },
    });
    await assert.rejects(x.restore(), { message });
    assert.equal(x.location.search, '?session=target');
    failing = false;
    await x.restore();
    assert.equal(x.location.search, '');
    assert.deepEqual(x.calls, [['session', 'target'], ['session', 'target']]);
  });
}

test('combined links load the explicit session once and select the formalization after hydration', async () => {
  const x = restoration('?session=explicit&formalization=target');
  await x.restore();
  assert.deepEqual(x.calls, [['session', 'explicit'], ['scope', 'target']]);
  assert.equal(x.location.search, '');
});

test('formalization-only links select their associated session instead of the saved conversation', async () => {
  const x = restoration('?formalization=target');
  await x.restore();
  assert.deepEqual(x.calls, [['session', 'target-session'], ['scope', 'target']]);
  assert.equal(x.location.search, '');
});

test('failed formalization lookups leave both navigation parameters intact', async () => {
  const search = '?session=explicit&formalization=missing';
  const x = restoration(search, { loadFormalization: async () => { throw new Error('Formalization not found'); } });
  await assert.rejects(x.restore(), /Formalization not found/);
  assert.equal(x.location.search, search);
  assert.deepEqual(x.calls, []);
});

test('superseded session loads do not consume the link or change the selected formalization', async () => {
  const search = '?session=explicit&formalization=target';
  const x = restoration(search, { loadSession: async () => null });
  await x.restore();
  assert.equal(x.location.search, search);
  assert.deepEqual(x.calls, []);
});

test('a formalization with no conversation opens its project instead of the saved conversation', async () => {
  const x = restoration('?formalization=target', {
    loadFormalization: async (id) => ({ id, sessions: [], project_id: 'project' }),
  });
  await x.restore();
  assert.deepEqual(x.calls, [['project', 'project']]);
  assert.equal(x.location.search, '');
});

test('an unavailable formalization session surfaces an error and retains its link', async () => {
  const x = restoration('?formalization=target', {
    loadFormalization: async (id) => ({ id, sessions: [] }),
  });
  await assert.rejects(x.restore(), /No LeaChat session is available/);
  assert.equal(x.location.search, '?formalization=target');
  assert.deepEqual(x.calls, []);
});

test('normal startup still restores the last saved session', async () => {
  const x = restoration('');
  await x.restore();
  assert.deepEqual(x.calls, [['session', 'saved']]);
});
