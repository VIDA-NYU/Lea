import { useMemo } from 'react';
import { BarChart3, Bot, PanelLeftClose, Plug, Plus, Sparkles, Wrench } from 'lucide-react';
import type { SessionSummary } from '../lib/api';
import { useSessions } from '../stores/sessions';
import { useProjects } from '../stores/projects';

// Left rail: brand, new-proof, (search stub), Projects group, date-grouped loose
// chats, footer. The Projects group (v2.1 F1) lists projects; loose chats are the
// `project_id IS NULL` sessions.
export function Sidebar({
  runningSessionId,
  userEmail,
  onSelectSession,
  onNewSession,
  onSelectProject,
  onNewProject,
  onOpenProjectsHub,
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
  onSelectSession: (id: string) => void;
  onNewSession: () => void;
  onSelectProject: (id: string) => void;
  onNewProject: () => void;
  onOpenProjectsHub: () => void;
  onOpenSkills: () => void;
  onOpenSubagents: () => void;
  onOpenMcp: () => void;
  onOpenTools: () => void;
  onOpenSearch: () => void;
  onOpenSettings: () => void;
  onOpenStats: () => void;
  onCollapse: () => void;
}) {
  // R3: session list + selection from the store, not props.
  const sessions = useSessions((s) => s.sessions);
  const selectedSessionId = useSessions((s) => s.selectedSessionId);
  // D36: the sidebar's Chats group is loose sessions only — in-project sessions
  // live in the project window (reachable there or via search), not here. Item 24:
  // and ROOTS only (`parent_id == null`) — sub-agent children never appear in the
  // Chats list; they surface in the contextual Sub-agents block below, scoped to the
  // tree root so it survives clicking into a candidate.
  const looseSessions = sessions.filter((s) => !s.project_id && !s.parent_id);
  const groups = groupByDate(looseSessions);
  // The Sub-agents block (item 24): scope to the tree ROOT of the selection, not the
  // selection itself (`children(parent_id ?? id)`) — so clicking a candidate keeps the
  // block (siblings + the way back) instead of vanishing (a candidate has no children).
  const selectedSession = sessions.find((s) => s.id === selectedSessionId);
  const treeRootId = selectedSession ? selectedSession.parent_id ?? selectedSession.id : undefined;
  const treeRoot = treeRootId ? sessions.find((s) => s.id === treeRootId) : undefined;
  const subagents = treeRootId ? sessions.filter((s) => s.parent_id === treeRootId) : [];
  const subagentsRunning = subagents.filter((s) => dotClass(s, runningSessionId) === 'run').length;
  // F1: projects list + which one is open, from the projects store.
  const projects = useProjects((s) => s.projects);
  const selectedProjectId = useProjects((s) => s.selectedProjectId);
  // D7: the flat project list doesn't scale — the sidebar shows only the 3
  // most-recently-updated (latest on top); the rest live in the Projects hub, opened
  // by clicking the "Projects" header. Same sort idiom the Chats group uses.
  const topProjects = useMemo(
    () => [...projects].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)).slice(0, 3),
    [projects],
  );

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="logo">L</div>
        <div className="name">Lea</div>
        <div className="ver">v2</div>
        <button className="icon-btn" style={{ marginLeft: 8 }} onClick={onCollapse} title="Collapse sidebar">
          <PanelLeftClose size={15} />
        </button>
      </div>
      <button className="newbtn" onClick={onNewSession}>
        <span className="plus">+</span> New proof
      </button>
      <button className="searchbtn" onClick={onOpenSearch} title="Search all proofs (⌘K)">
        🔍 Search all proofs <span className="kbd">⌘K</span>
      </button>

      <div className="sb-scroll">
        <div className="proj-group">
          <div className="group-label proj-head">
            <button
              className="proj-head-btn"
              onClick={onOpenProjectsHub}
              title="Open the Projects hub"
            >
              Projects
            </button>
            <button className="proj-add" onClick={onNewProject} title="New project" aria-label="New project">
              <Plus size={13} />
            </button>
          </div>
          {projects.length === 0 ? (
            <div className="proj-empty">No projects yet.</div>
          ) : (
            <>
              {topProjects.map((project) => (
                <button
                  key={project.id}
                  className={`row ${selectedProjectId === project.id ? 'active' : ''}`}
                  onClick={() => onSelectProject(project.id)}
                >
                  <span className="picon">∑</span>
                  <span className="rtitle">{project.title}</span>
                  {typeof project.session_count === 'number' && project.session_count > 0 && (
                    <span className="count">{project.session_count}</span>
                  )}
                </button>
              ))}
              {projects.length > topProjects.length && (
                <button className="proj-seeall" onClick={onOpenProjectsHub}>
                  See all {projects.length} projects →
                </button>
              )}
            </>
          )}
        </div>

        <div className="proj-group">
          <div className="group-label">Library</div>
          <button className="row" onClick={onOpenSkills}>
            <span className="picon"><Sparkles size={13} /></span>
            <span className="rtitle">Skills</span>
          </button>
          <button className="row" onClick={onOpenSubagents}>
            <span className="picon"><Bot size={13} /></span>
            <span className="rtitle">Sub-agents</span>
          </button>
          <button className="row" onClick={onOpenMcp}>
            <span className="picon"><Plug size={13} /></span>
            <span className="rtitle">MCP servers</span>
          </button>
          <button className="row" onClick={onOpenTools}>
            <span className="picon"><Wrench size={13} /></span>
            <span className="rtitle">Tools</span>
          </button>
        </div>

        {looseSessions.length > 0 && <div className="group-label">Chats</div>}
        {looseSessions.length === 0 && (
          <div className="group-label" style={{ textTransform: 'none', letterSpacing: 0 }}>
            No proofs yet — start one below.
          </div>
        )}
        {groups.map((group) => (
          <div key={group.label}>
            <div className="group-label">{group.label}</div>
            {group.sessions.map((session) => (
              <button
                key={session.id}
                className={`row ${selectedSessionId === session.id ? 'active' : ''}`}
                onClick={() => onSelectSession(session.id)}
              >
                <span className={`dot ${dotClass(session, runningSessionId)}`} />
                <span className="rtitle">{session.title}</span>
              </button>
            ))}
          </div>
        ))}

        {treeRoot && subagents.length > 0 && (
          <div className="sa-group">
            <div className="group-label">
              Sub-agents
              <span className="sa-count">
                {subagentsRunning ? `${subagentsRunning} running` : `${subagents.length} done`}
              </span>
            </div>
            <button className="sa-parent" onClick={() => onSelectSession(treeRoot.id)}>
              <span className="chev">◂</span>
              <span className="ptitle">{treeRoot.title}</span>
            </button>
            {subagents.map((child) => {
              const running = dotClass(child, runningSessionId) === 'run';
              return (
                <button
                  key={child.id}
                  className={`row ${selectedSessionId === child.id ? 'active' : ''}`}
                  onClick={() => onSelectSession(child.id)}
                >
                  {running ? (
                    <span className="sa-spin" />
                  ) : (
                    <span className={`dot ${dotClass(child, runningSessionId)}`} />
                  )}
                  <span className="rtitle">{child.title}</span>
                  {child.role && <span className="role">{child.role.split('-')[0]}</span>}
                </button>
              );
            })}
          </div>
        )}
      </div>

      <div className="sidebar-foot">
        <span>{userEmail || 'local'}</span>
        <button
          className="gear stats"
          onClick={onOpenStats}
          title="Usage & statistics"
          aria-label="Usage & statistics"
        >
          <BarChart3 size={15} strokeWidth={1.75} />
        </button>
        <button className="gear" onClick={onOpenSettings} title="Settings" aria-label="Settings">
          ⚙
        </button>
      </div>
    </aside>
  );
}

