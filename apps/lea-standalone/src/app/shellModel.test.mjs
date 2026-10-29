import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  blueprintTally,
  blueprintBar,
  countNotes,
  docExcerpt,
  fileStatuses,
  flattenTree,
  projectActivity,
  projectRunning,
  projectSummary,
  projectTitleFromStatement,
  proveNodeMessage,
  recentSessions,
  relativeTime,
  sessionNodeMap,
  sessionState,
  sessionStateLabel,
  sidebarTally,
  sortProjectsByActivity,
  unfiledSessions,
  upNext,
} from './lib/shellModel.js';

const node = (key, status, extra = {}) => ({ key, status, uses: [], sessions: [], verified: false, ...extra });

test('blueprintTally counts every status and audited proofs', () => {
  const tally = blueprintTally([
    node('a', 'proved', { verified: true }),
    node('b', 'proved'),
    node('c', 'ready'),
    node('d', 'stated'),
    node('e', 'failed'),
    node('f', 'planned'),
    node('g', 'weird'),
  ]);
  assert.deepEqual(tally, { total: 7, proved: 2, ready: 1, stated: 1, failed: 1, planned: 2, verified: 1 });
  assert.deepEqual(blueprintTally([]), { total: 0, proved: 0, ready: 0, stated: 0, failed: 0, planned: 0, verified: 0 });
  assert.deepEqual(blueprintTally(undefined).total, 0);
});

test('blueprintBar draws non-empty statuses in order with percentages that sum to 100', () => {
  const tally = blueprintTally([
    node('a', 'proved'), node('b', 'proved'), node('c', 'proved'),
    node('d', 'ready'), node('e', 'stated'), node('f', 'planned'),
  ]);
  const bar = blueprintBar(tally);
  assert.deepEqual(bar.map((s) => s.status), ['proved', 'ready', 'stated', 'planned']);
  assert.deepEqual(bar.map((s) => s.pct), [50, 16.7, 16.7, 16.7]);
  assert.deepEqual(bar.map((s) => s.label), ['proved', 'ready', 'sorry', 'planned']);
  assert.ok(Math.abs(bar.reduce((sum, s) => sum + s.pct, 0) - 100) < 0.2);
  assert.deepEqual(blueprintBar(blueprintTally([])), []);
});

test('projectSummary phrases the card line like the mockup', () => {
  const six = blueprintTally([
    node('a', 'proved'), node('b', 'proved'), node('c', 'proved'),
    node('d', 'ready'), node('e', 'stated'), node('f', 'planned'),
  ]);
  assert.equal(projectSummary(six, 4, 7), '6 nodes · 3 proved · 4 sessions · 7 files');
  assert.equal(projectSummary(blueprintTally([node('a', 'proved', { verified: true })]), 1), '1 node · proved · verified · 1 session');
  assert.equal(projectSummary(blueprintTally([node('a', 'proved')]), 1), '1 node · proved · 1 session');
  assert.equal(projectSummary(blueprintTally([node('a', 'ready'), node('b', 'ready')]), 0), '2 nodes · 2 ready · no sessions yet');
  assert.equal(projectSummary(blueprintTally([]), 2), 'no blueprint yet · 2 sessions');
  assert.equal(sidebarTally(six), '3/6');
  assert.equal(sidebarTally(blueprintTally([])), '');
});

test('relativeTime steps from "just now" to a dated label', () => {
  const now = Date.parse('2026-10-29T12:00:00');
  assert.equal(relativeTime('2026-10-29T11:59:50', now), 'just now');
  assert.equal(relativeTime('2026-10-29T11:58:00', now), '2 min ago');
  assert.equal(relativeTime('2026-10-29T09:00:00', now), '3 h ago');
  assert.equal(relativeTime('2026-10-26T12:00:00', now), 'Mon');
  assert.equal(relativeTime('2026-10-21T12:00:00', now), 'Oct 21');
  assert.equal(relativeTime('2025-10-21T12:00:00', now), 'Oct 21, 2025');
  assert.equal(relativeTime('not a date', now), '');
  assert.equal(relativeTime(undefined, now), '');
});

