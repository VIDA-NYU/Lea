import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Download, LoaderCircle, PanelLeftOpen } from 'lucide-react';
import { sessionExportUrl, downloadWorkspaceExport } from '../lib/api';
import type {
  ApprovalDecision,
  ApprovalRecord,
  ChatMessage,
  CodeStep,
  Diagnostic,
  DiagnosticAction,
  SessionSummary,
  TimelineItem,
} from '../lib/api';
import { MarkdownMessage } from './MarkdownMessage';
import { LeanCheckRuntimeStatus } from './LeanCheckRuntimeStatus';
import { usageCostText, usageDetailText } from '../lib/usageDetails.mjs';
import { ModelPicker } from './ModelPicker';
import { OriginBadge } from './OriginBadge';
import { NodeChip } from './blueprint/NodeChip';
import { buildTimeline, sortCodeSteps } from '../lib/timeline.mjs';
import { matchSlashCommands } from '../lib/slashCommands.js';
import {
  deriveCodeStepProofStatus,
  deriveRunCompletionStatus,
  latestCodeStep,
} from '../lib/proofDisplay.mjs';
import { sorryLines } from '../lib/canvasModel.js';
import {
  composerStatus,
  deriveToolCalls,
  errorCount,
  previousStepByPath,
  runClock,
  type ToolCall,
} from '../lib/chatModel.js';
import { useProofSession, type CompactionPayload } from '../stores/proofSession';
import { useModel } from '../stores/model';
import { useSessions } from '../stores/sessions';
import { useWorkbench } from '../stores/workbench';
import { describeComposerContext, useFiles } from '../stores/files';
import { ApprovalRow, StepRow, ToolCallRow } from './chat/ToolRow';
import { SessionRows, subagentBadge } from './chat/SessionRows';
import { DiagnosticCard } from './chat/DiagnosticCard';
import { ComposerStatus, LeaAvatar, ThinkingRow, UserBubble } from './chat/LiveBits';
import { ChatIcon } from './chat/icons';
import '../../styles/files.css';
import '../../styles/chat.css';

type MergedNode =
  | { kind: 'message'; key: string; runId: string | null; seqKey: number; message: ChatMessage }
  // A code snapshot no tool call claimed (your edit, a file a command changed).
  | { kind: 'code'; key: string; runId: string | null; seqKey: number; step: CodeStep; codeIndex: number }
  // An approval no tool call claimed; claimed ones render inside their tool row.
  | { kind: 'approval'; key: string; runId: string | null; seqKey: number; approval: ApprovalRecord }
  // One tool call, with whatever it produced (deriveToolCalls).
  | { kind: 'tool'; key: string; runId: string | null; seqKey: number; call: ToolCall }
  | { kind: 'spawn'; key: string; runId: string | null; seqKey: number; child: SessionSummary }
  // v2.4: a failure, placed in the thread at the point it happened. Its `seq` is the
  // timeline id, so it lands next to the step that produced it — which is most of
  // what "anchored" buys over a banner floating at the bottom of the page.
  | { kind: 'diagnostic'; key: string; runId: string | null; seqKey: number; diagnostic: Diagnostic };

// A run's nodes with consecutive spawn nodes coalesced into one box, so N sub-agents
// spawned back-to-back render as a single group at their shared point in the thread.
type RenderUnit =
  | { kind: 'single'; node: MergedNode }
  | { kind: 'spawn-group'; key: string; children: SessionSummary[] };

function coalesceUnits(nodes: MergedNode[]): RenderUnit[] {
  const units: RenderUnit[] = [];
  for (const n of nodes) {
    if (n.kind === 'spawn') {
      const last = units[units.length - 1];
      if (last && last.kind === 'spawn-group') last.children.push(n.child);
      else units.push({ kind: 'spawn-group', key: n.key, children: [n.child] });
    } else {
      units.push({ kind: 'single', node: n });
    }
  }
  return units;
}

function parseTime(iso?: string | null): number {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? 0 : t;
}

