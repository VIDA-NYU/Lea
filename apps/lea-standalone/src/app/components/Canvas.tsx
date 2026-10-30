import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import '../../styles/canvas.css';
import {
  getCurrentFormalization,
  putProjectFile,
  type SafeVerifyStatus,
  type EditorDraftBase,
  type FileWriteResult,
} from '../lib/api';
import { LiveEditor, type EditorStatus, type LiveDiagnostic, type LiveEditorHandle, type SaveState } from './LiveEditor';
import { ReformalizationDraft } from './ReformalizationDraft';
import { diffForStep, type DiffRow } from '../lib/codeDiff';
import { deriveCodeStepProofStatus } from '../lib/proofDisplay.mjs';
import { sortCodeSteps } from '../lib/timeline.mjs';
import { distinctFiles, latestIndexForPath, mainFilePath } from '../lib/canvasFiles.mjs';
import { formalizationCanvasSteps, stepsForFormalization } from '../lib/formalizations.mjs';
import {
  caretLine,
  changedLines,
  chipTab,
  deriveStatusChip,
  drawerSummary,
  editorNote,
  errorLines,
  formatSeconds,
  goalHeadline,
  latestCheckTiming,
  parseLeanProblems,
  problemsForStep,
  revealTarget,
  samePath,
  sorryLines,
  stepDelta,
  stepLabel,
  theoremName,
  timelineDots,
  verifyAvailability,
  withSorryWarnings,
} from '../lib/canvasModel.js';
import { useProofSession } from '../stores/proofSession';
import { useSessions } from '../stores/sessions';
import { useProjects } from '../stores/projects';
import { BLUEPRINT_PATH, useWorkbench } from '../stores/workbench';
import { FilesRail } from './files/FilesRail';
import { PathCrumbs } from './files/PathCrumbs';
import { BlueprintView } from './blueprint/BlueprintView';
import { BlueprintStrip } from './blueprint/BlueprintStrip';
import { CanvasHeader, type MenuItem, type StatusChip } from './canvas/CanvasHeader';
import { TimelineStrip, type TimelineDot } from './canvas/TimelineStrip';
import { StepView, plainRows, type LineMarks, type StepViewHandle } from './canvas/StepView';
import { CanvasDrawer, DrawerPlaceholder, type Problem } from './canvas/CanvasDrawer';
import { CanvasEmpty } from './canvas/CanvasEmpty';
import { FocusRail } from './canvas/FocusRail';
import { FileSelect } from './canvas/FileSelect';
import { RevisionNotices } from './canvas/RevisionNotices';
import { ProjectFileView, type ProjectFileState } from './canvas/ProjectFileView';
import { composeInChat } from './canvas/composerBridge';

export interface CheckOutcome {
  status: string;
  detail?: string | null;
  current?: boolean;
  check_current?: boolean;
  check_stale_reason?: string | null;
}

type VerifyState = { status: SafeVerifyStatus; detail?: string | null; current?: boolean } | null;

/** Class on <html> while focus mode is on; canvas.css collapses the chat with it. */
export const FOCUS_CLASS = 'lea-focus';

const NO_MARKS: LineMarks = {};

function baseName(path: string): string {
  return path.split('/').pop() || path;
}

