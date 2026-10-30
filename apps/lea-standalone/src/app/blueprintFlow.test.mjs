import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  NODE_W,
  NODE_H,
  computeLayout,
  computeFileLaneLayout,
} from '../../../../packages/lea-blueprint/blueprintLayout.mjs';
import {
  BLUEPRINT_NODE,
  LANE_NODE,
  graphLayout,
  flowNodes,
  flowEdges,
  neighbourKey,
  firstKey,
  nodeCenter,
  fileBaseName,
  isLaneId,
} from './lib/blueprintFlow.js';

// The √2 decomposition from the mockup (frames 8–9).
const graph = {
  nodes: [
    { key: 'sqrt_two_irrational', kind: 'theorem', uses: ['parity_descent', 'rational_sq_form'], file: 'Lea/Sqrt2.lean', status: 'planned' },
    { key: 'parity_descent', kind: 'lemma', uses: ['even_of_sq_even', 'not_coprime_of_even', 'sq_parity'], file: 'Lea/Sqrt2/Descent.lean', status: 'failed' },
    { key: 'rational_sq_form', kind: 'lemma', uses: [], file: null, status: 'ready' },
    { key: 'even_of_sq_even', kind: 'lemma', uses: [], file: 'Lea/Sqrt2/Parity.lean', status: 'proved', verified: true },
    { key: 'not_coprime_of_even', kind: 'lemma', uses: [], file: 'Lea/Sqrt2/Parity.lean', status: 'proved' },
    { key: 'sq_parity', kind: 'lemma', uses: [], file: 'Lea/Sqrt2/Lemmas.lean', status: 'stated' },
  ],
  edges: [
    { from: 'sqrt_two_irrational', to: 'parity_descent' },
    { from: 'sqrt_two_irrational', to: 'rational_sq_form' },
    { from: 'parity_descent', to: 'even_of_sq_even' },
    { from: 'parity_descent', to: 'not_coprime_of_even' },
    { from: 'parity_descent', to: 'sq_parity' },
  ],
};

test('graphLayout uses the shared layout module for both groupings', () => {
  const level = graphLayout(graph, 'level');
  const ref = computeLayout(graph);
  assert.deepEqual(level.lanes, []);
  assert.equal(level.width, ref.width);
  for (const [key, p] of ref.placed) assert.deepEqual([level.placed.get(key).x, level.placed.get(key).y], [p.x, p.y]);

  const file = graphLayout(graph, 'file');
  const lanes = computeFileLaneLayout(graph);
  assert.equal(file.lanes.length, lanes.lanes.length);
  for (const [key, p] of lanes.placed) assert.deepEqual([file.placed.get(key).x, file.placed.get(key).y], [p.x, p.y]);
});

test('flowNodes: one card per node at the layout position, sized and with fixed handles', () => {
  const layout = graphLayout(graph, 'level');
  const nodes = flowNodes(layout, { selectedKey: 'parity_descent', runningKeys: new Set(['rational_sq_form']) });
  assert.equal(nodes.length, graph.nodes.length);
  for (const n of nodes) {
    assert.equal(n.type, BLUEPRINT_NODE);
    const p = layout.placed.get(n.id);
    assert.deepEqual(n.position, { x: p.x, y: p.y });
    assert.equal(n.width, NODE_W);
    assert.equal(n.height, NODE_H);
    assert.deepEqual(n.measured, { width: NODE_W, height: NODE_H });
    assert.equal(n.draggable, false);
    assert.equal(n.focusable, true);
    const target = n.handles.find((h) => h.type === 'target');
    const source = n.handles.find((h) => h.type === 'source');
    assert.equal(target.position, 'top');
    assert.equal(source.position, 'bottom');
    assert.equal(target.y, 0);
    assert.equal(source.y + source.height, NODE_H);
  }
  const byId = new Map(nodes.map((n) => [n.id, n]));
  assert.equal(byId.get('parity_descent').selected, true);
  assert.equal(byId.get('sq_parity').selected, false);
  assert.equal(byId.get('rational_sq_form').data.running, true);
  assert.equal(byId.get('rational_sq_form').data.label, 'lemma · ready · running');
  assert.equal(byId.get('even_of_sq_even').data.label, 'lemma · proved ✓ · verified');
  assert.equal(byId.get('sq_parity').data.tone, 'stated');
  assert.equal(byId.get('sq_parity').data.fileName, 'Lemmas.lean');
  assert.equal(byId.get('rational_sq_form').data.fileName, null);
  assert.match(byId.get('sq_parity').ariaLabel, /^sq_parity: lemma · stated · sorry · Lea\/Sqrt2\/Lemmas\.lean$/);
});

test('flowNodes: a proposal paints every node planned and "proposed"', () => {
  const nodes = flowNodes(graphLayout(graph, 'level'), { proposal: true });
  for (const n of nodes) {
    assert.equal(n.data.tone, 'planned');
    assert.equal(n.data.statusText, 'proposed');
    assert.equal(n.data.proposal, true);
  }
});

