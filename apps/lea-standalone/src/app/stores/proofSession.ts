import { create } from 'zustand';
import type {
  ApprovalRecord,
  ChatMessage,
  CodeStep,
  Diagnostic,
  Formalization,
  FormalizationCurrentSnapshot,
  RunStatus,
  SafeVerifyResult,
  StatusEvent,
} from '../lib/api';

// React-style setter signature: accept a value or an updater fn, so existing
// `setX((cur) => ...)` call sites migrate from useState unchanged.
type Updater<T> = T | ((current: T) => T);

// Ephemeral live state for ONE running sub-agent (E1), keyed by its child session id.
// The child's steps stream in as `subagent_progress` events for VISIBILITY only — they
// are NOT the durable record (the full transcript replays into the child session on
// finish); this is what the coordinator's spawn box shows while the child works, then is
// cleared when the child finishes.
export interface SubagentLive {
  text: string;       // the child's assistant narration for the current turn (accumulates)
  tool?: string;      // the tool it is currently running, if any
  check?: string;     // its latest lean_check verdict ('ok' | 'error')
  turn?: number;      // its current turn
}
// The context-compaction (G1/G3) payload carried in a `kind='compaction'` timeline
// message's `content` (JSON). Durable — it rides the message channel, so a compaction
// marker survives a reload like any message. `manual` distinguishes user `/compact` (G3)
// from the automatic threshold (G1); `summarized` a prune-only pass from a folded one.
export interface CompactionPayload {
  manual?: boolean;
  changed?: boolean;
  pending?: boolean; // true → the /compact request is in flight (renders a "Compacting…" card)
  pruned: number;
  summarized: boolean;
  before_tokens: number;
  after_tokens: number;
  freed_tokens: number;
  referenced_files?: string[];
}

const apply = <T,>(update: Updater<T>, current: T): T =>
  typeof update === 'function' ? (update as (c: T) => T)(current) : update;

/**
 * The proof-session store (v2.0.1 R1).
 *
 * Shared state for the proof view, so the chat header, ChatThread and Canvas
 * read it straight from here instead of being prop-drilled through App. We move
 * App's `useState` into this store one cohesive slice at a time — this file
 * grows per sub-todo (R1a, R1b, …).
 *
 * Usage:
 *   const editedPath = useProofSession((s) => s.editedPath);  // subscribe to a slice
 *   useProofSession.getState().setEditedPath(path);           // write from non-React code
 */
interface ProofSessionState {
  formalizations: Formalization[];
  setFormalizations: (formalizations: Formalization[]) => void;
  formalizationScope: 'project' | 'new' | string;
  setFormalizationScope: (scope: 'project' | 'new' | string) => void;
  composerScopeOverride: 'project' | 'new' | string | null;
  setComposerScopeOverride: (scope: 'project' | 'new' | string | null) => void;
  currentFormalizationSnapshot: FormalizationCurrentSnapshot | null;
  setCurrentFormalizationSnapshot: (
    snapshot: FormalizationCurrentSnapshot | null
  ) => void;
  formalizationRefreshToken: number;
  bumpFormalizationRefresh: () => void;
  canvasRevisionMode: 'current' | 'historical';
  setCanvasRevisionMode: (mode: 'current' | 'historical') => void;

  // Canvas-edit nudge (M20): the file the user just edited, prompting a note in
  // the composer. Set after a canvas edit; cleared on send / new session / load.
  editedPath?: string;
  setEditedPath: (path?: string) => void;

  // Error banner shown in the chat thread. NARROWED in v2.4 to what it is actually
  // good at: transient, client-side, action-scoped failures (a fetch that didn't
  // land, a failed button press). Everything the RUN reports now goes to
  // `diagnostics` instead — a single mutable string could only ever hold one
  // failure, so a second one erased the first and a reload erased both.
  error?: string;
  setError: (error?: string) => void;

  // Every failure the backend reported for this session (v2.4), live + replayed
  // from `session_detail` on load, deduped by id. Anchored via `context` — rendered
  // on the code card / sub-agent row / tool step it names, and in the run-level
  // block when it names nothing. Append-only within a session: a diagnostic is
  // history, so a later one never overwrites an earlier one.
  diagnostics: Diagnostic[];
  setDiagnostics: (update: Updater<Diagnostic[]>) => void;
  /** Merge one in, ignoring a duplicate id (SSE replay after a reconnect). */
  addDiagnostic: (diagnostic: Diagnostic) => void;

  // Transient reconnect notice (v2.3 item 14): set while the run EventSource is
  // backing off between reattach attempts (a dropped stream, or a 409 the browser
  // can't read as anything but onerror — e.g. waiting for a free run slot).
  // Cleared once the stream reopens, the run settles, or we give up.
  reconnecting?: string;
  setReconnecting: (reconnecting?: string) => void;

  // Persisted SafeVerify verdict for the latest proof, shown in the canvas foot.
  // Set on verify / load; cleared on edit / new session.
  safeVerify: SafeVerifyResult | null;
  setSafeVerify: (safeVerify: SafeVerifyResult | null) => void;

