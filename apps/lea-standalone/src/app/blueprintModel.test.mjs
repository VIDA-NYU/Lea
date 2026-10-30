import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';

import {
  statusCounts,
  isReady,
  byKey,
  readyNodes,
  dependents,
  otherBlockers,
  nodeStatusText,
  headerChip,
  summaryLine,
  nodeSessions,
  runningNodeKeys,
  parseBlueprintMarkdown,
  serializeBlueprint,
  appendNodeMarkdown,
  extractNameValue,
  isValidNodeKey,
  firstLine,
  suggestFileFromLean,
  displayFile,
  bindingPrompt,
  draftingPrompt,
  nodeSessionTitle,
  nodeOriginKey,
  relativeTime,
  findDeclLine,
} from './lib/blueprintModel.js';

// The mockup's √2 graph as /api/projects/{id}/graph returns it (frame 9).
const nodes = [
  { key: 'sqrt_two_irrational', kind: 'theorem', lean: null, uses: ['parity_descent', 'rational_sq_form'], statement: 'Irrational (Real.sqrt 2).', file: null, status: 'planned', verified: false, sessions: [] },
  { key: 'parity_descent', kind: 'lemma', lean: 'Lea.Sqrt2.parity_descent', uses: ['even_of_sq_even', 'not_coprime_of_even', 'sq_parity'], statement: 'No coprime a, b satisfy a² = 2b².', file: 'Lea/Sqrt2/Descent.lean', status: 'failed', verified: false, sessions: [{ session_id: 's-descent', title: 'Descent step', last_at: '2026-10-29T14:10:00Z' }, { session_id: 's-root', title: 'Irrationality of √2', last_at: '2026-10-29T14:02:00Z' }] },
  { key: 'rational_sq_form', kind: 'lemma', lean: 'Lea.Sqrt2.rational_sq_form', uses: [], statement: 'If √2 = a/b in lowest terms then a² = 2b².', file: 'Lea/Sqrt2/Rational.lean', status: 'stated', verified: false, sessions: [] },
  { key: 'even_of_sq_even', kind: 'lemma', lean: 'Lea.Sqrt2.even_of_sq_even', uses: [], statement: 'If a² is even then a is even.', file: 'Lea/Sqrt2/Parity.lean', status: 'proved', verified: true, sessions: [] },
  { key: 'not_coprime_of_even', kind: 'lemma', lean: 'Lea.Sqrt2.not_coprime_of_even', uses: [], statement: 'Two even integers are never coprime.', file: 'Lea/Sqrt2/Parity.lean', status: 'proved', verified: false, sessions: [] },
  { key: 'sq_parity', kind: 'lemma', lean: 'Lea.Sqrt2.sq_parity', uses: [], statement: 'a² = 2b² implies a² is even.', file: 'Lea/Sqrt2/Lemmas.lean', status: 'stated', verified: false, sessions: [] },
];

const sessions = [
  { id: 's-descent', title: 'Descent step', updated_at: '2026-10-29T14:10:00Z', active_run_count: 1 },
  { id: 's-root', title: 'Irrationality of √2', updated_at: '2026-10-29T14:02:00Z', active_run_count: 0 },
  { id: 's-rational', title: 'Rational form', updated_at: '2026-10-29T14:12:00Z', active_run_count: 1 },
];
const bindings = { 's-descent': 'parity_descent', 's-rational': 'rational_sq_form' };

test('statusCounts tallies every status plus verified and no-file nodes', () => {
  assert.deepEqual(statusCounts(nodes), { total: 6, proved: 2, ready: 0, stated: 2, failed: 1, planned: 1, verified: 1, noFile: 1 });
  assert.equal(statusCounts([]).total, 0);
});

test('isReady treats a leaf planned/stated node as ready, a blocked one as not', () => {
  const index = byKey(nodes);
  assert.equal(isReady(index.get('rational_sq_form'), index), true, 'leaf stated node');
  assert.equal(isReady(index.get('sqrt_two_irrational'), index), false, 'waits on unproved deps');
  assert.equal(isReady(index.get('even_of_sq_even'), index), false, 'already proved');
  assert.equal(isReady(index.get('parity_descent'), index), false, 'failed is not ready');
  assert.equal(isReady({ key: 'x', uses: ['a'], status: 'ready' }, index), true, 'adapter ready wins');
  assert.deepEqual(readyNodes(nodes).map((n) => n.key), ['rational_sq_form', 'sq_parity']);
});

