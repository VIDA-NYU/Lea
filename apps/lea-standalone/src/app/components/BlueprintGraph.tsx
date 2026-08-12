import { useEffect, useMemo, useState } from 'react';
import { getProjectGraph, type GraphNode, type ProjectGraph } from '../lib/api';
// Layout geometry + status label/class strings are shared, framework-free, with the
// Overleaf extension's blueprint view so both front ends lay the graph out identically
// (one source of truth — see packages/lea-blueprint/blueprintLayout.mjs).
import {
  NODE_W,
  NODE_H,
  STATUS_LABEL,
  computeLayout,
  statusClass,
  statusLabel,
  truncate,
} from '../../../../../packages/lea-blueprint/blueprintLayout.mjs';

// The interactive blueprint graph (F7/D28/D29). Renders the derived dependency
// graph: shape encodes `kind` (definition → box, lemma/theorem → ellipse), color
// encodes the live-derived `status`, edges are `uses` dependencies. Clicking a node
// opens a detail panel listing the sessions that built it (deep-link into the chat).
//
// Layout is a hand-rolled layered DAG (in the shared module): a node's level is the
// longest chain of dependencies beneath it, so foundations sit at the bottom and the
// theorems that build on them rise to the top. No external graph library.

export function BlueprintGraph({
  projectId,
  onOpenSession,
  refreshSignal = 0,
}: {
  projectId: string;
  onOpenSession: (sessionId: string) => void;
  refreshSignal?: number;
}) {
  const [graph, setGraph] = useState<ProjectGraph | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getProjectGraph(projectId)
      .then((g) => !cancelled && (setGraph(g), setError(null)))
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : String(err)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [projectId, refreshSignal]);

  const layout = useMemo(() => (graph ? computeLayout(graph) : null), [graph]);

  // Keep a selection only while its node still exists after a refresh.
  const selectedNode = selected && layout?.placed.get(selected)?.node;

  if (loading) return <div className="bp-graph-msg">Loading graph…</div>;
  if (error) return <div className="bp-graph-msg bp-graph-err">{error}</div>;
  if (!graph || graph.nodes.length === 0) {
    return (
      <div className="bp-graph-empty">
        <p>No blueprint nodes yet.</p>
        <p className="bp-graph-empty-hint">
          Switch to <b>Markdown</b> and add a <code>## node</code> section, or let Lea sketch the
          decomposition as it proves — each node appears here, colored by its live status.
        </p>
      </div>
    );
  }

  return (
    <div className="bp-graph">
      <div className="bp-graph-canvas">
        <svg width={layout!.width} height={layout!.height} role="img" aria-label="Blueprint dependency graph">
          {/* edges first so nodes paint on top */}
          {graph.edges.map((e, i) => {
            const from = layout!.placed.get(e.from);
            const to = layout!.placed.get(e.to);
            if (!from || !to) return null;
            const x1 = from.x + NODE_W / 2;
            const y1 = from.y + NODE_H;
            const x2 = to.x + NODE_W / 2;
            const y2 = to.y;
            const my = (y1 + y2) / 2;
            return (
              <path
                key={`${e.from}-${e.to}-${i}`}
                className="bp-edge"
                d={`M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`}
                markerEnd="url(#bp-arrow)"
              />
            );
          })}
          <defs>
            <marker id="bp-arrow" markerWidth="9" markerHeight="9" refX="7" refY="3"
              orient="auto" markerUnits="strokeWidth">
              <path d="M0,0 L7,3 L0,6 Z" className="bp-arrowhead" />
            </marker>
          </defs>

          {layout!.order.map((key) => {
            const p = layout!.placed.get(key)!;
            const n = p.node;
            const isDef = n.kind === 'definition';
            const cls = `bp-node ${statusClass(n)}${selected === key ? ' is-selected' : ''}`;
            return (
              <g
                key={key}
                className="bp-node-g"
                transform={`translate(${p.x},${p.y})`}
                onClick={() => setSelected((s) => (s === key ? null : key))}
              >
                {isDef ? (
                  <rect className={cls} width={NODE_W} height={NODE_H} rx={7} />
                ) : (
                  <ellipse className={cls} cx={NODE_W / 2} cy={NODE_H / 2} rx={NODE_W / 2} ry={NODE_H / 2} />
                )}
                <text className="bp-node-key" x={NODE_W / 2} y={NODE_H / 2 - 3}>
                  {truncate(n.key)}
                </text>
                <text className="bp-node-kind" x={NODE_W / 2} y={NODE_H / 2 + 13}>
                  {n.kind ?? '—'} · {statusLabel(n)}
                </text>
              </g>
            );
          })}
        </svg>
      </div>

      <div className="bp-graph-side">
        <ul className="bp-legend">
          <li><span className="bp-swatch bp-proved bp-audited" /> Proved ✓ <span className="bp-legend-note">(SafeVerify-audited)</span></li>
          <li><span className="bp-swatch bp-proved bp-unaudited" /> check ✓ <span className="bp-legend-note">(audit pending)</span></li>
          {(['ready', 'stated', 'planned', 'failed'] as const).map((s) => (
            <li key={s}>
              <span className={`bp-swatch bp-${s}`} /> {STATUS_LABEL[s]}
            </li>
          ))}
          <li className="bp-legend-shape"><span className="bp-shape-ellipse" /> lemma / theorem</li>
          <li className="bp-legend-shape"><span className="bp-shape-box" /> definition</li>
        </ul>

        {selectedNode ? (
          <NodeDetail node={selectedNode} onOpenSession={onOpenSession} />
        ) : (
          <div className="bp-detail-empty">Click a node to see its statement and the sessions that built it.</div>
        )}
      </div>
    </div>
  );
}

function NodeDetail({
  node,
  onOpenSession,
}: {
  node: GraphNode;
  onOpenSession: (sessionId: string) => void;
}) {
  return (
    <div className="bp-detail">
      <div className="bp-detail-head">
        <span className="bp-detail-key">{node.key}</span>
        <span className={`bp-detail-status ${statusClass(node)}`}>{statusLabel(node)}</span>
      </div>
      <div className="bp-detail-meta">
        {node.kind ?? 'node'}
        {node.lean && <> · <code>{node.lean}</code></>}
      </div>
      {node.status === 'proved' && (
        <p className={`bp-detail-audit ${node.verified ? 'is-audited' : 'is-pending'}`}>
          {node.verified
            ? 'SafeVerify-audited — kernel replay + axiom whitelist confirm this proof.'
            : 'Lean check passed (compiles, no sorry). SafeVerify audit pending — run Verify on the session to seal it.'}
        </p>
      )}
      {node.statement && <p className="bp-detail-stmt">{node.statement}</p>}

      <div className="bp-detail-sub">Worked on by</div>
      {node.sessions.length === 0 ? (
        <div className="bp-detail-empty">
          No sessions yet{node.status === 'planned' ? ' — this node is still planned.' : '.'}
        </div>
      ) : (
        <ul className="bp-detail-sessions">
          {node.sessions.map((s) => (
            <li key={s.session_id}>
              <button className="bp-session-link" onClick={() => onOpenSession(s.session_id)} title="Open this session">
                {s.title || 'session'}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
