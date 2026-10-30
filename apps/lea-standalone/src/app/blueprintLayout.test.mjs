import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  NODE_W,
  NODE_H,
  H_GAP,
  PAD,
  LANE_PAD,
  LANE_HEAD,
  LANE_GAP,
  LANE_V_GAP,
  NO_FILE_LANE,
  computeLayout,
  computeLevels,
  computeFileLaneLayout,
} from '../../../../packages/lea-blueprint/blueprintLayout.mjs';

// The √2 decomposition from the mockup: theorem on top, three leaves, one planned
// node (no file) and two lemmas sharing Parity.lean.
const graph = {
  nodes: [
    { key: 'sqrt_two_irrational', uses: ['parity_descent', 'rational_sq_form'], file: 'Lea/Sqrt2.lean' },
    { key: 'parity_descent', uses: ['even_of_sq_even', 'not_coprime_of_even', 'sq_parity'], file: 'Lea/Sqrt2/Descent.lean' },
    { key: 'rational_sq_form', uses: [], file: null },
    { key: 'even_of_sq_even', uses: [], file: 'Lea/Sqrt2/Parity.lean' },
    { key: 'not_coprime_of_even', uses: [], file: 'Lea/Sqrt2/Parity.lean' },
    { key: 'sq_parity', uses: [], file: 'Lea/Sqrt2/Lemmas.lean' },
  ],
};

test('computeLevels matches the levels computeLayout places rows by', () => {
  const levels = computeLevels(graph);
  assert.equal(levels.get('sqrt_two_irrational'), 2);
  assert.equal(levels.get('parity_descent'), 1);
  assert.equal(levels.get('rational_sq_form'), 0);
  assert.equal(levels.get('sq_parity'), 0);
  // Same vertical order as the level layout: deeper level → lower on the canvas.
  const placed = computeLayout(graph).placed;
  assert.ok(placed.get('sqrt_two_irrational').y < placed.get('parity_descent').y);
  assert.ok(placed.get('parity_descent').y < placed.get('sq_parity').y);
});

test('computeLevels breaks cycles instead of recursing forever', () => {
  const levels = computeLevels({ nodes: [{ key: 'a', uses: ['b'] }, { key: 'b', uses: ['a'] }] });
  assert.equal(levels.size, 2);
});

test('file lanes: one lane per (file, row), same-file nodes adjacent, no-file lane dashed and last', () => {
  const { lanes, placed, order, width, height } = computeFileLaneLayout(graph);
  assert.equal(placed.size, graph.nodes.length);
  assert.equal(order.length, graph.nodes.length);

  // rational_sq_form has no dependencies, so it is a level-0 leaf like the others;
  // its lane is the dashed one and goes last in its row.
  const labels = lanes.map((l) => `${l.level}:${l.label}`);
  assert.deepEqual(labels, [
    '2:Lea/Sqrt2.lean',
    '1:Lea/Sqrt2/Descent.lean',
    '0:Lea/Sqrt2/Parity.lean',
    '0:Lea/Sqrt2/Lemmas.lean',
    `0:${NO_FILE_LANE}`,
  ]);

  const parity = lanes.find((l) => l.file === 'Lea/Sqrt2/Parity.lean');
  assert.deepEqual(parity.keys, ['even_of_sq_even', 'not_coprime_of_even']);
  assert.equal(parity.width, 2 * NODE_W + H_GAP + 2 * LANE_PAD);
  assert.equal(parity.height, LANE_HEAD + 2 * LANE_PAD + NODE_H);

  const noFile = lanes.find((l) => l.file === null);
  assert.equal(noFile.dashed, true);
  assert.deepEqual(noFile.keys, ['rational_sq_form']);
  for (const lane of lanes) if (lane.file !== null) assert.equal(lane.dashed, false);

  // Nodes sit inside their lane, under the header strip.
  for (const lane of lanes) {
    for (const key of lane.keys) {
      const p = placed.get(key);
      assert.ok(p.x >= lane.x + LANE_PAD - 1e-9, `${key} starts inside ${lane.label}`);
      assert.ok(p.x + NODE_W <= lane.x + lane.width - LANE_PAD + 1e-9, `${key} ends inside ${lane.label}`);
      assert.equal(p.y, lane.y + LANE_HEAD + LANE_PAD);
    }
  }

  // Lanes in one row do not overlap and keep LANE_GAP between them.
  const row0 = lanes.filter((l) => l.level === 0);
  assert.equal(row0.length, 3);
  assert.equal(row0[1].x - (row0[0].x + row0[0].width), LANE_GAP);
  assert.equal(row0[2].x - (row0[1].x + row0[1].width), LANE_GAP);

  // Rows are LANE_V_GAP apart; the canvas wraps everything with PAD.
  const top = lanes.find((l) => l.level === 2);
  const mid = lanes.find((l) => l.level === 1);
  assert.equal(mid.y - (top.y + top.height), LANE_V_GAP);
  assert.equal(top.y, PAD);
  assert.ok(width >= Math.max(...lanes.map((l) => l.x + l.width)) + PAD - 1e-9);
  assert.equal(height, PAD * 2 + 3 * top.height + 2 * LANE_V_GAP);
});

test('file lanes: a custom fileOf lets a consumer group by something else', () => {
  const { lanes } = computeFileLaneLayout(graph, { fileOf: () => 'all.lean' });
  assert.deepEqual([...new Set(lanes.map((l) => l.label))], ['all.lean']);
  assert.equal(lanes.length, 3, 'one lane per row');
});

test('file lanes: empty graph yields no lanes and a non-zero canvas', () => {
  const l = computeFileLaneLayout({ nodes: [] });
  assert.deepEqual(l.lanes, []);
  assert.equal(l.placed.size, 0);
  assert.ok(l.width > 0 && l.height > 0);
});

test('computeLayout is unchanged by the lane additions (extension mirror contract)', () => {
  const l = computeLayout(graph);
  assert.equal(l.placed.size, 6);
  assert.equal(l.order.length, 6);
  // Three levels, three rows: height is the level formula exactly.
  assert.equal(l.height, PAD * 2 + 3 * NODE_H + 2 * 72);
});