test('flowNodes: By file adds background lane nodes first, below the cards and inert', () => {
  const layout = graphLayout(graph, 'file');
  const nodes = flowNodes(layout);
  const lanes = nodes.filter((n) => n.type === LANE_NODE);
  assert.equal(lanes.length, layout.lanes.length);
  assert.deepEqual(nodes.slice(0, lanes.length), lanes, 'lanes come first');
  const ids = new Set(nodes.map((n) => n.id));
  assert.equal(ids.size, nodes.length, 'ids are unique');
  for (const lane of lanes) {
    assert.ok(isLaneId(lane.id));
    assert.equal(lane.zIndex, -1);
    assert.equal(lane.selectable, false);
    assert.equal(lane.focusable, false);
    assert.equal(lane.style.pointerEvents, 'none');
  }
  const noFile = lanes.find((l) => l.data.file === null);
  assert.equal(noFile.data.dashed, true);
  assert.equal(noFile.data.label, 'no file yet');
  const parity = lanes.find((l) => l.data.file === 'Lea/Sqrt2/Parity.lean');
  assert.equal(parity.data.count, 2);
  // Every card sits inside the lane that lists it.
  for (const lane of layout.lanes) {
    for (const key of lane.keys) {
      const card = nodes.find((n) => n.id === key);
      assert.ok(card.position.x >= lane.x && card.position.x + NODE_W <= lane.x + lane.width);
      assert.ok(card.position.y >= lane.y && card.position.y + NODE_H <= lane.y + lane.height);
    }
  }
});

test('flowEdges: one per uses entry, dependent → dependency, unknown and duplicate targets dropped', () => {
  const g = {
    nodes: [
      { key: 'a', uses: ['b', 'c', 'b', 'missing', 'a'] },
      { key: 'b', uses: ['c'] },
      { key: 'c', uses: [] },
    ],
  };
  const edges = flowEdges(g, { marker: 'm', activeMarker: 'm-on' });
  assert.deepEqual(
    edges.map((e) => [e.source, e.target]),
    [
      ['a', 'b'],
      ['a', 'c'],
      ['b', 'c'],
    ],
  );
  for (const e of edges) {
    assert.equal(e.markerEnd, 'm');
    assert.equal(e.className, 'bpv-edge');
    assert.equal(e.selectable, false);
  }
});

test('flowEdges: edges touching the selected node are active', () => {
  const edges = flowEdges(graph, { selectedKey: 'parity_descent', marker: 'm', activeMarker: 'm-on' });
  assert.equal(edges.length, graph.edges.length);
  const active = edges.filter((e) => e.className.includes('is-active')).map((e) => e.id).sort();
  assert.deepEqual(active, [
    'parity_descent->even_of_sq_even',
    'parity_descent->not_coprime_of_even',
    'parity_descent->sq_parity',
    'sqrt_two_irrational->parity_descent',
  ]);
  for (const e of edges) assert.equal(e.markerEnd, e.className.includes('is-active') ? 'm-on' : 'm');
});

test('neighbourKey: ↓ dependency, ↑ dependent, ←/→ along the row', () => {
  for (const grouping of ['level', 'file']) {
    const layout = graphLayout(graph, grouping);
    assert.equal(neighbourKey(layout, 'sqrt_two_irrational', 'up'), null, grouping);
    assert.ok(['parity_descent', 'rational_sq_form'].includes(neighbourKey(layout, 'sqrt_two_irrational', 'down')));
    assert.equal(neighbourKey(layout, 'parity_descent', 'up'), 'sqrt_two_irrational');
    assert.equal(neighbourKey(layout, 'sq_parity', 'up'), 'parity_descent');
    assert.equal(neighbourKey(layout, 'sq_parity', 'down'), null);
    assert.ok(['even_of_sq_even', 'not_coprime_of_even', 'sq_parity'].includes(neighbourKey(layout, 'parity_descent', 'down')));

    // The row order is the layout's left-to-right order.
    const row = [...layout.placed.entries()]
      .filter(([, p]) => p.y === layout.placed.get('sq_parity').y)
      .sort((a, b) => a[1].x - b[1].x)
      .map(([k]) => k);
    assert.equal(neighbourKey(layout, row[0], 'left'), null);
    assert.equal(neighbourKey(layout, row[0], 'right'), row[1]);
    assert.equal(neighbourKey(layout, row[row.length - 1], 'right'), null);
    assert.equal(neighbourKey(layout, row[1], 'left'), row[0]);
  }
  assert.equal(neighbourKey(graphLayout(graph, 'level'), 'nope', 'up'), null);
});

test('neighbourKey ↓ picks the dependency closest in x', () => {
  const layout = graphLayout(graph, 'level');
  const here = layout.placed.get('parity_descent');
  const pick = neighbourKey(layout, 'parity_descent', 'down');
  const dist = (k) => Math.abs(layout.placed.get(k).x - here.x);
  for (const dep of ['even_of_sq_even', 'not_coprime_of_even', 'sq_parity']) assert.ok(dist(pick) <= dist(dep));
});

test('firstKey is the top-left node; nodeCenter is the card centre', () => {
  const layout = graphLayout(graph, 'level');
  assert.equal(firstKey(layout), 'sqrt_two_irrational');
  assert.equal(firstKey(graphLayout({ nodes: [] }, 'level')), null);
  const p = layout.placed.get('sq_parity');
  assert.deepEqual(nodeCenter(layout, 'sq_parity'), { x: p.x + NODE_W / 2, y: p.y + NODE_H / 2 });
  assert.equal(nodeCenter(layout, 'nope'), null);
});

test('fileBaseName', () => {
  assert.equal(fileBaseName('Lea/Sqrt2/Parity.lean'), 'Parity.lean');
  assert.equal(fileBaseName('Top.lean'), 'Top.lean');
  assert.equal(fileBaseName(null), null);
  assert.equal(fileBaseName(''), null);
});
