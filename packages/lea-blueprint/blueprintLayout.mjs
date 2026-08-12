// Framework-free blueprint graph layout + status presentation.
//
// Shared by both front ends so the derived dependency graph looks and lays out
// identically in each (one source of truth, no drift):
//   - the standalone React view (`apps/lea-standalone/src/app/components/BlueprintGraph.tsx`)
//   - the Overleaf extension's Lean-pane blueprint view (FEATURE-overleaf-blueprint-view)
//
// Pure geometry + string helpers — no DOM, no framework. The consumer owns
// drawing (SVG in React / `createElementNS` in the content script). Node shape is
// keyed off `kind` (definition = box, lemma/theorem = ellipse) and color off the
// live-derived `status`; those decisions live at the draw site, this module only
// supplies the layout coordinates and the status label/class strings.

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