// The right-hand canvas (docs/PLAN-ui-redesign.md §3 "canvas"): one surface for
// the proof. A header (name over crumbs, one status chip, lean_check, SafeVerify,
// more, hide), a timeline strip over the session's code steps, the editor, and a
// Goals / Problems / Verify drawer.
//
// The editor is always the editor: Monaco (LiveEditor) holds the latest snapshot
// of the followed file, read-only and following Lea while a run is live, editable
// otherwise. Scrubbing the timeline to an older step shows that snapshot read-only
// (StepView) while Monaco stays mounted behind it, so a draft survives a look back.
// Project files and the blueprint are documents in the same surface
// (useWorkbench().openPath). Network calls are delegated to the parent, which owns
// the session id + state, via onSaveAndCheck / onCheck / onVerify.
export function Canvas({
  sessionId,
  onClose,
  onSaveAndCheck,
  onCheck,
  onVerify,
  onOpenSession,
  captureSelection,
}: {
  // Present once the session is persisted; the editor needs it for the queued
  // editor requests. Absent on a brand-new, unsaved session.
  sessionId?: string;
  captureSelection: () => () => boolean;
  onClose: () => void;
  onSaveAndCheck: (
    content: string,
    path: string,
    base: EditorDraftBase,
    draftId: string,
  ) => Promise<FileWriteResult>;
  onCheck?: (path: string, base: EditorDraftBase) => Promise<CheckOutcome>;
  onVerify: (path?: string) => Promise<CheckOutcome>;
  onOpenSession?: (sessionId: string) => void;
}) {
  // ── stores ────────────────────────────────────────────────────────────────
  const session = useSessions((s) => (sessionId ? s.sessions.find((x) => x.id === sessionId) : undefined));
  // With project first every session has one; until then a missing project
  // means no rail, no crumbs, no blueprint.
  const projectId = session?.project_id ?? null;
  const project = useProjects((s) => (projectId ? s.projects.find((p) => p.id === projectId) : undefined));
  const sessionPersistedVerify = useProofSession((s) => s.safeVerify);
  const isRunning = useProofSession((s) => s.isRunning);
  const rawSteps = useProofSession((s) => s.codeSteps);
  const allCodeSteps = useMemo(() => sortCodeSteps(rawSteps), [rawSteps]);
  const scope = useProofSession((s) => s.formalizationScope);
  const formalizations = useProofSession((s) => s.formalizations);
  const revisionMode = useProofSession((s) => s.canvasRevisionMode);
  const setRevisionMode = useProofSession((s) => s.setCanvasRevisionMode);
  const currentSnapshot = useProofSession((s) => s.currentFormalizationSnapshot);
  const setCurrentSnapshot = useProofSession((s) => s.setCurrentFormalizationSnapshot);
  const refreshToken = useProofSession((s) => s.formalizationRefreshToken);
  const statusEvents = useProofSession((s) => s.statusEvents);
  const manualCheckSessionId = useProofSession((s) => s.manualCheckSessionId);
  const goalSurface = useProofSession((s) => s.goalSurface);
  const setVerifySurface = useProofSession((s) => s.setVerifySurface);
  const setGlobalIndex = useProofSession((s) => s.setCodeIndex);
  const messageCount = useProofSession((s) => s.messages.length);
  const openPath = useWorkbench((s) => s.openPath);
  const openFile = useWorkbench((s) => s.openFile);
  const filesRailOpen = useWorkbench((s) => s.filesRailOpen);
  const toggleFilesRail = useWorkbench((s) => s.toggleFilesRail);
  const setFilesRailOpen = useWorkbench((s) => s.setFilesRailOpen);
  const focusMode = useWorkbench((s) => s.focusMode);
  const setFocusMode = useWorkbench((s) => s.setFocusMode);
  const drawerTab = useWorkbench((s) => s.drawerTab);
  const setDrawerTab = useWorkbench((s) => s.setDrawerTab);
  const pendingLine = useWorkbench((s) => s.pendingLine);
  const clearPendingLine = useWorkbench((s) => s.clearPendingLine);

  // ── formalization scope / revision (unchanged behaviour) ──────────────────
  const snapshotContext = useRef('');
  const [currentLoading, setCurrentLoading] = useState(false);
  const [currentError, setCurrentError] = useState<string | null>(null);
  const scoped = scope !== 'project' && scope !== 'new';
  useEffect(() => {
    if (!scoped || !sessionId) {
      setCurrentSnapshot(null);
      setCurrentError(null);
      return;
    }
    let cancelled = false;
    const context = JSON.stringify([sessionId, scope]);
    // A data refresh must not unmount the editor or erase its cursor/draft.
    // Clear the old snapshot only when the author navigates to another context.
    if (snapshotContext.current !== context) setCurrentSnapshot(null);
    snapshotContext.current = context;
    setCurrentLoading(true);
    setCurrentError(null);
    getCurrentFormalization(scope, sessionId)
      .then((snapshot) => {
        if (!cancelled) setCurrentSnapshot(snapshot);
      })
      .catch((err) => {
        if (!cancelled) setCurrentError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setCurrentLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [scope, scoped, sessionId, refreshToken, setCurrentSnapshot]);

  const persistedVerify: VerifyState = scoped
    ? currentSnapshot?.formalization_id === scope && currentSnapshot.safe_verify?.current
      ? { status: currentSnapshot.safe_verify.status, detail: currentSnapshot.safe_verify.detail, current: true }
      : null
    : sessionPersistedVerify;
  const historicalSteps = useMemo(
    () => (scoped ? stepsForFormalization(allCodeSteps, scope) : allCodeSteps),
    [allCodeSteps, scope, scoped],
  );
  const showingCurrent = Boolean(scoped && revisionMode === 'current');
  const codeSteps = useMemo(
    () =>
      scoped
        ? formalizationCanvasSteps({ mode: revisionMode, formalizationId: scope, snapshot: currentSnapshot, historicalSteps })
        : historicalSteps,
    [currentSnapshot, historicalSteps, revisionMode, scope, scoped],
  );

  // ── documents: which file, which step ─────────────────────────────────────
  // openPath (files package) picks a document: the blueprint, a file this
  // session touched (shown in the session editor with its timeline), or any
  // other project file (ProjectFileView). null = the session's own proof.
  const files = useMemo(() => distinctFiles(codeSteps), [codeSteps]);
  const mainPath: string | null = useMemo(
    () =>
      showingCurrent
        ? currentSnapshot?.files.find((file) => file.role === 'primary')?.path || mainFilePath(codeSteps)
        : mainFilePath(codeSteps),
    [codeSteps, currentSnapshot, showingCurrent],
  );
  const total = codeSteps.length;
  const blueprintOpen = !!projectId && openPath === BLUEPRINT_PATH;
  const openSessionPath: string | null =
    projectId && openPath && !blueprintOpen ? files.find((f) => samePath(f.path, openPath))?.path ?? null : null;
  const fileOpen = !!projectId && !!openPath && !blueprintOpen && !openSessionPath;

  // The canvas follows the latest snapshot of the followed file unless the user
  // pinned an older step from the timeline (or a chat chip). Following keeps
  // Monaco mounted across the user's own saves and tracks Lea's edits live.
  const [pinnedId, setPinnedId] = useState<string | null>(null);
  const [filePick, setFilePick] = useState<string | null>(null);
  // In the current-revision view a picked file that vanished stays picked, so
  // its notice shows and a mounted draft is kept (pre-redesign behaviour).
  const followPath: string | null =
    openSessionPath ?? (filePick && (showingCurrent || files.some((f) => f.path === filePick)) ? filePick : mainPath);
  const followIndex = followPath ? latestIndexForPath(codeSteps, followPath) : -1;
  const pinnedIndex = pinnedId ? codeSteps.findIndex((s) => s.id === pinnedId) : -1;
  const following = pinnedIndex < 0;
  const safeIndex = following ? followIndex : pinnedIndex;
  const step = safeIndex >= 0 ? codeSteps[safeIndex] : undefined;
  const followStep = followIndex >= 0 ? codeSteps[followIndex] : undefined;
  const shownPath: string | undefined = step?.path ?? followPath ?? undefined;
  const isFileCurrent = !!step && (showingCurrent || safeIndex === latestIndexForPath(codeSteps, shownPath));
  const isNewest = safeIndex >= 0 && safeIndex === total - 1;

  // openFile() calls the canvas makes itself must not reset the pin; the rail /
  // QuickOpen opening a document shows that document's latest snapshot.
  const selfOpenRef = useRef(false);
  const openDocument = useCallback(
    (path: string | null) => {
      if (useWorkbench.getState().openPath === path) return;
      selfOpenRef.current = true;
      openFile(path);
    },
    [openFile],
  );
  useEffect(() => {
    const pending = useWorkbench.getState().pendingLine;
    if (pending?.path && openPath && !samePath(pending.path, openPath)) clearPendingLine();
    if (selfOpenRef.current) {
      selfOpenRef.current = false;
      return;
    }
    setPinnedId(null);
  }, [openPath, clearPendingLine]);

  // Landing on a file's latest step follows that file; landing on an older step
  // pins it and leaves the followed file (and its mounted editor) alone, so a
  // look back never costs the draft.
  const go = useCallback(
    (next: number) => {
      const bounded = Math.min(Math.max(next, 0), Math.max(total - 1, 0));
      const target = codeSteps[bounded];
      if (!target) return;
      if (bounded !== latestIndexForPath(codeSteps, target.path)) {
        setPinnedId(target.id);
        return;
      }
      setFilePick(target.path);
      setPinnedId(null);
      const open = useWorkbench.getState().openPath;
      if (open && !samePath(open, target.path)) openDocument(null);
    },
    [codeSteps, openDocument, total],
  );
  const pickFile = (path: string) => {
    setFilePick(path);
    setPinnedId(null);
    if (openPath && !samePath(openPath, path)) openDocument(null);
  };

  // Keep the store's codeIndex on the shown snapshot (chat chips highlight it)
  // and follow a selection made elsewhere (a chat chip → App.selectStep). A store
  // change counts as a selection only when codeIndex moves on its own: re-indexing
  // after new steps (useProofStream.replaceCodeSteps) keeps the same step id.
  const knownIdRef = useRef<string | null>(null);
  const selfWriteRef = useRef(false);
  const [adoptId, setAdoptId] = useState<string | null>(null);
  useEffect(
    () =>
      useProofSession.subscribe((state, prev) => {
        if (selfWriteRef.current || state.codeIndex === prev.codeIndex || state.codeSteps !== prev.codeSteps) return;
        const id = sortCodeSteps(state.codeSteps)[state.codeIndex]?.id ?? null;
        if (!id || id === knownIdRef.current) return;
        knownIdRef.current = id;
        setAdoptId(id);
      }),
    [],
  );
  useEffect(() => {
    if (!adoptId) return;
    setAdoptId(null);
    const i = codeSteps.findIndex((s) => s.id === adoptId);
    if (i >= 0) go(i);
  }, [adoptId, codeSteps, go]);
  useEffect(() => {
    if (!step) return;
    const g = allCodeSteps.findIndex((s) => s.id === step.id);
    if (g < 0) return;
    knownIdRef.current = step.id;
    if (g !== useProofSession.getState().codeIndex) {
      selfWriteRef.current = true;
      try {
        setGlobalIndex(g);
      } finally {
        selfWriteRef.current = false;
      }
    }
  }, [step, allCodeSteps, setGlobalIndex]);

  // ── the editor (Monaco on the followed file's latest snapshot) ────────────
  const liveAllowed = !!sessionId && (!scoped || showingCurrent);
  const liveKeyFor = liveAllowed && followPath ? `${sessionId}:${followPath}:${scoped ? scope : ''}` : null;
  const [mountedLiveKey, setMountedLiveKey] = useState<string | null>(null);
  // Retain a mounted draft when a background publication removes its file.
  const liveWanted = !!liveKeyFor && (!!followStep || (showingCurrent && mountedLiveKey === liveKeyFor));
  const liveKey = liveWanted ? liveKeyFor : null;
  useEffect(() => {
    if (liveKey) setMountedLiveKey(liveKey);
  }, [liveKey]);
  const liveVisible = liveWanted && following;
  const liveRef = useRef<LiveEditorHandle>(null);
  const stepViewRef = useRef<StepViewHandle>(null);
  const fileViewRef = useRef<StepViewHandle>(null);
  // Status is keyed to the editor that reported it, so the previous file's
  // "ready" never stands in for a freshly mounted editor.
  const [liveStatusFor, setLiveStatus] = useState<{ key: string | null; status: EditorStatus; error: string | null }>({
    key: null,
    status: 'loading',
    error: null,
  });
  const liveStatus =
    liveStatusFor.key === liveKey ? liveStatusFor : { key: liveKey, status: 'loading' as EditorStatus, error: null };
  const [saveState, setSaveState] = useState<SaveState>({ state: 'clean', detail: '' });
  const [analysis, setAnalysis] = useState('pending');
  const [goals, setGoals] = useState<string | null>(null);
  const [liveDiags, setLiveDiags] = useState<LiveDiagnostic[] | null>(null);
  const [liveValue, setLiveValue] = useState<string | null>(null);
  const [goalNote, setGoalNote] = useState<string | null>(null);
  useEffect(() => {
    setSaveState({ state: 'clean', detail: '' });
    setAnalysis('pending');
    setGoals(null);
    setLiveDiags(null);
    setLiveValue(null);
    setGoalNote(null);
  }, [liveKey]);
  const onLiveStatus = useCallback(
    (status: EditorStatus, error?: string | null) => setLiveStatus({ key: liveKey, status, error: error ?? null }),
    [liveKey],
  );
  const liveReady = liveWanted && liveStatus.status === 'ready';
  const liveFailed = liveWanted && liveStatus.status === 'error';
  /** Monaco is what the user sees. */
  const showLive = liveVisible && liveReady;
  const liveDirty = showLive && (saveState.state === 'dirty' || saveState.state === 'saving');

  // ── rows, problems, line marks ────────────────────────────────────────────
  const rows: DiffRow[] = useMemo(() => {
    if (!step) return [];
    if (showingCurrent) return plainRows(step.code);
    // unchanged/added/removed against this file's previous snapshot; the view
    // shows the non-removed rows, the removed ones only feed the delta.
    return diffForStep(codeSteps, safeIndex);
  }, [codeSteps, safeIndex, showingCurrent, step]);
  const visibleRows = useMemo(() => rows.filter((r) => r.kind !== 'removed'), [rows]);
  const delta = useMemo(() => stepDelta(rows), [rows]);
  const stepProblems: Problem[] = useMemo(() => problemsForStep(step), [step]);
  const stepSorries: number[] = useMemo(() => sorryLines(step?.code ?? ''), [step?.code]);
  const sorries: number[] = useMemo(
    () => (showLive && liveValue != null ? sorryLines(liveValue) : stepSorries),
    [showLive, liveValue, stepSorries],
  );
  // Live LSP diagnostics once the editor's analysis is in (paused while a run
  // holds the lock); otherwise the persisted lean_check verdict of the step.
  const useLiveDiags = showLive && !isRunning && analysis === 'available' && !!liveDiags;
  const problems: Problem[] = useMemo(() => {
    const base: Problem[] = useLiveDiags && liveDiags ? liveDiags : liveDirty ? [] : stepProblems;
    return withSorryWarnings(base, sorries, shownPath ?? null);
  }, [useLiveDiags, liveDiags, liveDirty, shownPath, sorries, stepProblems]);
  const errLines: number[] = useMemo(() => errorLines(problems), [problems]);
  const caret =
    isRunning && following && isNewest && step?.author === 'agent' && !showingCurrent ? caretLine(rows) : null;
  const marks: LineMarks = useMemo(
    () => ({
      changed: liveDirty || showingCurrent ? [] : changedLines(rows),
      errors: errLines,
      errorAt: useLiveDiags
        ? []
        : problems
            .filter((p) => p.severity === 'error' && p.line != null)
            .map((p) => ({ line: p.line as number, column: p.column })),
      sorries,
      caret,
      caretLabel: 'Lea · editing',
    }),
    [caret, errLines, liveDirty, problems, rows, showingCurrent, sorries, useLiveDiags],
  );

  // ── SafeVerify ────────────────────────────────────────────────────────────
  const [busy, setBusy] = useState(false);
  const [verify, setVerify] = useState<VerifyState>(null);
  // A transient result belongs to the step it ran on; a fresh edit makes it stale.
  useEffect(() => {
    setVerify(null);
  }, [safeIndex, step?.id]);
  useEffect(() => {
    if (liveDirty) setVerify(null);
  }, [liveDirty]);
  const verifyGeneration = useRef(0);
  useEffect(() => {
    verifyGeneration.current += 1;
    setBusy(false);
  }, [shownPath, step?.id]);
  const runVerify = async () => {
    const owned = captureSelection();
    const generation = verifyGeneration.current;
    const current = () => owned() && generation === verifyGeneration.current;
    setBusy(true);
    setVerify({ status: 'running' });
    setVerifySurface(null); // clear any prior box while this run is in flight
    try {
      const result = await onVerify(shownPath);
      if (!current()) return;
      const next = { status: result.status as SafeVerifyStatus, detail: result.detail, current: result.current };
      setVerify(next);
      // Also surface it above the composer (collapse / send-to-Lea "fix it").
      setVerifySurface({ status: next.status, detail: next.detail ?? null, current: next.current });
    } catch (err) {
      if (!current()) return;
      const detail = err instanceof Error ? err.message : String(err);
      setVerify({ status: 'error', detail });
      setVerifySurface({ status: 'error', detail });
    } finally {
      if (current()) setBusy(false);
    }
  };

  const proofStatus = deriveCodeStepProofStatus(step, { historical: !isFileCurrent });
  // Live result (this session) wins; otherwise the persisted verdict when viewing
  // the shown file's current snapshot, so a reload still shows SafeVerify ✓.
  const liveVerify = verify?.status === 'ok' && (verify.current !== true || step?.check_current !== true) ? null : verify;
  const shownVerify: VerifyState = liveVerify ?? (isFileCurrent && persistedVerify?.current === true ? persistedVerify : null);
  const availability = verifyAvailability({
    hasStep: !!step,
    latest: isFileCurrent,
    running: isRunning,
    busy,
    draftPending: liveVisible && (liveStatus.status === 'loading' || liveDirty),
    proof: proofStatus,
  });

  // ── status chip, timeline, drawer ─────────────────────────────────────────
  const checking =
    following &&
    ((!!manualCheckSessionId && manualCheckSessionId === sessionId) ||
      (showLive && (saveState.state === 'saving' || (saveState.state === 'saved' && /^checking/.test(saveState.detail)))));
  const chip: StatusChip = useMemo(
    () =>
      deriveStatusChip({
        step,
        historical: !isFileCurrent,
        running: isRunning && following,
        checking,
        dirty: liveDirty,
        verify: shownVerify,
        problems,
        sorries,
      }),
    [checking, following, isFileCurrent, isRunning, liveDirty, problems, shownVerify, sorries, step],
  );
  const dots: TimelineDot[] = useMemo(() => timelineDots(codeSteps, safeIndex), [codeSteps, safeIndex]);
  const label = showingCurrent
    ? { head: 'Current project version', tail: shownPath ? baseName(shownPath) : '' }
    : stepLabel({ index: safeIndex, total, step, latest: isNewest });
  const ring =
    chip.kind === 'running'
      ? 'running'
      : chip.kind === 'error'
        ? 'error'
        : chip.kind === 'proved' || chip.kind === 'verified'
          ? 'proved'
          : chip.kind === 'sorry'
            ? 'sorry'
            : 'neutral';
  const note = editorNote({
    following,
    step,
    previous: safeIndex > 0 ? codeSteps[safeIndex - 1] : undefined,
    previousIndex: safeIndex - 1,
    live: liveVisible ? liveStatus.status : 'none',
    liveError: liveStatus.error,
    running: isRunning,
    save: saveState,
    hasSession: !!sessionId,
  });

  const [drawerOpen, setDrawerOpen] = useState(false);
  const timing = useMemo(
    () => (following && !showingCurrent ? latestCheckTiming(statusEvents) : null),
    [following, showingCurrent, statusEvents],
  );
  const liveGoals = showLive && !isRunning;
  const goalsText = liveGoals ? goals : goalSurface?.rendered ?? null;
  const summary = drawerSummary({ goal: goalHeadline(goalsText), chip, errors: errLines.length, sorries: sorries.length });
  const meta =
    drawerTab === 'verify' && drawerOpen && shownVerify
      ? `SafeVerify · step ${safeIndex + 1}`
      : timing
        ? `lean_check · ${formatSeconds(timing.ms)}${timing.backend === 'cold' ? ' · cold' : ''}`
        : null;
  const jumpToLine = (line: number) => {
    if (showLive) liveRef.current?.jumpToLine(line);
    else stepViewRef.current?.jumpToLine(line);
  };
  const onChipClick = () => {
    setDrawerOpen(true);
    setDrawerTab(chipTab(chip));
    if (chip.line) jumpToLine(chip.line);
  };
  const askAboutGoal = async () => {
    setGoalNote(null);
    const result = await liveRef.current?.askAboutGoal();
    if (result) setGoalNote(result);
  };
  const goalsHint = liveVisible && !liveReady
    ? liveFailed
      ? 'Editor unavailable — goals need the live editor.'
      : 'Opening editor…'
    : showLive && isRunning
      ? 'Goals appear once Lea finishes this run.'
      : showLive
        ? 'Click inside a proof to inspect its goals.'
        : !sessionId
          ? 'Goals appear once the session has a file.'
          : 'Go to the latest step to inspect goals live.';

  // ── header actions ────────────────────────────────────────────────────────
  const canCheck = showLive && !isRunning && !!onCheck;
  const checkTitle = canCheck
    ? liveDirty
      ? 'Save and run lean_check'
      : 'Run lean_check on the saved file'
    : isRunning
      ? 'Wait for Lea to finish'
      : liveVisible && liveStatus.status === 'loading'
        ? 'Opening editor…'
        : 'Go to the latest step to run lean_check';

  // Focus mode (frame 4): a class on <html> lets canvas.css collapse the chat
  // into nothing while the FocusRail inside the canvas stands in for it. App.tsx
  // is not touched; the integrator may move the class onto .main-area.
  useEffect(() => {
    document.documentElement.classList.toggle(FOCUS_CLASS, focusMode);
    return () => {
      document.documentElement.classList.remove(FOCUS_CLASS);
    };
  }, [focusMode]);
  const [focusBaseline, setFocusBaseline] = useState(0);
  useEffect(() => {
    if (focusMode) setFocusBaseline(messageCount);
    // Only the moment focus turns on sets the baseline.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusMode]);
  const unread = focusMode ? Math.max(0, messageCount - focusBaseline) : 0;
  const handleClose = () => {
    if (focusMode) setFocusMode(false);
    onClose();
  };

  // ── project file / blueprint documents ────────────────────────────────────
  const [fileStateAny, setFileState] = useState<ProjectFileState | null>(null);
  const onFileState = useCallback((s: ProjectFileState) => setFileState(s), []);
  // Only the open file's own report counts (the view reports its path).
  const fileState = fileOpen && fileStateAny && fileStateAny.path === openPath ? fileStateAny : null;
  const fileProblems: Problem[] = useMemo(
    () =>
      fileOpen && fileState?.lean
        ? withSorryWarnings(parseLeanProblems(fileState.check?.detail), fileState.sorries, openPath)
        : [],
    [fileOpen, fileState, openPath],
  );
  const fileChip: StatusChip = !fileState || fileState.loading || !fileState.lean
    ? { kind: 'idle', label: '', line: null }
    : fileState.editing
      ? { kind: 'unchecked', label: 'Editing…', line: null }
      : fileState.saving
        ? { kind: 'running', label: 'Checking…', line: null }
        : fileState.check
          ? deriveStatusChip({
              step: {
                check_status: fileState.check.status,
                check_current: true,
                check_detail: fileState.check.detail,
                code: fileState.content,
                path: openPath,
              },
              problems: fileProblems,
              sorries: fileState.sorries,
            })
          : { kind: 'unchecked', label: 'Not checked', line: null };

  // ── reveal-line contract (useWorkbench().pendingLine) ─────────────────────
  const docPath: string | null = fileOpen ? openPath : blueprintOpen ? null : shownPath ?? null;
  const docReady = fileOpen
    ? !!fileState && !fileState.loading && !fileState.error
    : !blueprintOpen && !!step && (!liveVisible || liveReady || liveFailed);
  const unmountedRef = useRef(false);
  useEffect(() => {
    unmountedRef.current = false;
    return () => {
      unmountedRef.current = true;
    };
  }, []);
  useEffect(() => {
    if (!pendingLine || !docReady) return;
    const line = revealTarget(pendingLine, docPath);
    if (line == null) return;
    clearPendingLine();
    // Next frame: Monaco may have just become visible and not laid out yet.
    window.requestAnimationFrame(() => {
      if (unmountedRef.current) return;
      if (fileOpen) fileViewRef.current?.jumpToLine(line);
      else if (showLive) liveRef.current?.jumpToLine(line);
      else stepViewRef.current?.jumpToLine(line);
    });
  }, [pendingLine, docReady, docPath, fileOpen, showLive, clearPendingLine]);

  // ── empty state actions ───────────────────────────────────────────────────
  const [notice, setNotice] = useState<string | null>(null);
  const handleAskLea = () => {
    if (focusMode) setFocusMode(false);
    if (!composeInChat('')) setNotice('Type your theorem in the chat on the left.');
  };
  const handleOpenProjectFile = () => {
    setFilesRailOpen(true);
    if (!projectId) setNotice('This session is not in a project yet. Open a session from a project to browse its files.');
  };
  const handlePasteLean = (code: string) => {
    if (focusMode) setFocusMode(false);
    const text = `Continue this Lean proof:\n\n\`\`\`lean\n${code}\n\`\`\``;
    if (!composeInChat(text)) setNotice('Paste the code into the chat on the left.');
  };
  const namespace = project?.namespace?.trim() || '';
  const newFileDefault = namespace ? `${namespace.split('.').join('/')}/Untitled.lean` : 'Untitled.lean';
  const handleNewFile = async (path: string): Promise<string | null> => {
    if (!projectId) return 'Start a session in a project to create files.';
    const template = /\.lean$/i.test(path)
      ? `import Mathlib\n\n${namespace ? `namespace ${namespace}\n\n\n\nend ${namespace}\n` : ''}`
      : '';
    try {
      await putProjectFile(projectId, path, template);
      openFile(path);
      setFilesRailOpen(true);
      return null;
    } catch (err) {
      return err instanceof Error ? err.message : String(err);
    }
  };

  // ── render ────────────────────────────────────────────────────────────────
  const name = fileOpen
    ? fileState?.name ?? baseName(openPath as string)
    : blueprintOpen
      ? 'Blueprint'
      : step
        ? theoremName(step.code, shownPath)
        : null;
  const crumbPath = fileOpen || blueprintOpen ? openPath : shownPath ?? null;
  const crumbs = projectId ? (
    <PathCrumbs projectId={projectId} path={crumbPath} />
  ) : (
    <span className="cv-crumb-fallback mono">{crumbPath ?? 'proofs / —'}</span>
  );
  const filePicker =
    !fileOpen && !blueprintOpen && files.length > 1 ? (
      <FileSelect files={files} current={shownPath} mainPath={mainPath} onPick={pickFile} />
    ) : undefined;
  const menu: MenuItem[] = [
    { label: focusMode ? 'Exit focus mode' : 'Focus mode', onSelect: () => setFocusMode(!focusMode) },
    ...(showLive ? [{ label: 'Reload published file', onSelect: () => void liveRef.current?.reload() }] : []),
    ...(!following && shownPath
      ? [{ label: 'Go to the latest step of this file', onSelect: () => go(latestIndexForPath(codeSteps, shownPath)) }]
      : []),
    ...(scoped
      ? [
          revisionMode === 'historical'
            ? { label: 'View current project version', onSelect: () => setRevisionMode('current') }
            : {
                label: "View this conversation's version",
                onSelect: () => setRevisionMode('historical'),
                disabled: historicalSteps.length === 0,
              },
        ]
      : []),
    ...(currentSnapshot?.last_updated_session?.id &&
    currentSnapshot.last_updated_session.id !== sessionId &&
    onOpenSession
      ? [
          {
            label: 'Open updating conversation',
            onSelect: () => onOpenSession(currentSnapshot.last_updated_session!.id),
          },
        ]
      : []),
    ...(crumbPath ? [{ label: 'Copy file path', onSelect: () => void navigator.clipboard?.writeText(crumbPath) }] : []),
    ...(openPath && projectId ? [{ label: 'Back to the proof', onSelect: () => openFile(null) }] : []),
  ];

  const verifyNote = shownVerify && step ? `step ${safeIndex + 1}${shownVerify.current === false ? ' · recheck required' : ''}` : null;
  const showEmpty = !step && !currentLoading && !liveWanted;
  const missingFile = showingCurrent && !!shownPath && !step && !currentLoading;

  return (
    <section className={`canvas cv ${focusMode ? 'focus' : ''}`} aria-label="Proof canvas">
      {focusMode && <FocusRail title={session?.title} unread={unread} onExit={() => setFocusMode(false)} />}
      <div className="cv-surface">
        <CanvasHeader
          railAvailable={!!projectId}
          railOpen={filesRailOpen}
          onToggleRail={toggleFilesRail}
          name={name}
          crumbs={crumbs}
          filePicker={filePicker}
          chip={fileOpen ? fileChip : blueprintOpen ? { kind: 'idle', label: '', line: null } : chip}
          onChipClick={fileOpen ? () => setDrawerOpen(true) : blueprintOpen ? undefined : onChipClick}
          check={{
            enabled: canCheck && !fileOpen && !blueprintOpen,
            title: fileOpen ? 'Saving a project file runs lean_check' : checkTitle,
            onRun: () => void liveRef.current?.check(),
          }}
          verify={{
            enabled: availability.canRun && !fileOpen && !blueprintOpen,
            busy,
            title: fileOpen || blueprintOpen ? 'SafeVerify runs on session proofs' : availability.reason,
            again: !!shownVerify && shownVerify.status !== 'running',
            onRun: () => void runVerify(),
          }}
          menu={menu}
          onClose={handleClose}
        />
        <div className="cv-row">
          {projectId && filesRailOpen && <FilesRail projectId={projectId} />}
          <div className="cv-main">
            {projectId && !blueprintOpen && <BlueprintStrip projectId={projectId} sessionId={sessionId} />}
            {blueprintOpen && (
              <BlueprintView projectId={projectId as string} onOpenSession={onOpenSession ?? (() => {})} />
            )}
            {fileOpen && (
              <>
                <ProjectFileView
                  key={openPath as string}
                  ref={fileViewRef}
                  projectId={projectId as string}
                  path={openPath as string}
                  onState={onFileState}
                  onBack={() => openFile(null)}
                />
                <CanvasDrawer
                  open={drawerOpen}
                  onToggle={() => setDrawerOpen((o) => !o)}
                  tab={drawerTab}
                  onTab={setDrawerTab}
                  summary={drawerSummary({
                    chip: fileChip,
                    errors: errorLines(fileProblems).length,
                    sorries: fileState?.sorries.length ?? 0,
                  })}
                  meta={fileState?.check ? 'lean_check on save' : null}
                  problems={fileProblems}
                  onJump={(line) => fileViewRef.current?.jumpToLine(line)}
                  goals={{ text: null, hint: 'Goals are available for files a session works on.', canAsk: false, onAsk: () => {} }}
                  verify={{
                    result: null,
                    canRun: false,
                    busy: false,
                    onRun: () => {},
                    disabledReason: 'SafeVerify runs on session proofs.',
                  }}
                />
              </>
            )}
            {/* The session's own document. Parked (hidden, still mounted) while a
                project file or the blueprint is open, so the editor keeps its
                draft, undo and connection. */}
            <div className={`cv-doc ${fileOpen || blueprintOpen ? 'parked' : ''}`}>
              <RevisionNotices
                scoped={scoped}
                revisionMode={revisionMode}
                setRevisionMode={setRevisionMode}
                snapshot={currentSnapshot}
                sessionId={sessionId}
                hasHistory={historicalSteps.length > 0}
                missingFile={missingFile}
                error={currentError}
                onOpenSession={onOpenSession}
              />
              {notice && (
                <div className="revision-notice" role="status">
                  <span>{notice}</span>
                  <button type="button" onClick={() => setNotice(null)}>
                    Dismiss
                  </button>
                </div>
              )}

              <ReformalizationDraft formalization={formalizations.find((item) => item.id === scope)} refresh={refreshToken} />

              {showEmpty ? (
                <>
                  <CanvasEmpty
                    scoped={scoped}
                    onAskLea={handleAskLea}
                    onOpenProjectFile={handleOpenProjectFile}
                    onPasteLean={handlePasteLean}
                    onNewFile={handleNewFile}
                    newFileDefault={newFileDefault}
                    newFileDisabledReason={projectId ? null : 'Start a session in a project to create files'}
                  />
                  <DrawerPlaceholder note="Nothing to check yet" />
                </>
              ) : (
                <>
                  {total > 0 && (
                    <TimelineStrip
                      dots={dots}
                      index={safeIndex}
                      total={total}
                      head={label.head}
                      tail={label.tail}
                      delta={showingCurrent ? null : delta}
                      ring={ring}
                      note={note}
                      onPrev={() => go(safeIndex - 1)}
                      onNext={() => go(safeIndex + 1)}
                      onPick={go}
                    />
                  )}
                  <div className="cv-editor">
                    {liveKey && sessionId && (
                      <div className={`cv-live-host ${showLive ? 'ready' : ''} ${liveVisible ? '' : 'parked'}`}>
                        <LiveEditor
                          key={liveKey}
                          ref={liveRef}
                          sessionId={sessionId}
                          path={followPath ?? undefined}
                          formalizationId={scoped ? scope : undefined}
                          captureSelection={captureSelection}
                          locked={isRunning}
                          onSave={onSaveAndCheck}
                          onCheck={onCheck}
                          followKey={followStep?.id}
                          marks={liveVisible ? marks : NO_MARKS}
                          onStatus={onLiveStatus}
                          onSaveState={setSaveState}
                          onAnalysis={setAnalysis}
                          onGoals={setGoals}
                          onDiagnostics={setLiveDiags}
                          onContentChange={setLiveValue}
                        />
                      </div>
                    )}
                    {step && !showLive && (
                      <StepView
                        ref={stepViewRef}
                        code={step.code}
                        rows={visibleRows}
                        marks={marks}
                        dim={liveVisible && liveStatus.status === 'loading'}
                      />
                    )}
                    {!step && currentLoading && <div className="cv-note">Loading current project version…</div>}
                  </div>
                  <CanvasDrawer
                    open={drawerOpen}
                    onToggle={() => setDrawerOpen((o) => !o)}
                    tab={drawerTab}
                    onTab={setDrawerTab}
                    summary={summary}
                    meta={meta}
                    problems={problems}
                    onJump={jumpToLine}
                    goals={{ text: goalsText, hint: goalsHint, canAsk: liveGoals, onAsk: () => void askAboutGoal(), note: goalNote }}
                    verify={{
                      result: shownVerify,
                      canRun: availability.canRun,
                      busy,
                      onRun: () => void runVerify(),
                      disabledReason: availability.canRun ? null : availability.reason,
                      note: verifyNote,
                    }}
                  />
                </>
              )}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
