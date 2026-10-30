import '../../../styles/blueprint.css';
import { Suspense, lazy, useCallback, useEffect, useMemo, useState } from 'react';
import { FileText, GitBranch, Plus, X } from 'lucide-react';
import { getProjectBlueprint, type BlueprintWarning, type GraphNode } from '../../lib/api';
import { openOrDraftBlueprint, startNodeSession } from '../../lib/blueprintActions';
import { STATUS_TEXT, headerChip, runningNodeKeys, summaryLine } from '../../lib/blueprintModel.js';
import { useProofSession } from '../../stores/proofSession';
import { useSessions } from '../../stores/sessions';
import { useWorkbench } from '../../stores/workbench';
import { MarkdownDoc } from '../MarkdownDoc';
import { AddNodeForm } from './AddNodeForm';
import type { Grouping } from './BlueprintGraph';
import { NodeDetailPanel } from './NodeDetailPanel';
import { ProposalPanel } from './ProposalPanel';
import { StatusDot, StatusPill } from './StatusDot';
import { useProjectGraph } from './useProjectGraph';

// Work package "blueprint" (mockup frames 8–9). The blueprint as a document in the
// canvas: Graph (By level / By file lanes) and Markdown views, the proposal state
// for a freshly drafted blueprint, and the node detail panel. Canvas mounts it
// instead of the editor when useWorkbench().openPath is BLUEPRINT_PATH.

// React Flow loads with the graph, not with the app.
const BlueprintGraph = lazy(() => import('./BlueprintGraph'));

type View = 'graph' | 'markdown';

const LEGEND: { tone: 'proved' | 'ready' | 'stated' | 'failed' | 'planned' | 'running'; text: string }[] = [
  { tone: 'proved', text: STATUS_TEXT.proved },
  { tone: 'ready', text: STATUS_TEXT.ready },
  { tone: 'stated', text: STATUS_TEXT.stated },
  { tone: 'failed', text: STATUS_TEXT.failed },
  { tone: 'planned', text: STATUS_TEXT.planned },
  { tone: 'running', text: STATUS_TEXT.running },
];