export function ChatThread({
  title,
  sidebarCollapsed,
  onExpandSidebar,
  session,
  onSelectSession,
  onSelectStep,
  onDecide,
  draft,
  onDraftChange,
  onSubmit,
  onSubmitGeneral,
  submissionBusy = false,
  restoringSession = false,
  onRetryRestore,
  sourceConnection,
  onInterrupt,
  onResumeRun,
  onOpenLibrary,
  onOpenSettings,
  canvasCollapsed,
  onToggleCanvas,
  onRenameSession,
  composerFocusSignal = 0,
}: {
  title: string;
  sidebarCollapsed?: boolean;
  onExpandSidebar?: () => void;
  session?: SessionSummary;
  onSelectSession?: (id: string) => void;
  onSelectStep: (codeIndex: number) => void;
  onDecide: (decision: ApprovalDecision) => void;
  draft: string;
  onDraftChange: (value: string) => void;
  onSubmit: () => void;
  onSubmitGeneral?: () => void;
  submissionBusy?: boolean;
  restoringSession?: boolean;
  onRetryRestore?: () => void;
  sourceConnection?: { targetId: string; label: string; state: 'preparing' | 'connected' | 'error' } | null;
  onInterrupt: () => void;
  onResumeRun?: (runId: string, action?: 'resume' | 'continue') => Promise<void> | void;
  onOpenSettings?: () => void;
  onOpenLibrary?: (focus?: string) => void;
  canvasCollapsed: boolean;
  onToggleCanvas: () => void;
  onRenameSession?: (title: string) => Promise<void> | void;
  /** Bumped by App when it puts text in the composer from elsewhere (⌘P `>`, a blueprint draft). */
  composerFocusSignal?: number;
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  // v2.4: bumped to open the model picker from a diagnostic's "Change model" button.
  // A counter, not a flag, so pressing it twice re-opens the picker.
  const [modelPickerSignal, setModelPickerSignal] = useState(0);
  // Route a diagnostic's action to the thing that can actually fix it: "Change model"
  // opens the model picker itself; anything else goes to Settings. Sending both to
  // Settings made the user go and hunt for the picker they'd just asked for.
  const runDiagnosticAction = useCallback(
    (action: DiagnosticAction) => {
      // v2.5 G4: a diagnostic can now point at a Library page, not just Settings — a
      // remedy that says "check it under Library → MCP servers" is still work for the
      // user to go and find.
      if (action.action === 'open-library') onOpenLibrary?.(action.focus);
      else if (action.focus === 'model') setModelPickerSignal((n) => n + 1);
      else onOpenSettings?.();
    },
    [onOpenSettings, onOpenLibrary],
  );
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState(title);
  useEffect(() => setTitleDraft(title), [title]);
  const commitTitle = async () => {
    const next = titleDraft.trim();
    if (next && next !== title) await onRenameSession?.(next);
    else setTitleDraft(title);
    setEditingTitle(false);
  };
  // R4: model picker state + key-missing nudge from the model store.
  const model = useModel((s) => s.model);
  const modelCatalog = useModel((s) => s.modelCatalog);
  const modelFeatured = useModel((s) => s.modelFeatured);
  const keyMissing = useModel((s) => s.keyMissing);
  const onModelChange = useModel((s) => s.changeModel);
  // R1a/R1b/R1c: read shared proof-session state straight from the store (no props).
  const editedPath = useProofSession((s) => s.editedPath);
  // SafeVerify result surfaced from Edit mode as a box above the composer.
  const verifySurface = useProofSession((s) => s.verifySurface);
  const setVerifySurface = useProofSession((s) => s.setVerifySurface);
  const [verifyCollapsed, setVerifyCollapsed] = useState(false);
  // A fresh result re-opens the box (expanded).
  useEffect(() => {
    setVerifyCollapsed(false);
  }, [verifySurface]);

  // Push the SafeVerify output into the composer as a fix-it prompt, then focus so
  // the user can add context or just send. Appends if the draft already has text.
  const sendVerifyToDraft = () => {
    if (!verifySurface) return;
    const head =
      verifySurface.status === 'ok'
        ? 'SafeVerify passed, but here is the output:'
        : `SafeVerify reported "${verifySurface.status}" on this proof:`;
    const prompt = `${head}\n\n${verifySurface.detail ?? ''}\n\nPlease fix the proof so it passes SafeVerify.`.trim();
    onDraftChange(draft.trim() ? `${draft.trim()}\n\n${prompt}` : prompt);
    setVerifySurface(null);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  // Shared InfoView: the goal state captured from the live editor, surfaced above
  // the composer so the user can ask Lea about it.
  const goalSurface = useProofSession((s) => s.goalSurface);
  const setGoalSurface = useProofSession((s) => s.setGoalSurface);
  const [goalCollapsed, setGoalCollapsed] = useState(false);
  useEffect(() => {
    setGoalCollapsed(false);
  }, [goalSurface]);

  const sendGoalToDraft = () => {
    if (!goalSurface) return;
    const text = `Here's the goal I'm working on (line ${goalSurface.line}):\n\n${goalSurface.rendered}\n\n`;
    onDraftChange(draft.trim() ? `${draft.trim()}\n\n${text}` : text);
    setGoalSurface(null);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  // Files package: a file put in context ("Ask Lea about this file", "Prove the
  // sorries here", ⌘⏎ in ⌘P) shows as a chip; App prepends it to the next
  // message and clears it. "Prove the sorries" prefills an empty composer.
  const composerContext = useFiles((s) => s.composerContext);
  const setComposerContext = useFiles((s) => s.setComposerContext);
  const contextView = composerContext ? describeComposerContext(composerContext) : null;
  const focusSeen = useRef(composerFocusSignal);
  useEffect(() => {
    if (composerFocusSignal === focusSeen.current) return;
    focusSeen.current = composerFocusSignal;
    requestAnimationFrame(() => textareaRef.current?.focus());
  }, [composerFocusSignal]);
  useEffect(() => {
    if (!composerContext) return;
    const { prefill } = describeComposerContext(composerContext);
    if (prefill && !draft.trim()) onDraftChange(prefill);
    requestAnimationFrame(() => textareaRef.current?.focus());
    // Only a new context prefills; typing must not re-trigger it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [composerContext]);
  const error = useProofSession((s) => s.error);
  const reconnecting = useProofSession((s) => s.reconnecting);
  const activeCodeIndex = useProofSession((s) => s.codeIndex);
  const messages = useProofSession((s) => s.messages);
  const codeSteps = useProofSession((s) => s.codeSteps);
  const statusEvents = useProofSession((s) => s.statusEvents);
  // R1c-2b: run lifecycle + approvals from the store.
  const runStatus = useProofSession((s) => s.runStatus);
  const runStatusById = useProofSession((s) => s.runStatusById);
  const runResultKindById = useProofSession((s) => s.runResultKindById);
  const runStopReasonById = useProofSession((s) => s.runStopReasonById);
  const runRecoveryById = useProofSession((s) => s.runRecoveryById);
  const runFocusById = useProofSession((s) => s.runFocusById);
  const formalizations = useProofSession((s) => s.formalizations);
  const formalizationScope = useProofSession((s) => s.formalizationScope);
  const isRunning = useProofSession((s) => s.isRunning);
  const currentRunId = useProofSession((s) => s.currentRunId);
  const approvals = useProofSession((s) => s.approvals);
  const approvalBusy = useProofSession((s) => s.approvalBusy);
  const subagentProgress = useProofSession((s) => s.subagentProgress);
  const subagentQueued = useProofSession((s) => s.subagentQueued);
  const subagentErrors = useProofSession((s) => s.subagentErrors);
  const blueprintProposal = useWorkbench((s) => s.blueprintProposal);
  // Sub-agents (item 24): this session's children (the spawn node lists them) and, if
  // THIS session is a child, its parent (the provenance bar replaces the composer). Both
  // derive from the session list — the child rows arrive there via the subagent_finished
  // stream event / a reload.
  const allSessions = useSessions((s) => s.sessions);
  const isChild = Boolean(session?.parent_id);
  const childSessions = useMemo(
    () => (session ? allSessions.filter((s) => s.parent_id === session.id) : []),
    [allSessions, session],
  );
  const parentSession = session?.parent_id
    ? allSessions.find((s) => s.id === session.parent_id)
    : undefined;
  // R1c-2a: the timeline is derived here from store messages + codeSteps.
  const { items } = useMemo<{ items: TimelineItem[] }>(
    () => (buildTimeline as any)({ messages, codeSteps }) as { items: TimelineItem[] },
    [messages, codeSteps],
  );

  // v2.4 · every failure the backend reported, live + replayed on load. Split by
  // where each one BELONGS, so nothing is shown twice and nothing is dropped:
  //   degraded → the header strip (an ongoing condition, deduped by code)
  //   child_id → the sub-agent row that owns it (rendered by SessionRows)
  //   the rest → inline in the thread at its own seq
  const diagnostics = useProofSession((s) => s.diagnostics);
  const degradedDiagnostics = useMemo(() => {
    const byCode = new Map<string, Diagnostic>();
    for (const d of diagnostics) {
      // Keep the LATEST of each code: a condition reported repeatedly is one
      // condition, and its most recent description is the current one.
      if (d.severity === 'degraded' && !d.code.startsWith('lean.lsp_')) byCode.set(d.code, d);
    }
    return [...byCode.values()];
  }, [diagnostics]);
  const threadDiagnostics = useMemo(
    () => diagnostics.filter((d) => (d.severity !== 'degraded' || d.code.startsWith('lean.lsp_')) && !d.context?.child_id),
    [diagnostics],
  );
  const childDiagnostics = useMemo(() => {
    const byChild = new Map<string, Diagnostic[]>();
    for (const d of diagnostics) {
      const childId = d.context?.child_id;
      if (!childId) continue;
      byChild.set(childId, [...(byChild.get(childId) ?? []), d]);
    }
    return byChild;
  }, [diagnostics]);

  // M8: grow the composer with its content up to a cap, then scroll inside.
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = `${Math.min(ta.scrollHeight, 200)}px`;
  }, [draft]);

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);
  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    setAutoScroll(el.scrollHeight - el.scrollTop - el.clientHeight <= 60);
  }, []);
  useEffect(() => {
    if (autoScroll) requestAnimationFrame(scrollToBottom);
  }, [autoScroll, scrollToBottom, items, statusEvents, approvals, error]);

  // Tool rows. Every `tool_call` status the run reported, paired with what it
  // produced — its lean_check verdict, its code snapshot, its approval (which
  // carries the arguments) — so each call is one row. A write's intent narration
  // stays prose above its row, as in the mockup; the row is the action.
  const sortedSteps = useMemo(() => sortCodeSteps(codeSteps) as CodeStep[], [codeSteps]);
  const codeIndexById = useMemo(() => {
    const map = new Map<string, number>();
    for (const item of items) if (item.kind === 'code') map.set(item.step.id, item.codeIndex);
    return map;
  }, [items]);
  const previousByStep = useMemo(
    () => previousStepByPath(sortedSteps) as Map<string, CodeStep | null>,
    [sortedSteps],
  );
  const liveRunId = isRunning ? currentRunId ?? null : null;
  const derived = useMemo(
    () => deriveToolCalls({ statusEvents, codeSteps: sortedSteps, approvals, messages, diagnostics, liveRunId, runStatusById }),
    [statusEvents, sortedSteps, approvals, messages, diagnostics, liveRunId, runStatusById],
  );
  // Sub-agents render as their own session rows; the coordinator's spawn_subagent
  // call would only say the same thing once they exist.
  const hasChildren = childSessions.length > 0;
  const toolCalls = useMemo(
    () => (hasChildren ? derived.calls.filter((c) => c.name !== 'spawn_subagent') : derived.calls),
    [derived, hasChildren],
  );

  // When the UI last saw each message's text grow. A streamed message's
  // created_at is when it STARTED; this is what tells "text is flowing" from
  // "the text stopped and the model is now writing a long tool call". A message
  // first seen already written (a reload) keeps its server time.
  const seenLength = useRef<Record<string, number>>({});
  const seenGrowth = useRef<Record<string, number>>({});
  const seenAt = useMemo(() => {
    const now = Date.now();
    for (const m of messages) {
      const length = m.content?.length ?? 0;
      const previous = seenLength.current[m.id];
      seenLength.current[m.id] = length;
      if (previous !== undefined && previous !== length) seenGrowth.current[m.id] = now;
    }
    return { ...seenGrowth.current };
  }, [messages]);
  // The fallback start of a live run: when this UI first saw it running.
  const runSeen = useRef<Record<string, number>>({});
  if (isRunning && currentRunId && runSeen.current[currentRunId] == null) runSeen.current[currentRunId] = Date.now();
  const clock = useMemo(
    () => runClock({
      runId: currentRunId, calls: toolCalls, messages, codeSteps, statusEvents, approvals, seenAt,
      fallbackStart: currentRunId ? runSeen.current[currentRunId] ?? null : null,
    }),
    [currentRunId, toolCalls, messages, codeSteps, statusEvents, approvals, seenAt],
  );

  // Split the thread into contiguous per-run blocks (runs are sequential in seq
  // order). After a *finished* run that completed a checked artifact, we drop the
  // appropriate outcome card. The card is keyed on the run's outcome, never on a
  // message — so it lands once and stays (M16).
  const runGroups = useMemo(() => {
    const nodes: MergedNode[] = [];
    // Timeline anchors (created_at ↔ seqKey), ordered by seqKey — used to slot each
    // sub-agent spawn into the thread at the point it was spawned (bug-fix: interleave
    // rather than lump every child into one box at the end). seqKey and created_at are
    // both monotonic in insertion order, so mapping a child's created_at to the seqKey of
    // the last timeline row that predates it places the spawn box right after its origin.
    const anchors: { seqKey: number; createdAt: number; runId: string | null }[] = [];
    for (const it of items) {
      if (it.kind === 'message') {
        const seqKey = it.message.seq ?? Number.MAX_SAFE_INTEGER;
        nodes.push({
          kind: 'message',
          key: it.key,
          runId: it.message.run_id ?? null,
          seqKey,
          message: it.message,
        });
        anchors.push({ seqKey, createdAt: parseTime(it.message.created_at), runId: it.message.run_id ?? null });
      } else {
        const seqKey = it.step.seq ?? Number.MAX_SAFE_INTEGER;
        // A snapshot a tool call produced renders inside that call's row; it still
        // anchors the calls that came after it.
        if (!derived.pairedStepIds.has(it.step.id)) {
          nodes.push({
            kind: 'code',
            key: it.key,
            runId: it.step.run_id ?? null,
            seqKey,
            step: it.step,
            codeIndex: it.codeIndex,
          });
        }
        anchors.push({ seqKey, createdAt: parseTime(it.step.created_at), runId: it.step.run_id ?? null });
      }
    }
    for (const a of approvals) {
      if (derived.pairedApprovalIds.has(a.approval_id)) continue;
      nodes.push({
        kind: 'approval',
        key: `a:${a.run_id}:${a.approval_id}`,
        runId: a.run_id ?? null,
        seqKey: typeof a.seq === 'number' ? a.seq : Number.MAX_SAFE_INTEGER,
        approval: a,
      });
    }
    // Interleave the coordinator's children as spawn nodes. Anchor by created_at: the
    // greatest timeline row that predates the child, inheriting that row's runId so the
    // box groups with the run it belongs to. A child whose time can't be placed (or that
    // predates everything) sinks to the end rather than jumping to the top.
    anchors.sort((x, y) => x.seqKey - y.seqKey);
    const anchorFor = (createdAt: number): { seqKey: number; runId: string | null } => {
      let best: { seqKey: number; runId: string | null } | null = null;
      for (const a of anchors) {
        if (a.createdAt <= createdAt) best = { seqKey: a.seqKey, runId: a.runId };
        else break;
      }
      return best ?? { seqKey: Number.MAX_SAFE_INTEGER, runId: null };
    };
    // Tool calls carry no seq, only the time they were issued: slot each in after
    // the last row that predates it (+0.2, before any spawn box on the same row),
    // in issue order. A call older than every row goes first.
    toolCalls.forEach((call, i) => {
      let seqKey = Number.MAX_SAFE_INTEGER;
      if (call.calledAt != null) {
        const anchor = anchorFor(call.calledAt);
        seqKey = anchor.seqKey !== Number.MAX_SAFE_INTEGER
          ? anchor.seqKey + 0.2 + i * 1e-6
          : anchors.length && call.calledAt < anchors[0].createdAt
            ? anchors[0].seqKey - 0.5 + i * 1e-6
            : Number.MAX_SAFE_INTEGER;
      }
      nodes.push({ kind: 'tool', key: call.key, runId: call.runId, seqKey, call });
    });
    childSessions.forEach((child, i) => {
      const t = parseTime(child.created_at);
      const anchor = t ? anchorFor(t) : { seqKey: Number.MAX_SAFE_INTEGER, runId: null };
      nodes.push({
        kind: 'spawn',
        key: `sa:${child.id}`,
        runId: anchor.runId,
        // +0.25 so the spawn sits just after its anchor row; +i·ε keeps sibling spawns
        // stably ordered by creation among themselves.
        seqKey: anchor.seqKey + 0.25 + i * 1e-4,
        child,
      });
    });
    // v2.4: diagnostics ride the same seq ordering as everything else, so a failure
    // renders where it happened rather than in a banner detached from its cause.
    // Two exceptions are rendered by their owner instead of inline, to avoid saying
    // the same thing twice: `child_id` ones belong on the sub-agent row, and
    // `degraded` ones are ongoing conditions shown once in the header strip.
    for (const d of threadDiagnostics) {
      nodes.push({
        kind: 'diagnostic',
        key: `dg:${d.id ?? `${d.code}-${d.seq ?? ''}`}`,
        runId: d.run_id ?? null,
        seqKey: typeof d.seq === 'number' ? d.seq : Number.MAX_SAFE_INTEGER,
        diagnostic: d,
      });
    }
    nodes.sort((x, y) => x.seqKey - y.seqKey || x.key.localeCompare(y.key));
    const groups: { runId: string | null; nodes: MergedNode[] }[] = [];
    for (const node of nodes) {
      const last = groups[groups.length - 1];
      if (last && last.runId === node.runId) last.nodes.push(node);
      else groups.push({ runId: node.runId, nodes: [node] });
    }
    return groups;
  }, [items, approvals, childSessions, threadDiagnostics, derived, toolCalls]);

  const latestProofStatus = useMemo(
    () => deriveCodeStepProofStatus(latestCodeStep(codeSteps)),
    [codeSteps],
  );
  const latestRunOutcome = useMemo(
    () => {
      const values = Object.values(runStatusById);
      return values.length ? values[values.length - 1] : undefined;
    },
    [runStatusById],
  );
  const latestRunResultKind = useMemo(
    () => {
      const values = Object.values(runResultKindById);
      return values.length ? values[values.length - 1] : undefined;
    },
    [runResultKindById],
  );
  const stopReasons = Object.values(runStopReasonById);
  const latestStopReason = stopReasons.length ? stopReasons[stopReasons.length - 1] : undefined;
  const runIds = Object.keys(runStatusById);
  const latestRunId = runIds.length ? runIds[runIds.length - 1] : undefined;
  const currentTarget = formalizations.find(item =>
    (formalizationScope === item.id || (formalizationScope === 'project' && formalizations.length === 1)));
  const headChip = isRunning
    ? { cls: 'run', text: currentRunId && runRecoveryById[currentRunId]?.state === 'recovering'
      ? '● recovering saved progress' : '● proving' }
    : currentTarget && currentTarget.activity.status === 'idle'
    ? { cls: formalizationStatusClass(currentTarget.validity_status, 'idle'), text: currentTarget.validity_status }
    : latestProofStatus === 'stubbed'
    ? { cls: 'run', text: '○ stubbed' }
    : latestRunOutcome === 'disproved' && latestProofStatus === 'proved'
    ? { cls: 'warn', text: '⊘ disproved' }
    : latestRunOutcome === 'needs_review'
    ? { cls: 'idle', text: 'unchecked' }
    : (latestRunOutcome === 'proved' || latestRunOutcome === 'success') &&
        latestRunResultKind === 'defined' &&
        (latestProofStatus === 'proved' || latestProofStatus === 'defined')
    ? { cls: 'ok', text: '✓ defined' }
    : (latestRunOutcome === 'proved' || latestRunOutcome === 'success') &&
        latestProofStatus === 'proved'
    ? { cls: 'ok', text: '✓ proved' }
    : runStatus === 'failed' || runStatus === 'max_turns'
    ? { cls: 'fail', text: '✕ unproved' }
    : runStatus === 'cancelled'
    ? { cls: 'fail', text: latestRunId && runRecoveryById[latestRunId]?.state === 'review'
      ? 'Interrupted — review before resuming' : latestStopReason === 'timeout' ? '◼ time limit reached'
      : latestStopReason === 'user_stop' ? '◼ stopped by user' : '◼ stopped' }
    : null;

  const hasPendingApproval = approvals.some((a) => !a.decision && a.actionable !== false);
  const runningChildren = isRunning
    ? childSessions.filter((c) => !subagentErrors[c.id]
      && (subagentQueued[c.id] || subagentProgress[c.id] || subagentBadge(c).running)).length
    : 0;

  // Stop: the click registers at once ("Stopping…") and stays registered until
  // the run settles; on reload the run row's stop request says the same thing.
  const [stopSentFor, setStopSentFor] = useState<string | null>(null);
  useEffect(() => {
    if (error) setStopSentFor(null); // the interrupt request itself failed
  }, [error]);
  const stopRequested = Boolean(
    isRunning && currentRunId && (stopSentFor === currentRunId || runStopReasonById[currentRunId]),
  );
  const handleStop = () => {
    if (currentRunId) setStopSentFor(currentRunId);
    onInterrupt();
  };

  // The model's own turn, made visible: a live run with no text flowing, no tool
  // in flight, nothing waiting on the user and no sub-agent working gets a
  // "Thinking · 1m 23s" row from the last thing the run did. A streamed message
  // that stops growing hands over to it after a few quiet seconds (the model is
  // then usually writing a long tool call, which streams nothing).
  const thinking =
    isRunning && !hasPendingApproval && !clock.busy && runningChildren === 0;
  const thinkingRow = thinking ? (
    <ThinkingRow
      key="thinking"
      since={clock.streaming ? clock.streamingSince : clock.lastActivityAt}
      quietMs={clock.streaming ? 3000 : 0}
      label={runStatus === 'pending' ? 'Waiting to start' : 'Thinking'}
    />
  ) : null;

  // The composer's status line.
  const outcome = useMemo(() => {
    const runId = latestRunId;
    const status = (runId && runStatusById[runId]) || runStatus;
    if (!status) return null;
    const runSteps = runId ? sortedSteps.filter((s) => s.run_id === runId) : [];
    const latest = runSteps.length ? runSteps[runSteps.length - 1] : null;
    return {
      status,
      resultKind: runId ? runResultKindById[runId] : null,
      stopReason: runId ? runStopReasonById[runId] : latestStopReason,
      proof: latest ? deriveCodeStepProofStatus(latest) : null,
      sorries: latest ? sorryLines(latest.code).length : 0,
      errors: latest ? errorCount(latest.check_detail) : null,
    };
  }, [latestRunId, runStatusById, runStatus, sortedSteps, runResultKindById, runStopReasonById, latestStopReason]);
  const status = composerStatus({
    live: isRunning,
    pending: runStatus === 'pending',
    stopRequested,
    waitingApproval: hasPendingApproval,
    proposalInReview: Boolean(
      blueprintProposal?.phase === 'review' && session?.project_id && blueprintProposal.projectId === session.project_id,
    ),
    turn: clock.turn,
    startedAt: clock.startedAt,
    subagentsRunning: runningChildren,
    outcome,
  });

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!isRunning && !submissionBusy && draft.trim()) onSubmit();
    }
  };

  const renderNode = (node: MergedNode): ReactNode => {
    // Spawn nodes are rendered by coalesceUnits → <SessionRows>, never here.
    if (node.kind === 'spawn') return null;
    if (node.kind === 'diagnostic') {
      return (
        <DiagnosticCard key={node.key} diagnostic={node.diagnostic} onAction={runDiagnosticAction} />
      );
    }
    if (node.kind === 'approval') {
      return <ApprovalRow key={node.key} approval={node.approval} busy={approvalBusy} onDecide={onDecide} />;
    }
    if (node.kind === 'tool') {
      const step = node.call.step as CodeStep | null;
      return (
        <ToolCallRow
          key={node.key}
          call={node.call}
          previousStep={step ? previousByStep.get(step.id) ?? null : null}
          codeIndexById={codeIndexById}
          activeCodeIndex={activeCodeIndex}
          onSelectStep={onSelectStep}
          onDecide={onDecide}
          approvalBusy={approvalBusy}
        />
      );
    }
    if (node.kind === 'message') {
      const m = node.message;
      if (m.kind === 'compaction') {
        return <CompactionMarker key={node.key} content={m.content} />;
      }
      if (m.role === 'user') {
        return <UserBubble key={node.key} content={m.content} />;
      }
      if (!m.content.trim()) return null;
      return (
        <div className="chat-prose" key={node.key}>
          <MarkdownMessage content={m.content} />
        </div>
      );
    }
    return (
      <StepRow
        key={node.key}
        step={node.step}
        previousStep={previousByStep.get(node.step.id) ?? null}
        codeIndex={node.codeIndex}
        active={node.codeIndex === activeCodeIndex}
        onSelectStep={onSelectStep}
      />
    );
  };

  // Lea's output between two user messages is one block: the avatar once, then
  // prose and tool rows in a column, as in the mockup. User bubbles and
  // compaction markers stand on their own.
  const renderBlocks = (units: RenderUnit[], tail: ReactNode[], keyPrefix: string) => {
    const out: ReactNode[] = [];
    let block: ReactNode[] = [];
    let blockKey = '';
    const flush = () => {
      if (!block.length) return;
      out.push(
        <div className="chat-turn" key={`${keyPrefix}:${blockKey}`}>
          <LeaAvatar />
          <div className="chat-turn-col">{block}</div>
        </div>,
      );
      block = [];
    };
    for (const unit of units) {
      if (unit.kind === 'single' && unit.node.kind === 'message'
        && (unit.node.message.role === 'user' || unit.node.message.kind === 'compaction')) {
        flush();
        const rendered = renderNode(unit.node);
        if (rendered) out.push(rendered);
        continue;
      }
      const rendered = unit.kind === 'spawn-group' ? (
        <SessionRows
          key={unit.key}
          children={unit.children}
          onSelectSession={onSelectSession}
          diagnosticsByChild={childDiagnostics}
        />
      ) : renderNode(unit.node);
      if (!rendered) continue;
      if (!block.length) blockKey = unit.kind === 'single' ? unit.node.key : unit.key;
      block.push(rendered);
    }
    for (const item of tail) {
      if (!item) continue;
      if (!block.length) blockKey = 'tail';
      block.push(item);
    }
    flush();
    return out;
  };
  const lastGroupRunId = runGroups.length ? runGroups[runGroups.length - 1].runId : undefined;
  const thinkingInLastGroup = Boolean(thinkingRow && runGroups.length && lastGroupRunId === currentRunId);

  return (
    <main className="chat">
      <div className="pane-head">
        {sidebarCollapsed && (
          <button className="icon-btn" onClick={onExpandSidebar} title="Open sidebar">
            <PanelLeftOpen size={15} />
          </button>
        )}
        {editingTitle ? (
          <input
            className="title-edit"
            value={titleDraft}
            autoFocus
            onChange={(event) => setTitleDraft(event.target.value)}
            onBlur={() => void commitTitle()}
            onKeyDown={(event) => {
              if (event.key === 'Enter') void commitTitle();
              if (event.key === 'Escape') {
                setTitleDraft(title);
                setEditingTitle(false);
              }
            }}
          />
        ) : (
          <button
            className="ttl title-button"
            onDoubleClick={() => session && setEditingTitle(true)}
            title={session ? 'Double-click to rename this conversation' : undefined}
          >
            {title}
          </button>
        )}
        {headChip && <span className={`chip ${headChip.cls}`}>{headChip.text}</span>}
        <NodeChip sessionId={session?.id} />
        <OriginBadge origin={session?.origin} originUrl={session?.origin_url} />
        <span className="head-spacer" />
        {/* Download this session's files as a zip (#14). Loose sessions only — a
            project session's files download from the project's Filesystem tab. Shown
            once the session has written at least one file (else export would 404). */}
        {session && !session.project_id && codeSteps.length > 0 && (
          <a
            className="head-download"
            href={sessionExportUrl(session.id)}
            onClick={(event)=>{event.preventDefault();downloadWorkspaceExport(sessionExportUrl(session.id)).catch(error=>window.alert(String(error)));}}
            download
            title="Download this session's files as a zip"
          >
            <Download size={13} /> Download
          </a>
        )}
        <ModelPicker
          value={model || ''}
          onChange={onModelChange}
          catalog={modelCatalog}
          featured={modelFeatured}
          openSignal={modelPickerSignal}
        />
        {/* Only a "Show canvas" affordance when the canvas is hidden — when it's
            open the Canvas's own × closes it, so a second toggle here is redundant. */}
        {canvasCollapsed && (
          <button className="canvas-toggle" onClick={onToggleCanvas}>
            ◧ Show canvas
          </button>
        )}
      </div>

      <div className="thread" ref={scrollRef} onScroll={onScroll} aria-busy={restoringSession}>
        <div className="thread-inner">
          {restoringSession && !error && (
            <div className="session-restore" role="status">
              <LoaderCircle size={20} className="session-restore-spinner" aria-hidden="true" />
              <strong>Restoring session…</strong>
              <p>Loading your conversation and proof context.</p>
            </div>
          )}
          {items.length === 0 && !error && !restoringSession && (
            <div className="thread-empty">
              Enter a theorem or natural-language proof task to start Lea.
            </div>
          )}

          {runGroups.map((group, gi) => {
            const isLastGroup = gi === runGroups.length - 1;
            const finished = !(isRunning && isLastGroup);
            const status = group.runId ? runStatusById[group.runId] : undefined;
            // This run's snapshots, whether they render as their own rows or
            // inside the tool row that produced them.
            const codeStepList = group.runId
              ? sortedSteps.filter((s) => s.run_id === group.runId)
              : group.nodes.flatMap((n) =>
                n.kind === 'code' ? [n.step] : n.kind === 'tool' && n.call.step ? [n.call.step as CodeStep] : []);
            const steps = codeStepList.length;
            const resultKind = group.runId ? runResultKindById[group.runId] : undefined;
            const focusId = group.runId ? runFocusById[group.runId] : undefined;
            const focused = focusId
              ? formalizations.find((item) => item.id === focusId)
              : undefined;
            const completion = deriveRunCompletionStatus(status, codeStepList, resultKind);
            return (
              <Fragment key={group.runId ?? `g${gi}`}>
                {group.runId && (
                  <div className="run-episode">
                    <span>Run {gi + 1}</span>
                    <strong>
                      {focused
                        ? `Working on ${focused.declaration_name || focused.display_title}`
                        : 'Project discussion'}
                    </strong>
                    {runRecoveryById[group.runId]?.state === 'recovering' && <span>Recovering saved progress</span>}
                    {runRecoveryById[group.runId]?.state === 'review' && (
                      <span title={runRecoveryById[group.runId]?.reason || undefined}>
                        Interrupted — review before resuming. {runRecoveryById[group.runId]?.reason}
                        {onResumeRun && <button type="button" disabled={isRunning || submissionBusy} onClick={() => void onResumeRun(group.runId!, 'resume')}>
                          Resume after review
                        </button>}
                      </span>
                    )}
                    {runRecoveryById[group.runId]?.action === 'continue' && onResumeRun && (
                      <button type="button" disabled={isRunning || submissionBusy} onClick={() => void onResumeRun(group.runId!, 'continue')}>
                        Resume faithfully
                      </button>
                    )}
                  </div>
                )}
                {/* The 'proved' and 'failed/max_turns' banners are gone: both restated
                    what the header chip already says (✓ proved / ✕ unproved), so the
                    common outcomes ended every run with a badge saying what you could
                    already see. The remaining cards are kept because each carries
                    something the chip does NOT: a definition rather than a proof, a
                    counterexample, or a proof that compiles but still has a `sorry`. */}
                {renderBlocks(coalesceUnits(group.nodes), [
                  finished && completion === 'defined' ? <DefinedCard key="defined" steps={steps} session={session} /> : null,
                  finished && completion === 'disproved' ? <DisprovedCard key="disproved" steps={steps} session={session} /> : null,
                  finished && completion === 'stubbed' ? <StubCard key="stubbed" steps={steps} session={session} /> : null,
                  isLastGroup && thinkingInLastGroup ? thinkingRow : null,
                ], group.runId ?? `g${gi}`)}
              </Fragment>
            );
          })}

          {thinkingRow && !thinkingInLastGroup && renderBlocks([], [thinkingRow], 'thinking')}

          {reconnecting && (
            <div className="reconnect-chip">
              <span className="reconnect-spinner" />
              {reconnecting}
            </div>
          )}
          {/* Degraded capabilities are pinned at the foot of the thread, not
              interleaved: they describe a condition that is STILL TRUE, so they
              belong where they stay visible rather than scrolling away like an
              event. One row per code — a fallback reported forty times is one fact. */}
          <LeanCheckRuntimeStatus sessionId={session?.id} active={isRunning} refreshKey={diagnostics.length} />
          {degradedDiagnostics.map((d) => (
            <DiagnosticCard key={`deg:${d.code}`} diagnostic={d} onAction={runDiagnosticAction} />
          ))}
          {/* The banner survives, narrowed (v2.4) to client-side action failures —
              a fetch that didn't land, a button that failed. Everything the run
              reports is a Diagnostic with a place to live. */}
          {error && <div className="err-banner" role="alert">
            {error}
            {onRetryRestore && <button className="restore-retry" onClick={onRetryRestore}>Retry loading</button>}
          </div>}
        </div>
      </div>

      {isChild ? (
        /* A child is a session, not a chat (item 24): read-only, so the composer is
           replaced by a provenance bar. The child's interlocutor is the coordinator,
           which is blocked awaiting the typed result — typing here would inject the
           user into a delegation the parent thinks it owns. */
        <div className="prov">
          <span className="lock">🔒</span>
          <span>
            Delegated by <b>{parentSession?.title ?? 'coordinator'}</b>
            {session?.spawned_at_turn != null && <> · turn {session.spawned_at_turn}</>}
            {session?.role && (
              <>
                {' · '}
                <span className="role">{session.role}</span>
              </>
            )}
          </span>
          {parentSession && (
            <button className="up" onClick={() => onSelectSession?.(parentSession.id)}>
              Back to parent
            </button>
          )}
        </div>
      ) : (
      <div className="composer">
        <FormalizationScope session={session} />
        {keyMissing && (
          <div className="key-nudge">
            <span>
              👋 To start proving, add your model preference and API key.
            </span>
            <button className="key-nudge-btn" onClick={onOpenSettings}>
              Open Settings
            </button>
          </div>
        )}
        {editedPath && (
          <div className="edit-badge">
            ✎ You edited <code>{editedPath.split('/').pop()}</code> — describe your change so Lea
            picks up where you left off.
          </div>
        )}
        {verifySurface && (
          <div className={`verify-box ${verifySurface.status === 'ok' && verifySurface.current === true ? 'ok' : 'bad'}`}>
            <div className="verify-box-head">
              <span className="verify-box-title">
                🛡 SafeVerify {verifySurface.status === 'ok'
                  ? verifySurface.current === true ? '✓ passed' : 'recheck required'
                  : verifySurface.status}
              </span>
              <span className="verify-box-spacer" />
              {verifySurface.detail && (
                <button
                  className="verify-box-icon"
                  onClick={() => setVerifyCollapsed((c) => !c)}
                  title={verifyCollapsed ? 'Expand' : 'Collapse'}
                >
                  {verifyCollapsed ? '▸' : '▾'}
                </button>
              )}
              <button
                className="verify-box-icon"
                onClick={() => setVerifySurface(null)}
                title="Dismiss"
              >
                ✕
              </button>
            </div>
            {!verifyCollapsed && verifySurface.detail && (
              <pre className="verify-box-detail">{verifySurface.detail}</pre>
            )}
            {verifySurface.status !== 'ok' && (
              <div className="verify-box-actions">
                <button className="verify-box-fix" onClick={sendVerifyToDraft}>
                  ↑ Send to Lea to fix
                </button>
              </div>
            )}
          </div>
        )}
        {goalSurface && (
          <div className="verify-box goal">
            <div className="verify-box-head">
              <span className="verify-box-title">🧩 Goal at line {goalSurface.line}</span>
              <span className="verify-box-spacer" />
              <button
                className="verify-box-icon"
                onClick={() => setGoalCollapsed((c) => !c)}
                title={goalCollapsed ? 'Expand' : 'Collapse'}
              >
                {goalCollapsed ? '▸' : '▾'}
              </button>
              <button className="verify-box-icon" onClick={() => setGoalSurface(null)} title="Dismiss">
                ✕
              </button>
            </div>
            {!goalCollapsed && <pre className="verify-box-detail">{goalSurface.rendered}</pre>}
            <div className="verify-box-actions">
              <button className="verify-box-fix" onClick={sendGoalToDraft}>
                ↑ Ask Lea about this goal
              </button>
            </div>
          </div>
        )}
        {(() => {
          // Slash-command autocomplete (G3 framework): while the draft is a command being
          // typed (`/` + name, before any space), list matching commands from the registry.
          // Clicking one fills the draft; Enter still runs it via the normal submit path.
          const q = draft.trim();
          const typing = q.startsWith('/') && !q.includes(' ');
          const matches = typing ? matchSlashCommands(q) : [];
          if (!matches.length) return null;
          return (
            <div className="slash-menu">
              {matches.map((c) => (
                <button
                  key={c.name}
                  type="button"
                  className="slash-item"
                  onClick={() => {
                    onDraftChange(`/${c.name}`);
                    textareaRef.current?.focus();
                  }}
                >
                  <span className="slash-name">/{c.name}</span>
                  <span className="slash-desc">{c.description}</span>
                </button>
              ))}
              <div className="slash-hint">↵ to run</div>
            </div>
          );
        })()}
        {sourceConnection && (
          <div className="composer-source" role="status" aria-live="polite">
            <span>{sourceConnection.state === 'preparing' ? 'Refreshing Overleaf source' : sourceConnection.state === 'connected' ? 'Overleaf source refreshed' : 'Overleaf source unavailable'} · {sourceConnection.label}</span>
            {sourceConnection.state === 'error' && onSubmitGeneral && draft.trim() && (
              <button type="button" disabled={isRunning || submissionBusy} onClick={onSubmitGeneral} title="Send this message without publishing an Overleaf status assessment">
                Send as general chat
              </button>
            )}
          </div>
        )}
        {composerContext && contextView && (
          <div className="composer-context">
            <span className="composer-context-chip" title={composerContext.path}>
              <span className="composer-context-name">{contextView.chip}</span>
              <span className="composer-context-note">· in context</span>
              <button type="button" onClick={() => setComposerContext(null)} aria-label={`Remove ${contextView.chip} from context`} title="Remove from context">
                ×
              </button>
            </span>
          </div>
        )}
        <div className="composer-inner" aria-busy={submissionBusy}>
          <textarea
            ref={textareaRef}
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              editedPath
                ? 'What did you change? A short note helps Lea pick up where you left off.'
                : contextView?.placeholder ?? 'Ask a follow-up, or state a theorem to prove…'
            }
            rows={1}
            disabled={submissionBusy}
          />
          <div className="crow">
            <ComposerStatus status={status} />
            <span className="crow-gap" />
            <ComposerScopeChip session={session} />
            {isRunning ? (
              <button
                type="button"
                className="chat-stop"
                onClick={handleStop}
                title={stopRequested ? 'Stop requested — Lea stops after the current step' : 'Stop the run'}
              >
                <ChatIcon name="stop" size={12} />
                Stop
              </button>
            ) : (
              <button className="send" onClick={onSubmit} disabled={!draft.trim() || submissionBusy} title={submissionBusy ? 'Preparing your message' : 'Send'}>
                ↑
              </button>
            )}
          </div>
        </div>
      </div>
      )}
    </main>
  );
}