test('dependents and otherBlockers power the Unblocks list', () => {
  assert.deepEqual(dependents(nodes, 'parity_descent').map((n) => n.key), ['sqrt_two_irrational']);
  assert.deepEqual(dependents(nodes, 'sq_parity').map((n) => n.key), ['parity_descent']);
  const index = byKey(nodes);
  assert.deepEqual(otherBlockers(index.get('sqrt_two_irrational'), 'parity_descent', index), ['rational_sq_form']);
  assert.deepEqual(otherBlockers(index.get('parity_descent'), 'sq_parity', index), []);
});

test('nodeStatusText pairs every colour with a label', () => {
  assert.equal(nodeStatusText({ status: 'proved', verified: true }), 'proved ✓ · verified');
  assert.equal(nodeStatusText({ status: 'proved', verified: false }), 'proved ✓');
  assert.equal(nodeStatusText({ status: 'stated' }), 'stated · sorry');
  assert.equal(nodeStatusText({ status: 'failed' }), 'failed');
  assert.equal(nodeStatusText({ status: 'ready' }), 'ready');
  assert.equal(nodeStatusText({ status: 'planned' }), 'planned');
  assert.equal(nodeStatusText({ status: 'planned' }, { proposal: true }), 'proposed');
});

test('headerChip summarises progress or the proposal', () => {
  assert.deepEqual(headerChip(nodes), { text: '2 of 6 proved', tone: 'failed' });
  assert.deepEqual(headerChip([]), { text: 'No nodes yet', tone: 'planned' });
  assert.deepEqual(headerChip(nodes.filter((n) => n.status === 'proved')), { text: 'All 2 proved', tone: 'proved' });
  assert.deepEqual(headerChip(nodes, { proposal: true }), { text: 'Proposal · 6 nodes · unsaved', tone: 'stated' });
  assert.deepEqual(headerChip([], { proposal: true }), { text: 'Proposal · drafting', tone: 'stated' });
});

test('summaryLine reads like the mockup footer and omits empty buckets', () => {
  assert.equal(
    summaryLine(nodes, { running: 2 }),
    '6 nodes · 2 proved · 2 stated · 1 failed · 1 planned · 2 sessions running · 1 node has no file yet',
  );
  assert.equal(summaryLine([{ key: 'a', uses: [], status: 'proved', file: 'A.lean' }]), '1 node · 1 proved');
  assert.equal(summaryLine([]), 'No nodes yet');
});

test('nodeSessions merges bound and committing sessions, running first then newest', () => {
  const rows = nodeSessions(nodes[1], sessions, bindings);
  assert.deepEqual(rows.map((r) => [r.session_id, r.running, r.bound]), [
    ['s-descent', true, true],
    ['s-root', false, false],
  ]);
  // A node with only a UI binding and no commits yet still lists its session.
  const rational = nodeSessions(nodes[2], sessions, bindings);
  assert.deepEqual(rational.map((r) => r.session_id), ['s-rational']);
  assert.equal(rational[0].title, 'Rational form');
  assert.deepEqual(nodeSessions(nodes[3], sessions, bindings), []);
});

test('runningNodeKeys marks nodes with a live bound or committing session', () => {
  const keys = runningNodeKeys(nodes, sessions, bindings);
  assert.deepEqual([...keys].sort(), ['parity_descent', 'rational_sq_form']);
  assert.deepEqual([...runningNodeKeys(nodes, sessions.map((s) => ({ ...s, active_run_count: 0 })), bindings)], []);
});

