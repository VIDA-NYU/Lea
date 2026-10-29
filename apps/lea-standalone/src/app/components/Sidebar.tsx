import '../../styles/shell.css';
import { useEffect, useMemo, useState, type ReactElement } from 'react';
import {
  BarChart3,
  Bot,
  ChevronDown,
  ChevronRight,
  FileText,
  Folder,
  LayoutGrid,
  MessageSquare,
  PanelLeftClose,
  Plug,
  Plus,
  Search,
  Settings,
  Sparkles,
  Workflow,
  Wrench,
} from 'lucide-react';
import type { GraphNode, Project, SessionSummary } from '../lib/api';
import { useSessions } from '../stores/sessions';
import { useProjects } from '../stores/projects';
import { useProofSession } from '../stores/proofSession';
import { useWorkbench } from '../stores/workbench';
import {
  blueprintTally,
  flattenTree,
  projectRunning,
  projectSessions,
  sessionNodeMap,
  sessionState,
  sessionStateLabel,
  sidebarTally,
  sortProjectsByActivity,
  unfiledSessions,
} from '../lib/shellModel.js';
import { useProjectData } from './shell/projectData';
import { StatusDot, nodeDotKind, sessionDotKind } from './shell/bits';
// The desktop package's update button (docs/PLAN-ui-redesign.md): a stub on this
// branch, replaced at integration. Mounted in the footer slot below.
import { DesktopUpdateButton } from './shell/DesktopUpdateButton';

const NODE_ORDER: Record<string, number> = { ready: 1, stated: 2, failed: 3, planned: 4, proved: 5 };