// The rail: which formalization you are LOOKING AT. Purely a view filter — with one
// exception, "+ New formalization", which also pins the composer, because "show me the
// one that doesn't exist yet" only means anything as an intention about the next
// message. Where the next message GOES is the composer chip's job (`ComposerScopeChip`).
function FormalizationScope({ session }: { session?: SessionSummary }) {
  const formalizations = useProofSession((state) => state.formalizations);
  const scope = useProofSession((state) => state.formalizationScope);
  const setScope = useProofSession((state) => state.setFormalizationScope);
  const setOverride = useProofSession((state) => state.setComposerScopeOverride);
  const setCanvasRevisionMode = useProofSession(
    (state) => state.setCanvasRevisionMode,
  );
  const viewScope = (nextScope: string) => {
    setScope(nextScope);
    setOverride(null);
    setCanvasRevisionMode('current');
  };
  const manuallyTarget = (nextScope: string) => {
    setScope(nextScope);
    setOverride(nextScope);
    setCanvasRevisionMode('current');
  };

  return (
    <div className="form-scope">
      <div className="form-rail" aria-label="Session formalizations">
        <button
          type="button"
          className={scope === 'project' ? 'active' : ''}
          onClick={() => viewScope('project')}
        >
          All work
        </button>
        {formalizations.map((item) => (
          <button
            type="button"
            key={item.id}
            className={scope === item.id ? 'active' : ''}
            onClick={() => viewScope(item.id)}
            title={item.primary_path || item.statement || item.display_title}
          >
            <span className={`form-dot ${formalizationStatusClass(item.validity_status, item.activity.status)}`} />
            {/* Its own element so it can ellipsize: a bare text node in a flex row has no
                box to clip, which is how a whole prompt ended up spilling across the rail. */}
            <span className="form-label">{item.declaration_name || item.display_title}</span>
            <small title={item.validity_reason || undefined}>
              {item.activity.status !== 'idle' ? item.activity.status : item.validity_status}
            </small>
          </button>
        ))}
        <button
          type="button"
          className={scope === 'new' ? 'active new' : 'new'}
          onClick={() => manuallyTarget('new')}
        >
          + New formalization
        </button>
      </div>
    </div>
  );
}