export function BlueprintView({
  projectId,
  onOpenSession,
}: {
  projectId: string;
  onOpenSession: (sessionId: string) => void;
}) {
  const [view, setView] = useState<View>('graph');
  const [grouping, setGrouping] = useState<Grouping>('level');
  const [adding, setAdding] = useState(false);
  const [proving, setProving] = useState<string | null>(null);
  const [doc, setDoc] = useState<{ content: string; warnings: BlueprintWarning[] }>({ content: '', warnings: [] });

  const sessions = useSessions((s) => s.sessions);
  const refreshSessions = useSessions((s) => s.refreshSessions);
  const selectedSessionId = useSessions((s) => s.selectedSessionId);
  const nodeBindings = useWorkbench((s) => s.nodeBindings);
  const selectedKey = useWorkbench((s) => s.selectedNodeKey);
  const selectNode = useWorkbench((s) => s.selectNode);
  const proposalAll = useWorkbench((s) => s.blueprintProposal);
  const setProposal = useWorkbench((s) => s.setBlueprintProposal);
  const refresh = useWorkbench((s) => s.blueprintRefresh);
  const bump = useWorkbench((s) => s.bumpBlueprintRefresh);
  const notice = useWorkbench((s) => s.blueprintNotice);
  const setNotice = useWorkbench((s) => s.setBlueprintNotice);
  const proposal = proposalAll?.projectId === projectId ? proposalAll : null;
  const drafting = proposal?.phase === 'drafting';

  // A live run in this project (or a drafting proposal) keeps the graph fresh.
  const projectLive = useMemo(
    () => sessions.some((s) => s.project_id === projectId && (s.active_run_count ?? 0) > 0),
    [sessions, projectId],
  );
  const { graph, error, loading, refetch } = useProjectGraph(projectId, { pollMs: drafting ? 3000 : projectLive ? 6000 : 0 });

  // The markdown behind the graph (for the proposal list and the warnings).
  const loadDoc = useCallback(() => {
    let cancelled = false;
    getProjectBlueprint(projectId)
      .then((b) => !cancelled && setDoc(b))
      .catch(() => !cancelled && setDoc({ content: '', warnings: [] }));
    return () => {
      cancelled = true;
    };
  }, [projectId]);
  useEffect(() => loadDoc(), [loadDoc, refresh]);
  useEffect(() => {
    if (!drafting) return;
    const id = window.setInterval(() => {
      loadDoc();
      void refreshSessions().catch(() => {});
    }, 3000);
    return () => window.clearInterval(id);
  }, [drafting, loadDoc, refreshSessions]);

  // Agent-drafted proposal: once the file has nodes and the drafting session has
  // settled, move from drafting to review.
  useEffect(() => {
    if (!proposal || proposal.phase !== 'drafting' || proposal.source !== 'agent') return;
    const nodes = graph?.nodes.length ?? 0;
    if (nodes === 0) return;
    const session = proposal.draftingSessionId ? sessions.find((s) => s.id === proposal.draftingSessionId) : undefined;
    const settled = !proposal.draftingSessionId || (session ? (session.active_run_count ?? 0) === 0 : false);
    if (settled) setProposal({ ...proposal, phase: 'review' });
  }, [proposal, graph, sessions, setProposal]);

  const runningKeys = useMemo(() => runningNodeKeys(graph?.nodes ?? [], sessions, nodeBindings), [graph, sessions, nodeBindings]);
  const nodes = graph?.nodes ?? [];
  const selectedNode: GraphNode | undefined = selectedKey ? nodes.find((n) => n.key === selectedKey) : undefined;
  const chip = headerChip(nodes, { proposal: !!proposal });

  const prove = async (node: GraphNode) => {
    if (!graph) return;
    setProving(node.key);
    try {
      const started = await startNodeSession(projectId, node, graph);
      setNotice(`Started a session bound to ${node.key}.`);
      onOpenSession(started.sessionId);
    } catch (err) {
      useProofSession.getState().setError(err instanceof Error ? err.message : String(err));
    } finally {
      setProving(null);
    }
  };

  const draft = async () => {
    try {
      await openOrDraftBlueprint(projectId, { sessionId: selectedSessionId });
    } catch (err) {
      useProofSession.getState().setError(err instanceof Error ? err.message : String(err));
    }
  };

  const onSaved = useCallback(() => {
    bump();
    void refetch();
  }, [bump, refetch]);

  const empty = !loading && !error && nodes.length === 0 && !proposal;
  const showAside = view === 'graph';

  return (
    <section className="blueprint-view" aria-label="Blueprint">
      <div className="bpv-bar">
        <span className="bpv-seg" role="group" aria-label="View">
          <button className={view === 'graph' ? 'on' : ''} onClick={() => setView('graph')}>
            <GitBranch size={12} /> Graph
          </button>
          <button className={view === 'markdown' ? 'on' : ''} onClick={() => setView('markdown')}>
            <FileText size={12} /> Markdown
          </button>
        </span>
        <span className="bpv-seg" role="group" aria-label="Group nodes">
          <button className={grouping === 'level' ? 'on' : ''} onClick={() => setGrouping('level')} disabled={view !== 'graph'}>
            By level
          </button>
          <button className={grouping === 'file' ? 'on' : ''} onClick={() => setGrouping('file')} disabled={view !== 'graph'}>
            By file
          </button>
        </span>
        <span className="bpv-spacer" />
        <StatusPill tone={chip.tone} className="bpv-chip">
          {chip.text}
        </StatusPill>
        <button className="bpv-btn is-small" onClick={() => setAdding((v) => !v)} disabled={!!proposal} title="Append a node to the blueprint">
          <Plus size={13} /> Add node
        </button>
      </div>

      {notice && (
        <div className="bpv-notice" role="status">
          <span>{notice}</span>
          <button className="bpv-icon" aria-label="Dismiss" onClick={() => setNotice(null)}>
            <X size={13} />
          </button>
        </div>
      )}
      {doc.warnings.length > 0 && !proposal && (
        <div className="bpv-warnings">
          {doc.warnings.length} blueprint {doc.warnings.length === 1 ? 'warning' : 'warnings'}: {doc.warnings.map((w) => w.message).join(' · ')}
        </div>
      )}

      {view === 'markdown' ? (
        <div className="bpv-markdown">
          <MarkdownDoc
            projectId={projectId}
            doc="blueprint"
            title="Blueprint"
            agentWritten
            refreshSignal={refresh}
            onSaved={onSaved}
            emptyHint="No blueprint yet. Add a `## node` section with a kind, an optional lean: decl and uses: dependencies — the graph builds itself from it."
          />
        </div>
      ) : (
        <div className="bpv-body">
          <div className="bpv-canvas">
            {loading && <div className="bpv-msg">Loading graph…</div>}
            {error && <div className="bpv-msg is-error">{error}</div>}
            {empty && (
              <div className="bpv-empty-state">
                <div className="bpv-empty-title">No blueprint yet</div>
                <p>
                  A blueprint decomposes the goal into nodes — definitions, lemmas, the theorem on top — each bound to a file and a
                  session. Draft one before proving.
                </p>
                <div className="bpv-empty-actions">
                  <button className="bpv-btn is-primary" onClick={draft}>
                    Draft with /blueprint
                  </button>
                  <button className="bpv-btn" onClick={() => setAdding(true)}>
                    <Plus size={13} /> Add node
                  </button>
                  <button className="bpv-btn" onClick={() => setView('markdown')}>
                    Write markdown
                  </button>
                </div>
              </div>
            )}
            {graph && nodes.length > 0 && (
              <Suspense fallback={<div className="bpv-msg">Loading graph…</div>}>
                <BlueprintGraph
                  graph={graph}
                  grouping={grouping}
                  selectedKey={selectedKey}
                  runningKeys={runningKeys}
                  proposal={!!proposal}
                  onSelect={selectNode}
                />
              </Suspense>
            )}
            {graph && nodes.length > 0 && (
              <ul className="bpv-legend" aria-label="Legend">
                {LEGEND.map((l) => (
                  <li key={l.tone}>
                    <StatusDot tone={l.tone} /> {l.text}
                  </li>
                ))}
              </ul>
            )}
          </div>

          {showAside && proposal && (
            <ProposalPanel projectId={projectId} proposal={proposal} markdown={doc.content} graph={graph} onEditMarkdown={() => setView('markdown')} />
          )}
          {showAside && !proposal && adding && <AddNodeForm projectId={projectId} graph={graph} onClose={() => setAdding(false)} />}
          {showAside && !proposal && !adding && selectedNode && graph && (
            <aside className="bpv-aside" aria-label="Node details">
              <NodeDetailPanel
                projectId={projectId}
                node={selectedNode}
                graph={graph}
                sessions={sessions}
                nodeBindings={nodeBindings}
                running={runningKeys.has(selectedNode.key)}
                proving={proving === selectedNode.key}
                onOpenSession={onOpenSession}
                onProve={prove}
              />
            </aside>
          )}
          {showAside && !proposal && !adding && !selectedNode && nodes.length > 0 && (
            <aside className="bpv-aside is-hint" aria-label="Node details">
              <div className="bpv-aside-body">
                <p className="bpv-note">Select a node to see its statement, file, dependencies and sessions.</p>
              </div>
            </aside>
          )}
        </div>
      )}

      <div className="bpv-foot">
        {proposal ? (
          <span>Dashed nodes exist only in this proposal. Accepting writes .lea/blueprint.md; discarding empties it again.</span>
        ) : (
          <span>{summaryLine(nodes, { running: runningKeys.size })}</span>
        )}
      </div>
    </section>
  );
}
