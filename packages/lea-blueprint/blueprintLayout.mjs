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

// ── By-file lanes (UI redesign, mockup frame 9) ────────────────────────────
//
// The "By file" grouping keeps the layered rows of `computeLayout` (foundations at
// the bottom, theorems on top) but reorders each row so nodes that live in the same
// `.lean` file sit side by side, and draws a lane around every such group: a rounded
// rectangle with the file name and node count in a header strip. Nodes without a
// resolved file (planned nodes) share a dashed "no file yet" lane. One lane per
// (file, row): a file that spans levels gets one lane segment per level.
//
// `computeLayout` is deliberately untouched (the Overleaf extension mirrors its
// source byte for byte); the level computation is duplicated in `computeLevels`.

export const LANE_PAD = 12; // node edge → lane edge (sides and bottom)
export const LANE_HEAD = 20; // header strip above the nodes (file name, count)
export const LANE_GAP = 24; // between two lanes in one row
export const LANE_V_GAP = 56; // between lane rows
export const NO_FILE_LANE = 'no file yet';

/**
 * Longest-dependency-path level per node (cycle-guarded). Same rule as
 * `computeLayout`: edge `from → to` means `from` depends on `to`, so `to` is deeper.
 * @template {{ key: string, uses: string[] }} T
 * @param {{ nodes: T[] }} graph
 * @returns {Map<string, number>}
 */
export function computeLevels(graph) {
  const keys = new Set(graph.nodes.map((n) => n.key));
  /** @type {Map<string, string[]>} */
  const deps = new Map();
  for (const n of graph.nodes) deps.set(n.key, (n.uses ?? []).filter((u) => keys.has(u)));
  /** @type {Map<string, number>} */
  const level = new Map();
  /** @type {Set<string>} */
  const visiting = new Set();
  /** @param {string} key @returns {number} */
  const levelOf = (key) => {
    if (level.has(key)) return level.get(key);
    if (visiting.has(key)) return 0;
    visiting.add(key);
    let lv = 0;
    for (const d of deps.get(key) ?? []) lv = Math.max(lv, levelOf(d) + 1);
    visiting.delete(key);
    level.set(key, lv);
    return lv;
  };
  for (const n of graph.nodes) levelOf(n.key);
  return level;
}

/**
 * @typedef {Object} Lane
 * @property {string | null} file     repo-relative file, or null for the dashed lane
 * @property {string} label           file name or NO_FILE_LANE
 * @property {boolean} dashed         true for the "no file yet" lane
 * @property {number} level           the row (0 = foundations)
 * @property {number} x
 * @property {number} y
 * @property {number} width
 * @property {number} height
 * @property {string[]} keys          nodes inside, left to right
 */

/**
 * Layered layout with a lane per (file, row). Returns the same `placed` / `width` /
 * `height` / `order` shape as `computeLayout` plus `lanes`, so a consumer draws the
 * lanes first, then edges and nodes exactly as before.
 * @template {{ key: string, uses: string[], file?: string | null }} T
 * @param {{ nodes: T[] }} graph
 * @param {{ fileOf?: (node: T) => string | null }} [options]
 * @returns {{ placed: Map<string, { node: T, x: number, y: number }>, lanes: Lane[], width: number, height: number, order: string[] }}
 */
export function computeFileLaneLayout(graph, options = {}) {
  const fileOf = options.fileOf ?? ((n) => n.file ?? null);
  const level = computeLevels(graph);
  const maxLevel = graph.nodes.reduce((m, n) => Math.max(m, level.get(n.key) ?? 0), 0);

  /** @type {Map<number, T[]>} */
  const byLevel = new Map();
  for (const n of graph.nodes) {
    const lv = level.get(n.key) ?? 0;
    (byLevel.get(lv) ?? byLevel.set(lv, []).get(lv)).push(n);
  }

  // Global file order: first appearance scanning from the top row down, so a file
  // keeps roughly the same column across rows. The "no file yet" lane always goes last.
  /** @type {string[]} */
  const fileOrder = [];
  for (let lv = maxLevel; lv >= 0; lv--) {
    for (const n of byLevel.get(lv) ?? []) {
      const f = fileOf(n);
      if (f != null && !fileOrder.includes(f)) fileOrder.push(f);
    }
  }
  /** @param {string | null} f */
  const rank = (f) => (f == null ? fileOrder.length : fileOrder.indexOf(f));

  /** @param {number} count */
  const groupWidth = (count) => count * NODE_W + (count - 1) * H_GAP + LANE_PAD * 2;
  const laneHeight = LANE_HEAD + LANE_PAD * 2 + NODE_H;

  /** @type {Map<number, { file: string | null, nodes: T[] }[]>} */
  const rows = new Map();
  let maxRowWidth = NODE_W + LANE_PAD * 2;
  for (const [lv, nodes] of byLevel) {
    /** @type {Map<string | null, T[]>} */
    const groups = new Map();
    for (const n of nodes) {
      const f = fileOf(n);
      (groups.get(f) ?? groups.set(f, []).get(f)).push(n);
    }
    const ordered = [...groups.entries()]
      .map(([file, members]) => ({ file, nodes: members }))
      .sort((a, b) => rank(a.file) - rank(b.file));
    rows.set(lv, ordered);
    const rowWidth =
      ordered.reduce((w, g) => w + groupWidth(g.nodes.length), 0) + (ordered.length - 1) * LANE_GAP;
    maxRowWidth = Math.max(maxRowWidth, rowWidth);
  }

  const width = maxRowWidth + PAD * 2;
  const rowCount = maxLevel + 1;
  const height = PAD * 2 + rowCount * laneHeight + (rowCount - 1) * LANE_V_GAP;

  /** @type {Map<string, { node: T, x: number, y: number }>} */
  const placed = new Map();
  /** @type {Lane[]} */
  const lanes = [];
  /** @type {string[]} */
  const order = [];
  for (let lv = maxLevel; lv >= 0; lv--) {
    const ordered = rows.get(lv) ?? [];
    const rowWidth =
      ordered.reduce((w, g) => w + groupWidth(g.nodes.length), 0) + (ordered.length - 1) * LANE_GAP;
    let x = (width - rowWidth) / 2;
    const laneY = PAD + (maxLevel - lv) * (laneHeight + LANE_V_GAP);
    const nodeY = laneY + LANE_HEAD + LANE_PAD;
    for (const g of ordered) {
      const w = groupWidth(g.nodes.length);
      const keys = [];
      g.nodes.forEach((node, i) => {
        placed.set(node.key, { node, x: x + LANE_PAD + i * (NODE_W + H_GAP), y: nodeY });
        order.push(node.key);
        keys.push(node.key);
      });
      lanes.push({
        file: g.file,
        label: g.file ?? NO_FILE_LANE,
        dashed: g.file == null,
        level: lv,
        x,
        y: laneY,
        width: w,
        height: laneHeight,
        keys,
      });
      x += w + LANE_GAP;
    }
  }
  return { placed, lanes, width, height, order };
}