test('projectTitleFromStatement strips the lead-in, caps and title-cases', () => {
  assert.equal(projectTitleFromStatement('Prove that the square root of 2 is irrational.'), 'The Square Root of 2 Is Irrational');
  assert.equal(projectTitleFromStatement('prove that every bounded monotone sequence converges'), 'Every Bounded Monotone Sequence Converges');
  assert.equal(projectTitleFromStatement('Show that for all n, n^2 ≥ n'), 'For All n, n^2 ≥ n');
  assert.equal(projectTitleFromStatement('Theorem: Cauchy–Schwarz holds in ℝ^n'), 'Cauchy–Schwarz Holds in ℝ^n');
  assert.equal(projectTitleFromStatement('  '), 'Untitled project');
  assert.equal(projectTitleFromStatement('|a + b| ≤ |a| + |b|'), '|a + b| ≤ |a| + |b|', 'symbols untouched');
  const long = projectTitleFromStatement('Prove that ' + 'word '.repeat(40));
  assert.ok(long.length <= 60, `capped: ${long.length}`);
  assert.ok(!long.endsWith(' '));
  assert.equal(projectTitleFromStatement('abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz').length, 60, 'hard cap without spaces');
});

test('sessionNodeMap takes graph attribution and lets bindings override', () => {
  const graph = {
    nodes: [
      node('parity', 'proved', { sessions: [{ session_id: 's1', title: 'Parity', last_at: '' }] }),
      node('descent', 'proved', { sessions: [{ session_id: 's1', title: 'Parity', last_at: '' }, { session_id: 's2', title: 'Descent', last_at: '' }] }),
    ],
  };
  assert.deepEqual(sessionNodeMap(graph, {}), { s1: 'parity', s2: 'descent' });
  assert.deepEqual(sessionNodeMap(graph, { s1: 'descent', s3: 'main' }), { s1: 'descent', s2: 'descent', s3: 'main' });
  assert.deepEqual(sessionNodeMap(null, { s9: 'x' }), { s9: 'x' });
});

test('sessionState and its label', () => {
  assert.equal(sessionState({ id: 'a', status: 'proved' }, 'a'), 'running');
  assert.equal(sessionState({ id: 'a', status: 'proved', active_run_count: 1 }), 'running');
  assert.equal(sessionState({ id: 'a', status: 'proved' }), 'proved');
  assert.equal(sessionState({ id: 'a', status: 'ok' }), 'proved');
  assert.equal(sessionState({ id: 'a', status: 'error' }), 'failed');
  assert.equal(sessionState({ id: 'a', status: 'unchecked', latest_recovery_state: 'review' }), 'review');
  assert.equal(sessionState({ id: 'a', status: 'needs_review' }), 'review');
  assert.equal(sessionState({ id: 'a', status: 'disproved' }), 'disproved');
  assert.equal(sessionState({ id: 'a', status: 'empty' }), 'idle');
  assert.equal(sessionStateLabel('failed'), 'error');
  assert.equal(sessionStateLabel('review'), 'needs review');
});

test('project helpers: running, activity, sort, unfiled, recent', () => {
  const sessions = [
    { id: 's1', project_id: 'p1', status: 'proved', updated_at: '2026-10-29T10:00:00Z' },
    { id: 's2', project_id: 'p2', status: 'unchecked', active_run_count: 1, updated_at: '2026-10-29T11:00:00Z' },
    { id: 's3', project_id: null, status: 'ok', updated_at: '2026-10-28T11:00:00Z' },
    { id: 's4', project_id: 'p1', parent_id: 's1', status: 'ok', updated_at: '2026-10-29T12:00:00Z' },
  ];
  const projects = [
    { id: 'p1', updated_at: '2026-10-20T00:00:00Z' },
    { id: 'p2', updated_at: '2026-10-21T00:00:00Z' },
    { id: 'p3', updated_at: '2026-10-30T00:00:00Z' },
  ];
  assert.equal(projectRunning('p2', sessions), true);
  assert.equal(projectRunning('p1', sessions), false);
  assert.equal(projectRunning('p1', sessions, 's1'), true);
  assert.equal(projectActivity(projects[0], sessions), Date.parse('2026-10-29T12:00:00Z'));
  assert.deepEqual(sortProjectsByActivity(projects, sessions).map((p) => p.id), ['p3', 'p1', 'p2']);
  assert.deepEqual(unfiledSessions(sessions).map((s) => s.id), ['s3']);
  assert.deepEqual(recentSessions(sessions, 2).map((s) => s.id), ['s2', 's1'], 'roots only, newest first');
});

