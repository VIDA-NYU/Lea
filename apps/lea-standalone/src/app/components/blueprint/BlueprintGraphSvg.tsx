import { useMemo } from 'react';
import type { GraphNode, ProjectGraph } from '../../lib/api';
import {
  NODE_W,
  NODE_H,
  computeLayout,
  computeFileLaneLayout,
  truncate,
} from '../../../../../../packages/lea-blueprint/blueprintLayout.mjs';
import { nodeStatusText } from '../../lib/blueprintModel.js';

export type Grouping = 'level' | 'file';

// The dependency graph as SVG (mockup frames 8–9). Layout comes from the shared
// blueprintLayout.mjs: `computeLayout` for By level, `computeFileLaneLayout` for By
// file (a lane per file around its nodes, a dashed "no file yet" lane). Nodes are
// styled by status; a bound session with a live run adds a running badge and the
// word "running" to the node's label (never hue alone). In a proposal every node is
// dashed and reads "proposed".

export function BlueprintGraphSvg({
  graph,
  grouping,
  selectedKey,
  runningKeys,
  proposal = false,
  onSelect,
}: {
  graph: ProjectGraph;
  grouping: Grouping;
  selectedKey: string | null;
  runningKeys: Set<string>;
  proposal?: boolean;
  onSelect: (key: string | null) => void;
}) {
  const layout = useMemo(
    () => (grouping === 'file' ? computeFileLaneLayout(graph) : { ...computeLayout(graph), lanes: [] }),
    [graph, grouping],
  );

  const edges = graph.edges.map((e, i) => {
    const from = layout.placed.get(e.from);
    const to = layout.placed.get(e.to);
    if (!from || !to) return null;
    const x1 = from.x + NODE_W / 2;
    const y1 = from.y + NODE_H;
    const x2 = to.x + NODE_W / 2;
    const y2 = to.y;
    const my = (y1 + y2) / 2;
    return (
      <path
        key={`${e.from}-${e.to}-${i}`}
        className="bpv-edge"
        d={`M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`}
        markerEnd="url(#bpv-arrow)"
      />
    );
  });

  return (
    <svg
      className="bpv-svg"
      viewBox={`0 0 ${layout.width} ${layout.height}`}
      style={{ maxWidth: layout.width }}
      role="img"
      aria-label={`Blueprint dependency graph, ${grouping === 'file' ? 'grouped by file' : 'by level'}`}
      onClick={(e) => {
        if (e.target === e.currentTarget) onSelect(null);
      }}
    >
      <defs>
        <marker id="bpv-arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 Z" className="bpv-arrowhead" />
        </marker>
      </defs>

      {layout.lanes.map((lane) => (
        <g key={`${lane.level}:${lane.label}`} className={`bpv-lane${lane.dashed ? ' is-dashed' : ''}`}>
          <rect x={lane.x} y={lane.y} width={lane.width} height={lane.height} rx={12} />
          <text className="bpv-lane-label" x={lane.x + 12} y={lane.y + 15}>
            {truncate(lane.label, Math.max(12, Math.floor(lane.width / 7)))}
          </text>
          <text className="bpv-lane-count" x={lane.x + lane.width - 12} y={lane.y + 15} textAnchor="end">
            {lane.keys.length === 1 ? '1 node' : `${lane.keys.length} nodes`}
          </text>
        </g>
      ))}

      {edges}

      {layout.order.map((key) => {
        const p = layout.placed.get(key)!;
        const n = p.node as GraphNode;
        const running = runningKeys.has(key);
        const tone = proposal ? 'planned' : n.status;
        const selected = selectedKey === key;
        const label = `${n.kind ?? 'node'} · ${nodeStatusText(n, { proposal })}${running ? ' · running' : ''}`;
        return (
          <g
            key={key}
            className={`bpv-node is-${tone}${proposal ? ' is-proposal' : ''}${selected ? ' is-selected' : ''}${running ? ' is-running' : ''}`}
            transform={`translate(${p.x},${p.y})`}
            role="button"
            tabIndex={0}
            aria-label={`${n.key}: ${label}`}
            aria-pressed={selected}
            onClick={(e) => {
              e.stopPropagation();
              onSelect(selected ? null : key);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onSelect(selected ? null : key);
              }
            }}
          >
            {selected && <rect className="bpv-node-ring" x={-4} y={-4} width={NODE_W + 8} height={NODE_H + 8} rx={14} />}
            <rect className="bpv-node-box" width={NODE_W} height={NODE_H} rx={10} />
            <text className="bpv-node-key" x={NODE_W / 2} y={NODE_H / 2 - 4}>
              {truncate(n.key, 24)}
            </text>
            <text className="bpv-node-sub" x={NODE_W / 2} y={NODE_H / 2 + 12}>
              {truncate(label, 30)}
            </text>
            {running && <circle className="bpv-node-badge" cx={NODE_W - 2} cy={2} r={6} />}
          </g>
        );
      })}
    </svg>
  );
}
