import { useEffect, useState } from 'react';
import { ArrowRight, MessageSquare } from 'lucide-react';
import { getProjectFile, type GraphNode, type ProjectGraph, type SessionSummary } from '../../lib/api';
import {
  byKey,
  dependents,
  findDeclLine,
  isReady,
  nodeSessions,
  nodeStatusText,
  otherBlockers,
  relativeTime,
} from '../../lib/blueprintModel.js';
import { useWorkbench } from '../../stores/workbench';
import { StatusDot, StatusPill, toneOf } from './StatusDot';

// The node detail panel (mockup frame 9): statement, kind, lean name, file:line as a
// link into the canvas, checked-at, Uses and Unblocks with live status, Sessions
// (running first, newest first) with an Open link, and the actions Open running
// session / Ask Lea / Prove.

const lineCache = new Map<string, number | null>();

function useDeclLine(projectId: string, file: string | null, lean: string | null): number | null | undefined {
  const [line, setLine] = useState<number | null | undefined>(undefined);
  useEffect(() => {
    if (!file || !lean) {
      setLine(null);
      return;
    }
    const cacheKey = `${projectId}:${file}:${lean}`;
    if (lineCache.has(cacheKey)) {
      setLine(lineCache.get(cacheKey));
      return;
    }
    let cancelled = false;
    setLine(undefined);
    getProjectFile(projectId, file)
      .then((f) => {
        const found = findDeclLine(f.content, lean);
        lineCache.set(cacheKey, found);
        if (!cancelled) setLine(found);
      })
      .catch(() => !cancelled && setLine(null));
    return () => {
      cancelled = true;
    };
  }, [projectId, file, lean]);
  return line;
}