test('upNext orders open > prove > prove the sorry > retry > blocked, with reasons', () => {
  const nodes = [
    node('parity', 'proved'),
    node('main', 'planned', { uses: ['parity', 'rational'] }),
    node('rational', 'ready', { uses: ['parity'] }),
    node('sq_parity', 'stated', { file: 'Lea/Sqrt2/Lemmas.lean' }),
    node('bad', 'failed', { file: 'Lea/Sqrt2/Bad.lean' }),
    node('orphan', 'planned'),
  ];
  const byNode = { rational: [{ id: 's1', title: 'Rational form', state: 'running' }] };
  const items = upNext(nodes, byNode);
  assert.deepEqual(items.map((i) => [i.node.key, i.action]), [
    ['rational', 'open'],
    ['sq_parity', 'prove-sorry'],
    ['bad', 'retry'],
    ['main', 'blocked'],
    ['orphan', 'blocked'],
  ]);
  assert.equal(items[0].sessionId, 's1');
  assert.equal(items[0].reason, 'ready · session Rational form is on it');
  assert.equal(items[1].reason, 'stated · sorry left in Lea/Sqrt2/Lemmas.lean');
  assert.equal(items[3].reason, 'planned · waits on rational');
  assert.equal(items[4].reason, 'planned · no statement yet');
  assert.equal(upNext(nodes, {}, 2).length, 2);
  assert.equal(upNext(nodes, {})[0].action, 'prove');
  assert.equal(upNext(nodes, {})[0].reason, 'ready · uses parity');
});

test('fileStatuses takes the weakest node per file', () => {
  const statuses = fileStatuses([
    node('a', 'proved', { file: 'A.lean' }),
    node('b', 'stated', { file: 'A.lean' }),
    node('c', 'proved', { file: 'B.lean' }),
    node('d', 'failed', { file: 'C.lean' }),
    node('e', 'ready', { file: 'C.lean' }),
    node('f', 'planned', { file: null }),
  ]);
  assert.deepEqual(statuses, { 'A.lean': 'sorry', 'B.lean': 'proved', 'C.lean': 'error' });
});

test('flattenTree lists files, skipping hidden dirs', () => {
  const tree = [
    { name: 'Lea', path: 'Lea', type: 'dir', children: [
      { name: 'Sqrt2.lean', path: 'Lea/Sqrt2.lean', type: 'file' },
      { name: 'Sqrt2', path: 'Lea/Sqrt2', type: 'dir', children: [{ name: 'Parity.lean', path: 'Lea/Sqrt2/Parity.lean', type: 'file' }] },
    ] },
    { name: '.lea', path: '.lea', type: 'dir', children: [{ name: 'memory.md', path: '.lea/memory.md', type: 'file' }] },
    { name: 'lakefile.toml', path: 'lakefile.toml', type: 'file' },
  ];
  assert.deepEqual(flattenTree(tree), ['Lea/Sqrt2.lean', 'Lea/Sqrt2/Parity.lean', 'lakefile.toml']);
  assert.deepEqual(flattenTree(tree, { leanOnly: true }), ['Lea/Sqrt2.lean', 'Lea/Sqrt2/Parity.lean']);
});

test('proveNodeMessage names the node, declaration, file and statement', () => {
  const n = node('rational_sq_form', 'ready', {
    lean: 'Lea.Sqrt2.rational_sq_form', file: 'Lea/Sqrt2/Rational.lean', kind: 'lemma',
    statement: 'If q^2 = 2 then q is not rational.', uses: ['sq_parity'],
  });
  const prove = proveNodeMessage(n);
  assert.match(prove, /^Prove blueprint node `rational_sq_form` \(declaration `Lea\.Sqrt2\.rational_sq_form`\) in `Lea\/Sqrt2\/Rational\.lean`\./);
  assert.match(prove, /lemma statement: If q\^2 = 2 then q is not rational\./);
  assert.match(prove, /It may use: sq_parity\./);
  assert.match(proveNodeMessage(n, 'prove-sorry'), /^Prove the remaining sorry in blueprint node/);
  assert.match(proveNodeMessage(n, 'prove-sorry'), /Keep the statement unchanged/);
  assert.match(proveNodeMessage(n, 'retry'), /^Retry blueprint node .* the last check failed/);
  assert.equal(proveNodeMessage(node('x', 'ready')), 'Prove blueprint node `x`.');
});

test('countNotes and docExcerpt', () => {
  assert.equal(countNotes('# Memory\n\n- one\n- two\n\n1. three\n'), 3);
  assert.equal(countNotes('# Memory\n\nA paragraph.\n\nAnother one.\n'), 2);
  assert.equal(countNotes(''), 0);
  assert.equal(docExcerpt('# Instructions\n\n- Work over ℤ, not ℕ.\n- Prefer `Int.even_*` lemmas.\n'), 'Work over ℤ, not ℕ. Prefer Int.even_* lemmas.');
  const long = docExcerpt('word '.repeat(100), 50);
  assert.ok(long.length <= 52 && long.endsWith('…'), long);
  assert.equal(docExcerpt(''), '');
});