function dotClass(session: SessionSummary, runningSessionId?: string): string {
  // The open session's live run shows instantly via runningSessionId; background
  // runs (a different chat, incl. an Overleaf-driven one) surface through
  // active_run_count, which the sessions_changed feed refreshes on every run
  // start/finish. Either one lights the running dot (v2.3 item 13).
  if (session.id === runningSessionId) return 'run';
  if ((session.active_run_count ?? 0) > 0) return 'run';
  if (session.status === 'ok' || session.status === 'proved' || session.status === 'defined') return 'ok';
  if (session.status === 'disproved') return 'run';
  if (session.status === 'error') return 'fail';
  return 'idle';
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

function groupByDate(sessions: SessionSummary[]): { label: string; sessions: SessionSummary[] }[] {
  const sorted = [...sessions].sort(
    (a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at),
  );
  const now = new Date();
  const today = startOfDay(now);
  const yesterday = today - 86400000;
  const buckets = new Map<string, SessionSummary[]>();
  const order: string[] = [];
  const put = (label: string, s: SessionSummary) => {
    if (!buckets.has(label)) {
      buckets.set(label, []);
      order.push(label);
    }
    buckets.get(label)!.push(s);
  };
  for (const session of sorted) {
    const t = startOfDay(new Date(session.updated_at || session.created_at));
    if (t >= today) put('Today', session);
    else if (t >= yesterday) put('Yesterday', session);
    else put('Earlier', session);
  }
  return order.map((label) => ({ label, sessions: buckets.get(label)! }));
}