  // Edit-mode SafeVerify result surfaced as a collapsible box above the composer,
  // so the user can dismiss it or push the error into the draft ("fix it"). Set on
  // an Edit-mode SafeVerify run; cleared on dismiss / send / new session.
  verifySurface: SafeVerifyResult | null;
  setVerifySurface: (verifySurface: SafeVerifyResult | null) => void;

  // Shared InfoView (v2.2 · Phase 3): the goal state at the human's cursor, captured
  // from the live editor and surfaced above the composer so they can ask Lea about
  // it. Cleared on dismiss / send / new session.
  goalSurface: { rendered: string; line: number } | null;
  setGoalSurface: (goalSurface: { rendered: string; line: number } | null) => void;

  // Lean proof snapshots for the session (stored raw; consumers sort via
  // sortCodeSteps) + the canvas stepper position. Written by the run stream /
  // session load; codeIndex follows the latest step or the user's stepper choice.
  codeSteps: CodeStep[];
  setCodeSteps: (codeSteps: CodeStep[]) => void;
  codeIndex: number;
  setCodeIndex: (codeIndex: number) => void;

  // Chat thread content: assistant/user messages + tool/compile status events.
  // Written by the run stream / session load; ChatThread derives its timeline.
  messages: ChatMessage[];
  setMessages: (update: Updater<ChatMessage[]>) => void;
  statusEvents: StatusEvent[];
  setStatusEvents: (update: Updater<StatusEvent[]>) => void;

  // Run lifecycle: whether a run is active, its id + status, and the per-run
  // final/active status map that places the "Proved" milestone (M16).
  isRunning: boolean;
  setIsRunning: (isRunning: boolean) => void;
  currentRunId?: string;
  setCurrentRunId: (currentRunId?: string) => void;
  runStatus?: RunStatus;
  setRunStatus: (runStatus?: RunStatus) => void;
  runStatusById: Record<string, string>;
  setRunStatusById: (update: Updater<Record<string, string>>) => void;
  runResultKindById: Record<string, string | null | undefined>;
  setRunResultKindById: (update: Updater<Record<string, string | null | undefined>>) => void;
  runFocusById: Record<string, string | null | undefined>;
  setRunFocusById: (update: Updater<Record<string, string | null | undefined>>) => void;

  // Theorem-approval gate: the approval history (each gains a decision once
  // resolved; M13) + a busy flag while a decision is in flight.
  approvals: ApprovalRecord[];
  setApprovals: (update: Updater<ApprovalRecord[]>) => void;
  approvalBusy: boolean;
  setApprovalBusy: (approvalBusy: boolean) => void;

  // Live sub-agent progress (E1): child session id -> its ephemeral live state. Fed by
  // `subagent_progress` SSE, rendered on a running child's spawn-node row, and cleared
  // when the child finishes (its durable transcript then takes over).
  subagentProgress: Record<string, SubagentLive>;
  setSubagentProgress: (update: Updater<Record<string, SubagentLive>>) => void;

  // Sub-agent FAILURES: child session id -> the error message, for children that could
  // not run at all (API/config error, crash). Surfaced as a red "failed" child instead
  // of being hidden behind the coordinator's fallback narration.
  subagentErrors: Record<string, string>;
  setSubagentErrors: (update: Updater<Record<string, string>>) => void;

  // D4: child session ids that are SPAWNED BUT NOT YET RUNNING. Every spawn in a
  // turn is announced at once, but the prover queues them behind a concurrency
  // semaphore (5) — so children beyond the cap were shown as "exploring" while
  // actually waiting. A child is removed from here on its first real event.
  subagentQueued: Record<string, true>;
  setSubagentQueued: (update: Updater<Record<string, true>>) => void;

  // D3: child session id -> why it stopped short (turn/cost budget, stopped, no
  // candidate). Distinct from `subagentErrors`, which means the child never ran at
  // all: "hit its budget" and "found nothing" call for different next moves.
  subagentStopNotices: Record<string, string>;
  setSubagentStopNotices: (update: Updater<Record<string, string>>) => void;

  /**
   * Clear everything scoped to ONE session, in one call.
   *
   * This exists because the alternative — a hand-written list of setters at each
   * switch point — silently rots: every slice added to this store has to be
   * remembered at every reset site, and one that isn't stays glued to the screen
   * across a session switch. That is exactly how a `step_error` card from the
   * previous session survived "New session" until a manual refresh.
   *
   * Anything session-scoped added below MUST be reset here. Non-session state (the
   * model, the session list) deliberately survives.
   */
  resetSessionScoped: () => void;
}

/** The session-scoped slice defaults — the single definition `resetSessionScoped`
 *  and the store's initial state both use, so they can never disagree. */
