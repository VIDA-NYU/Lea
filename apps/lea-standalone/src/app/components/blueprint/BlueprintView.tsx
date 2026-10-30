import '../../../styles/blueprint.css';
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { FileText, GitBranch, Plus, X } from 'lucide-react';
import { getProjectBlueprint, type GraphNode, type ProjectBlueprint, type ProjectGraph } from '../../lib/api';
import {
  openOrDraftBlueprint,
  readySessionsNotice,
  startNodeSession,
  startReadySessions,
  stopBlueprintAuto,
} from '../../lib/blueprintActions';
import {
  STATUS_TEXT,
  draftGraph,
  draftingOutcome,
  headerChip,
  readyNodes,
  runningNodeKeys,
  summaryLine,
  unlockNoticeText,
} from '../../lib/blueprintModel.js';
import type { ExportProject } from '../../lib/blueprintExport.js';
import { useProjects } from '../../stores/projects';
import { useProofSession } from '../../stores/proofSession';
import { useSessions } from '../../stores/sessions';
import { useWorkbench } from '../../stores/workbench';
import { MarkdownDoc } from '../MarkdownDoc';
import { AddNodeForm } from './AddNodeForm';
import type { BlueprintGraphHandle, Grouping } from './BlueprintGraph';
import { DownloadMenu } from './DownloadMenu';
import { DraftEditor } from './DraftEditor';
import { NodeDetailPanel } from './NodeDetailPanel';
import { ProposalPanel } from './ProposalPanel';
import { StatusDot, StatusPill } from './StatusDot';
import { useProjectGraph } from './useProjectGraph';

// Work package "blueprint" (mockup frames 8–9). The blueprint as a document in the
// canvas: Graph (By level / By file lanes) and Markdown views, the proposal state
// for a freshly drafted blueprint, the node detail panel and the Download menu.
// Canvas mounts it instead of the editor when useWorkbench().openPath is
// BLUEPRINT_PATH.
//
// Blueprint tools (docs/PLAN-blueprint-tools.md): a pending draft
// (.lea/blueprint.draft.md, GET /blueprint → `draft`) is a state — the view opens
// its review and draws the draft's nodes; the notice bar carries the latest
// unlock with "Prove next (n)" and the `/blueprint auto` status.

// React Flow (and html-to-image, which the graph imports on demand) load with the
// graph, not with the app.
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

