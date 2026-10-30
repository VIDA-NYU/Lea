import { observeSessionList } from './lib/sessionObserver.mjs';
import { useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import { Sidebar } from './components/Sidebar';
import { ChatThread } from './components/ChatThread';
import { Canvas, type CheckOutcome } from './components/Canvas';
import { StatsPage } from './components/StatsPage';
import { SettingsPage } from './components/SettingsPage';
import { ProjectWindow } from './components/ProjectWindow';
import { SkillFactory } from './components/SkillFactory';
import { SubagentFactory } from './components/SubagentFactory';
import { McpFactory } from './components/McpFactory';
import { ToolFactory } from './components/ToolFactory';
import { SkillsMcpPicker } from './components/SkillsMcpPicker';
import { useFactories } from './stores/factories';
import { ProjectsHub } from './components/ProjectsHub';
import { NewProjectDialog } from './components/NewProjectDialog';
import { SearchOverlay } from './components/SearchOverlay';
import { Home } from './components/shell/Home';
import { ProjectHome, type ProveMode } from './components/shell/ProjectHome';
import { QuickOpen } from './components/files/QuickOpen';
import { BLUEPRINT_PATH, useWorkbench } from './stores/workbench';
import { sortCodeSteps } from './lib/timeline.mjs';
import { inferComposerFormalizationScope } from './lib/formalizations.mjs';
import { createChatSubmission, createCheckpointSubmission } from './lib/overleafHandoff.mjs';
import { projectTitleFromStatement, proveNodeMessage, settleRestoredView, shownView as resolveShownView } from './lib/shellModel.js';
import { parseSlashCommand } from './lib/slashCommands.js';
import { runSlashCommand } from './lib/slashCommandRunner';
import {
  restoreInitialSession,
  stripNavigationParams,
} from './sessionDeepLink.mjs';
import { useProofSession } from './stores/proofSession';
import { useSessions } from './stores/sessions';
import { useProjects } from './stores/projects';
import { useDesktop } from './stores/desktop';
import { countRunning, finishedRuns, indexById } from './lib/desktopActivity.js';
import type { SessionSummary } from './lib/types';
import { useModel } from './stores/model';
import { useProofStream } from './hooks/useProofStream';
import { useLayout, type View } from './hooks/useLayout';
import {
  type ApprovalDecision,
  type ChatMessage,
  type CodeStep,
  type PendingApproval,
  type RunStatus,
  type SessionDetail,
  type StatusEvent,
  type Formalization,
  type EditorDraftBase,
  type FileWriteResult,
  type GraphNode,
  type Project,
  createRun,
  createSessionInProject,
  getFormalization,
  listProjectFormalizations,
  interruptRun,
  resumeInterruptedRun,
  prepareOverleafSource,
  continueOverleafRun,
  leanCheckSession,
  submitApproval,
  verifySession,
  writeSessionFile,
  updateSessionTitle,
  RevisionConflictError,
} from './lib/api';

const SELECTED_SESSION_KEY = 'lea:selectedSessionId';

// Shell (docs/PLAN-ui-redesign.md): whether a project started from the launch
// screen opens with `/blueprint` instead of the statement. See the
// TODO(blueprint-package) in handleStartProject.
const START_WITH_BLUEPRINT = false;

// Views the desktop shell's menu may jump to without a selected session/project.
const DESKTOP_MENU_VIEWS: ReadonlySet<string> = new Set(['main', 'stats', 'settings', 'projects-hub']);

export default function App() {
  // Session list + selection now live in the sessions store (R3).
  const sessions = useSessions((s) => s.sessions);
  const selectedSessionId = useSessions((s) => s.selectedSessionId);
  const setSelectedSessionId = useSessions((s) => s.setSelectedSessionId);
  const refreshSessions = useSessions((s) => s.refreshSessions);
  // F1: projects store — the list, the open project's detail, and open/close.
  const currentProject = useProjects((s) => s.currentProject);
  const currentProjectDeleted = useProjects((s) => s.currentProjectDeleted);
  const refreshProjects = useProjects((s) => s.refreshProjects);
  const openProject = useProjects((s) => s.openProject);
  const closeProject = useProjects((s) => s.closeProject);
  const createAndOpenProject = useProjects((s) => s.createAndOpen);
  // Redesign (shell): the project whose home is showing while its detail loads.
  const selectedProjectId = useProjects((s) => s.selectedProjectId);
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  // F9: ⌘K global search overlay — the only path to a sidebar-hidden project session.
  const [searchOpen, setSearchOpen] = useState(false);
  // messages + statusEvents (chat thread content) now live in the proofSession
  // store (R1c-2a): App writes them; ChatThread reads them + derives the timeline.
  const setMessages = useProofSession((s) => s.setMessages);
  const setStatusEvents = useProofSession((s) => s.setStatusEvents);
  // codeSteps + codeIndex (canvas snapshots + stepper) now live in the
  // proofSession store (R1c): App writes them; Canvas reads them directly.
  const codeSteps = useProofSession((s) => s.codeSteps);
  const setCodeSteps = useProofSession((s) => s.setCodeSteps);
  const setCodeIndex = useProofSession((s) => s.setCodeIndex);
  // Run lifecycle + approvals (M13/M16) now live in the proofSession store
  // (R1c-2b): App drives them; ChatThread/Canvas read them directly. App still
  // reads several here for its handlers (guards, the run to act on, pending
  // approval). runStatusById is write-only from App.
  const isRunning = useProofSession((s) => s.isRunning);
  const setIsRunning = useProofSession((s) => s.setIsRunning);
  const currentRunId = useProofSession((s) => s.currentRunId);
  const setCurrentRunId = useProofSession((s) => s.setCurrentRunId);
  const runStatus = useProofSession((s) => s.runStatus);
  const setRunStatus = useProofSession((s) => s.setRunStatus);
  const setRunStatusById = useProofSession((s) => s.setRunStatusById);
  const setRunResultKindById = useProofSession((s) => s.setRunResultKindById);
  const setRunFocusById = useProofSession((s) => s.setRunFocusById);
  const approvals = useProofSession((s) => s.approvals);
  const setApprovals = useProofSession((s) => s.setApprovals);
  const approvalBusy = useProofSession((s) => s.approvalBusy);
  const setApprovalBusy = useProofSession((s) => s.setApprovalBusy);
  // error (chat error banner) now lives in the proofSession store (R1b); App
  // sets it, ChatThread reads it.
  const setError = useProofSession((s) => s.setError);
  const [draft, setDraft] = useState('');
  const [submissionBusy, setSubmissionBusy] = useState(false);
  const [restoringContext,setRestoringContext]=useState(true);
  const [restorationFailed, setRestorationFailed] = useState(false);
  const restorationStarted=useRef(false);
  const [sourceConnection, setSourceConnection] = useState<{ targetId: string; label: string; state: 'preparing' | 'connected' | 'error' } | null>(null);
  const submitting = useRef(false);
  const submitChat = useRef(createChatSubmission({ prepare: prepareOverleafSource, create: createRun }));
  const submitCheckpoint = useRef(createCheckpointSubmission({ prepare: prepareOverleafSource, create: continueOverleafRun }));
  useEffect(() => { setSourceConnection(null); }, [selectedSessionId]);
  // View/render UI state (page, sidebar/canvas collapse, canvas resize) lives in
  // the useLayout hook now (R5).
  const {
    view,
    setView: setLayoutView,
    canvasCollapsed,
    setCanvasCollapsed,
    sidebarCollapsed,
    setSidebarCollapsed,
    canvasWidth,
    dragging,
    setDragging,
    mainAreaRef,
  } = useLayout();
  const setView = (next: Parameters<typeof setLayoutView>[0]) => {
    useProjects.getState().invalidateNavigation();
    setLayoutView(next);
  };
  // Desktop shell (apps/lea-desktop): native menu items such as Settings… (⌘,)
  // navigate the UI through the preload bridge, and the root element is tagged
  // so the stylesheet can reserve the macOS title-bar strip. No-op in a browser.
  useEffect(() => {
    const bridge = window.leaDesktop;
    if (!bridge) return undefined;
    document.documentElement.classList.add('lea-desktop');
    if (bridge.platform === 'darwin') document.documentElement.classList.add('lea-desktop-mac');
    const unsubscribeNavigate = bridge.onNavigate?.(({ view: next }) => {
      if (DESKTOP_MENU_VIEWS.has(next)) setView(next as View);
    });

    // Auto-update state → the sidebar's update button (stores/desktop).
    const setUpdate = useDesktop.getState().setUpdate;
    bridge.getUpdateState?.().then((state) => state && setUpdate(state)).catch(() => {});
    const unsubscribeUpdate = bridge.onUpdate?.((state) => setUpdate(state));

    // Dock badge and finished-run notifications, derived from the session list
    // the sidebar already follows. The shell decides whether a notification is
    // shown (not while the window is in front).
    let previous: ReturnType<typeof indexById> | null = null;
    let lastRunning = -1;
    const applySessions = (list: SessionSummary[]) => {
      const running = countRunning(list);
      if (running !== lastRunning) {
        lastRunning = running;
        bridge.setActivity?.({ running }).catch(() => {});
      }
      for (const finished of finishedRuns(previous, list)) bridge.notify?.(finished).catch(() => {});
      previous = indexById(list);
    };
    applySessions(useSessions.getState().sessions);
    const unsubscribeSessions = useSessions.subscribe((state, prior) => {
      if (state.sessions !== prior.sessions) applySessions(state.sessions);
    });

    return () => {
      unsubscribeNavigate?.();
      unsubscribeUpdate?.();
      unsubscribeSessions();
    };
  }, []);
  // editedPath (M20 canvas-edit nudge) now lives in the proofSession store
  // (v2.0.1 R1a): App sets it; ChatThread reads it straight from the store.
  const setEditedPath = useProofSession((s) => s.setEditedPath);
  // safeVerify (persisted SafeVerify verdict, survives reload via
  // session_detail.safe_verify; M24) now lives in the proofSession store (R1b);
  // App sets it, Canvas reads it.
  const setSafeVerify = useProofSession((s) => s.setSafeVerify);
  const setVerifySurface = useProofSession((s) => s.setVerifySurface);
  const setGoalSurface = useProofSession((s) => s.setGoalSurface);
  const formalizations = useProofSession((s) => s.formalizations);
  const setFormalizations = useProofSession((s) => s.setFormalizations);
  const formalizationScope = useProofSession((s) => s.formalizationScope);
  const setFormalizationScope = useProofSession((s) => s.setFormalizationScope);
  const composerScopeOverride = useProofSession((s) => s.composerScopeOverride);
  const setComposerScopeOverride = useProofSession((s) => s.setComposerScopeOverride);
  useEffect(() => {
    if (sourceConnection && sourceConnection.targetId !== (composerScopeOverride || formalizationScope)) {
      setSourceConnection(null);
    }
  }, [composerScopeOverride, formalizationScope, sourceConnection]);
  const currentFormalizationSnapshot = useProofSession(
    (s) => s.currentFormalizationSnapshot,
  );
  const setCurrentFormalizationSnapshot = useProofSession(
    (s) => s.setCurrentFormalizationSnapshot,
  );
  const bumpFormalizationRefresh = useProofSession(
    (s) => s.bumpFormalizationRefresh,
  );
  const setCanvasRevisionMode = useProofSession(
    (s) => s.setCanvasRevisionMode,
  );
  // Model state (active model, catalog, featured, key-missing) lives in the model
  // store (R4); ChatThread reads it directly. App only kicks off the startup load
  // (in the restore effect) + re-sync on returning from Settings.

  const selectedSession = useMemo(
    () => sessions.find((s) => s.id === selectedSessionId),
    [sessions, selectedSessionId],
  );
  const title = selectedSession?.title || (restoringContext ? 'Restoring session…' : restorationFailed ? 'Session unavailable' : 'New theorem session');

  const sortedCode = useMemo(() => sortCodeSteps(codeSteps), [codeSteps]);
  const pendingApproval = approvals.find((a) => !a.decision && a.actionable !== false);

  // The run EventSource lifecycle + session-detail hydration live in a hook now
  // (R2); it reads the proofSession + sessions stores directly.
  const { attachStream, reconcile, closeStream, loadSession: loadOwnedSession, captureSelection } = useProofStream();

  useEffect(() => {
    const restore = async () => {
      const loaded = await refreshSessions();
      useModel.getState().syncFromSettings();
      useModel.getState().loadCatalog();
      // The Overleaf extension's "View in Lea UI" action opens <ui>/?session=<id>;
      // that deep-link takes precedence over the last-opened session.
      await restoreInitialSession({
        search: window.location.search,
        savedId: window.localStorage.getItem(SELECTED_SESSION_KEY),
        sessions: loaded,
        loadSession,
        loadFormalization: getFormalization,
        selectFormalization: (id: string) => {
          setFormalizationScope(id);
          setCanvasCollapsed(false);
        },
        openProject: async (id: string) => {
          const project = await openProject(id);
          if (project) setView('project-home');
          return project;
        },
        onRestored: () => {
          // Consume the link only after its session or project loaded successfully.
          const cleaned = stripNavigationParams(window.location.search);
          window.history.replaceState(
            {},
            '',
            `${window.location.pathname}${cleaned}${window.location.hash}`,
          );
        },
      });
      // Project first: a restored session opens the workbench; nothing restored
      // (and no project deep-link) lands on the launch screen. Only the two
      // start views are settled, so a click made during the restore stands, and
      // the raw setter leaves any project load the user started in flight.
      const restored = Boolean(useSessions.getState().selectedSessionId);
      const projectOpened = Boolean(useProjects.getState().selectedProjectId);
      setLayoutView((current) => settleRestoredView(current, { restored, projectOpened }) as View);
    };
    if(restorationStarted.current)return;
    restorationStarted.current=true;
    restore().catch((err) => {
      setRestorationFailed(true);
      setError(`Could not restore LeaChat: ${err instanceof Error ? err.message : String(err)} Retry loading to open this session.`);
    })
      .finally(()=>setRestoringContext(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // One durable observer follows the watermark of a committed list snapshot.
  // Refreshing data does not choose a conversation, file, or revision.
  useEffect(() => observeSessionList({
    refresh: refreshSessions,
    readCursor: () => useSessions.getState().cursor,
    onChanged: () => {
      bumpFormalizationRefresh();
      const selected=useSessions.getState().selectedSessionId;
      if (selected && !useProofSession.getState().isRunning) {
        const owned = captureSelection(false);
        void reconcile(selected).catch((err) => {
          if (owned()) setError(`Could not refresh LeaChat: ${err instanceof Error ? err.message : String(err)} Reload this page to retry.`);
        });
      }
    },
  }), []);

  const loadSession = async (sessionId: string) => {
    setRestorationFailed(false);
    const detail = await loadOwnedSession(sessionId);
    if (detail) window.localStorage.setItem(SELECTED_SESSION_KEY, detail.id);
    return detail;
  };

  // Redesign (shell): creating a project lands on its home; the legacy project
  // window stays reachable from that page's ⋯ menu (view 'project').
  const handleCreateProject = async (title: string, description?: string) => {
    const project = await createAndOpenProject(title, description);
    setNewProjectOpen(false);
    if (project) setView('project-home');
  };

  // F3: start a proof inside the open project — create a project session (working
  // dir = the project repo, server-side), then run it like a normal submit and drop
  // the user into Chat+Canvas. The project stays selected (sidebar highlight).
  const handleStartProjectProof = async (message: string) => {
    const content = message.trim();
    if (!content || !currentProject) return;
    setError(undefined);
    try {
      resetForNewSession(); // clear the proof view for the fresh session
      const run = await createRun(
        content,
        undefined,
        useModel.getState().model,
        {
          project_slug: currentProject.slug,
          project_title: currentProject.title,
          project_namespace: currentProject.namespace,
          new_formalization: { display_title: content.slice(0, 120) },
        },
      );
      setSelectedSessionId(run.session_id);
      setCurrentRunId(run.run_id);
      setRunStatus('running');
      setRunStatusById((prev) => ({ ...prev, [run.run_id]: 'running' }));
      setRunResultKindById((prev) => ({ ...prev, [run.run_id]: null }));
      setRunFocusById((prev) => ({
        ...prev,
        [run.run_id]: run.focus_formalization_id,
      }));
      setIsRunning(true);
      setMessages([run.message]);
      setFormalizations(run.formalization ? [run.formalization] : []);
      setFormalizationScope(run.focus_formalization_id || 'new');
      window.localStorage.setItem(SELECTED_SESSION_KEY, run.session_id);
      setView('main');
      await refreshSessions();
      attachStream(run.run_id, run.session_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to start the proof.');
    }
  };

  // ── Redesign (shell package): project-first launches ──────────────────────
  // The run-start mechanics of handleStartProjectProof, lifted so the launch
  // screen and the project home can start a run in a session they created
  // (createSessionInProject) with a first message that is already known.
  const launchProjectRun = async (
    project: Project,
    sessionId: string,
    content: string,
    scope: {
      focus_formalization_id?: string;
      new_formalization?: { display_title: string; kind?: string; declaration_name?: string; statement?: string };
    },
  ) => {
    resetForNewSession(); // clear the proof view for the fresh session
    const run = await createRun(content, sessionId, useModel.getState().model, {
      project_slug: project.slug,
      project_title: project.title,
      project_namespace: project.namespace,
      ...scope,
    });
    setSelectedSessionId(run.session_id);
    setCurrentRunId(run.run_id);
    setRunStatus('running');
    setRunStatusById((prev) => ({ ...prev, [run.run_id]: 'running' }));
    setRunResultKindById((prev) => ({ ...prev, [run.run_id]: null }));
    setRunFocusById((prev) => ({ ...prev, [run.run_id]: run.focus_formalization_id }));
    setIsRunning(true);
    setMessages([run.message]);
    setFormalizations(run.formalization ? [run.formalization] : []);
    setFormalizationScope(run.focus_formalization_id || 'new');
    window.localStorage.setItem(SELECTED_SESSION_KEY, run.session_id);
    setView('main');
    await refreshSessions();
    attachStream(run.run_id, run.session_id);
  };

  // "What do you want to prove?": a statement becomes a project named from it
  // (the statement itself is the description), a session in that project, and
  // the first message, sent through the composer's own submit path
  // (handleSubmit: slash commands, scope, idempotent admission). Errors before
  // the workbench opens propagate so the launch screen shows them by its box.
  const startingProject = useRef(false);
  const queuedFirstMessage = useRef<{ sessionId: string; content: string } | null>(null);
  const handleStartProject = async (statement: string) => {
    const content = statement.trim();
    if (!content || startingProject.current) return;
    setError(undefined);
    startingProject.current = true;
    try {
      const project = await createAndOpenProject(projectTitleFromStatement(content), content);
      if (!project) return; // navigation moved on while the project was created
      const session = await createSessionInProject(project.id, project.title);
      await refreshSessions();
      const detail = await loadSession(session.id);
      if (!detail) return;
      setView('main');
      // TODO(blueprint-package): flip START_WITH_BLUEPRINT (top of this file)
      // once `/blueprint` and the workbench `pendingSubmission` consumer are
      // merged: the first message is then `/blueprint` (no arguments; it drafts
      // the decomposition from the project's title and description, which hold
      // the statement) instead of the bare statement.
      const firstMessage = START_WITH_BLUEPRINT ? '/blueprint' : content;
      // A fresh project has nothing to scope to: record the statement as a new
      // formalization, as the legacy project window's start did.
      if (!START_WITH_BLUEPRINT) setComposerScopeOverride('new');
      queuedFirstMessage.current = { sessionId: session.id, content: firstMessage };
      setDraft(firstMessage);
    } finally {
      startingProject.current = false;
    }
  };
  // Send the queued first message once its session is selected and the
  // composer holds it; drop it if the user went to another session first.
  useEffect(() => {
    const queued = queuedFirstMessage.current;
    if (!queued || restoringContext) return;
    if (selectedSessionId !== queued.sessionId) {
      queuedFirstMessage.current = null;
      return;
    }
    if (draft !== queued.content) return;
    queuedFirstMessage.current = null;
    void handleSubmit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedSessionId, draft, restoringContext]);

  // "Prove" / "Prove the sorry" / "Retry" on the project home: a session bound
  // to the blueprint node whose first message names the node, its declaration,
  // file and statement. An existing formalization for the declaration is
  // focused; otherwise one is recorded for the node.
  const handleProveNode = async (node: GraphNode, mode: ProveMode) => {
    const project = currentProject;
    if (!project || submitting.current) return;
    submitting.current = true;
    try {
      const session = await createSessionInProject(project.id, node.key);
      useWorkbench.getState().bindSessionToNode(session.id, node.key);
      useWorkbench.getState().selectNode(node.key);
      let existingId: string | undefined;
      if (node.lean) {
        try {
          const { formalizations: items } = await listProjectFormalizations(project.id);
          existingId = items.find((item) => item.declaration_name === node.lean)?.id;
        } catch {
          // No list: record a new formalization for the node below.
        }
      }
      await launchProjectRun(project, session.id, proveNodeMessage(node, mode), existingId
        ? { focus_formalization_id: existingId }
        : {
          new_formalization: {
            display_title: node.key,
            kind: node.kind ?? undefined,
            declaration_name: node.lean ?? undefined,
            statement: node.statement || undefined,
          },
        });
    } finally {
      submitting.current = false;
    }
  };

  // "New session" on the project home: an empty session in the project, opened
  // in the workbench, so the first submit lands in the project (nothing unfiled).
  const handleNewProjectSession = async () => {
    const project = currentProject;
    if (!project) return;
    setError(undefined);
    try {
      const session = await createSessionInProject(project.id);
      await refreshSessions();
      setView('main');
      await loadSession(session.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to start a session.');
    }
  };

  // The workbench shows a project surface (blueprint, files rail) through the
  // selected session's project, so one of the project's sessions must be
  // selected: the current one if it belongs there, else the latest, else a new
  // one titled after the project.
  const ensureProjectSession = async (project: Project): Promise<void> => {
    const state = useSessions.getState();
    const selected = state.sessions.find((s) => s.id === state.selectedSessionId);
    if (selected?.project_id === project.id) return;
    const latest = state.sessions
      .filter((s) => s.project_id === project.id && !s.parent_id)
      .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))[0];
    if (latest) {
      await loadSession(latest.id);
      return;
    }
    const created = await createSessionInProject(project.id, project.title);
    await refreshSessions();
    await loadSession(created.id);
  };

  const openProjectSurface = async (projectId: string, surface: { path?: string; rail?: boolean }) => {
    setError(undefined);
    try {
      const project = currentProject?.id === projectId ? currentProject : await openProject(projectId);
      if (!project) return;
      await ensureProjectSession(project);
      const workbench = useWorkbench.getState();
      if (surface.path) workbench.openFile(surface.path);
      if (surface.rail) workbench.setFilesRailOpen(true);
      setView('main');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  // Open a project's home (the redesign's Overview). The legacy project window
  // (view 'project') stays reachable from that page's ⋯ menu.
  const openProjectHome = (projectId: string) => {
    openProject(projectId)
      .then((detail) => { if (detail) setView('project-home'); })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  };

  const handleStartProjectFormalization = async (
    formalization: Formalization,
    message?: string,
    reformalize = false,
  ) => {
    if (!currentProject) return;
    const content = (
      message
      || `Continue working on ${formalization.declaration_name || formalization.display_title}.`
    ).trim();
    setError(undefined);
    let ownsQueuedSelection: (() => boolean) | undefined;
    try {
      const retrySession = reformalize
        ? (formalization.sessions.find(session => session.id === selectedSessionId)?.id || formalization.sessions[0]?.id)
        : undefined;
      if (retrySession) {
        if (!await loadSession(retrySession)) return;
      } else {
        resetForNewSession();
      }
      const owned = captureSelection(false);
      const run = await createRun(
        content,
        retrySession,
        useModel.getState().model,
        {
          project_slug: currentProject.slug,
          project_title: currentProject.title,
          project_namespace: currentProject.namespace,
          focus_formalization_id: formalization.id,
          ...(reformalize ? { artifact_operation: 'reformalize' as const } : {}),
        },
        operation => {
          if (!owned()) return;
          setSelectedSessionId(operation.session_id);
          setFormalizations([{ ...formalization, reformalization: {
            id: operation.reformalization_id, state: 'queued', has_drafts: false } }]);
          setFormalizationScope(formalization.id);
          setMessages(previous => [...(retrySession === operation.session_id ? previous : []), { id: `queued:${operation.reformalization_id}`, session_id: operation.session_id,
            role: 'assistant', content: 'Reformalization queued. It will start when this project is available.', created_at: new Date().toISOString() }]);
          window.localStorage.setItem(SELECTED_SESSION_KEY, operation.session_id);
          setView('main');
          ownsQueuedSelection = captureSelection(false);
        },
        () => (ownsQueuedSelection || owned)(),
      );
      if (!run.run_id || !(ownsQueuedSelection || owned)()) return;
      if (reformalize) {
        const detail = await loadSession(run.session_id);
        if (detail) {
          setFormalizationScope(formalization.id);
          setView('main');
          await refreshSessions();
        }
        return;
      }
      setSelectedSessionId(run.session_id);
      setCurrentRunId(run.run_id);
      setRunStatus('running');
      setRunStatusById({ [run.run_id]: 'running' });
      setRunResultKindById({ [run.run_id]: null });
      setRunFocusById({ [run.run_id]: run.focus_formalization_id });
      setIsRunning(true);
      setMessages([run.message]);
      setFormalizations([formalization]);
      setFormalizationScope(formalization.id);
      window.localStorage.setItem(SELECTED_SESSION_KEY, run.session_id);
      setView('main');
      await refreshSessions();
      attachStream(run.run_id, run.session_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to start the conversation.');
    }
  };

  // F3: open an existing project session into the normal Chat+Canvas view.
  const handleOpenProjectSession = (sessionId: string) => {
    setView('main');
    loadSession(sessionId).catch((err) => setError(err instanceof Error ? err.message : String(err)));
  };

  // F9: open a search hit — leave any project window, drop into its Chat+Canvas.
  // Works for loose and project sessions alike (loadSession is keyed only by id).
  const handleOpenSearchResult = (sessionId: string) => {
    closeProject();
    setView('main');
    loadSession(sessionId).catch((err) => setError(err instanceof Error ? err.message : String(err)));
  };

  const handleOpenFormalization = async (formalizationId: string) => {
    const owned = captureSelection();
    try {
      const formalization = await getFormalization(formalizationId);
      if (!owned()) return;
      const recentSession = formalization.sessions[0];
      if (recentSession) {
        closeProject();
        setView('main');
        if (!await loadSession(recentSession.id)) return;
        setFormalizationScope(formalization.id);
        setCanvasCollapsed(false);
      } else if (formalization.project_id) {
        if (await openProject(formalization.project_id)) setView('project-home');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to open formalization.');
    }
  };

  const handleRenameSession = async (nextTitle: string) => {
    if (!selectedSessionId) return;
    await updateSessionTitle(selectedSessionId, nextTitle);
    await refreshSessions();
  };

  // F9: ⌘K (and Ctrl+K) toggles the global search overlay, from any view.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setSearchOpen((v) => !v);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const resetForNewSession = () => {
    setRestorationFailed(false);
    closeStream();
    setSelectedSessionId(undefined);
    // One call, not a hand-maintained list of setters. The list version silently
    // rotted: every slice added to the store had to be remembered here too, and the
    // ones that weren't stayed glued to the screen across "New session" — a
    // `step_error` card from the previous session survived until a manual refresh,
    // and the sub-agent maps had the same hole. This merge is the proof: upstream
    // grew the same list from 18 setters to 23 while I was removing it, and all five
    // additions (formalizations, scope, composer override, snapshot, revision mode)
    // are covered by SESSION_SCOPED without anyone having to remember them here.
    // `setDraft` stays: the composer draft is App state, not store state.
    useProofSession.getState().resetSessionScoped();
    setDraft('');
    window.localStorage.removeItem(SELECTED_SESSION_KEY);
  };

  const handleSubmit = async (general = false) => {
    const content = draft.trim();
    if (!content || restoringContext || isRunning || submitting.current) return;
    setError(undefined);
    setEditedPath(undefined);
    setApprovals((prev) => prev.filter((a) => a.decision));

    // Slash command? Dispatch through the command framework instead of starting a run.
    // 'action' commands (e.g. /compact) do their work and return; 'prompt' commands fall
    // through to a normal run with their expanded template.
    const parsed = parseSlashCommand(content);
    if (parsed) {
      setDraft('');
      try {
        const dispatch = await runSlashCommand(parsed.name, {
          sessionId: selectedSessionId,
          args: parsed.args,
        });
        if (!dispatch.handled) {
          setError(`Unknown command: /${parsed.name}`);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : `Failed to run /${parsed.name}.`);
      }
      return;
    }

    submitting.current = true;
    setSubmissionBusy(true);
    let sourceTarget: { id: string; declaration_name?: string | null; display_title: string } | undefined;
    try {
      let scopeCandidates = formalizations;
      if (!composerScopeOverride && selectedSession?.project_id) {
        try {
          const projectItems = await listProjectFormalizations(selectedSession.project_id);
          const byId = new Map(
            [...formalizations, ...projectItems.formalizations].map((item) => [item.id, item]),
          );
          scopeCandidates = [...byId.values()];
        } catch {
          // Scope inference remains useful with the session-local list when the
          // project list is temporarily unavailable.
        }
      }
      const resolvedScope =
        composerScopeOverride
        || inferComposerFormalizationScope({
          message: content,
          formalizations: scopeCandidates,
          viewedScope: formalizationScope,
        });
      setFormalizationScope(resolvedScope);
      setCanvasRevisionMode('current');
      const selectedTarget = scopeCandidates.find((item) => item.id === resolvedScope);
      if (selectedTarget?.origin === 'overleaf' && !general) sourceTarget = selectedTarget;
      else setSourceConnection(null);
      const scopeIsCurrent = () => {
        const state = useProofSession.getState();
        return useSessions.getState().selectedSessionId === selectedSessionId
          && state.formalizationScope === resolvedScope && state.composerScopeOverride === composerScopeOverride;
      };
      const run = await submitChat.current({
        message: content, sessionId: selectedSessionId, model: useModel.getState().model,
        scope: resolvedScope, formalizations: scopeCandidates, general,
        isCurrent: scopeIsCurrent,
        onPreparing: (item: { id: string; declaration_name?: string | null; display_title: string }) => setSourceConnection({
          targetId: item.id, label: item.declaration_name || item.display_title, state: 'preparing'
        })
      });
      // Admission can finish after navigation. Keep the run in its own chat.
      if (!scopeIsCurrent()) {
        void refreshSessions().catch(() => {});
        return;
      }
      if (sourceTarget) setSourceConnection({ targetId: sourceTarget.id, label: sourceTarget.declaration_name || sourceTarget.display_title, state: 'connected' });
      setSelectedSessionId(run.session_id);
      setCurrentRunId(run.run_id);
      setRunStatus('running');
      setRunStatusById((prev) => ({ ...prev, [run.run_id]: 'running' }));
      setRunResultKindById((prev) => ({ ...prev, [run.run_id]: null }));
      setRunFocusById((prev) => ({
        ...prev,
        [run.run_id]: run.focus_formalization_id,
      }));
      setIsRunning(true);
      setMessages((current) => [...current, run.message]);
      if (run.formalization) {
        setFormalizations([
          ...formalizations.filter((item) => item.id !== run.formalization!.id),
          run.formalization,
        ]);
      }
      if (run.focus_formalization_id) {
        setFormalizationScope(run.focus_formalization_id);
      }
      setComposerScopeOverride(null);
      setDraft((value) => value.trim() === content ? '' : value);
      window.localStorage.setItem(SELECTED_SESSION_KEY, run.session_id);
      attachStream(run.run_id, run.session_id);
      void refreshSessions().catch(() => {});
    } catch (err) {
      if (useSessions.getState().selectedSessionId === selectedSessionId) {
        setIsRunning(false);
        setCurrentRunId(undefined);
        setError(err instanceof Error ? err.message : 'Unable to start Lea.');
        if (sourceTarget) setSourceConnection({ targetId: sourceTarget.id, label: sourceTarget.declaration_name || sourceTarget.display_title, state: 'error' });
      }
    } finally {
      submitting.current = false;
      setSubmissionBusy(false);
    }
  };

  const handleDecide = async (decision: ApprovalDecision) => {
    if (!currentRunId || !pendingApproval || approvalBusy) return;
    setApprovalBusy(true);
    try {
      await submitApproval(currentRunId, pendingApproval.approval_id, decision);
    } catch (err) {
      setApprovalBusy(false);
      setError(err instanceof Error ? err.message : 'Unable to submit decision.');
    }
  };

  const handleInterrupt = async () => {
    if (!currentRunId) return;
    try {
      await interruptRun(currentRunId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to interrupt the run.');
    }
  };

  const selectStep = (idx: number) => {
    setCodeIndex(idx);
    setCanvasRevisionMode('historical');
    setCanvasCollapsed(false);
  };

  // Publication owns the loaded draft's scope and preconditions. Checking has
  // its own receipt/lifecycle and cannot turn an acknowledged save into a failure.
  const handleSaveAndCheck = async (
    content: string,
    path: string,
    base: EditorDraftBase,
    draftId: string,
  ): Promise<FileWriteResult> => {
    const selectionOwned = captureSelection();
    const owned = () => selectionOwned() && useSessions.getState().selectedSessionId === base.session_id;
    try {
      const publication = await writeSessionFile(base.session_id, path, content, 'Manual edit from the canvas.',
        base.formalization_id, base.formalization_revision, base.file_revision, draftId);
      if (owned()) { setEditedPath(path); setSafeVerify(null); bumpFormalizationRefresh(); }
      return publication;
    } catch (err) {
      if (owned() && err instanceof RevisionConflictError) {
        setError(err.message);
      }
      throw err;
    }
  };

  const handleCheckPublished = async (target: string, base: EditorDraftBase): Promise<CheckOutcome> => {
    const selectionOwned = captureSelection();
    const owned = () => selectionOwned() && useSessions.getState().selectedSessionId === base.session_id;
    if (owned()) useProofSession.setState({ manualCheckSessionId: base.session_id });
    let result;
    try {
      result = await leanCheckSession(base.session_id, target, base.formalization_id, base.file_revision);
    } finally {
      if (owned() && useProofSession.getState().manualCheckSessionId === base.session_id) {
        useProofSession.setState({ manualCheckSessionId: undefined });
      }
    }
    if (!owned()) return result;
    await reconcile(base.session_id);
    await refreshSessions();
    if (!owned()) return result;
    setEditedPath(target); // after reconcile (which clears it) — surface the nudge
    setSafeVerify(null); // the edit invalidates any prior SafeVerify verdict
    setCanvasRevisionMode('current');
    bumpFormalizationRefresh();
    return result;
  };

  const handleVerify = async (path?: string): Promise<CheckOutcome> => {
    const target = path ?? sortedCode[sortedCode.length - 1]?.path;
    if (!selectedSessionId) return { status: 'error', detail: 'No session.' };
    const owned = captureSelection();
    const focusId =
      formalizationScope === 'project' || formalizationScope === 'new'
        ? undefined : formalizationScope;
    const result = await verifySession(selectedSessionId, target, focusId);
    if (!owned()) return result;
    setSafeVerify({ status: result.status, detail: result.detail, current: result.current });
    bumpFormalizationRefresh();
    return result;
  };

  // F9: the overlay rides alongside every view so ⌘K works from anywhere.
  // E0e: `/skills` and `/mcp` open this over whatever view is showing, so it is shared
  // alongside `searchOverlay` rather than living inside one page.
  const skillsMcpPickerKind = useFactories((s) => s.skillsMcpPicker);
  const setSkillsMcpPicker = useFactories((s) => s.setSkillsMcpPicker);
  const skillsMcpPicker =
    skillsMcpPickerKind && selectedSession?.id ? (
      <SkillsMcpPicker
        kind={skillsMcpPickerKind}
        sessionId={selectedSession.id}
        onClose={() => setSkillsMcpPicker(null)}
      />
    ) : null;

  const searchOverlay = (
    <SearchOverlay
      open={searchOpen}
      onClose={() => setSearchOpen(false)}
      onOpenSession={handleOpenSearchResult}
      onOpenFormalization={handleOpenFormalization}
    />
  );

  // The new-project dialog must be mountable from any view that can trigger it (the main
  // shell AND the Projects hub) — otherwise `setNewProjectOpen(true)` from the hub sets
  // state with nothing rendered to show it. Shared like `searchOverlay`.
  const newProjectDialog = (
    <NewProjectDialog
      open={newProjectOpen}
      onClose={() => setNewProjectOpen(false)}
      onCreate={handleCreateProject}
    />
  );

  // Redesign (docs/PLAN-ui-redesign.md): the project-first shell. The sidebar
  // is shared by the workbench, the launch screen (`home`) and the project
  // overview (`project-home`); sessions open through handleOpenProjectSession,
  // project surfaces (blueprint, files) through openProjectSurface.
  // There are no loose sessions: the workbench with nothing selected (Back from
  // Settings, the desktop menu, a session that did not restore) is the launch
  // screen. While restoring, or when it failed, the workbench keeps its notice.
  // A project home with no project to show is the launch screen too.
  const projectHomeId = currentProject?.id ?? selectedProjectId;
  const shownView = resolveShownView(view, {
    selectedSessionId,
    restoring: restoringContext,
    restorationFailed,
    projectHomeId,
    projectDeleted: currentProjectDeleted,
  }) as View;
  const sidebar = (
    <Sidebar
      runningSessionId={isRunning ? selectedSessionId : undefined}
      activeView={shownView}
      onSelectSession={handleOpenProjectSession}
      onNewProject={() => setNewProjectOpen(true)}
      onOpenHome={() => setView('home')}
      onOpenProject={openProjectHome}
      onOpenBlueprint={(projectId) => void openProjectSurface(projectId, { path: BLUEPRINT_PATH })}
      onOpenFiles={(projectId) => void openProjectSurface(projectId, { rail: true })}
      onOpenSkills={() => setView('skills')}
      onOpenSubagents={() => setView('subagents')}
      onOpenMcp={() => setView('mcp')}
      onOpenTools={() => setView('tools')}
      onOpenSearch={() => setSearchOpen(true)}
      onOpenSettings={() => setView('settings')}
      onOpenStats={() => setView('stats')}
      onCollapse={() => setSidebarCollapsed(true)}
    />
  );
  // A full-page shell view beside the sidebar, with the shared overlays.
  const shellFrame = (content: ReactElement) => (
    <div className="lea-app">
      {searchOverlay}
      {skillsMcpPicker}
      {newProjectDialog}
      <div className={`app ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
        {sidebar}
        {content}
      </div>
    </div>
  );

  if (shownView === 'home')
    return shellFrame(
      <Home
        onOpenProject={openProjectHome}
        onStartProject={handleStartProject}
        onNewProject={() => setNewProjectOpen(true)}
        onOpenSession={handleOpenProjectSession}
        startsWithBlueprint={START_WITH_BLUEPRINT}
        sidebarCollapsed={sidebarCollapsed}
        onExpandSidebar={() => setSidebarCollapsed(false)}
      />,
    );
  if ((view === 'project-home' || view === 'project') && currentProjectDeleted)
    return <main className="p-8"><button onClick={() => { closeProject(); setView('home'); }}>Back to projects</button>
      <h1>Project deleted</h1><p>This project is no longer available.</p></main>;
  if (view === 'project-home' && projectHomeId)
    return shellFrame(
      <ProjectHome
        projectId={projectHomeId}
        onOpenSession={handleOpenProjectSession}
        onNewSession={() => void handleNewProjectSession()}
        onOpenBlueprint={() => void openProjectSurface(projectHomeId, { path: BLUEPRINT_PATH })}
        onOpenFiles={(path) => void openProjectSurface(projectHomeId, { path, rail: true })}
        onProveNode={handleProveNode}
        onOpenLegacy={() => setView('project')}
        onOpenHome={() => setView('home')}
        sidebarCollapsed={sidebarCollapsed}
        onExpandSidebar={() => setSidebarCollapsed(false)}
      />,
    );
  // The pre-redesign project window, reachable from the project home's ⋯ menu
  // ("Legacy project window") until the integrator removes it.
  if (view === 'project' && currentProject)
    return (
      <>
        {searchOverlay}
        {skillsMcpPicker}
        <ProjectWindow
          project={currentProject}
          onBack={() => setView('project-home')}
          onStartProof={handleStartProjectProof}
          onStartFormalization={handleStartProjectFormalization}
          onOpenSession={handleOpenProjectSession}
        />
      </>
    );
  if (view === 'skills')
    return (
      <>
        {searchOverlay}
        {skillsMcpPicker}
        <SkillFactory onBack={() => setView('main')} />
      </>
    );
  if (view === 'subagents')
    return (
      <>
        {searchOverlay}
        {skillsMcpPicker}
        <SubagentFactory onBack={() => setView('main')} />
      </>
    );
  if (view === 'mcp')
    return (
      <>
        {searchOverlay}
        {skillsMcpPicker}
        <McpFactory onBack={() => setView('main')} />
      </>
    );
  if (view === 'tools')
    return (
      <>
        {searchOverlay}
        {skillsMcpPicker}
        <ToolFactory onBack={() => setView('main')} />
      </>
    );
  if (view === 'projects-hub')
    return (
      <>
        {searchOverlay}
        {skillsMcpPicker}
        {newProjectDialog}
        <ProjectsHub
          onBack={() => setView('home')}
          onOpenProject={openProjectHome}
          onNewProject={() => setNewProjectOpen(true)}
        />
      </>
    );
  if (view === 'stats')
    return (
      <>
        {searchOverlay}
        {skillsMcpPicker}
        <StatsPage onBack={() => setView('main')} />
      </>
    );
  if (view === 'settings')
    return (
      <>
        {searchOverlay}
        {skillsMcpPicker}
        <SettingsPage
          onBack={() => {
            setView('main');
            // re-sync model + key state after the user may have added a key
            useModel.getState().syncFromSettings();
          }}
        />
      </>
    );

  return (
    <div className="lea-app">
      {searchOverlay}
        {skillsMcpPicker}
      {newProjectDialog}
      <QuickOpen projectId={selectedSession?.project_id ?? null} />
      <div className={`app ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
        {sidebar}

        <div
          ref={mainAreaRef}
          className={`main-area ${canvasCollapsed ? 'canvas-collapsed' : ''}`}
          style={{ gridTemplateColumns: canvasCollapsed ? '1fr 0' : `minmax(0,1fr) ${canvasWidth}%` }}
        >
          <ChatThread
            title={title}
            sidebarCollapsed={sidebarCollapsed}
            onExpandSidebar={() => setSidebarCollapsed(false)}
            session={selectedSession}
            onSelectSession={(id) => {
              closeProject();
              loadSession(id).catch((err) => setError(err instanceof Error ? err.message : String(err)));
            }}
            onSelectStep={selectStep}
            onDecide={handleDecide}
            onOpenSettings={() => setView('settings')}
            onOpenLibrary={(focus) =>
              setView(focus === 'subagents' ? 'subagents' : focus === 'skills' ? 'skills' : 'mcp')
            }
            draft={draft}
            onDraftChange={setDraft}
            onSubmit={() => handleSubmit()}
            onSubmitGeneral={() => handleSubmit(true)}
            submissionBusy={submissionBusy || restoringContext || restorationFailed}
            restoringSession={restoringContext}
            onRetryRestore={restorationFailed ? () => window.location.reload() : undefined}
            sourceConnection={sourceConnection}
            onInterrupt={handleInterrupt}
            onResumeRun={async (runId, action = 'resume') => {
              if (submitting.current || isRunning) return;
              if (action === 'resume' && !window.confirm('The interrupted model request may already have incurred cost, or a tool may already have acted. Review the saved progress and current files before continuing. Resume in a new run?')) return;
              submitting.current = true;
              setSubmissionBusy(true);
              try {
                let result;
                if (action === 'continue') {
                  const targetId = useProofSession.getState().runFocusById[runId];
                  if (!targetId) throw new Error('Select the Overleaf formalization before resuming.');
                  const target = formalizations.find((item) => item.id === targetId);
                  const viewedScope = useProofSession.getState().formalizationScope;
                  const pinnedScope = useProofSession.getState().composerScopeOverride;
                  setSourceConnection({ targetId, label: target?.declaration_name || target?.display_title || 'Overleaf formalization', state: 'preparing' });
                  result = await submitCheckpoint.current({ runId, targetId,
                    isCurrent: () => useSessions.getState().selectedSessionId === selectedSessionId
                      && useProofSession.getState().formalizationScope === viewedScope
                      && useProofSession.getState().composerScopeOverride === pinnedScope });
                  if (useSessions.getState().selectedSessionId !== selectedSessionId) return;
                  setSourceConnection({ targetId, label: target?.declaration_name || target?.display_title || 'Overleaf formalization', state: 'connected' });
                } else result = await resumeInterruptedRun(runId);
                if (useSessions.getState().selectedSessionId !== selectedSessionId) return;
                await loadSession(result.session_id);
                void refreshSessions().catch(() => {});
              } catch (err) {
                if (useSessions.getState().selectedSessionId === selectedSessionId) {
                  setError(err instanceof Error ? err.message : String(err));
                  setSourceConnection((value) => value ? { ...value, state: 'error' } : null);
                }
              } finally {
                submitting.current = false;
                setSubmissionBusy(false);
              }
            }}
            canvasCollapsed={canvasCollapsed}
            onToggleCanvas={() => setCanvasCollapsed((v) => !v)}
            onRenameSession={handleRenameSession}
          />

          {!canvasCollapsed && (
            <div
              className={`col-resizer ${dragging ? 'dragging' : ''}`}
              style={{ right: `${canvasWidth}%` }}
              onMouseDown={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              title="Drag to resize"
            />
          )}

          <Canvas
            key={`${selectedSessionId}:${formalizationScope}`}
            captureSelection={captureSelection}
            sessionId={selectedSessionId}
            onClose={() => setCanvasCollapsed(true)}
            onSaveAndCheck={handleSaveAndCheck}
            onCheck={handleCheckPublished}
            onVerify={handleVerify}
            onOpenSession={(id) => {
              closeProject();
              loadSession(id).catch((err) =>
                setError(err instanceof Error ? err.message : String(err)),
              );
            }}
          />
        </div>
      </div>
    </div>
  );
}