export function NodeDetailPanel({
  projectId,
  node,
  graph,
  sessions,
  nodeBindings,
  running,
  proving,
  onOpenSession,
  onProve,
}: {
  projectId: string;
  node: GraphNode;
  graph: ProjectGraph;
  sessions: SessionSummary[];
  nodeBindings: Record<string, string>;
  running: boolean;
  proving: boolean;
  onOpenSession: (sessionId: string) => void;
  onProve: (node: GraphNode) => void;
}) {
  const index = byKey(graph.nodes);
  const selectNode = useWorkbench((s) => s.selectNode);
  const openFile = useWorkbench((s) => s.openFile);
  const setPendingSubmission = useWorkbench((s) => s.setPendingSubmission);
  const rows = nodeSessions(node, sessions, nodeBindings);
  const runningRow = rows.find((r) => r.running);
  const line = useDeclLine(projectId, node.file, node.lean);
  const unblocks = dependents(graph.nodes, node.key);
  const lastTouched = node.sessions[0]?.last_at;
  const canProve = !running && node.status !== 'proved';
  const ready = isReady(node, index);
  // The system assigned a name the repo does not declare yet (graph `declared === false`).
  const undeclared = node.declared === false && !!node.lean;
  const plannedFile = node.planned_file ?? null;

  const askLea = () => {
    const statement = node.statement ? ` — ${node.statement.trim().split('\n')[0]}` : '';
    setPendingSubmission({ text: `About the blueprint node \`${node.key}\`${statement}: `, mode: 'draft' });
  };

  return (
    <div className="bpv-detail">
      <div className="bpv-detail-body">
        <div className="bpv-detail-head">
          <span className="bpv-mono bpv-detail-key">{node.key}</span>
          <StatusPill tone={toneOf(node)}>{nodeStatusText(node)}</StatusPill>
          {running && <StatusPill tone="running">session running</StatusPill>}
        </div>
        {node.statement ? <p className="bpv-detail-stmt">{node.statement}</p> : <p className="bpv-detail-stmt is-muted">No statement yet.</p>}
        {undeclared && (
          <p className="bpv-note bpv-declare-note">
            <span>
              to declare as <code>{node.lean}</code>
              {plannedFile ? (
                <>
                  {' '}
                  in <code>{plannedFile}</code>
                </>
              ) : null}
            </span>
          </p>
        )}

        <dl className="bpv-kv">
          <dt>kind</dt>
          <dd>{node.kind ?? '—'}</dd>
          <dt>lean</dt>
          <dd className="bpv-mono">
            {node.lean ?? <span className="is-muted">not named yet</span>}
            {undeclared && <span className="is-muted"> · not declared yet</span>}
          </dd>
          <dt>file</dt>
          <dd>
            {node.file ? (
              <button className="bpv-link bpv-mono" onClick={() => openFile(node.file)} title="Open this file in the canvas">
                {node.file}
                {typeof line === 'number' ? `:${line}` : ''}
                <ArrowRight size={12} />
              </button>
            ) : plannedFile ? (
              <span className="bpv-mono">
                {plannedFile} <span className="is-muted">· planned</span>
              </span>
            ) : (
              <span className="is-muted">no file yet</span>
            )}
          </dd>
          <dt>checked</dt>
          <dd>
            {node.status === 'planned' ? (
              <span className="is-muted">not checked yet</span>
            ) : lastTouched ? (
              <>
                last edit {relativeTime(lastTouched)}
                {node.status === 'failed' && <> · <span className="bpv-ink-failed">check failed</span></>}
                {node.status === 'proved' && <> · {node.verified ? 'verified by SafeVerify' : 'checked, audit pending'}</>}
              </>
            ) : (
              <span className="is-muted">{nodeStatusText(node)}</span>
            )}
          </dd>
        </dl>

        <div className="bpv-group">
          <div className="bpv-grp">Uses</div>
          {node.uses.length === 0 ? (
            <div className="bpv-empty">Nothing — this node is a leaf.</div>
          ) : (
            <ul className="bpv-list">
              {node.uses.map((dep) => {
                const d = index.get(dep);
                return (
                  <li key={dep}>
                    <StatusDot tone={d ? toneOf(d) : 'planned'} />
                    <button className="bpv-link bpv-mono" onClick={() => d && selectNode(dep)} disabled={!d}>
                      {dep}
                    </button>
                    <span className="bpv-list-note">
                      {d ? nodeStatusText(d) : 'not in the blueprint'}
                      {d && (d.status === 'stated' || d.status === 'ready' || d.status === 'failed') ? ' · used as hypothesis' : ''}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <div className="bpv-group">
          <div className="bpv-grp">Unblocks</div>
          {unblocks.length === 0 ? (
            <div className="bpv-empty">Nothing depends on this node.</div>
          ) : (
            <ul className="bpv-list">
              {unblocks.map((u) => {
                const others = otherBlockers(u, node.key, index);
                return (
                  <li key={u.key}>
                    <StatusDot tone={toneOf(u)} />
                    <button className="bpv-link bpv-mono" onClick={() => selectNode(u.key)}>
                      {u.key}
                    </button>
                    <span className="bpv-list-note">
                      {nodeStatusText(u)}
                      {others.length > 0 ? ` · also waits on ${others.join(', ')}` : ''}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <div className="bpv-group">
          <div className="bpv-grp">Sessions</div>
          {rows.length === 0 ? (
            <div className="bpv-empty">No session has worked on this node yet.</div>
          ) : (
            <div className="bpv-sessions">
              {rows.map((r) => (
                <div key={r.session_id} className={`bpv-sess${r.running ? ' is-running' : ''}`}>
                  <StatusDot tone={r.running ? 'running' : 'planned'} />
                  <span className="bpv-sess-title">{r.title}</span>
                  <span className="bpv-sess-note">
                    {r.running ? 'running' : r.last_at ? `last edit ${relativeTime(r.last_at)}` : r.bound ? 'bound' : ''}
                  </span>
                  <button className="bpv-link bpv-sess-open" onClick={() => onOpenSession(r.session_id)}>
                    Open
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="bpv-detail-foot">
        {runningRow ? (
          <button className="bpv-btn is-primary" onClick={() => onOpenSession(runningRow.session_id)}>
            <MessageSquare size={13} /> Open running session
          </button>
        ) : (
          <button
            className="bpv-btn is-primary"
            onClick={() => onProve(node)}
            disabled={!canProve || proving}
            title={
              node.status === 'proved'
                ? 'Already proved'
                : ready
                  ? 'Start a session bound to this node'
                  : 'Start a session bound to this node; unproved dependencies are used as hypotheses'
            }
          >
            {proving ? 'Starting…' : 'Prove'}
          </button>
        )}
        <button className="bpv-btn" onClick={askLea} title="Prefill the composer with a question about this node">
          Ask Lea
        </button>
        {runningRow && (
          <button className="bpv-btn" disabled title="A session is already proving this node">
            Prove
          </button>
        )}
      </div>
    </div>
  );
}