const EMPTY_DOC: ProjectBlueprint = { content: '', warnings: [], nodes: [], draft: null };

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
  const [startingNext, setStartingNext] = useState(false);
  // The GET /blueprint payload and when its request started (the drafting settle
  // logic only trusts a read made after the drafting run ended).
  const [doc, setDoc] = useState<{ value: ProjectBlueprint; requestedAt: number }>({ value: EMPTY_DOC, requestedAt: 0 });

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
  const unlockAll = useWorkbench((s) => s.blueprintUnlock);
  const autoAll = useWorkbench((s) => s.blueprintAuto);
  const proposal = proposalAll?.projectId === projectId ? proposalAll : null;
  const unlock = unlockAll && (!unlockAll.projectId || unlockAll.projectId === projectId) ? unlockAll : null;
  const auto = autoAll?.projectId === projectId && autoAll.active ? autoAll : null;
  const drafting = proposal?.phase === 'drafting';
  const draftRunStatus = useProofSession((s) => (proposal?.draftingRunId ? s.runStatusById[proposal.draftingRunId] : undefined));
  const projectMeta = useProjects((s) =>
    s.currentProject?.id === projectId ? s.currentProject : s.projects.find((p) => p.id === projectId),
  );
  const exportProject: ExportProject = useMemo(
    () =>
      projectMeta
        ? { id: projectId, slug: projectMeta.slug, title: projectMeta.title, namespace: projectMeta.namespace }
        : { id: projectId },
    [projectId, projectMeta],
  );
  const graphHandle = useRef<BlueprintGraphHandle | null>(null);

  // A live run in this project (or a drafting proposal) keeps the graph fresh.
  const projectLive = useMemo(
    () => sessions.some((s) => s.project_id === projectId && (s.active_run_count ?? 0) > 0),
    [sessions, projectId],
  );
  const { graph, error, loading, refetch } = useProjectGraph(projectId, { pollMs: drafting ? 3000 : projectLive ? 6000 : 0 });

  // The markdown behind the graph: live content and warnings, the parsed nodes and the draft.
  const loadDoc = useCallback(() => {
    let cancelled = false;
    const requestedAt = Date.now();
    getProjectBlueprint(projectId)
      .then((value) => !cancelled && setDoc({ value, requestedAt }))
      .catch(() => !cancelled && setDoc({ value: EMPTY_DOC, requestedAt }));
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

  const draft = doc.value.draft;
  const draftNodes = draft?.nodes.length ?? 0;

  // A draft that shows up with no proposal open (opened later, a reload, a drafting
  // run started elsewhere) opens its review; a review whose draft is gone (accepted
  // or discarded elsewhere) closes. Only a read requested after the proposal last
  // changed counts, so a read in flight while Accept/Discard ran cannot reopen it.
  const proposalRef = useRef(proposal);
  proposalRef.current = proposal;
  const proposalChangedAt = useRef(0);
  const previousProposal = useRef(proposal);
  useEffect(() => {
    if (previousProposal.current === proposal) return; // mount: the first read counts
    previousProposal.current = proposal;
    proposalChangedAt.current = Date.now();
  }, [proposal]);
  useEffect(() => {
    if (doc.requestedAt <= proposalChangedAt.current) return;
    const current = proposalRef.current;
    const pending = doc.value.draft;
    if (!current && pending && pending.nodes.length > 0) {
      setProposal({ projectId, phase: 'review', source: 'agent' });
    } else if (current && current.source === 'agent' && current.phase === 'review' && !pending) {
      setProposal(null);
    }
  }, [doc, projectId, setProposal]);

  // Agent drafting: review once the drafting run has ended and the draft has nodes;
  // an ended run with no draft nodes is reported instead of spinning forever.
  const seenRunning = useRef(false);
  const endedAt = useRef<number | null>(null);
  useEffect(() => {
    seenRunning.current = false;
    endedAt.current = null;
  }, [proposal?.draftingRunId, proposal?.draftingSessionId]);
  useEffect(() => {
    if (!proposal || proposal.phase !== 'drafting' || proposal.source !== 'agent' || proposal.error) return;
    const session = proposal.draftingSessionId ? sessions.find((s) => s.id === proposal.draftingSessionId) : undefined;
    const running = (session?.active_run_count ?? 0) > 0;
    if (running) {
      seenRunning.current = true;
      endedAt.current = null;
    } else if ((seenRunning.current || (draftRunStatus && draftRunStatus !== 'running' && draftRunStatus !== 'pending')) && endedAt.current === null) {
      endedAt.current = Date.now();
      loadDoc();
    }
    const outcome = draftingOutcome({
      running,
      seenRunning: seenRunning.current,
      runStatus: draftRunStatus,
      draftNodes,
      readAfterEnd: endedAt.current !== null && doc.requestedAt >= endedAt.current,
    });
    if (outcome === 'review') setProposal({ ...proposal, phase: 'review' });
    else if (outcome === 'empty') {
      setProposal({ ...proposal, error: 'The drafting run ended without drafting any nodes. Retry, or add nodes by hand.' });
    }
  }, [proposal, doc, draftNodes, sessions, draftRunStatus, setProposal, loadDoc]);

  const draftShown = !!proposal && proposal.source === 'agent' && draftNodes > 0;
  const shownGraph: ProjectGraph | null = useMemo(
    () => (draftShown && draft ? (draftGraph(draft.nodes) as ProjectGraph) : graph),
    [draftShown, draft, graph],
  );
  const runningKeys = useMemo(() => runningNodeKeys(graph?.nodes ?? [], sessions, nodeBindings), [graph, sessions, nodeBindings]);
  const nodes = shownGraph?.nodes ?? [];
  const liveNodes = graph?.nodes ?? [];
  const selectedNode: GraphNode | undefined = selectedKey && !draftShown ? liveNodes.find((n) => n.key === selectedKey) : undefined;
  const chip = headerChip(nodes, { proposal: !!proposal });
  const startable = useMemo(() => readyNodes(liveNodes).filter((n) => !runningKeys.has(n.key)).length, [liveNodes, runningKeys]);
  const unlockNotice = unlock ? unlockNoticeText(unlock.cause, unlock.keys) : null;
  const showProveNext = !!unlock && notice === unlockNotice && !proposal;
  const proveNextCount = startable || unlock?.keys.length || 0;

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

  const proveNext = async () => {
    setStartingNext(true);
    try {
      const r = await startReadySessions(projectId);
      setNotice(readySessionsNotice(r));
      if (r.started.length === 1) onOpenSession(r.started[0].sessionId);
    } catch (err) {
      useProofSession.getState().setError(err instanceof Error ? err.message : String(err));
    } finally {
      setStartingNext(false);
    }
  };

  const startDraft = async () => {
    try {
      await openOrDraftBlueprint(projectId, { sessionId: selectedSessionId, openSession: onOpenSession });
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
  const graphShown = view === 'graph' && !!shownGraph && nodes.length > 0;
  const editingDraft = view === 'markdown' && !!proposal && proposal.source === 'agent' && !!draft;

  const renderPng = useCallback(() => {
    const handle = graphHandle.current;
    return handle ? handle.toPng() : Promise.reject(new Error('Open the Graph view to export an image.'));
  }, []);
  const reportError = useCallback((message: string) => useProofSession.getState().setError(message), []);

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
        <button className="bpv-btn is-small" onClick={() => setAdding((v) => !v)} disabled={!!proposal} title="Add a node to the blueprint">
          <Plus size={13} /> Add node
        </button>
        <DownloadMenu
          projectId={projectId}
          project={exportProject}
          graph={graph}
          hasMarkdown={doc.value.content.trim().length > 0 || liveNodes.length > 0}
          pngReason={graphShown ? null : view !== 'graph' ? 'Switch to the Graph view to export an image' : 'The graph has no nodes'}
          renderPng={renderPng}
          onError={reportError}
        />
      </div>

      {notice && (
        <div className="bpv-notice" role="status">
          <span>{notice}</span>
          {showProveNext && proveNextCount > 0 && (
            <button
              className="bpv-btn is-small is-primary"
              onClick={() => void proveNext()}
              disabled={startingNext || !!auto}
              title={auto ? '/blueprint auto is starting sessions' : 'Start a session for every ready node'}
            >
              {startingNext ? 'Starting…' : `Prove next (${proveNextCount})`}
            </button>
          )}
          <button className="bpv-icon" aria-label="Dismiss" onClick={() => setNotice(null)}>
            <X size={13} />
          </button>
        </div>
      )}
      {auto && (
        <div className="bpv-notice is-auto" role="status">
          <span>
            Auto is on: {auto.started.length} of {auto.cap} sessions started; it starts each node as it becomes ready.
          </span>
          <button className="bpv-btn is-small" onClick={() => stopBlueprintAuto()}>
            Stop auto
          </button>
        </div>
      )}
      {doc.value.warnings.length > 0 && !proposal && (
        <div className="bpv-warnings">
          {doc.value.warnings.length} blueprint {doc.value.warnings.length === 1 ? 'warning' : 'warnings'}:{' '}
          {doc.value.warnings.map((w) => w.message).join(' · ')}
        </div>
      )}

      {view === 'markdown' ? (
        <div className="bpv-markdown">
          {editingDraft && draft ? (
            <DraftEditor
              projectId={projectId}
              draft={draft}
              onSaved={() => {
                bump();
                loadDoc();
              }}
              onClose={() => setView('graph')}
            />
          ) : (
            <MarkdownDoc
              projectId={projectId}
              doc="blueprint"
              title="Blueprint"
              agentWritten
              refreshSignal={refresh}
              onSaved={onSaved}
              emptyHint="No blueprint yet. Add a `## node` section with a kind, an optional lean: decl and uses: dependencies — the graph builds itself from it."
            />
          )}
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
                  <button className="bpv-btn is-primary" onClick={startDraft}>
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
            {shownGraph && nodes.length > 0 && (
              <Suspense fallback={<div className="bpv-msg">Loading graph…</div>}>
                <BlueprintGraph
                  graph={shownGraph}
                  grouping={grouping}
                  selectedKey={draftShown ? null : selectedKey}
                  runningKeys={draftShown ? new Set<string>() : runningKeys}
                  proposal={!!proposal}
                  onSelect={selectNode}
                  handleRef={graphHandle}
                />
              </Suspense>
            )}
            {shownGraph && nodes.length > 0 && (
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
            <ProposalPanel
              projectId={projectId}
              proposal={proposal}
              doc={doc.value}
              graph={graph}
              onEditMarkdown={() => setView('markdown')}
              onOpenSession={onOpenSession}
            />
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
          proposal.source === 'agent' ? (
            <span>
              Dashed nodes exist only in the draft (.lea/blueprint.draft.md). Accepting merges the checked nodes into
              .lea/blueprint.md; discarding deletes the draft.
            </span>
          ) : (
            <span>Dashed nodes exist only in this proposal. Accepting writes .lea/blueprint.md; discarding empties it again.</span>
          )
        ) : (
          <span>{summaryLine(nodes, { running: runningKeys.size })}</span>
        )}
      </div>
    </section>
  );
}
