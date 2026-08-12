import { useEffect, useMemo, useState } from 'react';
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
import { sortCodeSteps } from './lib/timeline.mjs';
import { inferComposerFormalizationScope } from './lib/formalizations.mjs';
import { parseSlashCommand } from './lib/slashCommands.js';
import { runSlashCommand } from './lib/slashCommandRunner';
import {
  pickInitialSession,
  readDeepLinkFormalizationId,
  stripNavigationParams,
} from './sessionDeepLink.mjs';
import { useProofSession } from './stores/proofSession';
import { useSessions } from './stores/sessions';
import { useProjects } from './stores/projects';
import { useModel } from './stores/model';
import { useProofStream } from './hooks/useProofStream';
import { useLayout } from './hooks/useLayout';
import {
  type ApprovalDecision,
  type ChatMessage,
  type CodeStep,
  type PendingApproval,
  type RunStatus,
  type SessionDetail,
  type StatusEvent,
  type Formalization,
  createRun,
  getSession,
  getFormalization,
  listProjectFormalizations,
  interruptRun,
  leanCheckSession,
  submitApproval,
  verifySession,
  writeSessionFile,
  updateSessionTitle,
  RevisionConflictError,
} from './lib/api';

const SELECTED_SESSION_KEY = 'lea:selectedSessionId';