// Left rail, project first (mockup frames 11–12): brand, New project, search,
// the Projects list sorted by activity. The open project expands to Overview,
// Blueprint (its nodes with their sessions nested), Files and Sessions. There
// is no Chats group any more; legacy sessions without a project sit under a
// collapsed "Unfiled" group so nothing is lost. Footer: user, update slot,
// stats, settings.
export function Sidebar({
  runningSessionId,
  userEmail,
  activeView,
  onSelectSession,
  onNewProject,
  onOpenHome,
  onOpenProject,
  onOpenBlueprint,
  onOpenFiles,
  onOpenSkills,
  onOpenSubagents,
  onOpenMcp,
  onOpenTools,
  onOpenSearch,
  onOpenSettings,
  onOpenStats,
  onCollapse,
}: {
  runningSessionId?: string;
  userEmail?: string;
  /** The view App is showing, for the active row (home, project-home, main, …). */
  activeView?: string;
  onSelectSession: (id: string) => void;
  onNewProject: () => void;
  onOpenHome: () => void;
  /** Open a project's home (Overview). */
  onOpenProject: (id: string) => void;
  /** Open the project's blueprint in the workbench. */
  onOpenBlueprint: (projectId: string) => void;
  /** Open the workbench with the files rail shown. */
  onOpenFiles: (projectId: string) => void;
  onOpenSkills: () => void;
  onOpenSubagents: () => void;
  onOpenMcp: () => void;
  onOpenTools: () => void;
  onOpenSearch: () => void;
  onOpenSettings: () => void;
  onOpenStats: () => void;
  onCollapse: () => void;
}) {
  const sessions = useSessions((s) => s.sessions);
  const selectedSessionId = useSessions((s) => s.selectedSessionId);
  const projects = useProjects((s) => s.projects);
  const selectedProjectId = useProjects((s) => s.selectedProjectId);
  const refreshToken = useProofSession((s) => s.formalizationRefreshToken);
  const nodeBindings = useWorkbench((s) => s.nodeBindings);
  const graphs = useProjectData((s) => s.graphs);
  const trees = useProjectData((s) => s.trees);
  const loadGraphs = useProjectData((s) => s.loadGraphs);
  const loadTree = useProjectData((s) => s.loadTree);

  const selectedSession = sessions.find((s) => s.id === selectedSessionId);
  // The open project: the one whose home is showing, else the selected session's.
  const openProjectId = selectedProjectId ?? selectedSession?.project_id ?? undefined;

  const [treeCollapsed, setTreeCollapsed] = useState(false);
  const [showProved, setShowProved] = useState(false);
  const [showSessions, setShowSessions] = useState(false);
  const [showUnfiled, setShowUnfiled] = useState(false);
  const [showLibrary, setShowLibrary] = useState(false);

  useEffect(() => {
    setTreeCollapsed(false);
    setShowProved(false);
    setShowSessions(false);
  }, [openProjectId]);

  const projectIdsKey = projects.map((p) => p.id).join(',');
  useEffect(() => {
    if (projectIdsKey) loadGraphs(projectIdsKey.split(','));
  }, [projectIdsKey, refreshToken, loadGraphs]);
  useEffect(() => {
    if (openProjectId) void loadTree(openProjectId);
  }, [openProjectId, refreshToken, loadTree]);

  const sorted = useMemo<Project[]>(() => sortProjectsByActivity(projects, sessions), [projects, sessions]);
  const unfiled = useMemo<SessionSummary[]>(() => unfiledSessions(sessions), [sessions]);

  // Sub-agents of the selected session's tree root (item 24), unchanged from
  // before the redesign: children never appear as rows of their own.
  const treeRootId = selectedSession ? selectedSession.parent_id ?? selectedSession.id : undefined;
  const treeRoot = treeRootId ? sessions.find((s) => s.id === treeRootId) : undefined;
  const subagents = treeRootId ? sessions.filter((s) => s.parent_id === treeRootId) : [];
  const subagentsRunning = subagents.filter((s) => sessionState(s, runningSessionId) === 'running').length;

  const sessionActive = (id: string) => selectedSessionId === id && activeView === 'main';

  const renderSessionRow = (session: SessionSummary, indent: number, trailing?: string) => {
    const state = sessionState(session, runningSessionId);
    return (
      <button
        key={session.id}
        className={`sb-row ${sessionActive(session.id) ? 'is-active' : ''}`}
        style={{ paddingLeft: indent }}
        title={session.latest_recovery_state === 'review' ? 'Interrupted — review before resuming'
          : session.latest_recovery_state === 'recovering' ? 'Recovering saved progress' : session.title}
        onClick={() => onSelectSession(session.id)}
      >
        {indent >= 38 ? <MessageSquare size={12} className="sb-ico" /> : <StatusDot kind={sessionDotKind(state)} />}
        <span className="sb-name">{session.title}</span>
        <span className={`sb-right ${state === 'running' ? 'is-running' : ''}`}>{trailing ?? sessionStateLabel(state)}</span>
      </button>
    );
  };

  return (
    <aside className="sidebar sb">
      <div className="sb-brand">
        <button className="sb-brand-name" onClick={onOpenHome} title="All projects">Lea</button>
        <span className="sh-spacer" />
        <button className="sh-ibtn sh-ibtn-sm" onClick={onCollapse} title="Hide sidebar" aria-label="Hide sidebar">
          <PanelLeftClose size={15} />
        </button>
      </div>

      <div className="sb-actions">
        <button className="sh-btn sh-btn-pri sb-new" onClick={onNewProject}>
          <Plus size={14} /> New project
        </button>
        <button className="sb-search" onClick={onOpenSearch} title="Search projects, sessions and declarations (⌘K)">
          <Search size={13} />
          <span className="sb-search-label">Search</span>
          <span className="sb-kbd">⌘K</span>
        </button>
      </div>

      <div className="sb-scroll">
        <div className="sb-group-label">
          <button className="sb-group-btn" onClick={onOpenHome}>Projects</button>
          <span className="sh-spacer" />
          <button className="sb-group-add" onClick={onNewProject} title="New project" aria-label="New project">
            <Plus size={13} />
          </button>
        </div>

        {sorted.length === 0 && <div className="sb-empty">No projects yet.</div>}

        {sorted.map((project) => {
          const graph = graphs[project.id];
          const tally = blueprintTally(graph?.nodes ?? []);
          const running = projectRunning(project.id, sessions, runningSessionId);
          const isOpen = project.id === openProjectId;
          const tallyText = sidebarTally(tally);
          const rightText = [tallyText, running ? 'running' : ''].filter(Boolean).join(' · ');
          const rightClass = running ? 'is-running' : tally.total > 0 && tally.proved === tally.total ? 'is-proved' : '';
          return (
            <div key={project.id} className={`sb-project ${isOpen ? 'is-open' : ''}`}>
              <div className="sb-project-head">
                {isOpen ? (
                  <button
                    className="sb-chev"
                    onClick={() => setTreeCollapsed((v) => !v)}
                    aria-label={treeCollapsed ? 'Expand project' : 'Collapse project'}
                    aria-expanded={!treeCollapsed}
                  >
                    {treeCollapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
                  </button>
                ) : (
                  <span className="sb-chev sb-chev-placeholder" />
                )}
                <button
                  className={`sb-row sb-row-project ${isOpen && activeView === 'project-home' ? 'is-active' : ''}`}
                  onClick={() => onOpenProject(project.id)}
                  title={project.title}
                >
                  <Folder size={14} className="sb-ico" />
                  <span className="sb-name sb-name-strong">{project.title}</span>
                  {rightText && <span className={`sb-right ${rightClass}`}>{rightText}</span>}
                </button>
              </div>

              {isOpen && !treeCollapsed && (
                <ProjectTree
                  project={project}
                  nodes={graph?.nodes ?? []}
                  tally={tally}
                  fileCount={trees[project.id] ? flattenTree(trees[project.id], { leanOnly: true }).length : undefined}
                  sessions={projectSessions(sessions, project.id)}
                  nodeBindings={nodeBindings}
                  runningSessionId={runningSessionId}
                  activeView={activeView}
                  showProved={showProved}
                  onToggleProved={() => setShowProved((v) => !v)}
                  showSessions={showSessions}
                  onToggleSessions={() => setShowSessions((v) => !v)}
                  onOpenProject={onOpenProject}
                  onOpenBlueprint={onOpenBlueprint}
                  onOpenFiles={onOpenFiles}
                  renderSessionRow={renderSessionRow}
                />
              )}
            </div>
          );
        })}

        {unfiled.length > 0 && (
          <div className="sb-group sb-unfiled">
            <button className="sb-group-toggle" onClick={() => setShowUnfiled((v) => !v)} aria-expanded={showUnfiled}>
              {showUnfiled ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              Unfiled
              <span className="sb-count">{unfiled.length}</span>
            </button>
            {showUnfiled && unfiled.map((session) => renderSessionRow(session, 22))}
          </div>
        )}

        <div className="sb-group">
          <button className="sb-group-toggle" onClick={() => setShowLibrary((v) => !v)} aria-expanded={showLibrary}>
            {showLibrary ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            Library
          </button>
          {showLibrary && (
            <>
              <button className="sb-row" style={{ paddingLeft: 22 }} onClick={onOpenSkills}>
                <Sparkles size={13} className="sb-ico" /><span className="sb-name">Skills</span>
              </button>
              <button className="sb-row" style={{ paddingLeft: 22 }} onClick={onOpenSubagents}>
                <Bot size={13} className="sb-ico" /><span className="sb-name">Sub-agents</span>
              </button>
              <button className="sb-row" style={{ paddingLeft: 22 }} onClick={onOpenMcp}>
                <Plug size={13} className="sb-ico" /><span className="sb-name">MCP servers</span>
              </button>
              <button className="sb-row" style={{ paddingLeft: 22 }} onClick={onOpenTools}>
                <Wrench size={13} className="sb-ico" /><span className="sb-name">Tools</span>
              </button>
            </>
          )}
        </div>

        {treeRoot && subagents.length > 0 && (
          <div className="sb-group sb-subagents">
            <div className="sb-group-label">
              Sub-agents
              <span className="sb-count">
                {subagentsRunning ? `${subagentsRunning} running` : `${subagents.length} done`}
              </span>
            </div>
            <button className="sb-row" onClick={() => onSelectSession(treeRoot.id)} title={treeRoot.title}>
              <span className="sb-chev-inline">◂</span>
              <span className="sb-name">{treeRoot.title}</span>
            </button>
            {subagents.map((child) => {
              const state = sessionState(child, runningSessionId);
              return (
                <button
                  key={child.id}
                  className={`sb-row ${sessionActive(child.id) ? 'is-active' : ''}`}
                  style={{ paddingLeft: 22 }}
                  onClick={() => onSelectSession(child.id)}
                >
                  {state === 'running' ? <span className="sb-spin" /> : <StatusDot kind={sessionDotKind(state)} />}
                  <span className="sb-name">{child.title}</span>
                  {child.role && <span className="sb-right">{child.role.split('-')[0]}</span>}
                </button>
              );
            })}
          </div>
        )}
      </div>

      <div className="sb-foot">
        <span className="sb-foot-user">{userEmail || 'local'}</span>
        {/* Desktop update button slot (package "desktop" replaces the stub). */}
        <span className="sb-foot-update">
          <DesktopUpdateButton />
        </span>
        <button className="sh-ibtn sh-ibtn-sm" onClick={onOpenStats} title="Usage & statistics" aria-label="Usage & statistics">
          <BarChart3 size={14} strokeWidth={1.75} />
        </button>
        <button className="sh-ibtn sh-ibtn-sm" onClick={onOpenSettings} title="Settings" aria-label="Settings">
          <Settings size={14} strokeWidth={1.75} />
        </button>
      </div>
    </aside>
  );
}

function ProjectTree({
  project,
  nodes,
  tally,
  fileCount,
  sessions,
  nodeBindings,
  runningSessionId,
  activeView,
  showProved,
  onToggleProved,
  showSessions,
  onToggleSessions,
  onOpenProject,
  onOpenBlueprint,
  onOpenFiles,
  renderSessionRow,
}: {
  project: { id: string };
  nodes: GraphNode[];
  tally: ReturnType<typeof blueprintTally>;
  fileCount?: number;
  sessions: SessionSummary[];
  nodeBindings: Record<string, string>;
  runningSessionId?: string;
  activeView?: string;
  showProved: boolean;
  onToggleProved: () => void;
  showSessions: boolean;
  onToggleSessions: () => void;
  onOpenProject: (id: string) => void;
  onOpenBlueprint: (projectId: string) => void;
  onOpenFiles: (projectId: string) => void;
  renderSessionRow: (session: SessionSummary, indent: number, trailing?: string) => ReactElement;
}) {
  const nodeBySession = useMemo(() => sessionNodeMap({ nodes }, nodeBindings), [nodes, nodeBindings]);
  const sessionsByNode = useMemo(() => {
    const map: Record<string, SessionSummary[]> = {};
    for (const s of sessions) {
      const key = nodeBySession[s.id];
      if (key) (map[key] ??= []).push(s);
    }
    return map;
  }, [sessions, nodeBySession]);
  const nodeIsRunning = (key: string) =>
    (sessionsByNode[key] ?? []).some((s) => sessionState(s, runningSessionId) === 'running');
  const orderedNodes = useMemo(
    () => [...nodes].sort((a, b) => {
      const ra = nodeIsRunning(a.key) ? 0 : NODE_ORDER[a.status] ?? 4;
      const rb = nodeIsRunning(b.key) ? 0 : NODE_ORDER[b.status] ?? 4;
      return ra - rb;
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [nodes, sessionsByNode, runningSessionId],
  );
  const activeNodes = orderedNodes.filter((n) => n.status !== 'proved');
  const provedNodes = orderedNodes.filter((n) => n.status === 'proved');
  const chatSessions = sessions.filter((s) => !nodeBySession[s.id]);

  const nodeRight = (node: GraphNode): string => {
    const bound = sessionsByNode[node.key] ?? [];
    if (bound.some((s) => sessionState(s, runningSessionId) === 'running')) return 'running';
    if (node.status === 'stated') return 'sorry';
    if (node.status === 'failed') return 'error';
    if (node.status === 'ready') return bound.length ? 'ready' : 'ready · no session';
    if (node.status === 'planned') return 'waits';
    return '';
  };

  const openNode = (node: GraphNode) => {
    useWorkbench.getState().selectNode(node.key);
    onOpenBlueprint(project.id);
  };

  return (
    <div className="sb-tree">
      <button
        className={`sb-row ${activeView === 'project-home' ? 'is-active' : ''}`}
        style={{ paddingLeft: 24 }}
        onClick={() => onOpenProject(project.id)}
      >
        <LayoutGrid size={13} className="sb-ico" />
        <span className="sb-name">Overview</span>
      </button>
      <button className="sb-row" style={{ paddingLeft: 24 }} onClick={() => onOpenBlueprint(project.id)}>
        <Workflow size={13} className="sb-ico" />
        <span className="sb-name">Blueprint</span>
        {tally.total > 0 && (
          <span className={`sb-right ${tally.proved > 0 ? 'is-proved' : ''}`}>
            {tally.proved > 0 ? `${tally.proved} proved` : `${tally.total} planned`}
          </span>
        )}
      </button>
      {activeNodes.map((node) => (
        <div key={node.key}>
          <button className="sb-row sb-row-node" style={{ paddingLeft: 38 }} onClick={() => openNode(node)} title={node.statement || node.key}>
            <StatusDot kind={nodeIsRunning(node.key) ? 'running' : nodeDotKind(node.status)} />
            <span className="sb-name sh-mono">{node.key}</span>
            <span className={`sb-right ${nodeIsRunning(node.key) ? 'is-running' : ''}`}>{nodeRight(node)}</span>
          </button>
          {(sessionsByNode[node.key] ?? []).map((s) => renderSessionRow(s, 52))}
        </div>
      ))}
      {provedNodes.length > 0 && (
        <>
          <button className="sb-row sb-row-summary" style={{ paddingLeft: 38 }} onClick={onToggleProved} aria-expanded={showProved}>
            <span className="sb-name">{provedNodes.length} proved node{provedNodes.length === 1 ? '' : 's'}</span>
            {showProved ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
          </button>
          {showProved && provedNodes.map((node) => (
            <div key={node.key}>
              <button className="sb-row sb-row-node" style={{ paddingLeft: 38 }} onClick={() => openNode(node)} title={node.statement || node.key}>
                <StatusDot kind="proved" />
                <span className="sb-name sh-mono">{node.key}</span>
                <span className="sb-right is-proved">{node.verified ? 'verified' : 'proved'}</span>
              </button>
              {(sessionsByNode[node.key] ?? []).map((s) => renderSessionRow(s, 52))}
            </div>
          ))}
        </>
      )}
      <button className="sb-row" style={{ paddingLeft: 24 }} onClick={() => onOpenFiles(project.id)}>
        <FileText size={13} className="sb-ico" />
        <span className="sb-name">Files</span>
        {typeof fileCount === 'number' && <span className="sb-right">{fileCount}</span>}
      </button>
      <button className="sb-row" style={{ paddingLeft: 24 }} onClick={onToggleSessions} aria-expanded={showSessions}>
        <MessageSquare size={12} className="sb-ico" />
        <span className="sb-name">Sessions</span>
        <span className="sb-right">{sessions.length}</span>
      </button>
      {showSessions && (
        sessions.length === 0
          ? <div className="sb-empty" style={{ paddingLeft: 38 }}>No sessions yet.</div>
          : sessions.map((s) => renderSessionRow(s, 38, nodeBySession[s.id] ? undefined : chatSessions.includes(s) ? 'project chat' : undefined))
      )}
    </div>
  );
}
