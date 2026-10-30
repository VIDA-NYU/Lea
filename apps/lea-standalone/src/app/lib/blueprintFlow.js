// Blueprint graph → React Flow elements — the pure half of
// components/blueprint/BlueprintGraph.tsx. No DOM, no React, no @xyflow import:
// positions come from the shared layout module (packages/lea-blueprint/
// blueprintLayout.mjs, the single source of truth for both front ends) and are
// turned into plain node/edge objects in React Flow's shape, plus the keyboard
// navigation rule (↑ dependent, ↓ dependency, ←/→ same row). Tested alone in
// src/app/blueprintFlow.test.mjs.
//
// Lanes (By file) are background nodes with absolute positions, drawn below the
// cards (zIndex -1, not selectable, not focusable, pointer-events off so a click
// falls through to the pane). Cards are not children of their lane: the layout
// already gives absolute coordinates, and a parent/child split would only
// re-derive what it computed.
//
// Every node carries its size (`width`/`height`/`measured`) and its handle
// geometry (`handles`), so React Flow can draw edges and fit the view without
// waiting for a DOM measurement, and re-created node objects (a selection
// change, a refetch) never drop back to "unmeasured".

import {
  NODE_W,
  NODE_H,
  computeLayout,
  computeFileLaneLayout,
} from '../../../../../packages/lea-blueprint/blueprintLayout.mjs';
import { nodeStatusText } from './blueprintModel.js';

/** React Flow node type names (keys of the component's `nodeTypes`). */
export const BLUEPRINT_NODE = 'blueprintNode';
export const LANE_NODE = 'laneNode';
export const LANE_ID_PREFIX = 'lane:';

/** Target handle on the top edge (dependency side), source on the bottom (dependent side). */
const CARD_HANDLES = [
  { type: 'target', position: 'top', x: NODE_W / 2 - 0.5, y: 0, width: 1, height: 1 },
  { type: 'source', position: 'bottom', x: NODE_W / 2 - 0.5, y: NODE_H - 1, width: 1, height: 1 },
];

/**
 * @typedef {import('../../../../../packages/lea-blueprint/blueprintLayout.mjs').Lane} Lane
 * @typedef {'level' | 'file'} Grouping
 * @typedef {'planned' | 'stated' | 'ready' | 'proved' | 'failed'} Tone
 */

/**
 * @template {{ key: string, uses: string[], file?: string | null }} T
 * @typedef {Object} GraphLayout
 * @property {Map<string, { node: T, x: number, y: number }>} placed
 * @property {Lane[]} lanes   empty for By level
 * @property {number} width
 * @property {number} height
 * @property {string[]} order
 */

/**
 * The layout for a grouping: `computeLayout` (By level) or `computeFileLaneLayout`
 * (By file, one lane per file and row). The graph component never positions a
 * node itself.
 * @template {{ key: string, uses: string[], file?: string | null }} T
 * @param {{ nodes: T[], edges?: { from: string, to: string }[] }} graph
 * @param {Grouping} grouping
 * @returns {GraphLayout<T>}
 */
export function graphLayout(graph, grouping) {
  if (grouping === 'file') return computeFileLaneLayout(graph);
  const layout = computeLayout({ nodes: graph.nodes, edges: graph.edges ?? [] });
  return { ...layout, lanes: [] };
}

/**
 * Last path segment: `Lea/Sqrt2/Parity.lean` → `Parity.lean`.
 * @param {string | null | undefined} file
 * @returns {string | null}
 */
export function fileBaseName(file) {
  if (!file) return null;
  const parts = String(file).split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : null;
}

/** Stable id of a lane node. */
export function laneId(/** @type {Lane} */ lane) {
  return `${LANE_ID_PREFIX}${lane.level}:${lane.file ?? ''}`;
}

/** @param {string} id */
export function isLaneId(id) {
  return typeof id === 'string' && id.startsWith(LANE_ID_PREFIX);
}

/**
 * @typedef {Object} CardData
 * @property {any} node          the graph node (GraphNode)
 * @property {Tone} tone          colour family; `planned` for every node in a proposal
 * @property {string} statusText  "proved ✓", "stated · sorry", "proposed", …
 * @property {string} label       "lemma · stated · sorry · running"
 * @property {boolean} running
 * @property {boolean} proposal
 * @property {string | null} fileName
 */

/**
 * React Flow nodes for a layout: lanes first (so they paint and list below), then
 * one card per placed node in layout order.
 * @param {GraphLayout<any>} layout
 * @param {{ selectedKey?: string | null, runningKeys?: Set<string>, proposal?: boolean }} [options]
 */
