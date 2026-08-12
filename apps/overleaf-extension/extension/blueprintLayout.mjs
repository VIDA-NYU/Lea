// Framework-free blueprint graph layout + status presentation.
//
// ⚠️ MIRROR — the canonical source is `packages/lea-blueprint/blueprintLayout.mjs`.
// A Chrome MV3 content-script module can only `import` files inside the extension
// package (loaded via `chrome.runtime.getURL`), so it cannot reach `packages/` at
// runtime and this repo has no bundler/copy step. This committed copy is what the
// extension actually loads; `tests/blueprintLayout.mirror.test.mjs` asserts it stays
// behaviorally identical to the canonical module. Edit the canonical file and copy
// the change here (or vice-versa) — the guard test fails if they drift.
//
// Consumers: the standalone React view imports the canonical package copy
// (`BlueprintGraph.tsx`); the Overleaf Lean-pane view imports THIS copy
// (`blueprintPaneView.mjs`). Pure geometry + strings — no DOM, no framework.

export const NODE_W = 176;
export const NODE_H = 54;
export const H_GAP = 36;
export const V_GAP = 72;
export const PAD = 28;

/** @type {Record<string, string>} */
export const STATUS_LABEL = {
  planned: 'Planned',
  stated: 'Stated',
  ready: 'Ready',
  proved: 'Proved',
  failed: 'Failed',
};

// A `proved` node splits on the audit-grade verdict: SafeVerify-cleared reads as a
// sealed "Proved ✓"; a Lean-check-only pass reads as "check ✓ · audit pending" (the
// node compiles + has no sorry, but the kernel/axiom audit hasn't confirmed it).
/**
 * @param {{ status: string, verified: boolean }} n
 * @returns {string}
 */
export function statusLabel(n) {
  if (n.status === 'proved') return n.verified ? 'Proved ✓' : 'check ✓ · audit pending';
  return STATUS_LABEL[n.status] ?? n.status;
}

// Status class for color, refined for proved: audited keeps the strong green;
// unaudited gets a paler, dashed treatment so it doesn't read as fully sealed.
/**
 * @param {{ status: string, verified: boolean }} n
 * @returns {string}
 */
export function statusClass(n) {
  if (n.status === 'proved') return n.verified ? 'bp-proved bp-audited' : 'bp-proved bp-unaudited';
  return `bp-${n.status}`;
}

// Longest-dependency-path level per node (memoized, cycle-guarded), then a centered
// row per level. Edge `from → to` means `from` depends on `to`, so `to` is deeper.
/**
 * @template {{ key: string, uses: string[] }} T
 * @param {{ nodes: T[], edges: { from: string, to: string }[] }} graph
 * @returns {{ placed: Map<string, { node: T, x: number, y: number }>, width: number, height: number, order: string[] }}
 */
export function computeLayout(graph) {
  const keys = new Set(graph.nodes.map((n) => n.key));
  /** @type {Map<string, string[]>} */
  const deps = new Map();
  for (const n of graph.nodes) deps.set(n.key, n.uses.filter((u) => keys.has(u)));

  /** @type {Map<string, number>} */
  const level = new Map();
  /** @type {Set<string>} */
  const visiting = new Set();
  /** @param {string} key @returns {number} */
  const levelOf = (key) => {
    if (level.has(key)) return level.get(key);
    if (visiting.has(key)) return 0; // break cycles defensively
    visiting.add(key);
    let lv = 0;
    for (const d of deps.get(key) ?? []) lv = Math.max(lv, levelOf(d) + 1);
    visiting.delete(key);
    level.set(key, lv);
    return lv;
  };
  for (const n of graph.nodes) levelOf(n.key);

  const maxLevel = graph.nodes.reduce((m, n) => Math.max(m, level.get(n.key) ?? 0), 0);
  /** @type {Map<number, T[]>} */
  const byLevel = new Map();
  for (const n of graph.nodes) {
    const lv = level.get(n.key) ?? 0;
    (byLevel.get(lv) ?? byLevel.set(lv, []).get(lv)).push(n);
  }

  /** @param {number} count */
  const rowWidth = (count) => count * NODE_W + (count - 1) * H_GAP;
  const width = Math.max(
    NODE_W + PAD * 2,
    ...Array.from(byLevel.values(), (row) => rowWidth(row.length) + PAD * 2),
  );
  const height = PAD * 2 + (maxLevel + 1) * NODE_H + maxLevel * V_GAP;

  /** @type {Map<string, { node: T, x: number, y: number }>} */
  const placed = new Map();
  /** @type {string[]} */
  const order = [];
  for (const [lv, row] of byLevel) {
    const startX = (width - rowWidth(row.length)) / 2;
    const y = PAD + (maxLevel - lv) * (NODE_H + V_GAP);
    row.forEach((node, i) => {
      placed.set(node.key, { node, x: startX + i * (NODE_W + H_GAP), y });
      order.push(node.key);
    });
  }
  return { placed, width, height, order };
}

/**
 * @param {string} text
 * @param {number} [max]
 * @returns {string}
 */
export function truncate(text, max = 22) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}
