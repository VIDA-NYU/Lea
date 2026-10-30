import '../../../styles/shell.css';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowRight,
  Download,
  ExternalLink,
  Loader2,
  MessageSquare,
  MoreHorizontal,
  RefreshCw,
  Workflow,
} from 'lucide-react';
import {
  downloadWorkspaceExport,
  getProjectDoc,
  projectExportUrl,
  pushProject,
  setProjectRemote,
  type GraphNode,
  type SessionSummary,
} from '../../lib/api';
import { openOverleafDocument } from '../../lib/overleafLink';
import { useProjects } from '../../stores/projects';
import { useSessions } from '../../stores/sessions';
import { useProofSession } from '../../stores/proofSession';
import { useWorkbench } from '../../stores/workbench';
import { effectiveNodeBindings } from '../../lib/blueprintModel.js';
import {
  blueprintTally,
  countNotes,
  docExcerpt,
  fileStatuses,
  flattenTree,
  relativeTime,
  sessionNodeMap,
  sessionState,
  sessionStateLabel,
  upNext,
} from '../../lib/shellModel.js';
import { useProjectData, useProjectGraph, useProjectTree } from './projectData';
import {
  BlueprintBar,
  BlueprintLegend,
  ExpandSidebarButton,
  Pill,
  RunningPill,
  SectionHead,
  StatusDot,
  fileDotKind,
  nodeDotKind,
  sessionDotKind,
} from './bits';

export type ProveMode = 'prove' | 'prove-sorry' | 'retry';

const INSTRUCTIONS_PATH = '.lea/instructions.md';
const MEMORY_PATH = '.lea/memory.md';
const SESSIONS_PREVIEW = 5;
const FILES_PREVIEW = 8;

const FILE_STATUS_LABEL: Record<string, string> = {
  error: 'error',
  sorry: 'sorry',
  ready: 'ready',
  planned: 'stub',
  proved: 'proved',
};
const FILE_STATUS_ORDER: Record<string, number> = { error: 0, sorry: 1, ready: 2, planned: 3, proved: 4 };