test('parseBlueprintMarkdown mirrors the adapter parser, fence-aware, with file', () => {
  const md = [
    'Intro prose before any node.',
    '',
    '```',
    '## not_a_node',
    '- kind: lemma',
    '```',
    '',
    '## sqrt_two_irrational',
    '- kind: Theorem',
    '- uses: parity_descent, rational_sq_form',
    '',
    'Irrational (Real.sqrt 2).',
    '',
    '## parity_descent',
    '- kind: lemma',
    '- lean: `Lea.Sqrt2.parity_descent`',
    '- file: Lea/Sqrt2/Descent.lean',
    '- uses: even_of_sq_even,sq_parity',
    'No coprime a, b satisfy a² = 2b².',
    'Second line.',
  ].join('\n');
  const { nodes: parsed, preamble, blocks } = parseBlueprintMarkdown(md);
  assert.equal(parsed.length, 2);
  assert.ok(preamble.startsWith('Intro prose'));
  assert.match(preamble, /## not_a_node/, 'a fenced heading stays preamble text');
  assert.deepEqual(parsed[0], {
    type: 'node', key: 'sqrt_two_irrational', kind: 'theorem', lean: null, uses: ['parity_descent', 'rational_sq_form'], file: null,
    statement: 'Irrational (Real.sqrt 2).',
  });
  assert.deepEqual(parsed[1], {
    type: 'node', key: 'parity_descent', kind: 'lemma', lean: 'Lea.Sqrt2.parity_descent', uses: ['even_of_sq_even', 'sq_parity'],
    file: 'Lea/Sqrt2/Descent.lean', statement: 'No coprime a, b satisfy a² = 2b².\nSecond line.',
  });
  assert.deepEqual(blocks, parsed);
  assert.deepEqual(parseBlueprintMarkdown(''), { nodes: [], preamble: '', blocks: [] });
});

test('parseBlueprintMarkdown: prose sections are blocks, not nodes; unknown headers are dropped', () => {
  const md = [
    '# Blueprint — Demo',
    '',
    '## Goal',
    '',
    'Prove the theorem.',
    '',
    '## Notes',
    '- author: Lea',
    '',
    '## main',
    '- status: PROVED',
    '- kind: theorem',
    '- lean: `Lea.Demo.main` — file `Lea/Demo/Main.lean`',
    '- uses: none',
    '',
    '- `aux` is proved alongside it.',
  ].join('\n');
  const { nodes: parsed, blocks, preamble } = parseBlueprintMarkdown(md);
  assert.equal(preamble, '# Blueprint — Demo');
  assert.deepEqual(blocks.map((b) => [b.type, b.type === 'prose' ? b.heading : b.key]), [
    ['prose', 'Goal'],
    ['prose', 'Notes'],
    ['node', 'main'],
  ]);
  assert.equal(blocks[0].body, 'Prove the theorem.');
  assert.equal(blocks[1].body, '- author: Lea', 'a header block with no recognised key is prose');
  assert.deepEqual(parsed.map((n) => [n.key, n.kind, n.lean, n.file, n.uses]), [['main', 'theorem', 'Lea.Demo.main', null, []]]);
  assert.equal(parsed[0].statement, '- `aux` is proved alongside it.', 'a `- word:` line after the header block is prose');
});

test('uses sentinels mean empty only as a single token that is not a node key', () => {
  const md = '## a\n- kind: lemma\n- uses: none\n\n## b\n- kind: lemma\n- uses: None, a\n\n## c\n- kind: lemma\n- uses: nothing\n\n## nothing\n- kind: lemma\n';
  const byK = Object.fromEntries(parseBlueprintMarkdown(md).nodes.map((n) => [n.key, n.uses]));
  assert.deepEqual(byK.a, []);
  assert.deepEqual(byK.b, ['None', 'a'], 'a sentinel next to other tokens is kept');
  assert.deepEqual(byK.c, ['nothing'], 'a node named like the sentinel makes it a real dependency');
});

test('extractNameValue: first backtick span, else the first token without trailing punctuation', () => {
  assert.equal(extractNameValue('`Lea.V.a` — file `A.lean`'), 'Lea.V.a');
  assert.equal(extractNameValue('Lea.V.b (the declaration)'), 'Lea.V.b');
  assert.equal(extractNameValue('Lea.V.e.'), 'Lea.V.e');
  assert.equal(extractNameValue('Lea.V.f);'), 'Lea.V.f');
  assert.equal(extractNameValue(''), null);
  assert.equal(extractNameValue('  '), null);
});

test('serializeBlueprint is canonical: header order, backticked lean and file, one trailing newline', () => {
  const text = serializeBlueprint([
    { key: 'a', kind: 'lemma', lean: 'Lea.A.a', uses: [], file: 'Lea/A.lean', statement: 'A holds.' },
    { key: 'b', kind: 'theorem', lean: null, uses: ['a'], file: null, statement: 'B follows from A.' },
  ], { preamble: '# Blueprint — A' });
  assert.equal(text, [
    '# Blueprint — A',
    '',
    '## a',
    '- kind: lemma',
    '- lean: `Lea.A.a`',
    '- file: `Lea/A.lean`',
    '',
    'A holds.',
    '',
    '## b',
    '- kind: theorem',
    '- uses: a',
    '',
    'B follows from A.',
    '',
  ].join('\n'));
  const { nodes: parsed } = parseBlueprintMarkdown(text);
  assert.equal(parsed[0].file, 'Lea/A.lean', 'file is always written when present');
  assert.equal(serializeBlueprint([]), '');
  // A node with no header line keeps an empty kind line, so it reads back as a node.
  const bare = serializeBlueprint([{ key: 'x', kind: null, lean: null, uses: [], file: null, statement: 'X.' }]);
  assert.equal(bare, '## x\n- kind:\n\nX.\n');
  assert.deepEqual(parseBlueprintMarkdown(bare).nodes.map((n) => [n.key, n.kind]), [['x', null]]);
});

test('serializeBlueprint keeps prose sections in place when given the parsed blocks', () => {
  const md = '# T\n\n## Strategy\n\nFirst a.\n\n## a\n- kind: lemma\n\nA.\n\n## Assembly\n\nThen b.\n\n## b\n- kind: theorem\n- uses: a\n\nB.\n';
  const parsed = parseBlueprintMarkdown(md);
  assert.equal(serializeBlueprint(parsed.nodes, { preamble: parsed.preamble, prose: parsed.blocks }), md);
  // Unchecked nodes drop out; prose stays.
  const onlyB = serializeBlueprint(parsed.nodes.filter((n) => n.key === 'b'), { preamble: parsed.preamble, prose: parsed.blocks });
  assert.equal(onlyB, '# T\n\n## Strategy\n\nFirst a.\n\n## Assembly\n\nThen b.\n\n## b\n- kind: theorem\n- uses: a\n\nB.\n');
  // Prose sections alone render before the nodes.
  const proseFirst = serializeBlueprint([parsed.nodes[0]], { prose: [{ heading: 'Goal', body: 'G.' }] });
  assert.equal(proseFirst, '## Goal\n\nG.\n\n## a\n- kind: lemma\n\nA.\n');
});

test('every fixture renders idempotently; canonical fixtures render byte for byte', () => {
  const dir = new URL('../../fixtures/blueprint/', import.meta.url);
  const names = readdirSync(dir).filter((n) => n.endsWith('.md') && n !== 'README.md').sort();
  assert.ok(names.length >= 8);
  for (const name of names) {
    const text = readFileSync(new URL(name, dir), 'utf8');
    const expected = JSON.parse(readFileSync(new URL(name.replace(/\.md$/, '.expected.json'), dir), 'utf8'));
    const render = (t) => {
      const p = parseBlueprintMarkdown(t);
      return serializeBlueprint(p.nodes, { preamble: p.preamble, prose: p.blocks });
    };
    const once = render(text);
    assert.equal(render(once), once, `${name}: idempotent`);
    assert.ok(once.endsWith('\n') && !once.endsWith('\n\n'), `${name}: one trailing newline`);
    if (expected.canonical) assert.equal(once, text, `${name}: canonical`);
    const prose = parseBlueprintMarkdown(text).blocks.filter((b) => b.type === 'prose').map((b) => b.heading);
    assert.deepEqual(prose, expected.prose_sections, `${name}: prose sections`);
  }
});

test('appendNodeMarkdown adds a section after the existing content', () => {
  const out = appendNodeMarkdown('## a\n- kind: lemma\n\nA.\n\n\n', { key: 'b', kind: 'theorem', lean: null, uses: ['a'], file: null, statement: 'B.' });
  assert.equal(out, '## a\n- kind: lemma\n\nA.\n\n## b\n- kind: theorem\n- uses: a\n\nB.\n');
  assert.equal(parseBlueprintMarkdown(out).nodes.length, 2);
  assert.ok(appendNodeMarkdown('', { key: 'b', kind: null, lean: null, uses: [], file: null, statement: '' }).startsWith('## b'));
});

test('isValidNodeKey accepts identifiers only', () => {
  assert.equal(isValidNodeKey('parity_descent'), true);
  assert.equal(isValidNodeKey('x'.repeat(64)), true);
  assert.equal(isValidNodeKey('x'.repeat(65)), false, 'at most 64 characters');
  assert.equal(isValidNodeKey("lemma'"), true);
  assert.equal(isValidNodeKey('two words'), false);
  assert.equal(isValidNodeKey('1abc'), false);
  assert.equal(isValidNodeKey(''), false);
});

test('firstLine, suggestFileFromLean and displayFile', () => {
  assert.equal(firstLine('\n  First.  \nSecond.'), 'First.');
  assert.equal(firstLine('x'.repeat(200), 10), 'xxxxxxxxx…');
  assert.equal(suggestFileFromLean('Lea.Sqrt2.parity_descent'), 'Lea/Sqrt2/ParityDescent.lean');
  assert.equal(suggestFileFromLean('sq_parity'), 'SqParity.lean');
  assert.equal(suggestFileFromLean(null), null);
  assert.equal(displayFile({ file: 'A.lean', lean: 'Lea.b' }), 'A.lean');
  assert.equal(displayFile({ file: null, lean: 'Lea.b' }), 'Lea/B.lean');
  assert.equal(displayFile({ file: null, lean: null }), null);
});

test('bindingPrompt states the node, the file and every dependency status', () => {
  const p = bindingPrompt(nodes[1], nodes);
  assert.match(p, /^Prove the blueprint node `parity_descent` \(lemma\)/);
  assert.match(p, /Statement: No coprime a, b satisfy a² = 2b²\./);
  assert.match(p, /Lean name: Lea\.Sqrt2\.parity_descent/);
  assert.match(p, /Target file: Lea\/Sqrt2\/Descent\.lean/);
  assert.match(p, /- even_of_sq_even — proved \(Lea\/Sqrt2\/Parity\.lean\)/);
  assert.match(p, /- sq_parity — stated with sorry \(Lea\/Sqrt2\/Lemmas\.lean\): use it as a hypothesis/);
  const top = bindingPrompt(nodes[0], nodes);
  assert.match(top, /Target file: not written yet/);
  assert.match(top, /- parity_descent — failed to check/);
  const leaf = bindingPrompt(nodes[5], nodes);
  assert.match(leaf, /Dependencies: none — this node is a leaf\./);
  const missing = bindingPrompt({ key: 'z', uses: ['ghost'], statement: 'Z.' }, nodes);
  assert.match(missing, /- ghost — not in the blueprint/);
});

test('draftingPrompt names the project and the markdown format', () => {
  const p = draftingPrompt({ title: 'Sqrt2', description: 'Prove √2 is irrational.' });
  assert.match(p, /write it to \.lea\/blueprint\.md/);
  assert.match(p, /Project: Sqrt2/);
  assert.match(p, /Goal: Prove √2 is irrational\./);
  assert.match(p, /## node_key/);
  assert.doesNotMatch(draftingPrompt({}), /Project:/);
});

test('session naming helpers', () => {
  assert.equal(nodeSessionTitle('parity_descent'), 'Prove parity_descent');
  assert.equal(nodeOriginKey('parity_descent'), 'blueprint:parity_descent');
});

test('relativeTime buckets seconds, minutes, hours, days', () => {
  const now = Date.parse('2026-10-29T12:00:00Z');
  const at = (seconds) => new Date(now - seconds * 1000).toISOString();
  assert.equal(relativeTime(at(2), now), 'just now');
  assert.equal(relativeTime(at(12), now), '12 s ago');
  assert.equal(relativeTime(at(5 * 60), now), '5 min ago');
  assert.equal(relativeTime(at(3 * 3600), now), '3 h ago');
  assert.equal(relativeTime(at(2 * 86400), now), '2 d ago');
  assert.equal(relativeTime('', now), '');
  assert.equal(relativeTime('nope', now), '');
});

test('findDeclLine locates the declaration by its short name', () => {
  const src = [
    'import Mathlib',
    '',
    'namespace Lea.Sqrt2',
    '',
    '/-- No coprime integers a, b satisfy a² = 2b². -/',
    '@[simp] theorem parity_descent {a b : ℤ} (h : a ^ 2 = 2 * b ^ 2) : False := by',
    '  sorry',
    '',
    'private lemma parity_descent_aux : True := trivial',
    'end Lea.Sqrt2',
  ].join('\n');
  assert.equal(findDeclLine(src, 'Lea.Sqrt2.parity_descent'), 6);
  assert.equal(findDeclLine(src, 'parity_descent_aux'), 9);
  assert.equal(findDeclLine(src, 'Lea.Sqrt2.missing'), null);
  assert.equal(findDeclLine('', 'x'), null);
  assert.equal(findDeclLine(src, null), null);
  // A qualified name in the source still matches on its last component.
  assert.equal(findDeclLine('theorem Lea.Foo.bar : True := trivial', 'bar'), 1);
});