export function flowNodes(layout, options = {}) {
  const selectedKey = options.selectedKey ?? null;
  const runningKeys = options.runningKeys ?? new Set();
  const proposal = !!options.proposal;

  const lanes = layout.lanes.map((lane) => ({
    id: laneId(lane),
    type: LANE_NODE,
    position: { x: lane.x, y: lane.y },
    width: lane.width,
    height: lane.height,
    measured: { width: lane.width, height: lane.height },
    handles: [],
    data: {
      label: lane.label,
      file: lane.file,
      dashed: lane.dashed,
      count: lane.keys.length,
      level: lane.level,
    },
    zIndex: -1,
    selectable: false,
    draggable: false,
    connectable: false,
    focusable: false,
    deletable: false,
    style: { pointerEvents: 'none' },
  }));

  const cards = layout.order.map((key) => {
    const p = /** @type {{ node: any, x: number, y: number }} */ (layout.placed.get(key));
    const n = p.node;
    const running = runningKeys.has(key);
    const tone = /** @type {Tone} */ (proposal ? 'planned' : n.status ?? 'planned');
    const statusText = nodeStatusText(n, { proposal });
    const label = `${n.kind ?? 'node'} · ${statusText}${running ? ' · running' : ''}`;
    /** @type {CardData} */
    const data = { node: n, tone, statusText, label, running, proposal, fileName: fileBaseName(n.file) };
    return {
      id: key,
      type: BLUEPRINT_NODE,
      position: { x: p.x, y: p.y },
      width: NODE_W,
      height: NODE_H,
      measured: { width: NODE_W, height: NODE_H },
      handles: CARD_HANDLES.map((h) => ({ ...h })),
      data,
      selected: key === selectedKey,
      selectable: true,
      draggable: false,
      connectable: false,
      focusable: true,
      deletable: false,
      ariaLabel: `${key}: ${label}${n.file ? ` · ${n.file}` : ''}`,
    };
  });

  return [...lanes, ...cards];
}

/**
 * One edge per `uses` entry (dependent → dependency) whose target is in the graph;
 * duplicates and self-references are dropped. Edges touching the selected node are
 * marked active (class + the active marker).
 * @param {{ nodes: { key: string, uses: string[] }[] }} graph
 * @param {{ selectedKey?: string | null, marker?: string, activeMarker?: string }} [options]
 */
export function flowEdges(graph, options = {}) {
  const selectedKey = options.selectedKey ?? null;
  const keys = new Set(graph.nodes.map((n) => n.key));
  /** @type {Set<string>} */
  const seen = new Set();
  const edges = [];
  for (const n of graph.nodes) {
    for (const dep of n.uses ?? []) {
      if (dep === n.key || !keys.has(dep)) continue;
      const id = `${n.key}->${dep}`;
      if (seen.has(id)) continue;
      seen.add(id);
      const active = selectedKey != null && (n.key === selectedKey || dep === selectedKey);
      const marker = active ? options.activeMarker : options.marker;
      edges.push({
        id,
        source: n.key,
        target: dep,
        type: 'default',
        className: active ? 'bpv-edge is-active' : 'bpv-edge',
        ...(marker ? { markerEnd: marker } : {}),
        selectable: false,
        focusable: false,
        deletable: false,
        zIndex: active ? 1 : 0,
        ariaLabel: `${n.key} uses ${dep}`,
      });
    }
  }
  return edges;
}

/**
 * The node a keyboard arrow moves the selection to, or null when there is none:
 *   up    → a dependent (a node that uses `key`), the one closest in x
 *   down  → a dependency (`key`'s uses), the one closest in x
 *   left / right → the previous / next node on the same row
 * @param {GraphLayout<any>} layout
 * @param {string} key
 * @param {'up' | 'down' | 'left' | 'right'} direction
 * @returns {string | null}
 */
export function neighbourKey(layout, key, direction) {
  const here = layout.placed.get(key);
  if (!here) return null;
  /** @param {string[]} candidates */
  const closest = (candidates) => {
    let best = null;
    let bestDist = Infinity;
    for (const k of candidates) {
      const p = layout.placed.get(k);
      if (!p || k === key) continue;
      const d = Math.abs(p.x - here.x) + Math.abs(p.y - here.y) / 1000;
      if (d < bestDist) {
        best = k;
        bestDist = d;
      }
    }
    return best;
  };
  if (direction === 'down') return closest(here.node.uses ?? []);
  if (direction === 'up') {
    const dependents = [];
    for (const [k, p] of layout.placed) if ((p.node.uses ?? []).includes(key)) dependents.push(k);
    return closest(dependents);
  }
  const row = [...layout.placed.entries()]
    .filter(([, p]) => p.y === here.y)
    .sort((a, b) => a[1].x - b[1].x)
    .map(([k]) => k);
  const i = row.indexOf(key);
  const j = direction === 'left' ? i - 1 : i + 1;
  return j >= 0 && j < row.length ? row[j] : null;
}

/**
 * Where keyboard navigation starts when nothing is selected: the top-left node.
 * @param {GraphLayout<any>} layout
 * @returns {string | null}
 */
export function firstKey(layout) {
  let best = null;
  /** @type {{ x: number, y: number } | null} */
  let at = null;
  for (const [k, p] of layout.placed) {
    if (!at || p.y < at.y || (p.y === at.y && p.x < at.x)) {
      best = k;
      at = p;
    }
  }
  return best;
}

/**
 * Centre of a node in flow coordinates (for setCenter), or null.
 * @param {GraphLayout<any>} layout
 * @param {string} key
 * @returns {{ x: number, y: number } | null}
 */
export function nodeCenter(layout, key) {
  const p = layout.placed.get(key);
  return p ? { x: p.x + NODE_W / 2, y: p.y + NODE_H / 2 } : null;
}
