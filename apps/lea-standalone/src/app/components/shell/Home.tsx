import '../../../styles/shell.css';
import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowUp, Folder, Loader2, Plus } from 'lucide-react';
import type { Project, SessionSummary } from '../../lib/api';
import { useProjects } from '../../stores/projects';
import { useSessions } from '../../stores/sessions';
import { useProofSession } from '../../stores/proofSession';
import { useModel } from '../../stores/model';
import { useWorkbench } from '../../stores/workbench';
import {
  blueprintTally,
  projectRunning,
  projectSummary,
  projectTitleFromStatement,
  recentSessions,
  relativeTime,
  sessionNodeMap,
  sessionState,
  sessionStateLabel,
  sortProjectsByActivity,
} from '../../lib/shellModel.js';
import { useProjectData } from './projectData';
import { BlueprintBar, ExpandSidebarButton, RunningPill, SectionHead, StatusDot, sessionDotKind } from './bits';

// Work package "shell" (mockup frame 11). The launch screen: "What do you want
// to prove?" (a statement becomes a project), project cards with the blueprint
// bar, recent sessions. App renders it for view === 'home'.
export function Home({
  onOpenProject,
  onStartProject,
  onNewProject,
  onOpenSession,
  startsWithBlueprint = false,
  sidebarCollapsed,
  onExpandSidebar,
}: {
  onOpenProject: (projectId: string) => void;
  /** Create a project named from the statement, its first session, and start the run. */
  onStartProject: (statement: string) => void | Promise<void>;
  onNewProject: () => void;
  /** Open a recent session in the workbench (falls back to its project when absent). */
  onOpenSession?: (sessionId: string) => void;
  /** Whether onStartProject opens with `/blueprint` (App decides; the copy follows). */
  startsWithBlueprint?: boolean;
  sidebarCollapsed?: boolean;
  onExpandSidebar?: () => void;
}) {
  const projects = useProjects((s) => s.projects);
  const refreshProjects = useProjects((s) => s.refreshProjects);
  const sessions = useSessions((s) => s.sessions);
  const selectedSessionId = useSessions((s) => s.selectedSessionId);
  const isRunning = useProofSession((s) => s.isRunning);
  const refreshToken = useProofSession((s) => s.formalizationRefreshToken);
  const model = useModel((s) => s.model);
  const nodeBindings = useWorkbench((s) => s.nodeBindings);
  const graphs = useProjectData((s) => s.graphs);
  const loadGraphs = useProjectData((s) => s.loadGraphs);
  const runningSessionId = isRunning ? selectedSessionId : undefined;

  const [draft, setDraft] = useState('');
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    refreshProjects().catch(() => {});
  }, [refreshProjects]);

  // One graph per project for the five-colour bars; reloaded when the session
  // feed reports a change (a run finished, a file was checked).
  const projectIdsKey = projects.map((p) => p.id).join(',');
  useEffect(() => {
    if (projectIdsKey) loadGraphs(projectIdsKey.split(','));
  }, [projectIdsKey, refreshToken, loadGraphs]);

  const sorted = useMemo<Project[]>(() => sortProjectsByActivity(projects, sessions), [projects, sessions]);
  const runningCount = useMemo(
    () => projects.filter((p) => projectRunning(p.id, sessions, runningSessionId)).length,
    [projects, sessions, runningSessionId],
  );
  const recent = useMemo<SessionSummary[]>(() => recentSessions(sessions, 6), [sessions]);
  const projectById = useMemo(() => new Map(projects.map((p) => [p.id, p])), [projects]);
  const nodeBySession = useMemo(() => {
    const merged: Record<string, string> = {};
    for (const graph of Object.values(graphs)) Object.assign(merged, sessionNodeMap(graph, {}));
    return Object.assign(merged, sessionNodeMap(null, nodeBindings));
  }, [graphs, nodeBindings]);

  const previewTitle = draft.trim() ? projectTitleFromStatement(draft) : null;

  const submit = async () => {
    const statement = draft.trim();
    if (!statement || starting) return;
    setStarting(true);
    setStartError(null);
    try {
      await onStartProject(statement);
      setDraft('');
    } catch (err) {
      setStartError(err instanceof Error ? err.message : String(err));
    } finally {
      setStarting(false);
    }
  };

  return (
    <section className="home sh-page" aria-label="Projects">
      <header className="sh-topbar">
        <ExpandSidebarButton collapsed={sidebarCollapsed} onExpand={onExpandSidebar} />
        <span className="sh-topbar-title">Projects</span>
        <span className="sh-topbar-sub">
          {projects.length} project{projects.length === 1 ? '' : 's'}
          {runningCount > 0 && ` · ${runningCount} running`}
        </span>
        <span className="sh-spacer" />
        <button className="sh-btn" onClick={onNewProject}>
          <Plus size={14} /> New project
        </button>
      </header>

      <div className="sh-scroll home-body">
        <div className="home-ask">
          <p className="home-ask-title">What do you want to prove?</p>
          <p className="home-ask-sub">
            A statement becomes a project: its own folder, namespace and git history.
            {startsWithBlueprint ? ' Lea drafts the blueprint first.' : ' Lea starts on it in a fresh session.'}
          </p>
          <div className={`home-composer ${starting ? 'is-busy' : ''}`}>
            <label htmlFor="home-composer" className="sh-visually-hidden">Theorem to prove</label>
            <textarea
              id="home-composer"
              ref={composerRef}
              className="home-composer-input"
              rows={1}
              placeholder="Prove that every bounded monotone sequence converges"
              value={draft}
              disabled={starting}
              onChange={(e) => {
                setDraft(e.target.value);
                // Grow with the statement, up to the stylesheet's max-height.
                e.target.style.height = 'auto';
                e.target.style.height = `${Math.min(160, e.target.scrollHeight)}px`;
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  void submit();
                }
              }}
            />
            <div className="home-composer-foot">
              <span className="sh-pill sh-pill-neutral home-composer-pill">
                <Folder size={12} />
                {previewTitle ? `New project · ${previewTitle}` : 'New project · named from the statement'}
              </span>
              {model && <span className="home-composer-model">{model}</span>}
              <span className="sh-spacer" />
              <span className="home-composer-hint">
                {startsWithBlueprint ? 'Starts with /blueprint' : 'Enter to start'}
              </span>
              <button
                className="home-composer-send"
                aria-label="Start"
                title="Start (Enter)"
                disabled={!draft.trim() || starting}
                onClick={() => void submit()}
              >
                {starting ? <Loader2 size={16} className="sh-spin" /> : <ArrowUp size={16} />}
              </button>
            </div>
          </div>
          {startError && <div className="sh-error">{startError}</div>}
        </div>

        <div className="home-section">
          <SectionHead>Your projects</SectionHead>
          {sorted.length === 0 ? (
            <div className="sh-empty">
              No projects yet. Type a statement above, or create an empty project with New project.
            </div>
          ) : (
            <div className="home-grid">
              {sorted.map((project) => {
                const graph = graphs[project.id];
                const tally = blueprintTally(graph?.nodes ?? []);
                const projectSessions = sessions.filter((s) => s.project_id === project.id && !s.parent_id);
                const sessionCount = projectSessions.length || project.session_count || 0;
                const running = projectRunning(project.id, sessions, runningSessionId);
                const lastAt = projectSessions.reduce(
                  (max, s) => (Date.parse(s.updated_at) > Date.parse(max) ? s.updated_at : max),
                  project.updated_at,
                );
                return (
                  <button
                    key={project.id}
                    className="sh-card home-card"
                    onClick={() => onOpenProject(project.id)}
                    title={`Open ${project.title}`}
                  >
                    <div className="home-card-top">
                      <span className="home-card-name">{project.title}</span>
                      {running && <RunningPill />}
                      <span className="sh-spacer" />
                      <span className="home-card-when">{relativeTime(lastAt)}</span>
                    </div>
                    <span className="sh-mono home-card-ns">
                      {project.namespace}
                      {project.repo_path && ` · ${shortRepoPath(project.repo_path)}`}
                    </span>
                    {project.description && <p className="home-card-desc">{project.description}</p>}
                    <BlueprintBar tally={tally} />
                    <div className="home-card-meta">{projectSummary(tally, sessionCount)}</div>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {recent.length > 0 && (
          <div className="home-section">
            <SectionHead>Recent sessions</SectionHead>
            <div className="home-recent">
              {recent.map((session) => {
                const state = sessionState(session, runningSessionId);
                const project = session.project_id ? projectById.get(session.project_id) : undefined;
                const nodeKey = nodeBySession[session.id];
                const open = () => {
                  if (onOpenSession) onOpenSession(session.id);
                  else if (session.project_id) onOpenProject(session.project_id);
                };
                return (
                  <button key={session.id} className="home-recent-row" onClick={open}>
                    <StatusDot kind={sessionDotKind(state)} />
                    <span className="home-recent-title">{session.title}</span>
                    <span className="home-recent-project">{project ? `${project.title} ›` : 'unfiled'}</span>
                    {nodeKey && <span className="sh-mono home-recent-node">{nodeKey}</span>}
                    <span className={`home-recent-state ${state === 'running' ? 'is-running' : ''}`}>
                      {sessionStateLabel(state)} · {relativeTime(session.updated_at)}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

// "…/projects/sqrt2" → "sqrt2/": the last path segment, which is what the card
// has room for; the full path is on the project home.
function shortRepoPath(repoPath: string): string {
  const trimmed = repoPath.replace(/[\\/]+$/, '');
  const last = trimmed.split(/[\\/]/).pop();
  return last ? `${last}/` : trimmed;
}
