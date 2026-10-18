import { useEffect, useRef } from 'react';
import {
  getSession,
  awaitReformalization,
  type ChatMessage,
  type CodeStep,
  type Diagnostic,
  type PendingApproval,
  type RunStatus,
  type SessionDetail,
  type StatusEvent,
} from '../lib/api';
import { useProofSession } from '../stores/proofSession';
import { useSessions } from '../stores/sessions';
import { sortCodeSteps } from '../lib/timeline.mjs';
import { mainFileIndex } from '../lib/canvasFiles.mjs';
import { restoreFormalizationSelection } from '../lib/formalizations.mjs';

// SSE reattach backoff (v2.3 item 14). A browser EventSource cannot read an HTTP
// status — a 409 (server at capacity / a run driven elsewhere) surfaces only as
// `onerror`, identical to a dropped connection. Without a delay+cap the reattach
// loop becomes an unthrottled request storm the moment the server can 409, which
// is exactly the blocker for raising LEA_MAX_CONCURRENT_RUNS. Exponential backoff
// with jitter (1s→2s→4s…, capped at 15s) over a bounded number of attempts mirrors
// the Overleaf companion's already-proven retry loop.
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 15000;

// Decorrelated-ish jitter: full exponential value minus up to 25%, so a fleet of
// tabs that all dropped at once don't resynchronize onto the same retry tick.
function reconnectDelay(attempt: number): number {
  const exp = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** (attempt - 1));
  return exp * 0.75 + Math.random() * exp * 0.25;
}

/**
 * useProofStream — owns the run EventSource lifecycle (v2.0.1 R2).
 *
 * Subscribes to a run's SSE stream and writes every event into the proofSession
 * store, applies a fetched session detail, and reconciles after a run ends. The
 * session-list/selection bits it can't own (those live in App's `useState`) are
 * passed in. Everything it writes goes through `useProofSession.getState()`, so
 * there are no stale-closure hazards in the async event handlers.
 *
 * Returns the imperative entry points App drives: `attachStream` (start
 * streaming a run), `applyDetail` (hydrate from a session detail), and
 * `closeStream` (tear down before a session switch / new session).
 */
