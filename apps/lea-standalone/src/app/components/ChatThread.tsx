import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Download, PanelLeftOpen } from 'lucide-react';
import { sessionExportUrl, interruptSubagent } from '../lib/api';
import type {
  ApprovalDecision,
  ApprovalRecord,
  ChatMessage,
  CodeStep,
  Diagnostic,
  DiagnosticAction,
  PendingApproval,
  SessionSummary,
  StatusEvent,
  TimelineItem,
} from '../lib/api';
import { MarkdownMessage } from './MarkdownMessage';
import { ModelPicker } from './ModelPicker';
import { OriginBadge } from './OriginBadge';
import { buildTimeline } from '../lib/timeline.mjs';
import { matchSlashCommands } from '../lib/slashCommands.js';
import {
  deriveCodeStepProofStatus,
  deriveRunCompletionStatus,
  hasSorryLikeCheckDetail,
  latestCodeStep,
} from '../lib/proofDisplay.mjs';
import { useProofSession, type CompactionPayload } from '../stores/proofSession';
import { useModel } from '../stores/model';
import { useSessions } from '../stores/sessions';

type MergedNode =
  | { kind: 'message'; key: string; runId: string | null; seqKey: number; message: ChatMessage }
  | { kind: 'code'; key: string; runId: string | null; seqKey: number; step: CodeStep; codeIndex: number }
  | { kind: 'approval'; key: string; runId: string | null; seqKey: number; approval: ApprovalRecord }
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
  onInterrupt,
  onOpenLibrary,
  onOpenSettings,
  canvasCollapsed,
  onToggleCanvas,
  onRenameSession,
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
  onInterrupt: () => void;
  onOpenSettings?: () => void;
  onOpenLibrary?: (focus?: string) => void;
  canvasCollapsed: boolean;
  onToggleCanvas: () => void;
  onRenameSession?: (title: string) => Promise<void> | void;
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
  const runFocusById = useProofSession((s) => s.runFocusById);
  const formalizations = useProofSession((s) => s.formalizations);
  const isRunning = useProofSession((s) => s.isRunning);
  const currentRunId = useProofSession((s) => s.currentRunId);
  const approvals = useProofSession((s) => s.approvals);
  const approvalBusy = useProofSession((s) => s.approvalBusy);
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
  //   child_id → the sub-agent row that owns it (rendered by SpawnGroup)
  //   the rest → inline in the thread at its own seq
  const diagnostics = useProofSession((s) => s.diagnostics);
  const degradedDiagnostics = useMemo(() => {
    const byCode = new Map<string, Diagnostic>();
    for (const d of diagnostics) {
      // Keep the LATEST of each code: a condition reported repeatedly is one
      // condition, and its most recent description is the current one.
      if (d.severity === 'degraded') byCode.set(d.code, d);
    }
    return [...byCode.values()];
  }, [diagnostics]);
  const threadDiagnostics = useMemo(
    () => diagnostics.filter((d) => d.severity !== 'degraded' && !d.context?.child_id),
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

  // tool chips for a step come from that turn's tool_call / lean_check status events.
  const toolsByTurn = useMemo(() => {
    const map = new Map<number, StatusEvent[]>();
    for (const event of statusEvents) {
      if (event.turn == null) continue;
      if (event.status !== 'tool_call' && event.status !== 'lean_check') continue;
      if (!map.has(event.turn)) map.set(event.turn, []);
      map.get(event.turn)!.push(event);
    }
    return map;
  }, [statusEvents]);

  // M11: a write's intent narration is also stamped on its code step's summary.
  // Fold it into the step card instead of showing it twice.
  const foldedNarration = useMemo(() => {
    const set = new Set<string>();
    for (const item of items) {
      if (item.kind === 'code' && item.step.summary) {
        set.add(`${item.step.run_id || ''}|${item.step.summary.trim()}`);
      }
    }
    return set;
  }, [items]);

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
        nodes.push({
          kind: 'code',
          key: it.key,
          runId: it.step.run_id ?? null,
          seqKey,
          step: it.step,
          codeIndex: it.codeIndex,
        });
        anchors.push({ seqKey, createdAt: parseTime(it.step.created_at), runId: it.step.run_id ?? null });
      }
    }
    for (const a of approvals) {
      nodes.push({
        kind: 'approval',
        key: `a:${a.approval_id}`,
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
  }, [items, approvals, childSessions, threadDiagnostics]);

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
  const headChip = isRunning
    ? { cls: 'run', text: '● proving' }
    : latestProofStatus === 'stubbed'
    ? { cls: 'run', text: '○ stubbed' }
    : latestRunOutcome === 'disproved' && latestProofStatus === 'proved'
    ? { cls: 'warn', text: '⊘ disproved' }
    : (latestRunOutcome === 'proved' || latestRunOutcome === 'success' || latestRunOutcome === 'needs_review') &&
        latestRunResultKind === 'defined' &&
        (latestProofStatus === 'proved' || latestProofStatus === 'defined')
    ? { cls: 'ok', text: '✓ defined' }
    : (latestRunOutcome === 'proved' || latestRunOutcome === 'success' || latestRunOutcome === 'needs_review') &&
        latestProofStatus === 'proved'
    ? { cls: 'ok', text: '✓ proved' }
    : runStatus === 'failed' || runStatus === 'max_turns'
    ? { cls: 'fail', text: '✕ unproved' }
    : runStatus === 'cancelled'
    ? { cls: 'fail', text: '◼ stopped' }
    : null;

  // M18: show "Lea is thinking…" whenever a run is live and Lea isn't currently
  // streaming text (the gap after submit + between turns while a tool runs) and
  // we're not paused on an approval. The live bubble (live:true) means text is
  // already flowing, so the indicator yields to it.
  const hasPendingApproval = approvals.some((a) => !a.decision);
  const thinking =
    isRunning &&
    !hasPendingApproval &&
    !items.some((i) => i.kind === 'message' && i.message.live);

  // M17: the tool Lea is currently running (all tools), scoped to the active run
  // so a prior run's last tool never leaks in. Drives the activity label.
  const activity = useMemo(() => {
    if (!thinking) return null;
    for (let i = statusEvents.length - 1; i >= 0; i -= 1) {
      const s = statusEvents[i];
      if (currentRunId && s.run_id && s.run_id !== currentRunId) continue;
      if (s.status === 'tool_call' || s.status === 'lean_check') return activityLabel(s);
    }
    return null;
  }, [thinking, statusEvents, currentRunId]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!isRunning && draft.trim()) onSubmit();
    }
  };

  const renderNode = (node: MergedNode) => {
    // Spawn nodes are rendered by coalesceUnits → <SpawnGroup>, never here.
    if (node.kind === 'spawn') return null;
    if (node.kind === 'diagnostic') {
      return (
        <DiagnosticCard key={node.key} diagnostic={node.diagnostic} onAction={runDiagnosticAction} />
      );
    }
    if (node.kind === 'approval') {
      return (
        <ApprovalCard
          key={node.key}
          approval={node.approval}
          decision={node.approval.decision}
          busy={approvalBusy}
          onDecide={onDecide}
        />
      );
    }
    if (node.kind === 'message') {
      const m = node.message;
      if (m.kind === 'compaction') {
        return <CompactionMarker key={node.key} content={m.content} />;
      }
      if (m.role === 'user') {
        return (
          <div className="msg" key={node.key}>
            <div className="role">
              <span className="avatar you">Y</span> You
            </div>
            <div className="user-bubble">{m.content}</div>
          </div>
        );
      }
      // Folded into its step card (M11) — don't render it as prose too.
      if (foldedNarration.has(`${m.run_id || ''}|${m.content.trim()}`)) return null;
      return (
        <div className="msg assistant" key={node.key}>
          <div className="role">
            <span className="avatar lea">L</span> Lea
          </div>
          <MarkdownMessage content={m.content} />
        </div>
      );
    }
    const step = node.step;
    const tools = step.turn != null ? toolsByTurn.get(step.turn) || [] : [];
    return (
      <button
        key={node.key}
        className={`step ${node.codeIndex === activeCodeIndex ? 'active' : ''}`}
        onClick={() => onSelectStep(node.codeIndex)}
      >
        <div className="step-head">
          <span className="step-num">{node.codeIndex + 1}</span>
          <span className="step-title">{stepTitle(step)}</span>
          <span className="step-jump">view snapshot →</span>
        </div>
        {(step.summary || tools.length > 0) && (
          <div className="step-body">
            {step.summary && <p>{step.summary}</p>}
            {tools.length > 0 && (
              <div className="step-tools">
                <span className="tlabel">worked via</span>
                {tools.map((t, i) => (
                  <ToolChip key={i} event={t} />
                ))}
              </div>
            )}
          </div>
        )}
      </button>
    );
  };

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
        <OriginBadge origin={session?.origin} originUrl={session?.origin_url} />
        <span className="head-spacer" />
        {/* Download this session's files as a zip (#14). Loose sessions only — a
            project session's files download from the project's Filesystem tab. Shown
            once the session has written at least one file (else export would 404). */}
        {session && !session.project_id && codeSteps.length > 0 && (
          <a
            className="head-download"
            href={sessionExportUrl(session.id)}
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

      <div className="thread" ref={scrollRef} onScroll={onScroll}>
        <div className="thread-inner">
          {items.length === 0 && !error && (
            <div className="thread-empty">
              Enter a theorem or natural-language proof task to start Lea.
            </div>
          )}

          {runGroups.map((group, gi) => {
            const isLastGroup = gi === runGroups.length - 1;
            const finished = !(isRunning && isLastGroup);
            const status = group.runId ? runStatusById[group.runId] : undefined;
            const codeNodes = group.nodes.filter(
              (n): n is Extract<MergedNode, { kind: 'code' }> => n.kind === 'code',
            );
            const codeStepList = codeNodes.map((n) => n.step);
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
                  </div>
                )}
                {coalesceUnits(group.nodes).map((unit) =>
                  unit.kind === 'spawn-group' ? (
                    <SpawnGroup
                      key={unit.key}
                      children={unit.children}
                      onSelectSession={onSelectSession}
                      diagnosticsByChild={childDiagnostics}
                    />
                  ) : (
                    renderNode(unit.node)
                  ),
                )}
                {/* The 'proved' and 'failed/max_turns' banners are gone: both restated
                    what the header chip already says (✓ proved / ✕ unproved), so the
                    common outcomes ended every run with a badge saying what you could
                    already see. The remaining cards are kept because each carries
                    something the chip does NOT: a definition rather than a proof, a
                    counterexample, or a proof that compiles but still has a `sorry`. */}
                {finished && completion === 'defined' && <DefinedCard steps={steps} session={session} />}
                {finished && completion === 'disproved' && <DisprovedCard steps={steps} session={session} />}
                {finished && completion === 'stubbed' && <StubCard steps={steps} session={session} />}
                {finished && resultKind === 'needs_review' && <ReviewNote />}
              </Fragment>
            );
          })}

          {thinking && (
            <div className="thinking">
              <span className="avatar lea">L</span>
              <span>{activity ? activity : 'Lea is thinking'}</span>
              <span className="dots">
                <i />
                <i />
                <i />
              </span>
            </div>
          )}

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
          {degradedDiagnostics.map((d) => (
            <DiagnosticCard key={`deg:${d.code}`} diagnostic={d} onAction={runDiagnosticAction} />
          ))}
          {/* The banner survives, narrowed (v2.4) to client-side action failures —
              a fetch that didn't land, a button that failed. Everything the run
              reports is a Diagnostic with a place to live. */}
          {error && <div className="err-banner">{error}</div>}
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
          <div className={`verify-box ${verifySurface.status === 'ok' ? 'ok' : 'bad'}`}>
            <div className="verify-box-head">
              <span className="verify-box-title">
                🛡 SafeVerify {verifySurface.status === 'ok' ? '✓ passed' : verifySurface.status}
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
        <div className="composer-inner">
          <textarea
            ref={textareaRef}
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              editedPath
                ? 'What did you change? A short note helps Lea pick up where you left off.'
                : 'Ask a follow-up, or state a theorem to prove…'
            }
            rows={1}
          />
          <div className="crow">
            <ComposerScopeChip session={session} />
            {isRunning ? (
              <button className="send stop" onClick={onInterrupt} title="Stop the run">
                ◼
              </button>
            ) : (
              <button className="send" onClick={onSubmit} disabled={!draft.trim()} title="Send">
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
            <small>{item.activity.status !== 'idle' ? item.activity.status : item.validity_status}</small>
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

// The spawn_subagent node in the chat timeline, interleaved at the point the
// coordinator delegated (bug-fix). Groups the children spawned together; each child row
// shows a one-line preview of its final output and expands in place to the whole thing
// (or opens the full read-only child session).
function SpawnGroup({
  children,
  onSelectSession,
  diagnosticsByChild,
}: {
  children: SessionSummary[];
  onSelectSession?: (id: string) => void;
  diagnosticsByChild?: Map<string, Diagnostic[]>;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [stopping, setStopping] = useState<Set<string>>(new Set());
  // E1: ephemeral live state per running child, fed by `subagent_progress` SSE.
  const progress = useProofSession((s) => s.subagentProgress);
  // Sub-agents that could not run (API/config error, crash) → the real error message,
  // surfaced as a red "failed" child instead of hidden behind the coordinator.
  const errors = useProofSession((s) => s.subagentErrors);
  // D4: spawned but still waiting for a concurrency slot — not yet exploring.
  const queued = useProofSession((s) => s.subagentQueued);
  // D3: ran, but stopped short (budget / stopped / no candidate). Distinct from
  // `errors`, which means it never ran at all.
  const stopNotices = useProofSession((s) => s.subagentStopNotices);
  const toggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  const stop = (id: string) => {
    setStopping((prev) => new Set(prev).add(id));
    interruptSubagent(id).catch(() => {}); // 404 (already done) is fine; the row will settle on finish
  };
  return (
    <details className="spawn" open>
      <summary>
        <span className="tool">spawn_subagent</span> × {children.length}
        <span className="act">
          {children.length} candidate{children.length === 1 ? '' : 's'}
        </span>
      </summary>
      <div className="kids">
        {children.map((child) => {
          const error = errors[child.id];
          const stopNotice = stopNotices[child.id];
          const isQueued = Boolean(queued[child.id]);
          const childDiags = diagnosticsByChild?.get(child.id) ?? [];
          // A failed child (couldn't run) is a red "failed" — surfaced, not hidden.
          // D4: one that hasn't cleared the concurrency semaphore is 'queued', not
          // 'exploring' — it is doing nothing at all yet.
          const badge = error
            ? { dot: 'fail', cls: 'err', text: 'failed' }
            // A muted dot, not the amber 'running' one: queued means nothing is
            // happening yet, and the pulse would say otherwise.
            : isQueued
            ? { dot: 'idle', cls: 'wait', text: 'queued' }
            : subagentBadge(child);
          const isOpen = expanded.has(child.id);
          const running = !error && !isQueued && badge.dot === 'run';
          const live = progress[child.id];
          // While running, the live feed IS the preview: the current tool, else the
          // streaming narration, else the plain 'exploring…'. A failed child shows its
          // error; else the durable final summary.
          const liveLine = running
            ? (live?.tool ? `running ${live.tool}…` : firstLine(live?.text) || 'exploring…')
            : '';
          const preview = isQueued
            ? 'waiting for a free slot…'
            : running
            ? liveLine
            : error
            ? firstLine(error)
            // D3: a child that stopped short leads with WHY, not with a summary that
            // makes a truncated run look like a considered conclusion.
            : stopNotice
            ? `Stopped — ${stopNotice}`
            : firstLine(child.final_summary);
          return (
            <div
              className={`kid ${isOpen ? 'open' : ''} ${running ? 'running' : ''} ${error ? 'errored' : ''}`}
              key={child.id}
            >
              <button className="kid-head" onClick={() => toggle(child.id)}>
                <span className={`caret ${isOpen ? 'open' : ''}`}>▸</span>
                <span className={`dot ${badge.dot}`} />
                <span className="rtitle">{child.title}</span>
                {child.role && <span className="role">{child.role.split('-')[0]}</span>}
                {running && (
                  <button
                    className="kid-stop"
                    disabled={stopping.has(child.id)}
                    onClick={(e) => {
                      e.stopPropagation();
                      stop(child.id);
                    }}
                    title="Stop this sub-agent (the coordinator keeps running)"
                  >
                    {stopping.has(child.id) ? 'stopping…' : 'Stop'}
                  </button>
                )}
                <span className={`verdict ${badge.cls}`}>{badge.text}</span>
              </button>
              {!isOpen && preview && (
                <div className={`kid-preview ${error ? 'err' : ''}`}>{preview}</div>
              )}
              {isOpen && (
                <div className="kid-body">
                  {/* What the coordinator asked for. First, and shown for a RUNNING
                      child too — a child with no output yet is only judgeable by its
                      task, and "was this delegated correctly?" is a question worth
                      answering while there is still time to stop it. */}
                  {child.task && (
                    <div className="kid-task">
                      <div className="kid-task-label">Delegated task</div>
                      <pre className="kid-task-text">{child.task}</pre>
                    </div>
                  )}
                  {error ? (
                    <div className="kid-error">
                      <div className="kid-error-title">This sub-agent could not run</div>
                      <pre className="kid-error-msg">{error}</pre>
                    </div>
                  ) : (
                    <>
                      {/* D3: why it stopped, ABOVE its output — the summary of a
                          child that ran out of budget reads like a conclusion
                          unless you know it was cut off. */}
                      {stopNotice && (
                        <div className="kid-stopped">This sub-agent {stopNotice}.</div>
                      )}
                      {child.final_summary ? (
                        <MarkdownMessage content={child.final_summary} />
                      ) : isQueued ? (
                        <p className="kid-empty">Waiting for a free slot — not started yet.</p>
                      ) : running ? (
                        // "produced no final output" is a verdict, and it was wrong for
                        // a child that simply hasn't finished. An unfinished run has no
                        // output YET; saying it produced none reads as a result.
                        <p className="kid-empty">Still working — no output yet.</p>
                      ) : (
                        <p className="kid-empty">This sub-agent produced no final output.</p>
                      )}
                    </>
                  )}
                  {/* Failures the child itself reported (a tool that raised inside
                      it, a degraded capability). They are persisted against the
                      child's own session; shown here so a failure inside a child
                      isn't only discoverable by opening it. */}
                  {childDiags.map((d) => (
                    // No settings jump on a child's card: the child session is
                    // read-only, and its failures are the coordinator's to act on.
                    <DiagnosticCard key={`kd:${d.id ?? d.code}`} diagnostic={d} />
                  ))}
                  <button className="kid-open" onClick={() => onSelectSession?.(child.id)}>
                    Open full session →
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </details>
  );
}

// First non-empty line of a child's final output, trimmed for the collapsed preview.
// v2.4 · one failure, rendered where it happened.
//
// The shape is deliberately uniform across every severity and every source (prover,
// adapter, sub-agent): a user learning to read one of these has learned to read all
// of them. `title` and `remedy` come from the adapter's code catalog, so the copy is
// not whatever an exception happened to stringify to — and `remedy` is simply absent
// when there is no honest advice to give, rather than padded with a guess.
const DIAGNOSTIC_LABEL: Record<string, string> = {
  fatal: 'Run failed',
  step_error: 'Step failed',
  degraded: 'Reduced capability',
  notice: 'Notice',
};

function DiagnosticCard({
  diagnostic,
  onAction,
}: {
  diagnostic: Diagnostic;
  // One handler for every offer on the card; the card doesn't decide where an action
  // goes, it just reports which one was pressed.
  onAction?: (action: DiagnosticAction) => void;
}) {
  const { severity, title, message, detail, remedy, actions, context, code } = diagnostic;
  const anchor = context?.path || context?.tool;
  return (
    <div className={`diag diag-${severity}`} data-code={code}>
      <div className="diag-head">
        <span className="diag-sev">{DIAGNOSTIC_LABEL[severity] ?? 'Notice'}</span>
        <span className="diag-title">{title}</span>
        {anchor && <span className="diag-anchor">{anchor}</span>}
      </div>
      {message && message !== title && <pre className="diag-msg">{message}</pre>}
      {/* The raw exception, folded away. Present for anyone who needs it, never the
          first thing read — `BadRequestError: litellm.BadRequestError: …{json}` is
          noise when the provider already said "your api key is invalid". */}
      {detail && (
        <details className="diag-detail">
          <summary>Technical detail</summary>
          <pre>{detail}</pre>
        </details>
      )}
      {remedy && <div className="diag-remedy">{remedy}</div>}
      {/* Where a failure has more than one plausible cause, offer every route rather
          than picking one — an auth rejection cannot tell a wrong key from a model
          belonging to another provider, and guessing sends people to fix the wrong
          thing. */}
      {actions && actions.length > 0 && onAction && (
        <div className="diag-actions">
          {actions.map((a) => (
            <button
              key={`${a.action}:${a.focus ?? ''}`}
              className="diag-action"
              onClick={() => onAction(a)}
            >
              {a.label}
            </button>
          ))}
        </div>
      )}
      {diagnostic.persisted === false && (
        // Honesty about our own failure: this one is on screen but was not written,
        // so it will not survive a reload. Better to say so than to let someone
        // trust a record that isn't there.
        <div className="diag-unsaved">Not saved — this notice will disappear on reload.</div>
      )}
    </div>
  );
}

function firstLine(text?: string | null): string {
  if (!text) return '';
  for (const line of text.split('\n')) {
    const t = line.replace(/^[#>*\-\s]+/, '').trim();
    if (t) return t.length > 140 ? `${t.slice(0, 140)}…` : t;
  }
  return '';
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

function ToolChip({ event }: { event: StatusEvent }) {
  if (event.status === 'lean_check') {
    const ok = event.check_status === 'ok';
    const stubbed = ok && hasSorryLikeCheckDetail(event.check_detail);
    return (
      <span className="tool">
        <span className="tname">lean_check</span>
        <span className={`tstat ${stubbed ? 'stub' : ok ? 'ok' : 'err'}`}>
          {stubbed ? '✓ 0 errors · sorry' : ok ? '✓ 0 errors' : '✗ errors'}
        </span>
      </span>
    );
  }
  const name = (event.message || '').replace(/^Running\s+/, '') || 'tool';
  return (
    <span className="tool">
      <span className="tname">{name}</span>
    </span>
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
          {session.cost_usd ? <span>${session.cost_usd.toFixed(3)}</span> : null}
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
          {session.cost_usd ? <span>${session.cost_usd.toFixed(3)}</span> : null}
          {session.duration_seconds ? <span>{session.duration_seconds}s</span> : null}
        </div>
      )}
    </div>
  );
}

function ReviewNote() {
  return (
    <div className="final review">
      <div className="fhead">○ Review note — Lean checked this artifact, but Lea could not confirm it exactly matches the original request.</div>
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
          {session.cost_usd ? <span>${session.cost_usd.toFixed(3)}</span> : null}
          {session.duration_seconds ? <span>{session.duration_seconds}s</span> : null}
        </div>
      )}
    </div>
  );
}

function ApprovalCard({
  approval,
  decision,
  busy,
  onDecide,
}: {
  approval: PendingApproval;
  decision?: string | null;
  busy: boolean;
  onDecide: (decision: ApprovalDecision) => void;
}) {
  const preview = approvalPreview(approval);
  const resolved = !!decision;
  return (
    <div className={`approval ${resolved ? 'resolved' : ''}`}>
      <div className="ahead">
        🛡{' '}
        {resolved
          ? decisionHeadline(decision as string, approval.tool_name)
          : `Approve ${approval.tool_name}?`}
      </div>
      {preview && <div className="acode">{preview}</div>}
      {resolved ? (
        <span
          className="resolved-tag"
          style={decision === 'deny' ? { color: 'var(--red)' } : undefined}
        >
          {decisionTag(decision as string)}
        </span>
      ) : (
        <div className="actions">
          <button className="btn accept" disabled={busy} onClick={() => onDecide('allow')}>
            Allow once
          </button>
          <button className="btn session" disabled={busy} onClick={() => onDecide('always_session')}>
            Always this session
          </button>
          <button className="btn reject" disabled={busy} onClick={() => onDecide('deny')}>
            Deny
          </button>
        </div>
      )}
    </div>
  );
}

function decisionHeadline(decision: string, tool: string): string {
  if (decision === 'allow') return `Allowed ${tool} once`;
  if (decision === 'always_session') return `Always allowing ${tool} this session`;
  if (decision === 'deny') return `Denied ${tool}`;
  return `${tool} — resolved`;
}

function decisionTag(decision: string): string {
  if (decision === 'allow') return '✓ you allowed this once';
  if (decision === 'always_session') return '✓ you allowed this for the session';
  if (decision === 'deny') return '⛔ you denied this';
  return 'resolved';
}

// Absolute proof paths are long and noisy. Show only what matters: the path
// relative to the session repo (everything after `proofs/<session-id>/`).
function shortPath(p: string): string {
  const m = p.match(/[/\\]proofs[/\\][^/\\]+[/\\](.+)$/);
  return m ? m[1].replace(/\\/g, '/') : p;
}

function approvalPreview(approval: PendingApproval): string {
  const args = approval.args || {};
  if (approval.tool_name === 'bash' && typeof args.command === 'string') return args.command;
  const path = typeof args.path === 'string' ? shortPath(args.path) : '';
  const content =
    typeof args.content === 'string'
      ? args.content
      : typeof args.new_string === 'string'
      ? args.new_string
      : '';
  if (path || content) return [path, content].filter(Boolean).join('\n\n');
  return JSON.stringify(args, null, 2);
}

// Friendly "what Lea is doing" label for a tool_call / lean_check status event,
// covering every tool (M17).
const TOOL_ACTIVITY: Record<string, string> = {
  search_mathlib: '🔍 Searching Mathlib',
  bash: '💻 Running a shell command',
  read_file: '📖 Reading a file',
  write_file: '✎ Writing the proof',
  edit_file: '✎ Editing the proof',
  lean_check: '⚙ Checking with Lean',
};

function activityLabel(event: StatusEvent): string {
  if (event.status === 'lean_check') return TOOL_ACTIVITY.lean_check;
  const name = (event.message || '').replace(/^Running\s+/, '').trim();
  return TOOL_ACTIVITY[name] || `⚙ Running ${name || 'a tool'}`;
}

function stepTitle(step: CodeStep): string {
  // A short, generic label; the model's intent (step.summary) is the card body.
  const file = step.path.split('/').pop() || step.path;
  if (step.author === 'user') return `You edited ${file}`;
  return `Wrote ${file}`;
}

function formatTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

// The compiler's verdict on a sub-agent's candidate (item 24), for the spawn node's
// dot + badge. The ranking that actually picks a winner is the adapter's job (item 25);
// here we just report each child's own lean_check status.
function subagentBadge(child: SessionSummary): { dot: string; cls: string; text: string } {
  const ok =
    child.latest_check_status === 'ok' ||
    child.status === 'ok' ||
    child.status === 'proved' ||
    child.status === 'defined';
  if (ok) return { dot: 'ok', cls: 'ok', text: 'compiles' };
  if (child.status === 'error' || child.latest_check_status === 'error') {
    return { dot: 'fail', cls: 'err', text: 'errors' };
  }
  if ((child.active_run_count ?? 0) > 0 || child.status === 'running') {
    return { dot: 'run', cls: 'run', text: 'exploring…' };
  }
  return { dot: 'idle', cls: 'run', text: 'no candidate' };
}