const SESSION_SCOPED = {
  // Multi-formalization state (upstream). Every one of these belongs to ONE session,
  // so every one has to be here: carrying a previous session's formalization list or
  // selected scope into the next one is the same bug as the glued diagnostic card.
  formalizations: [] as Formalization[],
  formalizationScope: 'new' as 'project' | 'new' | string,
  composerScopeOverride: null as 'project' | 'new' | string | null,
  currentFormalizationSnapshot: null as FormalizationCurrentSnapshot | null,
  formalizationRefreshToken: 0,
  canvasRevisionMode: 'current' as 'current' | 'historical',
  runFocusById: {} as Record<string, string | null | undefined>,
  editedPath: undefined as string | undefined,
  error: undefined as string | undefined,
  reconnecting: undefined as string | undefined,
  codeIndex: 0,
  isRunning: false,
  currentRunId: undefined as string | undefined,
  runStatus: undefined as RunStatus | undefined,
  approvalBusy: false,
  safeVerify: null as SafeVerifyResult | null,
  verifySurface: null as SafeVerifyResult | null,
  goalSurface: null as { rendered: string; line: number } | null,
  diagnostics: [] as Diagnostic[],
  codeSteps: [] as CodeStep[],
  messages: [] as ChatMessage[],
  statusEvents: [] as StatusEvent[],
  runStatusById: {} as Record<string, string>,
  runResultKindById: {} as Record<string, string | null | undefined>,
  approvals: [] as ApprovalRecord[],
  subagentProgress: {} as Record<string, SubagentLive>,
  subagentErrors: {} as Record<string, string>,
  subagentQueued: {} as Record<string, true>,
  subagentStopNotices: {} as Record<string, string>,
};

export const useProofSession = create<ProofSessionState>((set) => ({
  ...SESSION_SCOPED,
  setFormalizations: (formalizations) => set({ formalizations }),
  setFormalizationScope: (formalizationScope) => set({ formalizationScope }),
  setComposerScopeOverride: (composerScopeOverride) => set({ composerScopeOverride }),
  setCurrentFormalizationSnapshot: (currentFormalizationSnapshot) =>
    set({ currentFormalizationSnapshot }),
  bumpFormalizationRefresh: () =>
    set((state) => ({ formalizationRefreshToken: state.formalizationRefreshToken + 1 })),
  setCanvasRevisionMode: (canvasRevisionMode) => set({ canvasRevisionMode }),
  setEditedPath: (editedPath) => set({ editedPath }),

  setError: (error) => set({ error }),

  setDiagnostics: (update) => set((s) => ({ diagnostics: apply(update, s.diagnostics) })),
  addDiagnostic: (diagnostic) =>
    set((s) => {
      // A reconnect replays the broker from the client's cursor, so the same
      // diagnostic can arrive twice. Dedupe by id; an unsaved one (no durable id)
      // falls back to code+message+turn, which is stable for the same failure.
      const key = (d: Diagnostic) =>
        d.id && !d.id.startsWith('unsaved-') ? d.id : `${d.code}|${d.message}|${d.turn ?? ''}`;
      const incoming = key(diagnostic);
      if (s.diagnostics.some((d) => key(d) === incoming)) return s;
      return { diagnostics: [...s.diagnostics, diagnostic] };
    }),

  setReconnecting: (reconnecting) => set({ reconnecting }),

  setSafeVerify: (safeVerify) => set({ safeVerify }),

  setVerifySurface: (verifySurface) => set({ verifySurface }),

  setGoalSurface: (goalSurface) => set({ goalSurface }),

  setCodeSteps: (codeSteps) => set({ codeSteps }),
  setCodeIndex: (codeIndex) => set({ codeIndex }),

  setMessages: (update) => set((s) => ({ messages: apply(update, s.messages) })),
  setStatusEvents: (update) => set((s) => ({ statusEvents: apply(update, s.statusEvents) })),

  setIsRunning: (isRunning) => set({ isRunning }),
  setCurrentRunId: (currentRunId) => set({ currentRunId }),
  setRunStatus: (runStatus) => set({ runStatus }),
  setRunStatusById: (update) => set((s) => ({ runStatusById: apply(update, s.runStatusById) })),
  setRunResultKindById: (update) => set((s) => ({ runResultKindById: apply(update, s.runResultKindById) })),
  setRunFocusById: (update) => set((s) => ({ runFocusById: apply(update, s.runFocusById) })),

  setApprovals: (update) => set((s) => ({ approvals: apply(update, s.approvals) })),
  setApprovalBusy: (approvalBusy) => set({ approvalBusy }),

  setSubagentProgress: (update) =>
    set((s) => ({ subagentProgress: apply(update, s.subagentProgress) })),

  setSubagentErrors: (update) =>
    set((s) => ({ subagentErrors: apply(update, s.subagentErrors) })),

  setSubagentQueued: (update) =>
    set((s) => ({ subagentQueued: apply(update, s.subagentQueued) })),

  setSubagentStopNotices: (update) =>
    set((s) => ({ subagentStopNotices: apply(update, s.subagentStopNotices) })),

  resetSessionScoped: () => set({ ...SESSION_SCOPED }),
}));
