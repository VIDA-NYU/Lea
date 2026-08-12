import assert from 'node:assert/strict';
import test from 'node:test';

import {
  pickInitialSession,
  readDeepLinkSessionId,
  readDeepLinkFormalizationId,
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