// Work package "shell" (mockup frame 12). The project home: description and
// progress bar, "Up next" (ready and sorry nodes with Prove), sessions with the
// node each owns, files, Overleaf, git, instructions, memory. App renders it for
// view === 'project-home'.
export function ProjectHome({
  projectId,
  onOpenSession,
  onNewSession,
  onOpenBlueprint,
  onOpenFiles,
  onProveNode,
  onOpenLegacy,
  onOpenHome,
  sidebarCollapsed,
  onExpandSidebar,
}: {
  projectId: string;
  onOpenSession: (sessionId: string) => void;
  onNewSession: () => void;
  /** Open the blueprint in the canvas (files store: openFile(BLUEPRINT_PATH)). */
  onOpenBlueprint: () => void;
  /** Open a project file in the canvas with the rail shown. */
  onOpenFiles: (path?: string) => void;
  /** Start a session bound to a node whose first message names it (App implements). */
  onProveNode?: (node: GraphNode, mode: ProveMode) => void | Promise<void>;
  /** The pre-redesign project window, kept reachable from the ⋯ menu. */
  onOpenLegacy?: () => void;
  onOpenHome?: () => void;
  sidebarCollapsed?: boolean;
  onExpandSidebar?: () => void;
}) {
  const project = useProjects((s) => (s.currentProject?.id === projectId ? s.currentProject : undefined));
  const projectError = useProjects((s) => s.projectError);
  const allSessions = useSessions((s) => s.sessions);
  const selectedSessionId = useSessions((s) => s.selectedSessionId);
  const isRunning = useProofSession((s) => s.isRunning);
  const refreshToken = useProofSession((s) => s.formalizationRefreshToken);
  const nodeBindings = useWorkbench((s) => s.nodeBindings);
  const blueprintUnlock = useWorkbench((s) => s.blueprintUnlock);
  const graph = useProjectGraph(projectId);
  const tree = useProjectTree(projectId);
  const loadGraph = useProjectData((s) => s.loadGraph);
  const loadTree = useProjectData((s) => s.loadTree);
  const runningSessionId = isRunning ? selectedSessionId : undefined;

  const [instructions, setInstructions] = useState<string | null>(null);
  const [memory, setMemory] = useState<string | null>(null);
  const [showAllSessions, setShowAllSessions] = useState(false);
  const [busyNode, setBusyNode] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const gitRef = useRef<HTMLDivElement>(null);
  const [remoteEditing, setRemoteEditing] = useState(false);

  useEffect(() => {
    void loadGraph(projectId);
    void loadTree(projectId);
  }, [projectId, refreshToken, loadGraph, loadTree]);

  useEffect(() => {
    let cancelled = false;
    getProjectDoc(projectId, 'instructions')
      .then((text) => !cancelled && setInstructions(text))
      .catch(() => !cancelled && setInstructions(''));
    getProjectDoc(projectId, 'memory')
      .then((text) => !cancelled && setMemory(text))
      .catch(() => !cancelled && setMemory(''));
    return () => {
      cancelled = true;
    };
  }, [projectId, refreshToken]);

  useEffect(() => {
    if (!menuOpen) return undefined;
    const onDown = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [menuOpen]);

  // Root sessions of the project: the live list first (it carries
  // active_run_count from the feed), the project payload for anything it lacks.
  const sessions = useMemo(() => {
    const byId = new Map<string, SessionSummary>();
    for (const s of project?.sessions ?? []) if (!s.parent_id) byId.set(s.id, s);
    for (const s of allSessions) if (s.project_id === projectId && !s.parent_id) byId.set(s.id, s);
    return [...byId.values()].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
  }, [project, allSessions, projectId]);

  const nodes = graph?.nodes ?? [];
  const tally = useMemo(() => blueprintTally(nodes), [nodes]);
  // Up next: a session's node is its `blueprint_node`, else the UI binding (older sessions).
  const nodeBySession = useMemo(
    () => sessionNodeMap(graph, effectiveNodeBindings(sessions, nodeBindings)),
    [graph, sessions, nodeBindings],
  );
  const sessionsByNode = useMemo(() => {
    const map: Record<string, Array<{ id: string; title: string; state: string }>> = {};
    for (const s of sessions) {
      const key = nodeBySession[s.id];
      if (!key) continue;
      (map[key] ??= []).push({ id: s.id, title: s.title, state: sessionState(s, runningSessionId) });
    }
    return map;
  }, [sessions, nodeBySession, runningSessionId]);
  // The keys of the latest `blueprint_unlocked` this page saw for the project get a badge.
  const unlockedKeys = blueprintUnlock && (!blueprintUnlock.projectId || blueprintUnlock.projectId === projectId) ? blueprintUnlock.keys : null;
  const nextItems = useMemo(() => upNext(nodes, sessionsByNode, 6, unlockedKeys), [nodes, sessionsByNode, unlockedKeys]);
  const runningCount = sessions.filter((s) => sessionState(s, runningSessionId) === 'running').length;

  const files = useMemo(() => {
    const statuses = fileStatuses(nodes);
    return flattenTree(tree ?? [], { leanOnly: true })
      .map((path: string) => ({ path, status: statuses[path] as string | undefined }))
      .sort((a, b) => (FILE_STATUS_ORDER[a.status ?? ''] ?? 9) - (FILE_STATUS_ORDER[b.status ?? ''] ?? 9));
  }, [tree, nodes]);

  const overleafSessions = sessions.filter((s) => s.origin === 'overleaf');
  const overleafUrl = overleafSessions.find((s) => s.origin_url)?.origin_url ?? null;
  const overleafLinked = Boolean(project?.overleaf_linked) || overleafSessions.length > 0;

  const prove = async (node: GraphNode, mode: ProveMode) => {
    if (!onProveNode || busyNode) return;
    setBusyNode(node.key);
    setActionError(null);
    try {
      await onProveNode(node, mode);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyNode(null);
    }
  };

  const exportZip = () => {
    downloadWorkspaceExport(projectExportUrl(projectId)).catch((err) =>
      setActionError(err instanceof Error ? err.message : String(err)),
    );
  };

  const share = () => {
    setRemoteEditing(!project?.remote_url);
    gitRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  };

  if (!project) {
    return (
      <section className="project-home sh-page" aria-label="Project overview">
        <header className="sh-topbar">
          <ExpandSidebarButton collapsed={sidebarCollapsed} onExpand={onExpandSidebar} />
          <span className="sh-topbar-title">{projectError ? 'Project unavailable' : 'Loading project…'}</span>
          <span className="sh-spacer" />
          {onOpenHome && (
            <button className="sh-btn" onClick={onOpenHome}>Back to projects</button>
          )}
        </header>
        {projectError && <div className="sh-error" style={{ margin: 20 }}>{projectError}</div>}
      </section>
    );
  }

  const visibleSessions = showAllSessions ? sessions : sessions.slice(0, SESSIONS_PREVIEW);
  const instructionsExcerpt = docExcerpt(instructions ?? '');
  const memoryExcerpt = docExcerpt(memory ?? '');
  const memoryNotes = countNotes(memory ?? '');

  return (
    <section className="project-home sh-page" aria-label="Project overview">
      <header className="sh-topbar">
        <ExpandSidebarButton collapsed={sidebarCollapsed} onExpand={onExpandSidebar} />
        <span className="sh-topbar-title">{project.title}</span>
        <span className="sh-mono sh-topbar-mono" title={project.repo_path}>
          {project.namespace}
          {project.repo_path && ` · ${shortRepoPath(project.repo_path)}`}
        </span>
        {runningCount > 0 && <RunningPill label={`${runningCount} session${runningCount === 1 ? '' : 's'} running`} />}
        <span className="sh-spacer" />
        <button className="sh-btn sh-btn-pri" onClick={onNewSession}>
          <MessageSquare size={13} /> New session
        </button>
        <button className="sh-btn" onClick={onOpenBlueprint}>
          <Workflow size={14} /> Blueprint
        </button>
        <button className="sh-btn" onClick={share}>Share</button>
        <div className="sh-menu-wrap" ref={menuRef}>
          <button
            className="sh-ibtn"
            aria-label="More"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((v) => !v)}
          >
            <MoreHorizontal size={16} />
          </button>
          {menuOpen && (
            <div className="sh-menu" role="menu">
              <button role="menuitem" onClick={() => { setMenuOpen(false); exportZip(); }}>
                <Download size={13} /> Export zip
              </button>
              <button role="menuitem" onClick={() => { setMenuOpen(false); onOpenFiles(); }}>
                Open files rail
              </button>
              {onOpenLegacy && (
                <button role="menuitem" onClick={() => { setMenuOpen(false); onOpenLegacy(); }}>
                  Legacy project window
                </button>
              )}
              {onOpenHome && (
                <button role="menuitem" onClick={() => { setMenuOpen(false); onOpenHome(); }}>
                  All projects
                </button>
              )}
            </div>
          )}
        </div>
      </header>

      <div className="sh-scroll ph-body">
        <div className="ph-main">
          {actionError && <div className="sh-error">{actionError}</div>}

          <div className="sh-card ph-progress">
            <p className="ph-desc">
              {project.description || 'No description yet.'}
              {' '}
              <span className="ph-desc-muted">Created {relativeTime(project.created_at)}.</span>
            </p>
            {tally.total > 0 ? (
              <>
                <div className="ph-progress-row">
                  <span className="ph-progress-main">{tally.proved} of {tally.total} proved</span>
                  <BlueprintLegend tally={tally} />
                  <span className="ph-progress-verify">SafeVerify: {tally.verified} of {tally.proved} audited</span>
                </div>
                <BlueprintBar tally={tally} />
              </>
            ) : (
              <div className="ph-progress-row">
                <span className="ph-progress-main">No blueprint yet.</span>
                <button className="sh-link" onClick={onOpenBlueprint}>Open the blueprint</button>
                <span className="ph-desc-muted">to draft the decomposition before proving.</span>
              </div>
            )}
          </div>

          <div className="ph-section">
            <SectionHead trailing={<button className="sh-link" onClick={onOpenBlueprint}>Blueprint</button>}>
              Up next
            </SectionHead>
            {nextItems.length === 0 ? (
              <div className="sh-empty">
                {tally.total === 0
                  ? 'Nothing queued: the blueprint is empty.'
                  : 'Everything in the blueprint is proved.'}
              </div>
            ) : (
              nextItems.map((item) => {
                const node = item.node as GraphNode;
                const blocked = item.action === 'blocked';
                const busy = busyNode === node.key;
                return (
                  <div key={node.key} className={`ph-next-row ${blocked ? 'is-blocked' : ''}`}>
                    <StatusDot kind={item.action === 'open' ? 'running' : nodeDotKind(node.status)} />
                    <span className="sh-mono ph-next-key" title={node.lean ?? undefined}>{node.key}</span>
                    {item.unlocked && (
                      <Pill kind="ready">unlocked</Pill>
                    )}
                    <span className="ph-next-reason" title={item.reason}>{item.reason}</span>
                    {item.action === 'open' ? (
                      <button className="sh-btn sh-btn-pri sh-btn-sm" onClick={() => onOpenSession(item.sessionId as string)}>
                        Open session
                      </button>
                    ) : (
                      <button
                        className={`sh-btn sh-btn-sm ${item.action === 'prove' ? 'sh-btn-pri' : ''}`}
                        disabled={blocked || !onProveNode || Boolean(busyNode)}
                        title={!onProveNode ? 'Proving from here is not wired yet' : undefined}
                        onClick={() => void prove(node, item.action as ProveMode)}
                      >
                        {busy ? <Loader2 size={13} className="sh-spin" /> : null}
                        {item.action === 'prove-sorry' ? 'Prove the sorry' : item.action === 'retry' ? 'Retry' : 'Prove'}
                      </button>
                    )}
                  </div>
                );
              })
            )}
          </div>

          <div className="ph-section">
            <SectionHead
              trailing={
                sessions.length > SESSIONS_PREVIEW ? (
                  <button className="sh-link" onClick={() => setShowAllSessions((v) => !v)}>
                    {showAllSessions ? 'Show fewer' : `All ${sessions.length}`}
                  </button>
                ) : (
                  sessions.length > 0 ? `${sessions.length}` : undefined
                )
              }
            >
              Sessions
            </SectionHead>
            {visibleSessions.length === 0 ? (
              <div className="sh-empty">No sessions yet. New session starts one in this project.</div>
            ) : (
              visibleSessions.map((session) => {
                const state = sessionState(session, runningSessionId);
                const nodeKey = nodeBySession[session.id];
                return (
                  <button key={session.id} className="ph-session-row" onClick={() => onOpenSession(session.id)}>
                    <StatusDot kind={sessionDotKind(state)} />
                    <span className="ph-session-title">{session.title}</span>
                    <span className={`sh-mono ph-session-node ${nodeKey ? '' : 'is-chat'}`}>{nodeKey ?? 'project chat'}</span>
                    <span className={`ph-session-state ${state === 'running' ? 'is-running' : ''}`}>
                      {sessionStateLabel(state)} · {relativeTime(session.updated_at)}
                    </span>
                  </button>
                );
              })
            )}
          </div>
        </div>

        <aside className="ph-rail">
          <OverleafCard
            linked={overleafLinked}
            url={overleafUrl}
            title={project.title}
            sessionCount={overleafSessions.length}
            lastAt={overleafSessions[0]?.updated_at}
          />

          <div className="sh-card ph-card">
            <SectionHead trailing={<button className="sh-link" onClick={() => onOpenFiles()}>Open rail</button>}>Files</SectionHead>
            {tree === undefined ? (
              <div className="sh-empty-sm">Loading files…</div>
            ) : files.length === 0 ? (
              <div className="sh-empty-sm">No Lean files yet.</div>
            ) : (
              <>
                {files.slice(0, FILES_PREVIEW).map((file) => (
                  <button key={file.path} className="ph-file-row" onClick={() => onOpenFiles(file.path)} title={file.path}>
                    <StatusDot kind={fileDotKind(file.status)} />
                    <span className="sh-mono ph-file-path">{file.path}</span>
                    <span className="ph-file-state">{file.status ? FILE_STATUS_LABEL[file.status] : 'unchecked'}</span>
                  </button>
                ))}
                {files.length > FILES_PREVIEW && (
                  <button className="sh-link ph-file-more" onClick={() => onOpenFiles()}>
                    {files.length - FILES_PREVIEW} more in the rail
                  </button>
                )}
              </>
            )}
          </div>

          <div ref={gitRef}>
            <GitCard
              projectId={projectId}
              remoteUrl={project.remote_url ?? null}
              lastSession={sessions[0]}
              editing={remoteEditing}
              onEditingChange={setRemoteEditing}
              onExport={exportZip}
            />
          </div>

          <div className="sh-card ph-card">
            <SectionHead
              trailing={
                <button className="sh-link sh-mono" onClick={() => onOpenFiles(INSTRUCTIONS_PATH)}>instructions.md</button>
              }
            >
              Instructions
            </SectionHead>
            <p className="ph-card-text">
              {instructions === null
                ? 'Loading…'
                : instructionsExcerpt || 'No instructions yet. Add the project goal and conventions so Lea follows them on every run.'}
            </p>
          </div>

          <div className="sh-card ph-card">
            <SectionHead trailing={memory ? `${memoryNotes} note${memoryNotes === 1 ? '' : 's'}` : undefined}>
              Memory
            </SectionHead>
            <p className="ph-card-text">
              {memory === null
                ? 'Loading…'
                : memoryExcerpt || 'No memory yet. Lea records facts, witnesses and dead ends here as it works.'}
            </p>
            {memory ? (
              <button className="sh-link ph-card-foot" onClick={() => onOpenFiles(MEMORY_PATH)}>
                Open memory.md <ArrowRight size={12} />
              </button>
            ) : null}
          </div>
        </aside>
      </div>
    </section>
  );
}

