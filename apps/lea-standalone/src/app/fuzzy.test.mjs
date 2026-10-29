import { test } from 'node:test';
import assert from 'node:assert/strict';

import { fuzzyMatch, fuzzyMatchPath, highlightMatch, isWordBoundary, rankMatches } from './lib/fuzzy.js';

test('fuzzyMatch finds a case-insensitive subsequence and reports its positions', () => {
  const hit = fuzzyMatch('par', 'Parity.lean');
  assert.ok(hit);
  assert.deepEqual(hit.positions, [0, 1, 2]);
  assert.ok(hit.score > 0);
  assert.deepEqual(fuzzyMatch('pfr', 'PrimeFactor.lean')?.positions, [0, 5, 10]);
});

test('fuzzyMatch returns null when the query is not a subsequence', () => {
  assert.equal(fuzzyMatch('xyz', 'Parity.lean'), null);
  assert.equal(fuzzyMatch('parity!', 'Parity.lean'), null);
  assert.equal(fuzzyMatch('longer', 'long'), null, 'query longer than text');
  assert.equal(fuzzyMatch('a', ''), null);
});

test('an empty query matches everything with score 0', () => {
  assert.deepEqual(fuzzyMatch('', 'anything'), { score: 0, positions: [] });
  assert.deepEqual(fuzzyMatch('', ''), { score: 0, positions: [] });
});

test('scoring prefers a prefix, then consecutive characters, over scattered ones', () => {
  const prefix = fuzzyMatch('par', 'Parity.lean').score;
  const inner = fuzzyMatch('par', 'sq_parity').score;
  const scattered = fuzzyMatch('par', 'PrimeFactor.lean').score;
  assert.ok(prefix > inner, 'prefix beats an inner run');
  assert.ok(inner > scattered, 'a consecutive run beats scattered characters');
});

test('scoring prefers word boundaries and shorter texts', () => {
  const boundary = fuzzyMatch('sp', 'sq_parity').score;
  const plain = fuzzyMatch('sp', 'aspirin').score;
  assert.ok(boundary > plain, 'a hit after `_` beats a hit mid-word');
  const short = fuzzyMatch('lem', 'Lemmas.lean').score;
  const long = fuzzyMatch('lem', 'LemmasAboutEverything.lean').score;
  assert.ok(short > long, 'the shorter text wins on equal matches');
});

test('isWordBoundary recognises separators and camelCase humps', () => {
  assert.equal(isWordBoundary('a/b', 0), true);
  assert.equal(isWordBoundary('a/b', 2), true);
  assert.equal(isWordBoundary('PrimeFactor', 5), true, 'the F of Factor');
  assert.equal(isWordBoundary('PrimeFactor', 2), false);
  assert.equal(isWordBoundary('sq_parity', 3), true);
});

test('fuzzyMatchPath matches the basename first and offsets positions into the full path', () => {
  const hit = fuzzyMatchPath('par', 'Lea/Sqrt2/Parity.lean');
  assert.ok(hit);
  assert.deepEqual(hit.positions, [10, 11, 12]);
  const whole = fuzzyMatch('par', 'Lea/Sqrt2/Parity.lean');
  assert.ok(hit.score > whole.score, 'a basename hit outranks the same hit scored on the whole path');
});

test('fuzzyMatchPath falls back to the whole path, and always uses it when the query has a slash', () => {
  const fallback = fuzzyMatchPath('sqpar', 'Lea/Sqrt2/Parity.lean');
  assert.ok(fallback, 'characters across directories still match');
  assert.equal(fallback.positions[0], 4, 'S of Sqrt2');
  const slashed = fuzzyMatchPath('sqrt2/par', 'Lea/Sqrt2/Parity.lean');
  assert.ok(slashed);
  assert.equal(fuzzyMatchPath('lea/', 'Parity.lean'), null);
});

test('highlightMatch splits text into hit and miss segments', () => {
  assert.deepEqual(highlightMatch('Parity.lean', [0, 1, 2]), [
    { text: 'Par', hit: true },
    { text: 'ity.lean', hit: false },
  ]);
  assert.deepEqual(highlightMatch('abc', [1]), [
    { text: 'a', hit: false },
    { text: 'b', hit: true },
    { text: 'c', hit: false },
  ]);
  assert.deepEqual(highlightMatch('', []), []);
});

test('rankMatches drops misses, sorts by score and keeps input order on ties', () => {
  const files = ['Lea/Sqrt2/PrimeFactor.lean', 'Lea/Sqrt2/Parity.lean', 'README.md', 'Lea/Sqrt2/Lemmas.lean'];
  const ranked = rankMatches('par', files, (f) => f, { path: true });
  assert.deepEqual(ranked.map((r) => r.item), ['Lea/Sqrt2/Parity.lean', 'Lea/Sqrt2/PrimeFactor.lean']);
  const all = rankMatches('', files, (f) => f);
  assert.deepEqual(all.map((r) => r.item), files, 'an empty query keeps every item in order');
  assert.equal(rankMatches('e', files, (f) => f, { limit: 2 }).length, 2);
});