export default function App() {
  // Session list + selection now live in the sessions store (R3).
  const sessions = useSessions((s) => s.sessions);
  const selectedSessionId = useSessions((s) => s.selectedSessionId);
  const setSelectedSessionId = useSessions((s) => s.setSelectedSessionId);
  const refreshSessions = useSessions((s) => s.refreshSessions);
  // F1: projects store — the list, the open project's detail, and open/close.
  const currentProject = useProjects((s) => s.currentProject);
  const refreshProjects = useProjects((s) => s.refreshProjects);
  const openProject = useProjects((s) => s.openProject);
  const closeProject = useProjects((s) => s.closeProject);
  const createAndOpenProject = useProjects((s) => s.createAndOpen);
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
  // View/render UI state (page, sidebar/canvas collapse, canvas resize) lives in
  // the useLayout hook now (R5).
  const {
    view,
    setView,
    canvasCollapsed,
    setCanvasCollapsed,
    sidebarCollapsed,
    setSidebarCollapsed,
    canvasWidth,
    dragging,
    setDragging,
    mainAreaRef,
  } = useLayout();
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
  const title = selectedSession?.title || 'New theorem session';

  const sortedCode = useMemo(() => sortCodeSteps(codeSteps), [codeSteps]);
  const pendingApproval = approvals.find((a) => !a.decision);

  // The run EventSource lifecycle + session-detail hydration live in a hook now
  // (R2); it reads the proofSession + sessions stores directly.
  const { attachStream, applyDetail, reconcile, closeStream } = useProofStream();

  useEffect(() => {
    const restore = async () => {
      const loaded = await refreshSessions();
      refreshProjects().catch(() => {});
      useModel.getState().syncFromSettings();
      useModel.getState().loadCatalog();
      // The Overleaf extension's "View in Lea UI" action opens <ui>/?session=<id>;
      // that deep-link takes precedence over the last-opened session.
      const { sessionId: initialSessionId, source } = pickInitialSession({
        search: window.location.search,
        savedId: window.localStorage.getItem(SELECTED_SESSION_KEY),
        sessions: loaded,
      });
      const deepLinkedFormalizationId = readDeepLinkFormalizationId(
        window.location.search,
      );
      if (source === 'deep-link' || deepLinkedFormalizationId) {
        // Strip the param so a later reload falls back to the saved-session restore.
        const cleaned = stripNavigationParams(window.location.search);
        window.history.replaceState(
          {},
          '',
          `${window.location.pathname}${cleaned}${window.location.hash}`,
        );
      }
      if (deepLinkedFormalizationId) {
        const formalization = await getFormalization(deepLinkedFormalizationId);
        const targetSessionId =
          source === 'deep-link'
            ? initialSessionId
            : formalization.sessions[0]?.id;
        if (targetSessionId && targetSessionId !== initialSessionId) {
          await loadSession(targetSessionId);
        }
        if (targetSessionId) {
          setFormalizationScope(formalization.id);
          setCanvasCollapsed(false);
        } else if (formalization.project_id) {
          await openProject(formalization.project_id);
          setView('project');
        }
      }
      if (initialSessionId) {
        try {
          await loadSession(initialSessionId);
        } catch (err) {
          // A stale saved id is unexpected (it was just found in the list); a bad
          // deep-link id is plausible — in that case leave the user on a fresh
          // session rather than surfacing an error.
          if (source !== 'deep-link') throw err;
        }
      }
    };
    restore().catch((err) => setError(err instanceof Error ? err.message : String(err)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Subscribe once to the session-list feed. Each `sessions_changed` event
  // refreshes the sidebar and invalidates the selected formalization's canonical
  // snapshot. It never rewrites the open session timeline. The browser EventSource auto-reconnects if
  // the capped server stream recycles. A session started anywhere — including an
  // Overleaf-driven formalization the companion creates via POST /api/runs —
  // appears live without a manual refresh.
  useEffect(() => {
    const source = new EventSource('/api/sessions/events');
    source.addEventListener('sessions_changed', () => {
      refreshSessions().catch(() => {});
      bumpFormalizationRefresh();
    });
    return () => source.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadSession = async (sessionId: string) => {
    closeStream();
    const detail = await getSession(sessionId);
    applyDetail(detail);
    window.localStorage.setItem(SELECTED_SESSION_KEY, detail.id);
  };

  // Open a project's window (F1/F2). Loading its detail also sets it as the
  // selected project (sidebar highlight); the view switch reveals the window.
  const openProjectWindow = (projectId: string) => {
    openProject(projectId)
      .then(() => setView('project'))
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  };

  const handleCreateProject = async (title: string, description?: string) => {
    await createAndOpenProject(title, description);
    setNewProjectOpen(false);
    setView('project');
  };

  // Leaving a project window back to the loose-chat view.
  const leaveProject = () => {
    closeProject();
    setView('main');
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

  const handleStartProjectFormalization = async (
    formalization: Formalization,
    message?: string,
  ) => {
    if (!currentProject) return;
    const content = (
      message
      || `Continue working on ${formalization.declaration_name || formalization.display_title}.`
    ).trim();
    setError(undefined);
    try {
      resetForNewSession();
      const run = await createRun(
        content,
        undefined,
        useModel.getState().model,
        {
          project_slug: currentProject.slug,
          project_title: currentProject.title,
          project_namespace: currentProject.namespace,
          focus_formalization_id: formalization.id,
        },
      );
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
    try {
      const formalization = await getFormalization(formalizationId);
      const recentSession = formalization.sessions[0];
      if (recentSession) {
        closeProject();
        setView('main');
        await loadSession(recentSession.id);
        setFormalizationScope(formalization.id);
        setCanvasCollapsed(false);
      } else if (formalization.project_id) {
        await openProject(formalization.project_id);
        setView('project');
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

  const handleSubmit = async () => {
    const content = draft.trim();
    if (!content || isRunning) return;
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
      const scope =
        resolvedScope === 'new'
          ? { new_formalization: { display_title: content.slice(0, 120) } }
          : resolvedScope === 'project'
            ? undefined
            : { focus_formalization_id: resolvedScope };
      const run = await createRun(
        content,
        selectedSessionId,
        useModel.getState().model,
        scope,
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
      setDraft('');
      window.localStorage.setItem(SELECTED_SESSION_KEY, run.session_id);
      await refreshSessions();
      attachStream(run.run_id, run.session_id);
    } catch (err) {
      setIsRunning(false);
      setCurrentRunId(undefined);
      setError(err instanceof Error ? err.message : 'Unable to start Lea.');
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

  // Canvas editing → write the file, then lean_check; reconcile to pick up the
  // new user-authored code step. Returns the verdict for the canvas foot.
  // The canvas passes the file it's showing (#10); fall back to the latest step's
  // file for the single-file case. So Edit/lean_check and SafeVerify act on the
  // *chosen* file, not always the newest (possibly scratch) one.
  const handleSaveAndCheck = async (
    content: string,
    path?: string,
    baseRevision?: string,
  ): Promise<CheckOutcome> => {
    const target = path ?? sortedCode[sortedCode.length - 1]?.path;
    if (!selectedSessionId || !target) return { status: 'error', detail: 'No file to edit.' };
    const focusId =
      formalizationScope === 'project' || formalizationScope === 'new'
        ? undefined : formalizationScope;
    try {
      await writeSessionFile(
        selectedSessionId,
        target,
        content,
        'Manual edit from the canvas.',
        focusId,
        baseRevision || currentFormalizationSnapshot?.revision_token || undefined,
      );
    } catch (err) {
      if (err instanceof RevisionConflictError) {
        setCanvasRevisionMode('current');
        bumpFormalizationRefresh();
        setError(err.message);
      }
      throw err;
    }
    const result = await leanCheckSession(selectedSessionId, target, focusId);
    await reconcile(selectedSessionId);
    await refreshSessions();
    setEditedPath(target); // after reconcile (which clears it) — surface the nudge
    setSafeVerify(null); // the edit invalidates any prior SafeVerify verdict
    setCanvasRevisionMode('current');
    bumpFormalizationRefresh();
    return result;
  };

  const handleVerify = async (path?: string): Promise<CheckOutcome> => {
    const target = path ?? sortedCode[sortedCode.length - 1]?.path;
    if (!selectedSessionId) return { status: 'error', detail: 'No session.' };
    const focusId =
      formalizationScope === 'project' || formalizationScope === 'new'
        ? undefined : formalizationScope;
    const result = await verifySession(selectedSessionId, target, focusId);
    setSafeVerify({ status: result.status, detail: result.detail });
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

  if (view === 'project' && currentProject)
    return (
      <>
        {searchOverlay}
        {skillsMcpPicker}
        <ProjectWindow
          project={currentProject}
          onBack={leaveProject}
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
          onBack={() => setView('main')}
          onOpenProject={openProjectWindow}
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
      <div className={`app ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
        <Sidebar
          runningSessionId={isRunning ? selectedSessionId : undefined}
          onSelectSession={(id) => {
            closeProject();
            loadSession(id).catch((err) => setError(err instanceof Error ? err.message : String(err)));
          }}
          onNewSession={() => {
            closeProject();
            resetForNewSession();
          }}
          onSelectProject={openProjectWindow}
          onNewProject={() => setNewProjectOpen(true)}
          onOpenProjectsHub={() => {
            closeProject();
            setView('projects-hub');
          }}
          onOpenSkills={() => {
            closeProject();
            setView('skills');
          }}
          onOpenSubagents={() => {
            closeProject();
            setView('subagents');
          }}
          onOpenMcp={() => {
            closeProject();
            setView('mcp');
          }}
          onOpenTools={() => {
            closeProject();
            setView('tools');
          }}
          onOpenSearch={() => setSearchOpen(true)}
          onOpenSettings={() => setView('settings')}
          onOpenStats={() => setView('stats')}
          onCollapse={() => setSidebarCollapsed(true)}
        />

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
            onSubmit={handleSubmit}
            onInterrupt={handleInterrupt}
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
            sessionId={selectedSessionId}
            onClose={() => setCanvasCollapsed(true)}
            onSaveAndCheck={handleSaveAndCheck}
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
