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
  blueprintFoldSummary,
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
import { effectiveNodeBindings } from '../lib/blueprintModel.js';
import { useProjectData } from './shell/projectData';
import { StatusDot, nodeDotKind, sessionDotKind } from './shell/bits';
// The desktop package's update button (mockup frame 15), mounted in the footer.
import { DesktopUpdateButton } from './shell/DesktopUpdateButton';

const NODE_ORDER: Record<string, number> = { ready: 1, stated: 2, failed: 3, planned: 4, proved: 5 };
// Up to this many unproved nodes the list under "Blueprint" opens by itself;
// above it, it folds to a summary row until the user expands it.
const NODE_AUTO_EXPAND_MAX = 12;

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
  // The expanded project: none on the launch screen (frame 11 lists projects
  // only); the project whose home is showing; otherwise the selected session's,
  // so opening a session of another project (Recent sessions, search) follows it.
  const openProjectId = activeView === 'home'
    ? undefined
    : activeView === 'project-home' || activeView === 'project'
      ? selectedProjectId
      : selectedSession?.project_id ?? selectedProjectId ?? undefined;

  const [treeCollapsed, setTreeCollapsed] = useState(false);
  // Blueprint nodes under the project: null = automatic (open for a small
  // blueprint, folded for a big one, see ProjectTree); a click pins it.
  const [showNodes, setShowNodes] = useState<boolean | null>(null);
  const [showProved, setShowProved] = useState(false);
  const [showSessions, setShowSessions] = useState(false);
  const [showUnfiled, setShowUnfiled] = useState(false);
  const [showLibrary, setShowLibrary] = useState(false);

  useEffect(() => {
    setTreeCollapsed(false);
    setShowNodes(null);
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
  // Collapsed unless the open session is one of them.
  const unfiledOpen = showUnfiled || unfiled.some((s) => s.id === selectedSessionId);

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
                  className="sb-row sb-row-project"
                  onClick={() => onOpenProject(project.id)}
                  aria-current={isOpen && activeView === 'project-home' ? 'page' : undefined}
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
                  selectedSessionId={activeView === 'main' ? selectedSessionId : undefined}
                  activeView={activeView}
                  showNodes={showNodes}
                  onToggleNodes={(open) => setShowNodes(open)}
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

        {/* The open session's sub-agents: workbench only. */}
        {activeView === 'main' && treeRoot && subagents.length > 0 && (
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

        {/* Legacy sessions without a project, last and collapsed: nothing is lost. */}
        {unfiled.length > 0 && (
          <div className="sb-group sb-unfiled">
            <button className="sb-group-toggle" onClick={() => setShowUnfiled((v) => !v)} aria-expanded={unfiledOpen}>
              {unfiledOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              Unfiled
              <span className="sb-count">{unfiled.length}</span>
            </button>
            {unfiledOpen && unfiled.map((session) => renderSessionRow(session, 22))}
          </div>
        )}
      </div>

      <div className="sb-foot">
        <span className="sb-foot-user">{userEmail || 'local'}</span>
        {/* Desktop update button: a direct child of this flex row; it renders
            null in a browser and pushes itself right with margin-left: auto. */}
        <DesktopUpdateButton />
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
  selectedSessionId,
  activeView,
  showNodes,
  onToggleNodes,
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
  /** The session open in the workbench: its node and row stay visible. */
  selectedSessionId?: string;
  activeView?: string;
  /** null = automatic: open while the blueprint is small, folded once it grows. */
  showNodes: boolean | null;
  onToggleNodes: (open: boolean) => void;
  showProved: boolean;
  onToggleProved: () => void;
  showSessions: boolean;
  onToggleSessions: () => void;
  onOpenProject: (id: string) => void;
  onOpenBlueprint: (projectId: string) => void;
  onOpenFiles: (projectId: string) => void;
  renderSessionRow: (session: SessionSummary, indent: number, trailing?: string) => ReactElement;
}) {
  const nodeBySession = useMemo(
    () => sessionNodeMap({ nodes }, effectiveNodeBindings(sessions, nodeBindings)),
    [nodes, sessions, nodeBindings],
  );
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
  // Proved nodes fold into one summary row, except the one holding the open
  // session (BlueprintSession frame: its row stays in view).
  const selectedNodeKey = selectedSessionId ? nodeBySession[selectedSessionId] : undefined;
  const activeNodes = orderedNodes.filter((n) => n.status !== 'proved' || n.key === selectedNodeKey);
  const provedNodes = orderedNodes.filter((n) => n.status === 'proved' && n.key !== selectedNodeKey);
  const chatSessions = sessions.filter((s) => !nodeBySession[s.id]);
  // An open project chat (no node) shows in the Sessions list, opened for it.
  const sessionsOpen = showSessions || chatSessions.some((s) => s.id === selectedSessionId);
  // A blueprint can hold hundreds of nodes: the list under "Blueprint" folds to
  // one summary row once it is bigger than a screenful, unless the user pins
  // it open. The node holding the open session stays visible either way.
  const runningCount = orderedNodes.filter((n) => nodeIsRunning(n.key)).length;
  const nodesOpen = showNodes ?? activeNodes.length <= NODE_AUTO_EXPAND_MAX;
  const foldSummary = blueprintFoldSummary(tally, runningCount);
  const selectedNode = selectedNodeKey ? orderedNodes.find((n) => n.key === selectedNodeKey) : undefined;

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
      <div className="sb-row-wrap">
        <button className="sb-row" style={{ paddingLeft: 24 }} onClick={() => onOpenBlueprint(project.id)}>
          <Workflow size={13} className="sb-ico" />
          <span className="sb-name">Blueprint</span>
          {tally.total > 0 && (
            <span className={`sb-right ${tally.proved > 0 ? 'is-proved' : ''}`}>
              {tally.proved > 0 ? `${tally.proved} proved` : `${tally.total} planned`}
            </span>
          )}
        </button>
        {nodes.length > 0 && (
          <button
            className="sb-chev sb-chev-end"
            onClick={() => onToggleNodes(!nodesOpen)}
            aria-label={nodesOpen ? 'Collapse blueprint nodes' : 'Expand blueprint nodes'}
            aria-expanded={nodesOpen}
            title={nodesOpen ? 'Collapse the node list' : 'Expand the node list'}
          >
            {nodesOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          </button>
        )}
      </div>
      {!nodesOpen && nodes.length > 0 && (
        <>
          <button
            className="sb-row sb-row-summary"
            style={{ paddingLeft: 38 }}
            onClick={() => onToggleNodes(true)}
            aria-expanded={false}
            title="Show every node"
          >
            <span className="sb-name">{foldSummary}</span>
            <ChevronRight size={11} />
          </button>
          {selectedNode && (
            <div key={selectedNode.key}>
              <button className="sb-row sb-row-node" style={{ paddingLeft: 38 }} onClick={() => openNode(selectedNode)} title={selectedNode.statement || selectedNode.key}>
                <StatusDot kind={nodeIsRunning(selectedNode.key) ? 'running' : nodeDotKind(selectedNode.status)} />
                <span className="sb-name sh-mono">{selectedNode.key}</span>
                <span className={`sb-right ${nodeIsRunning(selectedNode.key) ? 'is-running' : ''}`}>{nodeRight(selectedNode)}</span>
              </button>
              {(sessionsByNode[selectedNode.key] ?? []).map((s) => renderSessionRow(s, 52))}
            </div>
          )}
        </>
      )}
      {nodesOpen && activeNodes.map((node) => (
        <div key={node.key}>
          <button className="sb-row sb-row-node" style={{ paddingLeft: 38 }} onClick={() => openNode(node)} title={node.statement || node.key}>
            <StatusDot kind={nodeIsRunning(node.key) ? 'running' : nodeDotKind(node.status)} />
            <span className="sb-name sh-mono">{node.key}</span>
            <span className={`sb-right ${nodeIsRunning(node.key) ? 'is-running' : ''}`}>{nodeRight(node)}</span>
          </button>
          {(sessionsByNode[node.key] ?? []).map((s) => renderSessionRow(s, 52))}
        </div>
      ))}
      {nodesOpen && provedNodes.length > 0 && (
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
      <button className="sb-row" style={{ paddingLeft: 24 }} onClick={onToggleSessions} aria-expanded={sessionsOpen}>
        <MessageSquare size={12} className="sb-ico" />
        <span className="sb-name">Sessions</span>
        <span className="sb-right">{sessions.length}</span>
      </button>
      {sessionsOpen && (
        sessions.length === 0
          ? <div className="sb-empty" style={{ paddingLeft: 38 }}>No sessions yet.</div>
          : sessions.map((s) => renderSessionRow(s, 38, nodeBySession[s.id] ? undefined : chatSessions.includes(s) ? 'project chat' : undefined))
      )}
    </div>
  );
}