function OverleafCard({
  linked,
  url,
  title,
  sessionCount,
  lastAt,
}: {
  linked: boolean;
  url: string | null;
  title: string;
  sessionCount: number;
  lastAt?: string;
}) {
  const [draftUrl, setDraftUrl] = useState('');
  const [note, setNote] = useState<string | null>(null);

  if (linked) {
    return (
      <div className="sh-card ph-card ph-overleaf is-linked">
        <SectionHead>
          Overleaf
          <Pill kind="proved" className="ph-head-pill">
            <StatusDot kind="proved" />
            connected
          </Pill>
        </SectionHead>
        <div className="ph-overleaf-doc">
          <span className="ph-overleaf-badge" aria-hidden="true">O</span>
          <span className="ph-overleaf-title">{title} (paper)</span>
          {url && (
            <button className="sh-ibtn sh-ibtn-sm" aria-label="Open in Overleaf" onClick={() => void openOverleafDocument(url)}>
              <ExternalLink size={12} />
            </button>
          )}
        </div>
        {url && <span className="sh-mono ph-overleaf-url" title={url}>{url.replace(/^https?:\/\//, '')}</span>}
        <div className="ph-card-text ph-overleaf-lines">
          <span>
            {sessionCount} Overleaf session{sessionCount === 1 ? '' : 's'}
            {lastAt ? ` · last ${relativeTime(lastAt)}` : ''}
          </span>
          <span className="ph-desc-muted">Statements flow from the paper; status flows back as badges.</span>
        </div>
        <div className="ph-btn-row">
          <button
            className="sh-btn sh-btn-pri sh-btn-sm"
            onClick={() => setNote('Sync runs from the Overleaf extension (Lea → Sync); the UI has no sync endpoint yet.')}
          >
            <RefreshCw size={12} /> Sync now
          </button>
          {url && (
            <button className="sh-btn sh-btn-sm" onClick={() => void openOverleafDocument(url)}>Open in Overleaf</button>
          )}
        </div>
        {note && <div className="ph-note">{note}</div>}
      </div>
    );
  }

  return (
    <div className="sh-card ph-card ph-overleaf">
      <SectionHead>
        Overleaf
        <Pill kind="neutral" className="ph-head-pill">not linked</Pill>
      </SectionHead>
      <p className="ph-card-text">
        Link the paper and its marked theorems become blueprint nodes; proof status flows back as badges.
      </p>
      <div className="ph-overleaf-form">
        <label htmlFor="ph-overleaf-url" className="sh-visually-hidden">Overleaf project URL</label>
        <input
          id="ph-overleaf-url"
          className="sh-input"
          placeholder="https://www.overleaf.com/project/…"
          value={draftUrl}
          onChange={(e) => setDraftUrl(e.target.value)}
        />
        <button
          className="sh-btn sh-btn-sm"
          disabled={!draftUrl.trim()}
          onClick={() =>
            setNote(
              'Not wired yet: the project API has no Overleaf link field (updateProject takes title and description only). Link this project from the Overleaf extension instead.',
            )
          }
        >
          Connect
        </button>
      </div>
      {note && <div className="ph-note">{note}</div>}
    </div>
  );
}

function GitCard({
  projectId,
  remoteUrl,
  lastSession,
  editing,
  onEditingChange,
  onExport,
}: {
  projectId: string;
  remoteUrl: string | null;
  lastSession?: SessionSummary;
  editing: boolean;
  onEditingChange: (editing: boolean) => void;
  onExport: () => void;
}) {
  const refreshCurrentProject = useProjects((s) => s.refreshCurrentProject);
  const [draft, setDraft] = useState(remoteUrl ?? '');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDraft(remoteUrl ?? '');
  }, [remoteUrl]);

  const saveRemote = async () => {
    const url = draft.trim();
    if (!url || busy) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await setProjectRemote(projectId, url);
      await refreshCurrentProject();
      setMessage('Remote saved.');
      onEditingChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const push = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const res = await pushProject(projectId);
      setMessage(res.pushed ? `Pushed to ${res.remote_url}.` : res.detail || 'Nothing to push.');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sh-card ph-card">
      <SectionHead>Git</SectionHead>
      <div className="ph-card-text ph-git-lines">
        <span>
          {lastSession
            ? `Last activity by ${lastSession.title}, ${relativeTime(lastSession.updated_at)}`
            : 'No activity yet'}
        </span>
        {remoteUrl ? (
          <span className="sh-mono ph-git-remote" title={remoteUrl}>{remoteUrl.replace(/^https?:\/\//, '')}</span>
        ) : (
          <span className="ph-desc-muted">No remote yet · commits stay local</span>
        )}
      </div>
      {editing ? (
        <div className="ph-overleaf-form">
          <label htmlFor="ph-git-remote" className="sh-visually-hidden">GitHub remote URL</label>
          <input
            id="ph-git-remote"
            className="sh-input"
            placeholder="https://github.com/you/repo"
            value={draft}
            autoFocus
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void saveRemote();
              if (e.key === 'Escape') onEditingChange(false);
            }}
          />
          <button className="sh-btn sh-btn-sm sh-btn-pri" disabled={busy || !draft.trim()} onClick={() => void saveRemote()}>
            {busy ? <Loader2 size={12} className="sh-spin" /> : null} Save
          </button>
          <button className="sh-btn sh-btn-sm" disabled={busy} onClick={() => onEditingChange(false)}>Cancel</button>
        </div>
      ) : (
        <div className="ph-btn-row">
          {remoteUrl ? (
            <button className="sh-btn sh-btn-sm" disabled={busy} onClick={() => void push()}>
              {busy ? <Loader2 size={12} className="sh-spin" /> : null} Push
            </button>
          ) : null}
          <button className="sh-btn sh-btn-sm" onClick={() => onEditingChange(true)}>
            {remoteUrl ? 'Change remote…' : 'Set remote…'}
          </button>
          <button className="sh-btn sh-btn-sm" onClick={onExport}>
            <Download size={12} /> Export zip
          </button>
        </div>
      )}
      {message && <div className="ph-note">{message}</div>}
      {error && <div className="sh-error">{error}</div>}
    </div>
  );
}

function shortRepoPath(repoPath: string): string {
  const trimmed = repoPath.replace(/[\\/]+$/, '');
  const last = trimmed.split(/[\\/]/).pop();
  return last ? `${last}/` : trimmed;
}