export function useProofStream() {
  const eventSourceRef = useRef<EventSource | null>(null);
  const runCursorsRef = useRef(new Map<string, string>());
  // Highest seq seen + a counter, so an approval (which has no server seq) can be
  // stamped with a synthetic seq just after the latest item — interleaving it
  // into the thread at the point it actually happened, before the step it gates.
  const lastSeqRef = useRef(0);
  const approvalCounterRef = useRef(0);
  // Item 14 backoff state: consecutive failed reattach attempts, keyed by run so a
  // session switch to a different run starts backoff fresh (rather than inheriting
  // the previous run's count) while consecutive failures for the SAME run keep
  // growing. Reset on a successful open. Plus the pending backoff timer, so a
  // session switch can cancel an in-flight reconnect.
  const reconnectRef = useRef<{ runId: string; count: number }>({ runId: '', count: 0 });
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const selectionGeneration = useRef(0);
  const scopeGeneration = useRef(0);
  const streamGeneration = useRef(0);
  const applyingDetail = useRef(false);
  const pendingRequests = useRef(new Set<AbortController>());

  // Ownership survives renders, but expires on every navigation (including A→B→A).
  const captureSelection = (includeScope = true) => {
    const generation = selectionGeneration.current;
    const scope = scopeGeneration.current;
    const sessionId = useSessions.getState().selectedSessionId;
    return () => generation === selectionGeneration.current
      && sessionId === useSessions.getState().selectedSessionId
      && (!includeScope || scope === scopeGeneration.current);
  };
  const captureStream = () => {
    const owned = captureSelection(false);
    const generation = streamGeneration.current;
    return () => owned() && generation === streamGeneration.current;
  };
  const fetchDetail = async (sessionId: string) => {
    const controller = new AbortController();
    pendingRequests.current.add(controller);
    try { return await getSession(sessionId, controller.signal); }
    finally { pendingRequests.current.delete(controller); }
  };
  useEffect(() => useProofSession.subscribe((state, previous) => {
    if (!applyingDetail.current && state.formalizationScope !== previous.formalizationScope) {
      scopeGeneration.current += 1;
    }
  }), []);
  useEffect(() => () => closeStream(), []);

  useEffect(() => {
    const poll = async () => {
      if (document.visibilityState === 'hidden' || !useProofSession.getState().isRunning) return;
      const owned = captureStream();
      try {
        const response = await fetch('/api/health');
        if (!response.ok) return;
        const health = await response.json() as { lifecycle_state?: string };
        if (!owned()) return;
        if (health.lifecycle_state === 'draining') {
          useProofSession.getState().setReconnecting('Waiting to restart…');
        } else if (health.lifecycle_state === 'recovering') {
          useProofSession.getState().setReconnecting('Recovering saved progress…');
        } else if (health.lifecycle_state === 'ready' && eventSourceRef.current?.readyState === EventSource.OPEN) {
          const current = useProofSession.getState().reconnecting;
          if (current === 'Waiting to restart…' || current === 'Recovering saved progress…') {
            useProofSession.getState().setReconnecting(undefined);
          }
        }
      } catch { /* Connection errors are handled by the stream reconnect path. */ }
    };
    const timer = setInterval(() => { void poll(); }, 5000);
    return () => clearInterval(timer);
  }, []);

  const closeStream = (invalidateSelection = true) => {
    streamGeneration.current += 1;
    if (invalidateSelection) {
      selectionGeneration.current += 1;
      for (const controller of pendingRequests.current) controller.abort();
      pendingRequests.current.clear();
    }
    // Cancel any pending backoff so a switched-away run can't reconnect underneath
    // the newly-opened session. Attempt count is left to reset on a real open (or
    // a fresh user-initiated attach that opens), so it survives across reattach
    // cycles and the backoff actually grows.
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    useProofSession.getState().setReconnecting(undefined);
    eventSourceRef.current?.close();
    eventSourceRef.current = null;
  };

  // Timeline ordering can change on replay/backfill. Retain the selected step by
  // identity rather than letting its old numeric index select a different file.
  const replaceCodeSteps = (steps: CodeStep[]) => {
    const state = useProofSession.getState();
    const selectedId = sortCodeSteps(state.codeSteps)[state.codeIndex]?.id;
    state.setCodeSteps(steps);
    const index = sortCodeSteps(steps).findIndex((step) => step.id === selectedId);
    if (index >= 0) state.setCodeIndex(index);
  };

  const applyDetail = (detail: SessionDetail, { preservePresentation = false } = {}) => {
    const preserve = preservePresentation && useSessions.getState().selectedSessionId === detail.id;
    if (useSessions.getState().selectedSessionId !== detail.id) closeStream();
    const previousScope = useProofSession.getState().formalizationScope;
    const {
      setMessages,
      setStatusEvents,
      setCodeIndex,
      setError,
      setCurrentRunId,
      setIsRunning,
      setRunStatus,
      setApprovals,
      setApprovalBusy,
      setRunStatusById,
      setRunResultKindById,
      setRunStopReasonById,
      setRunRecoveryById,
      setRunFocusById,
      setEditedPath,
      setSafeVerify,
      setVerifySurface,
      setGoalSurface,
      setFormalizations,
      setFormalizationScope,
    } = useProofSession.getState();
    // Fresh session context → drop EVERYTHING scoped to the previous session, through
    // the same one call "New session" uses. Clearing a hand-picked subset here is what
    // let stale state leak across a switch; every field is re-set from `detail` below.
    // It also covers what upstream set by hand here (composerScopeOverride,
    // currentFormalizationSnapshot, canvasRevisionMode), which all reset to the same
    // defaults — they now live in SESSION_SCOPED instead of being repeated.
    applyingDetail.current = true;
    if (!preserve) useProofSession.getState().resetSessionScoped();
    const formalizations = detail.formalizations || [];
    setFormalizations(formalizations);
    // Not a plain reset: the selected scope is RESTORED from the detail, so it must be
    // applied after the wipe rather than left at the default.
    if (!preserve) {
      setFormalizationScope(restoreFormalizationSelection({
        currentId: previousScope,
        latestFocusId: detail.latest_focus_formalization_id,
        formalizations,
      }));
    }
    applyingDetail.current = false;
    if (preserve) {
      // These are replayable execution data, unlike the author's view state.
      const state = useProofSession.getState();
      state.setSubagentProgress({});
      state.setSubagentErrors({});
      state.setSubagentQueued({});
      state.setSubagentStopNotices({});
      state.bumpFormalizationRefresh();
    }
    useSessions.getState().setSelectedSessionId(detail.id);
    setMessages(detail.messages);
    replaceCodeSteps(detail.code_steps);
    setStatusEvents(detail.status_events || []);
    // Open on the main proof's latest snapshot, not the absolute last step — so a
    // session that ended on a throwaway `scratch` probe still shows the proof (#10).
    // Only explicit navigation chooses an initial step. Background refreshes
    // retain the author's file, revision, and interaction state.
    const sortedForOpen = sortCodeSteps(detail.code_steps);
    const openIndex = mainFileIndex(sortedForOpen);
    if (!preserve) setCodeIndex(openIndex >= 0 ? openIndex : Math.max(0, sortedForOpen.length - 1));
    lastSeqRef.current = Math.max(
      0,
      ...detail.messages.map((m) => m.seq ?? 0),
      ...detail.code_steps.map((c) => c.seq ?? 0),
    );
    if(!preserve)setError(undefined);
    // G1: replay this session's persisted failures. `setDiagnostics` (not merge) —
    // the DB is authoritative on load, and a live diagnostic that arrives after
    // this is added by `addDiagnostic`, which dedupes by id. This is what makes a
    // reloaded thread show the same failures, in the same places, as the live one.
    useProofSession.getState().setDiagnostics(detail.diagnostics || []);
    const active = detail.active_run;
    setCurrentRunId(active?.id);
    setIsRunning(Boolean(active));
    setRunStatus((active?.status as RunStatus) || undefined);
    setApprovals(
      active?.pending_approval
        ? [
            {
              ...active.pending_approval,
              session_id: detail.id,
              run_id: active.id,
              decision: null,
              seq: lastSeqRef.current + 0.5,
            },
          ]
        : [],
    );
    setApprovalBusy(false);
    const statuses: Record<string, string> = {};
    const resultKinds: Record<string, string | null | undefined> = {};
    const stopReasons: Record<string, string | null | undefined> = {};
    const focuses: Record<string, string | null | undefined> = {};
    const recoveries: Record<string, { state?: string | null; reason?: string | null; action?: 'resume' | 'continue' | null }> = {};
    for (const r of detail.runs || []) {
      statuses[r.id] = r.status;
      resultKinds[r.id] = r.result_kind;
      stopReasons[r.id] = r.stop_reason || r.stop_requested_reason;
      focuses[r.id] = r.focus_formalization_id;
      recoveries[r.id] = { state: r.recovery_state, reason: r.recovery_reason, action: r.available_recovery_action };
    }
    if (active) {
      statuses[active.id] = active.status;
      resultKinds[active.id] = active.result_kind;
      stopReasons[active.id] = active.stop_reason || active.stop_requested_reason;
      focuses[active.id] = active.focus_formalization_id;
      recoveries[active.id] = { state: active.recovery_state, reason: active.recovery_reason };
    }
    setRunStatusById(statuses);
    setRunResultKindById(resultKinds);
    setRunStopReasonById(stopReasons);
    setRunFocusById(focuses);
    setRunRecoveryById(recoveries);
    if (!preserve) setEditedPath(undefined);
    setSafeVerify(detail.safe_verify || null);
    if (!preserve) {
      setVerifySurface(null);
      setGoalSurface(null);
    }
    if (active && (active.status === 'running' || active.status === 'pending')) {
      attachStream(active.id, detail.id);
    }
  };

  const loadSession = async (sessionId: string) => {
    closeStream();
    useSessions.getState().setSelectedSessionId(sessionId);
    useProofSession.getState().resetSessionScoped();
    const owned = captureSelection(false);
    try {
      const detail = await fetchDetail(sessionId);
      if (!owned()) return null;
      applyDetail(detail);
      for (const queued of detail.queued_reformalizations || []) {
        void awaitReformalization({ reformalization_id: queued.id }, undefined, owned)
          .then(run => { if (owned() && run.run_id) attachStream(run.run_id, sessionId); })
          .catch(error => { if (owned()) useProofSession.getState().setError(error instanceof Error ? error.message : String(error)); });
      }
      return detail;
    } catch (error) {
      if (!owned()) return null;
      throw error;
    }
  };

  const reconcile = async (sessionId: string) => {
    if (useSessions.getState().selectedSessionId !== sessionId) return;
    const owned = captureStream();
    let detail: SessionDetail;
    try { detail = await fetchDetail(sessionId); }
    catch (error) { if (!owned()) return; throw error; }
    if (!owned()) return;
    // The store is the live source of truth (no stale-closure issue), so read the
    // just-finished status straight from it and re-apply it after applyDetail
    // (which would otherwise clear it, since the run is no longer active).
    const finished = useProofSession.getState().runStatus;
    const currentRunId = useProofSession.getState().currentRunId;
    const finishedResultKind = currentRunId
      ? useProofSession.getState().runResultKindById[currentRunId]
      : undefined;
    applyDetail(detail, { preservePresentation: true });
    if (finished && !detail.active_run) useProofSession.getState().setRunStatus(finished);
    if (currentRunId && finishedResultKind) {
      useProofSession.getState().setRunResultKindById((prev) => ({
        ...prev,
        [currentRunId]: finishedResultKind,
      }));
    }
  };

  // Item 14: reconnect a dropped/erroring run stream with capped exponential
  // backoff instead of hammering. Re-fetches the session detail (the persisted
  // source of truth) and lets applyDetail reattach if the run is still live;
  // settles quietly if it finished while we were away; gives up with an error
  // banner past the attempt cap. Reads setters via getState() to stay stale-free.
  const scheduleReconnect = (runId: string, sessionId: string) => {
    if (useSessions.getState().selectedSessionId !== sessionId) return;
    const owned = captureStream();
    const { setReconnecting } = useProofSession.getState();
    const prev = reconnectRef.current;
    const attempt = prev.runId === runId ? prev.count + 1 : 1;
    reconnectRef.current = { runId, count: attempt };
    // B3: a browser EventSource cannot read an HTTP status, so the adapter's honest
    // capacity 409 ("Lea is at capacity, try again shortly") reaches us as a bare
    // `onerror` — indistinguishable from a dropped connection. But the run row tells
    // us which it is: a run still `pending` was never admitted, so it is queued for a
    // slot, not disconnected. Saying "Reconnecting…" for a queue wait is a wrong
    // explanation of a working system, which is worse than a vague one.
    const queued = useProofSession.getState().runStatus === 'pending';
    setReconnecting(
      queued
        ? 'Waiting for a free run slot…'
        : 'Waiting for the adapter to reconnect…',
    );
    reconnectTimerRef.current = setTimeout(() => {
      reconnectTimerRef.current = null;
      if (!owned()) return;
      fetchDetail(sessionId)
        .then((detail) => {
          if (!owned()) return;
          const active = detail.active_run;
          if (active && (active.status === 'running' || active.status === 'pending')) {
            // Still live → applyDetail reattaches; the new stream's onopen resets
            // the backoff. Attempt count is intentionally left to grow until then,
            // so a run held off by a 409 keeps backing off across cycles.
            applyDetail(detail, { preservePresentation: true });
          } else {
            // Finished (or vanished) while we were away → settle cleanly.
            reconnectRef.current = { runId: '', count: 0 };
            setReconnecting(undefined);
            applyDetail(detail, { preservePresentation: true });
            useSessions.getState().refreshSessions().catch(() => {});
          }
        })
        .catch(() => { if (owned()) scheduleReconnect(runId, sessionId); });
    }, reconnectDelay(attempt));
  };

  const attachStream = (runId: string, sessionId: string) => {
    if (useSessions.getState().selectedSessionId !== sessionId) return;
    closeStream(false);
    const owned = captureStream();
    const cursor = runCursorsRef.current.get(runId);
    const source = new EventSource(`/api/runs/${runId}/events${cursor ? `?since=${encodeURIComponent(cursor)}` : ''}`);
    eventSourceRef.current = source;
    const listen = (type: string, handler: (event: Event) => void) => {
      source.addEventListener(type, (event) => {
        if (owned() && eventSourceRef.current === source) {
          const id = (event as MessageEvent).lastEventId;
          if (id) {
            const previous = runCursorsRef.current.get(runId);
            const split = id.lastIndexOf(':');
            const sameAttempt = previous && previous.slice(0, previous.lastIndexOf(':')) === id.slice(0, split);
            if (!sameAttempt || Number(id.slice(split + 1)) > Number(previous!.slice(previous!.lastIndexOf(':') + 1))) {
              runCursorsRef.current.set(runId, id);
            }
            if (runCursorsRef.current.size > 128) runCursorsRef.current.delete(runCursorsRef.current.keys().next().value!);
          }
          handler(event);
        }
      });
    };
    const liveId = `live-${runId}`;
    let sawDone = false;
    listen('resync', (event) => {
      const payload = JSON.parse((event as MessageEvent).data);
      // A reset is authoritative even when restoring a backup moves the cursor
      // backwards, or an execution attempt changed before its first event.
      runCursorsRef.current.set(runId, (event as MessageEvent).lastEventId ||
        `${payload.attempt_id || 'pending'}:${payload.cursor || 0}`);
      source.close();
      eventSourceRef.current = null;
      fetchDetail(sessionId).then((detail) => { if (owned()) applyDetail(detail, { preservePresentation: true }); })
        .catch(() => { if (owned()) scheduleReconnect(runId, sessionId); });
    });

    // A real open means any prior backoff succeeded: reset it and drop the
    // reconnecting chip, so a later drop starts fresh from the base delay.
    source.onopen = () => {
      if (!owned() || eventSourceRef.current !== source) return;
      reconnectRef.current = { runId, count: 0 };
      useProofSession.getState().setReconnecting(undefined);
    };
    const {
      setMessages,
      setStatusEvents,
      setApprovals,
      setApprovalBusy,
      setIsRunning,
      setRunStatus,
      setRunStatusById,
      setRunResultKindById,
      setRunStopReasonById,
      setCurrentRunId,
      setError,
    } = useProofSession.getState();

    listen('assistant_delta', (event) => {
      const { text, message } = JSON.parse((event as MessageEvent).data) as {
        text: string; message?: Omit<ChatMessage, 'content'>;
      };
      if (message) {
        if (typeof message.seq === 'number') lastSeqRef.current = Math.max(lastSeqRef.current, message.seq);
        const existing = useProofSession.getState().messages.find((m) => m.id === message.id);
        const sequence = message.stream_sequence ?? 0;
        if (existing && (existing.stream_sequence ?? 0) >= sequence) return;
        if (sequence !== (existing?.stream_sequence ?? 0) + 1) {
          // A gap is recovered from committed state, preserving presentation.
          source.close();
          eventSourceRef.current = null;
          fetchDetail(sessionId).then((detail) => { if (owned()) applyDetail(detail, { preservePresentation: true }); })
            .catch(() => { if (owned()) scheduleReconnect(runId, sessionId); });
          return;
        }
        setMessages((current) => {
          const next = { ...message, content: (existing?.content ?? '') + text } as ChatMessage;
          return existing ? current.map((m) => m.id === message.id ? next : m) : [...current, next];
        });
        return;
      }
      setMessages((current) => {
        const existing = current.find((m) => m.id === liveId);
        if (existing) {
          return current.map((m) => (m.id === liveId ? { ...m, content: m.content + text } : m));
        }
        return [
          ...current,
          {
            id: liveId,
            session_id: sessionId,
            run_id: runId,
            role: 'assistant',
            content: text,
            created_at: new Date().toISOString(),
            live: true,
          } as ChatMessage,
        ];
      });
    });

    listen('message', (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as ChatMessage;
      if (typeof payload.seq === 'number') lastSeqRef.current = Math.max(lastSeqRef.current, payload.seq);
      setMessages((current) => {
        const live = current.find((m) => m.id === liveId);
        const replacesLive =
          payload.role === 'assistant' &&
          !!live &&
          (payload.content.includes(live.content) || live.content.includes(payload.content));
        const base = replacesLive ? current.filter((m) => m.id !== liveId) : current;
        const existing = base.find((m) => m.id === payload.id);
        if (existing) {
          if ((existing.stream_sequence ?? 0) > (payload.stream_sequence ?? 0)) return base;
          return base.map((m) => m.id === payload.id ? payload : m);
        }
        return [...base, payload];
      });
    });

    listen('code_step', (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as CodeStep;
      if (payload.artifact_scope === 'attempt') {
        useProofSession.getState().bumpFormalizationRefresh();
        return;
      }
      if (typeof payload.seq === 'number') lastSeqRef.current = Math.max(lastSeqRef.current, payload.seq);
      const current = useProofSession.getState().codeSteps;
      if (current.some((s) => s.id === payload.id)) {
        replaceCodeSteps(current.map((s) => (s.id === payload.id ? { ...s, ...payload } : s)));
      } else {
        replaceCodeSteps(sortCodeSteps([...current, payload]));
      }
      // Attribution updates data, never the author's selected scope or history.
      if (payload.formalization_id) useProofSession.getState().bumpFormalizationRefresh();
    });

    listen('status', (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as Partial<StatusEvent>;
      setStatusEvents((current) => [
        ...current,
        {
          id: payload.id || `${runId}-${current.length}-${Date.now()}`,
          session_id: payload.session_id || sessionId,
          run_id: payload.run_id || runId,
          status: payload.status || null,
          message: payload.message || payload.status || 'status update',
          turn: payload.turn ?? null,
          check_status: payload.check_status ?? null,
          check_detail: payload.check_detail ?? null,
          created_at: payload.created_at || new Date().toISOString(),
        },
      ]);
    });

    // Server-side queue (Phase 2): a pending run's stream opens with its FIFO
    // position. Surface it on the status timeline so waiting reads as an
    // honest "queued behind N" instead of a bare spinner.
    listen('queued', (event) => {
      let position: number | null = null;
      try {
        const payload = JSON.parse((event as MessageEvent).data || '{}') as { position?: number };
        position = typeof payload.position === 'number' ? payload.position : null;
      } catch {
        /* position stays unknown */
      }
      setStatusEvents((current) => [
        ...current,
        {
          id: `queued-${runId}`,
          session_id: sessionId,
          run_id: runId,
          status: 'queued',
          message:
            position && position > 0
              ? `Queued behind ${position} other ${position === 1 ? 'run' : 'runs'}…`
              : 'Queued — starting shortly…',
          turn: null,
          check_status: null,
          check_detail: null,
          created_at: new Date().toISOString(),
        },
      ]);
    });

    listen('approval_requested', (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as PendingApproval;
      approvalCounterRef.current += 1;
      // The gate must sort BELOW everything currently rendered — it's the pending next
      // action. `lastSeqRef` alone lags: the gate fires on write_file *before* this turn's
      // assistant text / resulting code_step are persisted, so those land at higher DB
      // seqs and the gate (stamped only against the previous frontier) would render ABOVE
      // the last write box. Stamp against the true max seq across all known content.
      const state = useProofSession.getState();
      const maxKnown = Math.max(
        lastSeqRef.current,
        ...state.messages.map((m) => (typeof m.seq === 'number' ? m.seq : 0)),
        ...state.codeSteps.map((c) => (typeof c.seq === 'number' ? c.seq : 0)),
      );
      const seq = maxKnown + 0.5 + approvalCounterRef.current * 1e-4;
      setApprovals((prev) =>
        prev.some((a) => a.approval_id === payload.approval_id)
          ? prev
          : [...prev, { ...payload, session_id: sessionId, run_id: runId, decision: null, seq }],
      );
      setApprovalBusy(false);
    });

    listen('approval_resolved', (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as { approval_id?: string; decision?: string };
      setApprovals((prev) =>
        prev.map((a) =>
          a.approval_id === payload.approval_id ? { ...a, decision: payload.decision || 'resolved' } : a,
        ),
      );
      setApprovalBusy(false);
    });

    listen('subagent_started', (event) => {
      // D1: a child sub-agent was just spawned and the adapter has persisted it as a
      // RUNNING child session (a running run row → derived status 'running'). Refresh so
      // it lands in the store: the sidebar's Sub-agents block and the parent thread's
      // spawn node render it live as 'exploring…' instead of nothing until it finishes.
      // Same session-list-is-the-source-of-truth path as subagent_finished.
      const data = (event as MessageEvent).data;
      if (data) {
        try {
          // D4: spawned ⇒ queued until its own first event proves it is running.
          const p = JSON.parse(data) as { child_id?: string; state?: string };
          if (p.child_id && p.state === 'queued') {
            useProofSession.getState().setSubagentQueued(
              (prev) => ({ ...prev, [p.child_id as string]: true }),
            );
          }
        } catch {
          /* ignore a malformed payload — the refresh below still runs */
        }
      }
      useSessions.getState().refreshSessions().catch(() => {});
    });

    listen('subagent_progress', (event) => {
      // E1: a running child emitted one of its own steps. Fold it into that child's
      // ephemeral live state (rendered on its spawn-node row) so the user watches it
      // work — text streams, the current tool + latest check show. Visibility only; the
      // durable transcript still lands on finish.
      const data = (event as MessageEvent).data;
      if (!data) return;
      let p: { child_id?: string; kind?: string; text?: string; tool?: string; status?: string; turn?: number };
      try {
        p = JSON.parse(data);
      } catch {
        return;
      }
      const childId = p.child_id;
      if (!childId) return;
      useProofSession.getState().setSubagentProgress((prev) => {
        const cur = prev[childId] || { text: '' };
        let next = cur;
        if (p.kind === 'text') next = { ...cur, text: cur.text + (p.text || '') };
        else if (p.kind === 'turn') next = { ...cur, text: '', turn: p.turn, tool: undefined };
        else if (p.kind === 'tool') next = { ...cur, tool: p.tool };
        else if (p.kind === 'check') next = { ...cur, check: p.status, tool: undefined };
        else return prev;
        return { ...prev, [childId]: next };
      });
    });

    listen('subagent_finished', (event) => {
      // A child sub-agent finished and the adapter has already persisted it as its own
      // session (item 24). Refresh the list so the child lands in the store — the
      // sidebar's Sub-agents block and the parent thread's spawn node both derive from
      // it. The session list is the source of truth; also drop the child's ephemeral live
      // state now the durable transcript takes over (E1).
      const data = (event as MessageEvent).data;
      if (data) {
        try {
          const p = JSON.parse(data) as {
            child_id?: string; error?: string | null; stop_notice?: string | null;
          };
          const childId = p.child_id;
          if (childId) {
            useProofSession.getState().setSubagentProgress((prev) => {
              if (!(childId in prev)) return prev;
              const next = { ...prev };
              delete next[childId];
              return next;
            });
            // D4: finished ⇒ definitively not queued (covers a child that was
            // stopped or failed while still waiting for a slot).
            useProofSession.getState().setSubagentQueued((prev) => {
              if (!prev[childId]) return prev;
              const next = { ...prev };
              delete next[childId];
              return next;
            });
            // A child that couldn't run reports an `error` — record it so the spawn card
            // shows a red "failed" child with the real message, not a bland "no candidate".
            if (p.error) {
              useProofSession.getState().setSubagentErrors((prev) => ({ ...prev, [childId]: p.error as string }));
            }
            // D3: it DID run but stopped short (budget, stopped, no candidate).
            // Not a failure, so not `subagentErrors` — but not the clean finish it
            // used to render as either.
            if (p.stop_notice) {
              useProofSession.getState().setSubagentStopNotices(
                (prev) => ({ ...prev, [childId]: p.stop_notice as string }),
              );
            }
          }
        } catch {
          /* ignore a malformed payload — the refresh below still runs */
        }
      }
      useSessions.getState().refreshSessions().catch(() => {});
    });

    // v2.4: every backend failure arrives here — anchored, coded, and already
    // persisted server-side, so this listener only has to merge it in.
    listen('diagnostic', (event) => {
      const data = (event as MessageEvent).data;
      if (!data) return;
      try {
        const payload = JSON.parse(data) as Diagnostic;
        if (typeof payload.seq === 'number') {
          lastSeqRef.current = Math.max(lastSeqRef.current, payload.seq);
        }
        useProofSession.getState().addDiagnostic(payload);
      } catch {
        /* a malformed diagnostic must not take down the stream */
      }
    });

    // `run_error` still rides the wire for the Overleaf companion, which parses it
    // as a run's failure reason. In THIS client it is now redundant: the same
    // failure arrives as a fatal `diagnostic` with a code, a remedy, and an anchor.
    // Handling both would render every crash twice.

    // D4: a spawned child cleared the concurrency semaphore and actually started.
    // Until this arrives the child is queued, not running.
    listen('subagent_running', (event) => {
      const data = (event as MessageEvent).data;
      if (!data) return;
      try {
        const { child_id } = JSON.parse(data) as { child_id: string };
        useProofSession.getState().setSubagentQueued((prev) => {
          if (!prev[child_id]) return prev;
          const next = { ...prev };
          delete next[child_id];
          return next;
        });
      } catch {
        /* ignore a malformed payload */
      }
    });

    listen('done', (event) => {
      sawDone = true;
      source.close();
      eventSourceRef.current = null;
      let status: RunStatus = 'proved';
      let resultKind: string | null | undefined;
      let stopReason: string | null | undefined;
      let recoveryState: string | null | undefined;
      let recoveryReason: string | null | undefined;
      try {
        const payload = JSON.parse((event as MessageEvent).data || '{}');
        status = (payload.status as RunStatus) || 'proved';
        resultKind = payload.result_kind;
        stopReason = payload.stop_reason;
        recoveryState = payload.recovery_state;
        recoveryReason = payload.recovery_reason;
      } catch {
        /* keep default */
      }
      setIsRunning(false);
      setRunStatus(status);
      setRunStatusById((prev) => ({ ...prev, [runId]: status }));
      setRunResultKindById((prev) => ({ ...prev, [runId]: resultKind }));
      setRunStopReasonById((prev) => ({ ...prev, [runId]: stopReason }));
      useProofSession.getState().setRunRecoveryById((prev) => ({
        ...prev, [runId]: { state: recoveryState, reason: recoveryReason },
      }));
      setCurrentRunId(undefined);
      setApprovals((prev) => prev.filter((a) => a.decision));
      setApprovalBusy(false);
      reconcile(sessionId)
        .then(() => useSessions.getState().refreshSessions())
        .catch((err) => { if (owned()) setError(err instanceof Error ? err.message : String(err)); });
    });

    source.onerror = () => {
      // Ignore errors from a stream we've already replaced/closed, and the benign
      // error that follows a normal `done` close.
      if (!owned() || eventSourceRef.current !== source) return;
      if (sawDone) {
        source.close();
        eventSourceRef.current = null;
        return;
      }
      // Both a transient drop (readyState CONNECTING, which the browser would
      // retry immediately and uncapped) and a hard close / 409 (readyState CLOSED,
      // which it would not retry at all) funnel through our own backoff: close the
      // native source and schedule a capped, jittered reattempt. isRunning stays
      // true — the run is still live server-side; the reconnecting chip is the
      // honest signal while we wait.
      source.close();
      eventSourceRef.current = null;
      scheduleReconnect(runId, sessionId);
    };
  };

  return { attachStream, applyDetail, reconcile, closeStream, loadSession, captureSelection };
}