/**
 * Where the NEXT message goes — living in the composer, next to send, because that is
 * what it is a property of.
 *
 * It replaces a `<span className="mode">⚙ auto</span>` that had `cursor: pointer` and
 * no click handler: it advertised an affordance it did not have, and its tooltip was
 * about the prover's prompt ROUTING, not formalization scope. Meanwhile the control
 * that actually set scope sat above the composer, also reading "automatic". Two things
 * saying "auto", the inert one in the place people would look first.
 *
 * `auto` is not a mode the user picks so much as the absence of an override: with no
 * override, the backend infers the target from the message and the declarations Lea
 * actually edits. Pinning one is the exception, so the chip states the pin when there
 * is one and stays quiet otherwise.
 */
function ComposerScopeChip({ session }: { session?: SessionSummary }) {
  const formalizations = useProofSession((state) => state.formalizations);
  const override = useProofSession((state) => state.composerScopeOverride);
  const setOverride = useProofSession((state) => state.setComposerScopeOverride);
  const setScope = useProofSession((state) => state.setFormalizationScope);
  const setCanvasRevisionMode = useProofSession((state) => state.setCanvasRevisionMode);
  const [open, setOpen] = useState(false);
  const projectLabel = session?.project_id ? 'Project discussion' : 'General discussion';
  const pinned =
    override && override !== 'project' && override !== 'new'
      ? formalizations.find((item) => item.id === override)
      : undefined;
  const label =
    override === 'project'
      ? projectLabel
      : override === 'new'
        ? 'New formalization'
        : pinned
          ? pinned.declaration_name || pinned.display_title
          : null;

  const target = (next: string | null) => {
    setOverride(next);
    if (next) {
      setScope(next);
      setCanvasRevisionMode('current');
    }
    setOpen(false);
  };

  // Dismiss on Escape — the menu overlays the composer, and the keyboard is where the
  // user already is.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  return (
    <div className="scope-override">
      <button
        type="button"
        className={`mode scope-chip ${override ? 'manual' : ''}`}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((v) => !v)}
        title={
          label
            ? `The next message is pinned to ${label}. Click to change.`
            : 'Lea infers which formalization your message is about. Click to pin it.'
        }
      >
        <span className="scope-chip-dot" />
        {label ? `⚙ ${label}` : '⚙ auto'}
        <span aria-hidden="true">⌄</span>
      </button>
      {open && (
        <div className="scope-menu" role="menu" aria-label="Where the next message goes">
          <button type="button" className={!override ? 'active' : ''} onClick={() => target(null)}>
            <span>Automatic</span>
            <small>Infer from your message and actual edits</small>
          </button>
          <button
            type="button"
            className={override === 'project' ? 'active' : ''}
            onClick={() => target('project')}
          >
            <span>{projectLabel}</span>
            <small>Do not assign the next run to one item</small>
          </button>
          <button
            type="button"
            className={override === 'new' ? 'active' : ''}
            onClick={() => target('new')}
          >
            <span>New formalization</span>
            <small>Start a new one rather than continuing an existing one</small>
          </button>
          {formalizations.map((item) => (
            <button
              type="button"
              key={item.id}
              className={override === item.id ? 'active' : ''}
              onClick={() => target(item.id)}
            >
              <span>{item.declaration_name || item.display_title}</span>
              <small>Manually target this formalization</small>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function formalizationStatusClass(validity: string, activity: string): string {
  if (activity !== 'idle') return 'run';
  if (validity === 'proved' || validity === 'defined') return 'ok';
  if (validity === 'failing') return 'fail';
  if (validity === 'stale') return 'warn';
  return 'idle';
}

// A context-compaction marker (G1/G3), rendered inline from a `kind='compaction'` timeline
// message. Manual (/compact) → an expandable Claude-Code-style card with the freed tokens
// + the files still in the model's view; automatic (G1) → a quiet centered one-liner. Both
// are durable (they ride the message channel), so they survive a reload.
function CompactionMarker({ content }: { content: string }) {
  let c: CompactionPayload | null = null;
  try {
    c = JSON.parse(content) as CompactionPayload;
  } catch {
    c = null;
  }
  if (!c) return null;
  // In-flight: the /compact request hasn't returned yet. Show a spinner so the user knows
  // work is happening (the call can take seconds when it summarizes).
  if (c.pending) {
    return (
      <div className="compact-note">
        <span className="reconnect-spinner" />
        Compacting context…
      </div>
    );
  }
  const didWork = c.pruned > 0 || c.summarized;
  const parts: string[] = [];
  if (c.pruned > 0) parts.push(`pruned ${c.pruned} stale output${c.pruned === 1 ? '' : 's'}`);
  if (c.summarized) parts.push('summarized earlier work');
  const files = c.referenced_files || [];
  const before = c.before_tokens || 0;
  const after = c.after_tokens || 0;
  const freed = before - after;

  if (!c.manual) {
    return (
      <div className="compact-note">
        <span className="compact-icon">🗜</span>
        {didWork ? (
          <>
            Context compacted — {parts.join(', ')}
            {before > 0 && after > 0 && after < before ? (
              <span className="compact-delta">
                {' '}~{formatTokens(before)} → {formatTokens(after)} tokens
              </span>
            ) : null}
          </>
        ) : (
          'Context already compact'
        )}
      </div>
    );
  }

  return (
    <details className="compact-card" open>
      <summary>
        <span className="compact-icon">🗜</span>
        {didWork ? (
          <>
            Compacted{freed > 0 ? <> — freed ~{formatTokens(freed)} tokens</> : null}
          </>
        ) : (
          'Already compact — nothing to free'
        )}
        {didWork && (files.length > 0 || c.summarized) ? (
          <span className="compact-more">details</span>
        ) : null}
      </summary>
      {didWork ? (
        <div className="compact-body">
          {parts.length ? <div className="compact-line">{parts.join(', ')}</div> : null}
          {before > 0 && after > 0 ? (
            <div className="compact-line compact-delta">
              ~{formatTokens(before)} → {formatTokens(after)} tokens
            </div>
          ) : null}
          {files.length > 0 ? (
            <ul className="compact-files">
              {files.map((f) => (
                <li key={f}>
                  <code>{f}</code>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </details>
  );
}

function DefinedCard({ steps, session }: { steps: number; session?: SessionSummary }) {
  return (
    <div className="final">
      <div className="fhead">✓ Definition created — 0 errors</div>
      {session && (
        <div className="meta">
          <span>{steps} steps</span>
          {session.total_tokens ? <span>{formatTokens(session.total_tokens)} tokens</span> : null}
          {session.cost_usd || session.usage_details?.unpriced_calls ? <span title={usageDetailText(session.usage_details)}>{usageCostText(session.cost_usd, session.usage_details, 3)}</span> : null}
          {session.duration_seconds ? <span>{session.duration_seconds}s</span> : null}
        </div>
      )}
    </div>
  );
}

function DisprovedCard({ steps, session }: { steps: number; session?: SessionSummary }) {
  return (
    <div className="final disproof">
      <div className="fhead">⊘ Counterexample found — the original statement was disproven, not proven</div>
      {session && (
        <div className="meta">
          <span>{steps} steps</span>
          {session.total_tokens ? <span>{formatTokens(session.total_tokens)} tokens</span> : null}
          {session.cost_usd || session.usage_details?.unpriced_calls ? <span title={usageDetailText(session.usage_details)}>{usageCostText(session.cost_usd, session.usage_details, 3)}</span> : null}
          {session.duration_seconds ? <span>{session.duration_seconds}s</span> : null}
        </div>
      )}
    </div>
  );
}

function StubCard({ steps, session }: { steps: number; session?: SessionSummary }) {
  return (
    <div className="final stub">
      <div className="fhead">✓ Stub checked — 0 errors, proof still contains sorry</div>
      {session && (
        <div className="meta">
          <span>{steps} steps</span>
          {session.total_tokens ? <span>{formatTokens(session.total_tokens)} tokens</span> : null}
          {session.cost_usd || session.usage_details?.unpriced_calls ? <span title={usageDetailText(session.usage_details)}>{usageCostText(session.cost_usd, session.usage_details, 3)}</span> : null}
          {session.duration_seconds ? <span>{session.duration_seconds}s</span> : null}
        </div>
      )}
    </div>
  );
}

function formatTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}
