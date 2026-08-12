"""The prover seam — drives the vendored prover in-process and maps its typed
events onto the browser's SSE stream (architecture D1/D17).

This replaces the old ``runner.py``: there is no HTTP boundary and no loosely-typed
frame normalization. ``run_events()`` (imported as a library) yields *typed*
meaning-level events, so this is a flat ``isinstance`` dispatch:

  AssistantTextDelta -> assistant_delta      (narration, buffered)
  TurnStarted        -> flush narration into a `message`, mark the turn
  ToolCalled         -> flush narration, emit a status chip
  FileChanged        -> store the file's contents + insert a code_step
  CheckResult        -> back-fill that step's verdict
  UsageUpdated       -> accumulate per-turn token/cost rows
  Finished           -> terminal message + persist run + usage, then `done`

SQL owns proof content (C1/D7, inverted in v2.3): on every write the adapter reads
the file's after-state and stores it as a content-addressed blob, and the code_step
row points at that blob; the same bytes go out on the stream for the live canvas.
Content and pointer can't disagree because there is only one store. The verdict
lives on the row (D6) and is back-filled when ``lean_check`` returns.

Git is no longer the store — it stays only as *transport* for non-proof assets
(uploads, project files), which have their own path.

Scope (D1·bridge): single activation — ``messages = [{user: task}]``. Faithful
multi-turn transcript replay is D1·multiturn; the per-tool gate is D9/D10;
interrupt is D7; diff-on-divergence context is D6.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Any

from uuid import uuid4

from lea.interface import (
    AssistantTextDelta,
    CheckResult,
    Compacted,
    Diagnostic,
    Error,
    FileChanged,
    Finished,
    SubagentFinished,
    SubagentProgress,
    SubagentStarted,
    ToolApprovalRequested,
    ToolCalled,
    ToolResulted,
    TurnStarted,
    UsageUpdated,
    check as _lean_check_file,
    verify as _safe_verify_file,
    request_child_stop,
    run_events,
)

from .artifacts import classify_lean_artifact, declaration_present, extract_declaration_name
from .config import LeaConfig, configured_provider_keys, load_config
from .diagnostics import analyze_exception, resolve as resolve_diagnostic
from .gitstore import GitStore, GitStoreError
from . import collation, formalizations as formalization_service, projects, roles_catalog, runbroker, runregistry, skills_catalog, store, subagent_overrides, uploads

logger = logging.getLogger("lea-interface.bridge")
_FORMALIZATION_CONTEXT_MARKER = "<!-- lea:formalization-context -->"
_CREDENTIAL_LIKE = re.compile(
    r"(?:sk-(?:ant-)?[A-Za-z0-9_./+\-=]{8,}|AIza[A-Za-z0-9_-]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})"
)


def _redact(text: str) -> str:
    """Strip configured provider keys and credential-shaped strings from `text`.

    Split out of `_public_error_detail` so every user-facing path can reuse it — a
    diagnostic is persisted AND rendered, so it needs the same treatment as the run's
    stored error. Providers quote the credential back at you: the live DeepSeek
    failure read "Your api key: ... is invalid"."""
    try:
        for secret in configured_provider_keys().values():
            if len(secret) >= 8:
                text = text.replace(secret, "[redacted]")
    except Exception:  # noqa: BLE001 — redaction is best-effort on an error path
        pass
    return _CREDENTIAL_LIKE.sub("[redacted]", text)[:2000]


def _public_error_detail(exc: Exception) -> str:
    """A useful persisted/provider error with credentials removed and bounded size."""
    return _redact(f"{type(exc).__name__}: {exc}")

# Admission — which run may start and whether there's room — lives in
# `runregistry` (v2.3 items 9/10): one lock, an atomic check-that-is-the-claim.
# The FIFO dispatcher claims the slot before spawning the run thread, avoiding
# the old endpoint-peek/thread-claim TOCTOU. ``run_lea`` releases that slot in
# its ``finally`` on every exit.

# Per-run cooperative stop flags (D18). The run registers its Event when it starts
# and the interrupt endpoint sets it; the agent checks it at each turn boundary and
# stops cleanly. `setdefault` everywhere makes the order race-free — whoever touches
# a run_id first creates the shared Event.
_stop_events: dict[str, Event] = {}


def request_stop(run_id: str) -> None:
    """Flag a run for a clean cooperative stop (the interrupt endpoint calls this)."""
    _stop_events.setdefault(run_id, Event()).set()


# D2: live sub-agent children, mapping the child's SESSION id -> its prover `result_id`,
# so the stop endpoint can translate a UI action on a child session into a per-child
# stop. Populated when a child spawns (SubagentStarted) and cleared when it finishes /
# the coordinator run ends. Process-global (the endpoint runs on a different thread than
# the run), matching `_stop_events`.
_child_session_to_result: dict[str, str] = {}


def request_subagent_stop(child_session_id: str) -> bool:
    """Ask a single running child sub-agent to stop cleanly (D2), addressed by its child
    SESSION id (what the UI holds). Returns True if a live child was found and flagged.
    The child returns its partial findings at its next turn boundary; the coordinator run
    keeps going — this kills one runaway child, not the whole run."""
    result_id = _child_session_to_result.get(child_session_id)
    if result_id is None:
        return False
    return request_child_stop(result_id)


# --- Capacity-aware FIFO dispatcher (integrates v2.3 + hardening Phase 2) -----
# POST /api/runs enqueues immediately and GET /events only observes. A single
# dispatcher preserves FIFO admission order, while the v2.3 registry still allows
# up to LEA_MAX_CONCURRENT_RUNS admitted drivers at once. Each admitted run gets
# the existing rejoinable RunBroker, so reconnect/cursor semantics and concurrent
# sub-agent work remain intact. This replaces gen_repair's single-run event hub;
# the broker is the one event source for both interactive and Overleaf clients.
_run_queue: "Queue[str]" = Queue()
_worker_guard = Lock()
_worker_thread: Thread | None = None


def _resolve_task(run: dict[str, Any]) -> str | None:
    session = store.session_detail(run["session_id"])
    if not session or not session.get("messages"):
        return None
    return next(
        (m["content"] for m in reversed(session["messages"])
         if m["run_id"] == run["id"] and m["role"] == "user"),
        None,
    )


# One dispatch attempt's outcome. `DEFER` is the only one that keeps a run waiting.
_SETTLED, _DISPATCHED, _DEFER = "settled", "dispatched", "defer"

# How long the dispatcher waits between retries when every waiting run is blocked on
# capacity or on its own session. Only reached when nothing else is arriving.
_DISPATCH_RETRY_SECONDS = 0.1


def _try_dispatch(run_id: str, superseded: dict[str, str]) -> str:
    """Attempt to admit and start one run. Never blocks.

    Returns `_SETTLED` (nothing more to do — terminal, missing, or already driven),
    `_DISPATCHED` (a driver thread is running it), or `_DEFER` (not admissible *yet*).
    """
    run = store.get_run(run_id)
    if not run or run["status"] != "pending":
        if run:
            publish_terminal_from_row(run_id)
            runbroker.drop(run_id)
        return _SETTLED

    task = _resolve_task(run)
    if task is None:
        store.update_run(run_id, "failed", result_kind="failed",
                         result_detail="Run task not found.")
        publish_terminal_from_row(run_id)
        runbroker.drop(run_id)
        return _SETTLED

    admission = runregistry.registry.try_admit(run_id, run["session_id"])
    if admission.outcome == runregistry.ADMITTED:
        superseded.pop(run_id, None)
        broker = runbroker.get(run_id) or runbroker.create(run_id)
        # Run creation snapshots the selected model. Reload every other
        # live setting at admission time, but never let a later settings
        # or environment change switch a queued run to another model.
        config = load_config()
        if run.get("model"):
            config = replace(config, model=run["model"])
        context = RunnerContext(
            session_id=run["session_id"],
            run_id=run_id,
            task=task,
            config=config,
            events=broker,
            autonomous=bool(run.get("autonomous")),
        )
        try:
            Thread(target=run_lea, args=(context,), daemon=True,
                   name=f"lea-run-{run_id[:8]}").start()
        except BaseException:
            runregistry.registry.release(run_id)
            store.update_run(run_id, "failed", result_kind="failed",
                             result_detail="Could not start the run worker.")
            publish_terminal_from_row(run_id)
            runbroker.drop(run_id)
            raise
        return _DISPATCHED

    if admission.outcome == runregistry.ALREADY_ACTIVE:
        # A duplicate enqueue/recovery entry; the admitted driver owns it.
        return _SETTLED
    if admission.outcome == runregistry.SESSION_BUSY and admission.incumbent_run_id:
        # Preserve main's per-session supersede behavior without letting two turns
        # mutate the same session concurrently. Asked ONCE per incumbent: the flag is
        # an Event, so repeating is harmless, but the old loop re-issued it on every
        # 100 ms poll for as long as the incumbent took to wind down.
        if superseded.get(run_id) != admission.incumbent_run_id:
            superseded[run_id] = admission.incumbent_run_id
            request_stop(admission.incumbent_run_id)
    return _DEFER


def _worker_loop() -> None:
    """Dispatch queued runs, never letting one that cannot start hold up one that can.

    The previous shape spun in an inner loop on the run at the head of the queue until
    it was admissible (AUDIT-2026-07-24 X1). That is fine when the blocker is capacity
    — nothing else could start either — but `SESSION_BUSY` blocks on a *different*
    session's incumbent winding down, which can take a whole model call, or up to the
    900-second approval timeout if that incumbent is parked on an unanswered gate.
    Runs for unrelated sessions sat behind it with slots free, so
    `LEA_MAX_CONCURRENT_RUNS=4` did not deliver four in the multi-chat case Phase 6
    raised the default for.

    Now a run that cannot be admitted is set aside and retried on the next pass while
    the dispatcher keeps draining the queue. `deferred` preserves the relative order of
    the runs waiting, and is retried before newly arrived ones, so FIFO among the
    blocked set is unchanged — what is gone is one blocked run's claim on everyone
    else's turn.

    The queue is polled only while something is deferred; with nothing waiting the
    dispatcher blocks on `get()` as before and costs nothing when idle.
    """
    deferred: list[str] = []
    # run_id -> the incumbent we have already asked to stop for it, so a supersede is
    # requested once rather than on every retry.
    superseded: dict[str, str] = {}
    while True:
        try:
            arrived = _run_queue.get(timeout=_DISPATCH_RETRY_SECONDS if deferred else None)
        except Empty:
            arrived = None

        # Deferred first: they have been waiting longest.
        batch, deferred = deferred, []
        if arrived is not None:
            batch.append(arrived)

        for run_id in batch:
            try:
                if _try_dispatch(run_id, superseded) == _DEFER:
                    deferred.append(run_id)
                else:
                    superseded.pop(run_id, None)
            except Exception:  # noqa: BLE001 — the worker must survive any single run
                logger.exception("Run worker failed while driving run %s", run_id)
                superseded.pop(run_id, None)
                try:
                    current = store.get_run(run_id)
                    if current and current["status"] in {"pending", "running"}:
                        store.update_run(run_id, "failed")
                except Exception:
                    logger.exception("Failed to mark run %s failed", run_id)
                publish_terminal_from_row(run_id)
                runbroker.drop(run_id)


def enqueue_run(run_id: str) -> None:
    """FIFO-enqueue a created run for capacity-aware background admission."""
    global _worker_thread
    if runbroker.get(run_id) is None:
        runbroker.create(run_id)
    with _worker_guard:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = Thread(target=_worker_loop, daemon=True, name="lea-run-dispatcher")
            _worker_thread.start()
    _run_queue.put(run_id)


def recover_runs_at_startup() -> None:
    """Re-enqueue pending rows after stale running rows are reaped on startup."""
    for run in store.list_runs_by_status("pending"):
        enqueue_run(run["id"])


def publish_terminal_from_row(run_id: str) -> None:
    """Publish a terminal frame for a run finalized without ``run_lea``.

    Late observers can synthesize the same frame from the persisted row after
    the broker is dropped; observers already attached to this broker receive it
    live, which prevents an interrupted queued run from hanging.
    """
    run = store.get_run(run_id)
    broker = runbroker.get(run_id)
    if not run or broker is None:
        return
    payload: dict[str, Any] = {"status": run["status"]}
    if run.get("result_kind"):
        payload["result_kind"] = run["result_kind"]
    if run.get("result_detail"):
        payload["result_detail"] = run["result_detail"]
    broker.put({"type": "done", "payload": payload})


# --- Per-tool approval gate (D19) ------------------------------------------
# Impactful tools prompt the human for allow/deny/always-session before running;
# read-only tools + lean_check are auto-allowed (never gated). "Always allow this
# session" adds the tool to a per-session in-memory allowlist (persists across
# runs in a session, resets on process restart). The prover owns the yield/.send
# hook (A8); the adapter owns the policy + the human relay.
GATED_TOOLS = {"bash", "write_file", "edit_file"}
_APPROVAL_DECISIONS = {"allow", "deny", "always_session"}
# E1: not a decision the human can send — the run synthesizes it when a Stop arrives
# while a gate is pending. The tool still does not run (same safe outcome as a deny),
# but the model is told the call was CANCELLED rather than declined, and the human is
# told their Stop is what ended it. Previously both facts were lost: the decision fell
# through to "deny", so the agent narrated its way around a refusal the user never
# made, and nothing said the Stop had done anything at all.
_APPROVAL_CANCELLED = "cancelled"

# A run parked on an unanswered approval holds a bounded concurrency slot; after this
# it falls through to the safe `deny` so queued work can proceed.
APPROVAL_DECISION_TIMEOUT_SECONDS = 900

_session_allowlists: dict[str, set[str]] = {}
# One in-flight approval per active run: run_id -> the pending decision rendezvous
# between that run thread (waiting) and the endpoint (resolving).
_pending_approvals: dict[str, dict[str, Any]] = {}


def _make_gate(session_id: str):
    """The policy passed to run_events: True = this tool needs human approval."""
    def gate(tool_name: str, args: dict) -> bool:
        if tool_name not in GATED_TOOLS:
            return False
        return tool_name not in _session_allowlists.get(session_id, set())
    return gate


def resolve_approval(run_id: str, approval_id: str, decision: str) -> bool:
    """Deliver the human's decision to a waiting run (the approval endpoint calls
    this). Returns False if there is no matching pending approval (stale/unknown)."""
    pending = _pending_approvals.get(run_id)
    if not pending or pending["approval_id"] != approval_id:
        return False
    pending["decision"] = decision
    pending["event"].set()
    return True


def _await_decision(run_id, session_id, ev, events, stop_event) -> str:
    """Relay one gated tool call to the human and return the decision.

    Emits `approval_requested`, blocks the run thread until the endpoint resolves
    it (staying responsive to Stop — a stop bails to `deny`), records an
    `always_session` allowlist entry, emits `approval_resolved`, and returns
    `allow | deny | always_session`. Anything unexpected → `deny` (safe default)."""
    approval_id = uuid4().hex
    # Persist the pending approval onto the run row so it survives a stream drop,
    # a reconnect, or a session switch: `session_detail` re-surfaces
    # `active_run.pending_approval` and the UI rebuilds the same card. Without this
    # the approval lives ONLY in the one-shot `approval_requested` SSE event — a
    # client that missed it (reattached after it fired, or switched away and back)
    # waits forever with no card. The persisted bytes mirror the live event so the
    # rebuilt card is identical (same principle as the streamed/stored code rows).
    store.set_run_pending_approval(run_id, {
        "approval_id": approval_id, "tool_name": ev.tool_name, "args": ev.args,
    })
    # Only expose the in-memory rendezvous after the durable reconnect state is
    # present; then publish the event. A resolver can never observe the card before
    # its decision target exists, and a reconnect can never observe the target
    # before the persisted card exists.
    _pending_approvals[run_id] = {
        "approval_id": approval_id, "event": Event(), "decision": None,
    }
    emit(events, "approval_requested", {
        "approval_id": approval_id, "run_id": run_id, "session_id": session_id,
        "tool_name": ev.tool_name, "args": ev.args,
    })
    pending = _pending_approvals[run_id]
    cancelled = False
    # A run parked forever on an unanswered approval consumes one bounded
    # concurrency slot indefinitely. Time out to the safe `deny` so queued work
    # can continue even if its original observer disappeared.
    waited = 0.0
    while not pending["event"].wait(timeout=0.5):
        if stop_event.is_set():
            cancelled = True
            break
        waited += 0.5
        if waited >= APPROVAL_DECISION_TIMEOUT_SECONDS:
            break
    _pending_approvals.pop(run_id, None)

    decision = pending["decision"]
    if cancelled and decision is None:
        # E1: the Stop ended the wait, not the human's judgement of this tool call.
        decision = _APPROVAL_CANCELLED
        diagnose(events, session_id, run_id, "notice", "approval.cancelled",
                 f"Stop cancelled the pending approval for {ev.tool_name}.",
                 tool=ev.tool_name, approval_id=approval_id)
    elif decision not in _APPROVAL_DECISIONS:
        decision = "deny"
    if decision == "always_session":
        _session_allowlists.setdefault(session_id, set()).add(ev.tool_name)
    # Gate resolved → clear the persisted card so a reconnect doesn't re-raise a
    # decision the run has already consumed.
    store.set_run_pending_approval(run_id, None)
    emit(events, "approval_resolved", {"approval_id": approval_id, "decision": decision})
    return decision


@dataclass
class RunnerContext:
    """What a single run needs. Same shape main.py built for runner.py, minus the
    dead HTTP `client` field — the prover is now in-process."""

    session_id: str
    run_id: str
    task: str
    config: LeaConfig
    # A rejoinable RunBroker in the live endpoint; a plain Queue in unit tests. Both
    # expose `.put({"type","payload"})`, which is all `emit()` needs.
    events: "runbroker.RunBroker | Queue[dict[str, Any]]"
    # Autonomous (D19): no approval gate + the non-interactive `default` prompt
    # variant, so the run formalizes with zero human interaction (Overleaf path).
    autonomous: bool = False


def emit(events: "runbroker.RunBroker | Queue[dict[str, Any]]",
         event_type: str, payload: dict[str, Any]) -> None:
    events.put({"type": event_type, "payload": payload})


def diagnose(
    events,
    session_id: str,
    run_id: str | None,
    severity: str,
    code: str,
    message: str,
    *,
    turn: int | None = None,
    source: str = "adapter",
    remedy: str | None = None,
    title: str | None = None,
    detail: str | None = None,
    actions: list[dict] | None = None,
    **context,
) -> dict:
    """Persist one diagnostic and stream it (v2.4, A2) — the single way a failure
    reaches the human.

    Store-then-emit, with the SAME payload on both sides, so a reload can't disagree
    with what was on screen (the invariant the code rows already hold, and the one
    the old client-side error string broke by construction).

    A failure to STORE a diagnostic must never suppress it: the persistence is a
    bonus, the visibility is the point. So a store error is logged and the diagnostic
    is streamed anyway, marked as unsaved.
    """
    payload = resolve_diagnostic(
        severity, code, message, source=source, remedy=remedy,
        title=title, detail=detail, actions=actions,
        context={**context, "turn": turn},
    )
    row: dict
    try:
        row = store.add_diagnostic(session_id, run_id, payload, turn=turn)
    except Exception:  # noqa: BLE001 — see docstring: never swallow the diagnostic
        logger.exception("Could not persist diagnostic %s for run %s", code, run_id)
        row = {
            **payload,
            "id": f"unsaved-{uuid4().hex[:8]}",
            "session_id": session_id,
            "run_id": run_id,
            "turn": turn,
            "persisted": False,
            "created_at": store.utc_now(),
        }
    emit(events, "diagnostic", row)
    return row


# How long a run may hold streamed text before publishing it, and the largest single
# frame it will build. The interval matches the subscriber poll in
# `routes/runs._subscribe` — text produced between two polls is delivered together no
# matter how many frames it arrived in, so a finer granularity than this is invisible.
_DELTA_FLUSH_SECONDS = 0.08
_DELTA_FLUSH_CHARS = 512


class _DeltaStream:
    """Coalesce per-token ``assistant_delta`` frames into roughly one frame per poll.

    The prover yields one ``AssistantTextDelta`` per token and the adapter published
    one SSE frame for each. Nobody could see that granularity — the browser renders
    whatever accumulated since its last 80 ms poll — but every frame cost a dict and a
    list slot retained in the broker for the run's whole life, and made each poll walk
    further (AUDIT-2026-07-24 P1). A long run buffered tens of thousands of frames to
    deliver text a fraction of that size.

    Frames are merged **before** publication, never after. An event already in the
    broker carries a ``seq`` some subscriber may have consumed, so appending to it in
    place would silently lose text for any client whose cursor is already past it —
    which is why this batches upstream instead of compacting the buffer.

    A slow stream is not delayed: the deadline is checked on arrival, so when tokens
    come in slower than the interval each one flushes immediately.
    """

    def __init__(self, events: "runbroker.RunBroker | Queue[dict[str, Any]]") -> None:
        self._events = events
        self._parts: list[str] = []
        self._length = 0
        self._deadline: float | None = None

    def add(self, text: str) -> None:
        if not text:
            return
        self._parts.append(text)
        self._length += len(text)
        now = time.monotonic()
        if self._deadline is None:
            self._deadline = now + _DELTA_FLUSH_SECONDS
        if self._length >= _DELTA_FLUSH_CHARS or now >= self._deadline:
            self.flush()

    def flush(self) -> None:
        """Publish whatever is buffered. Must run before ANY other event is emitted, so
        streamed text can never appear after the step it preceded."""
        if not self._parts:
            return
        text = "".join(self._parts)
        self._parts.clear()
        self._length = 0
        self._deadline = None
        emit(self._events, "assistant_delta", {"text": text})


# A run's final status. "proved" / "disproved" / "needs_review" are terminal
# checked-artifact outcomes. "answered" is a chat / QA / sketch-pause turn that
# finished cleanly but proved nothing, so the UI never marks a conversational turn
# as a completed proof.
_FINISH_STATUS = {
    "assistant": "answered",
    "max_turns": "max_turns",
    "interrupted": "cancelled",
}

_COMPLETED_RESULTS = {"proved", "disproved", "needs_review"}

# Shown as result_detail when the bridge's own spend-cap stop ended the run, so
# clients can tell a cap stop (result_kind="max_spend") from a user cancel. The
# persisted run *status* stays "cancelled" — the status vocabulary is unchanged.
_MAX_SPEND_DETAIL = "Max spend limit reached; the run was stopped at a turn boundary."

# Usage is persisted only when a run finishes. While several admitted runs are
# live, their in-flight cost would otherwise be invisible to each other's global
# cap checks. This process-local overlay makes the check persisted-global plus
# every active run's observed ``UsageUpdated`` total.
_live_spend_lock = Lock()
_live_run_costs: dict[str, float] = {}


# Persisted spend changes only when a run *finishes* (`update_run` writes its
# cost_usd), but the cap is re-checked on every `UsageUpdated` and every turn — so the
# unmemoized read meant several DB aggregates per second per active run, against the
# same single-writer SQLite the runs are writing to (AUDIT-2026-07-24 P2). A short TTL
# removes that without weakening the cap: the term that moves continuously *within* a
# run is the in-memory `_live_run_costs` overlay, which is always exact, and the
# staleness this admits is bounded by one other run finishing inside the window.
_PERSISTED_SPEND_TTL_SECONDS = 2.0
_persisted_spend_lock = Lock()
# (database path, monotonic_at, usd). The path is part of the key because the total is
# a property of ONE database: production never repoints `db.DB_PATH`, but tests do, and
# a cache that ignored which database it measured would hand one test's total to the
# next. Same hazard `backup.py` documents for the path it copies.
_persisted_spend_cache: tuple[Path, float, float] | None = None


def _persisted_spend_usd() -> float:
    """Total spend on finished runs, cached for `_PERSISTED_SPEND_TTL_SECONDS`."""
    global _persisted_spend_cache
    from . import db

    path, now = db.DB_PATH, time.monotonic()
    with _persisted_spend_lock:
        cached = _persisted_spend_cache
        if (
            cached is not None
            and cached[0] == path
            and now - cached[1] < _PERSISTED_SPEND_TTL_SECONDS
        ):
            return cached[2]
    value = store.total_spend_usd()
    with _persisted_spend_lock:
        _persisted_spend_cache = (path, now, value)
    return value


def reset_persisted_spend_cache() -> None:
    """Drop the cached total. For tests, and for any caller that has just persisted a
    run's cost and wants the next check to see it immediately."""
    global _persisted_spend_cache
    with _persisted_spend_lock:
        _persisted_spend_cache = None


def _finished_status(ev: Finished) -> str:
    if ev.reason == "completed":
        return ev.result_kind if ev.result_kind in _COMPLETED_RESULTS else "proved"
    return _FINISH_STATUS.get(ev.reason, "failed")


def _completed_artifact_result(ev: Finished, artifact_kind: str) -> tuple[str, str | None]:
    """Return persisted run status + result kind for a completed Lea artifact."""
    if ev.reason != "completed":
        return _finished_status(ev), None
    if ev.result_kind == "disproved":
        return "disproved", "disproved"
    if artifact_kind == "definition":
        return "proved", "defined"
    result_kind = ev.result_kind if ev.result_kind in _COMPLETED_RESULTS else "proved"
    return result_kind, result_kind


def _final_text_for_result(ev: Finished) -> str:
    text = ev.text or ""
    if ev.result_kind != "disproved":
        return text
    lower = text.lower()
    if "disprov" in lower or "counterexample" in lower or "not proven" in lower:
        return text
    prefix = (
        "Lea found a verified counterexample or disproof. "
        "The original theorem was not proven; the verified result shows the statement is false."
    )
    return f"{prefix}\n\n{text}".strip()


def _read_after(path: str) -> str | None:
    """The file's contents after a write, or **None** if it could not be read
    (deleted, binary, or racing a later write).

    C2: this used to return `""` on failure, and every caller stored that as the
    file's contents — so an unreadable file was recorded, and rendered on the canvas,
    as a file the agent had written empty. That is a confident false claim about
    proof content. `None` forces each caller to say what it actually knows: the code
    path sets `content_lost` (the flag the schema has carried since 0003 and nothing
    ever set), and the divergence path declines to diff rather than reporting the
    whole file as deleted by the human."""
    try:
        return Path(path).read_text()
    except (OSError, UnicodeDecodeError):
        logger.debug("Could not read after-state of %s", path, exc_info=True)
        return None


def _classify(code: str) -> str | None:
    """`classify_lean_artifact`, but a parse failure is not a run failure."""
    try:
        return classify_lean_artifact(code)
    except Exception:  # noqa: BLE001 — classification is a presentation detail
        return None


def _divergence_context(
    session_id: str,
    repo_key: str,
    gs: GitStore,
    formalization_id: str | None = None,
) -> str | None:
    """Diff-on-divergence (D12): if the human edited the proof since the agent last
    acted, return a context block (a diff + any edit notes) to fold into the next
    run's task — so the agent sees and acknowledges the changes (D13). None when
    nothing diverged (cold start, or no edits since the agent's last write).

    Scoped to the file the agent last wrote. It used to be a repo-wide
    `git diff <sha> HEAD`, which is wrong for a *shared* project repo (D24): an edit
    to any file in the project — including one belonging to a different session —
    reported as this session's divergence and got pasted into its task.

    The 'before' is the agent's last stored content and the 'after' is the file on
    disk, so this compares the two things it actually claims to compare. The old
    version compared two git revisions, and its `before` was only as good as a
    pointer nobody verified (see 0004's backfill: one such pointer named a commit
    whose tree never contained the file)."""
    agent_step = (
        store.latest_agent_code_step_for_formalization(session_id, formalization_id)
        if formalization_id
        else store.latest_agent_code_step(session_id)
    )
    if not agent_step or not agent_step.get("path"):
        return None
    path = agent_step["path"]
    repo = gs.init_session(repo_key)
    before = agent_step.get("code") or ""
    after = _read_after(str(repo / path))
    if after is None:
        # Unreadable now. Previously this compared against `""` and produced a diff
        # showing the entire proof deleted — telling the agent the human had wiped
        # its work. Not knowing the current bytes is not evidence of an edit.
        return None
    if before == after:
        return None
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}",
        )
    )
    if not diff.strip():
        return None

    parts = [
        "The human edited the proof files since your last turn. Here is the diff of their changes:",
        "```diff",
        diff.strip(),
        "```",
    ]
    notes = store.edit_notes_since(session_id, agent_step["seq"])
    if notes:
        parts.append("Their note(s) on the edit(s):")
        parts.extend(f"- {note}" for note in notes)
    parts.append("Acknowledge these changes before continuing.")
    return "\n".join(parts)


def _transcript_gap_context(session_id: str, run_id: str) -> str | None:
    """Tell the agent when earlier turns are missing from the history it was handed
    (AUDIT-2026-07-24 C10), or None when the replayed conversation is continuous.

    A run that crashes mid-turn never reaches `Finished`, so it stores no transcript
    and `latest_transcript_for_session` quietly falls back to an older run. The
    conversation then looks continuous while a turn the user watched is simply absent.

    Why this says "unavailable" rather than replaying the partial history: the prover
    appends the assistant's tool_call message BEFORE the tools run and the tool_result
    messages after, so a transcript captured at an arbitrary crash point can end on a
    tool_call with no matching result — a shape every provider rejects. Replaying it
    would turn a lost turn into a session that cannot start a new one. Recovering the
    content safely means truncating to the last complete turn boundary, which the
    prover would have to expose; until then, being honest about the hole beats
    pretending there isn't one. The files those runs left behind are still on disk, and
    `_divergence_context` reports them.
    """
    gap = store.transcript_gap_for_session(session_id, exclude_run_id=run_id)
    if not gap:
        return None
    lines = [
        f"NOTE: {len(gap)} earlier attempt(s) in this session ended without a usable "
        "record of what they did, so the conversation above skips them:",
    ]
    for run in gap:
        detail = (run.get("result_detail") or "").strip().splitlines()
        reason = detail[0][:160] if detail else (run.get("result_kind") or run.get("status"))
        lines.append(f"- an attempt that ended as {run.get('status')}: {reason}")
    lines.append(
        "Any files they wrote are still on disk and are reflected in the working copy. "
        "Check the current state of the proof files before assuming work is undone, and "
        "do not assume the conversation above is the whole history."
    )
    return "\n".join(lines)


def _artifact_module_name(namespace: str | None, rel: str) -> str | None:
    """Lean module for a repo-relative .lean path in a project repo (whose root
    IS the namespace): `Lea.Project1` + `chapter/decl.lean` → `Lea.Project1.chapter.decl`.
    None for loose sessions (no stable namespace) and non-.lean files."""
    if not namespace or not rel.endswith(".lean"):
        return None
    return f"{namespace}.{rel[:-len('.lean')].replace('/', '.')}"


def _formalization_context_message(formalization: dict | None) -> dict | None:
    """Build the current, replaceable run-focus message for the prover."""
    if not formalization:
        return None
    files = store.list_formalization_files(formalization["id"])
    lines = [
        _FORMALIZATION_CONTEXT_MARKER,
        "The current run is focused on this formalization:",
        f"- id: {formalization['id']}",
        f"- title: {formalization['display_title']}",
        f"- kind: {formalization['kind']}",
    ]
    if formalization.get("declaration_name"):
        lines.append(f"- Lean declaration: {formalization['declaration_name']}")
    if formalization.get("statement"):
        lines.append(f"- statement: {formalization['statement']}")
    if formalization.get("validity_status"):
        lines.append(f"- current validity: {formalization['validity_status']}")
    activity = (formalization.get("activity") or {}).get("status")
    if activity:
        lines.append(f"- current activity: {activity}")
    if formalization.get("source_hash"):
        lines.append(f"- external source hash: {formalization['source_hash']}")
    if files:
        lines.append("- known files:")
        lines.extend(f"  - {item['role']}: {item['path']}" for item in files)
    current = formalization_service.current_snapshot(formalization["id"])
    updated_session = (current or {}).get("last_updated_session")
    if updated_session:
        lines.append(
            "- current project revision last updated in conversation: "
            f"{updated_session['title']} ({updated_session['id']})"
        )
    lines.append(
        "Keep new proof writes for this target in its known primary file when one "
        "exists. You may reference other project declarations without changing focus."
    )
    return {"role": "user", "content": "\n".join(lines)}


def _is_formalization_context_message(message: dict) -> bool:
    return _FORMALIZATION_CONTEXT_MARKER in str(message.get("content") or "")


def _attributable_formalization(
    path: str, focus_formalization_id: str | None
) -> str | None:
    """Attribute proof files, but never scratch or adapter-support output."""
    normalized = path.replace("\\", "/")
    lowered = normalized.lower()
    if (
        not focus_formalization_id
        or not normalized.endswith(".lean")
        or "scratch" in lowered
        or normalized.startswith(".lea/")
    ):
        return None
    return focus_formalization_id


def _record_run_artifacts(
    session_id,
    run_id,
    project,
    namespace,
    steps_by_path,
    *,
    focus_formalization_id: str | None = None,
    source_hash: str | None = None,
) -> None:
    """Write the structured artifact index rows for this run's checked files
    (PLAN-system-hardening 4.1). Only files whose latest step verdict is ok
    are recorded — a broken write is not an artifact. Best-effort by design:
    the index is a convenience the companion falls back gracefully without,
    so failures log rather than fail the run."""
    for rel, step_id in steps_by_path.items():
        try:
            step = store.latest_code_step_for_path(session_id, rel)
            if (
                not step
                or step.get("id") != str(step_id)
                or step.get("check_status") != "ok"
            ):
                continue
            # SQL owns proof content on main (timeline -> artifact_blobs), so use
            # the exact bytes carried by this run's checked code step. Reading a
            # git commit_sha here would resurrect the stale dual-store design.
            code = step.get("code") or ""
            focused = (
                store.get_formalization(focus_formalization_id)
                if focus_formalization_id else None
            )
            focused_declaration = (
                focused.get("declaration_name") if focused else None
            )
            if focused_declaration and not declaration_present(
                code, focused_declaration
            ):
                store.link_formalization_file(
                    focus_formalization_id, rel, "support"
                )
                continue
            declaration = focused_declaration or extract_declaration_name(code)
            if not declaration:
                continue
            formalization = focused
            if (
                formalization is not None
                and not formalization.get("declaration_name")
            ):
                formalization = store.update_formalization(
                    formalization["id"], declaration_name=declaration
                )
            if formalization is None:
                formalization = store.find_formalization_by_declaration(
                    project_id=project["id"] if project else None,
                    loose_session_id=None if project else session_id,
                    declaration_name=declaration,
                )
                if formalization is None:
                    formalization = store.create_formalization(
                        project_id=project["id"] if project else None,
                        loose_session_id=None if project else session_id,
                        display_title=declaration,
                        declaration_name=declaration,
                        kind=step.get("artifact_kind") or classify_lean_artifact(code),
                        origin="legacy-run",
                    )
            formalization_id = formalization["id"]
            store.link_session_formalization(session_id, formalization_id)
            store.link_formalization_file(formalization_id, rel, "primary")
            store.upsert_artifact(
                project_id=project["id"] if project else None,
                session_id=session_id,
                run_id=run_id,
                declaration_name=declaration,
                kind=step.get("artifact_kind") or classify_lean_artifact(code),
                path=rel,
                module_name=_artifact_module_name(namespace, rel),
                formalization_id=formalization_id,
                source_hash=source_hash,
            )
        except Exception:
            logger.exception("Could not record artifact row for %s in run %s", rel, run_id)


def _relativize(path: str, repo: Path) -> str:
    """Return a code step's path relative to the session workspace.

    The agent writes absolute paths under that workspace (the prompt is pointed
    there), so this normally just strips the prefix; a path outside it (drift)
    falls back to its basename.
    """
    p = Path(path)
    try:
        return str(p.resolve().relative_to(repo.resolve()))
    except ValueError:
        return p.name


class _UsageByTurn:
    """Accumulate per-turn token/cost rows for run_usage_breakdown."""

    def __init__(self) -> None:
        self._rows: dict[int, dict[str, Any]] = {}

    def add(self, turn: int, input_tokens: int, output_tokens: int, cost: float) -> None:
        if not (input_tokens or output_tokens or cost):
            return
        row = self._rows.get(turn)
        if row is None:
            row = {
                "phase": "proof_turn" if turn else "preflight",
                "label": f"Turn {turn}" if turn else "Setup",
                "turn": turn or None,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "event_count": 0,
            }
            self._rows[turn] = row
        row["input_tokens"] += input_tokens or 0
        row["output_tokens"] += output_tokens or 0
        row["cost_usd"] += cost or 0.0
        row["event_count"] += 1

    def rows(self) -> list[dict[str, Any]]:
        return [self._rows[k] for k in sorted(self._rows)]


def _with_subagents(cfg: LeaConfig) -> tuple[LeaConfig, str | None]:
    """Return `cfg` with `spawn_subagent` added to the coordinator's toolset (item 24).

    `spawn_subagent` is registered `opt_in=True` in the prover, so an unfiltered toolset
    (`tools=None`) never contains it. The coordinator gets its normal default toolset PLUS
    the opt-in ones it needs.

    **This used to resolve `build_toolset(None)` HERE and pass the result as an explicit
    allowlist**, which looked equivalent and was not: this runs in the adapter before the
    run starts, so the snapshot contained only the built-ins — and the explicit list then
    excluded every MCP and HTTP tool that registers once the run begins. The effect was
    total and silent: no MCP tool could ever be called from the UI, while the server
    started, warmed and reported 23 tools. `extra_tools` says "the default set, plus
    these" and leaves resolution where it belongs — inside the run, after everything has
    registered."""
    # D6: carry the user's per-role sub-agent overrides (Sub-agents page) onto the run's
    # config so the prover's `_child_config` merges them over each role's YAML defaults at
    # spawn — model / max_turns / max_cost / system_prompt / tools, retuned without touching
    # the vendored profile. Best-effort: a missing/corrupt overrides file → no overrides.
    # F2: a malformed overrides file used to revert every child to its vendored
    # defaults silently. The user configured those models, turn caps, and prompts on
    # the Sub-agents page and had no way to know none of it applied. Returned to the
    # caller (this helper has no event channel) so run_lea can report it.
    # `load_overrides_checked` — NOT `load_overrides`, which handles its own failure and
    # returns {}, so a `try` around it catches nothing and this diagnostic could never
    # fire. The reason travels back as a value because that is the only way it survives.
    try:
        overrides, override_error = subagent_overrides.load_overrides_checked()
    except Exception as exc:  # noqa: BLE001 — a bad overrides file must not fail the run
        logger.exception("Could not load sub-agent overrides")
        overrides, override_error = {}, f"{type(exc).__name__}: {exc}"
    if override_error:
        logger.warning("Sub-agent overrides did not load: %s", override_error)
    # Also give the coordinator `safe_verify` (opt-in in the prover registry) so it can run
    # a kernel-level anti-cheat audit on a finished proof before declaring it done — the
    # coordinator delegates proving, then verifies the assembled result. Read-only, so it's
    # not gated, and harmless if a child inherits it via the ⊆-parent tool composition.
    return (
        replace(cfg, tools=None, extra_tools=["spawn_subagent", "safe_verify"],
                subagent_overrides=overrides),
        override_error,
    )


def _text_from_content(content: Any) -> str:
    """Flatten a transcript message's `content` (a string, or a list of provider blocks)
    into plain text — the assistant prose, dropping tool-call/tool-result plumbing — so a
    child's exploration replays as ordinary chat in its read-only view."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
        return "\n".join(p for p in parts if p.strip())
    return ""


def _subagent_child_title(ev: SubagentFinished) -> str:
    """A short, human title for the child session row.

    Prefer the delegated TASK — the description line the coordinator passed, which is the
    first line of the child's first user message. It's stable and meaningful even when the
    child errors or hits max turns (whose `summary` is an error nudge, not a description).
    Fall back to a non-error summary, then the role."""
    for msg in ev.transcript or []:
        if msg.get("role") == "user":
            task = _text_from_content(msg.get("content")).strip()
            first = task.splitlines()[0].strip() if task else ""
            if first:
                return first[:80]
            break
    summary = (ev.summary or "").strip()
    if summary and not summary.lower().startswith("error"):
        first = summary.splitlines()[0].strip()
        if first:
            return first[:80]
    return ev.subagent_type or "sub-agent"


def _start_subagent(
    ev: SubagentStarted,
    *,
    parent_session_id: str,
    project_id: str | None,
    turn: int,
    cfg: LeaConfig,
) -> tuple[dict, str]:
    """D1: the moment a child spawns, create it as a RUNNING child session — a real
    session parented to the coordinator, with a running run row so its *derived* status
    is 'running' (no code yet + an active run → 'running', per `_derive_session_status`).
    So the sidebar's Sub-agents block and the parent's spawn node show a live child the
    instant it starts, instead of nothing until it finishes. `SubagentFinished` (same
    `result_id`) fills the transcript + candidate in and retires the run.

    Returns `(child_row, child_run_id)`. The child run row is also the first small step
    toward E1 (children as first-class runs) — a child now owns a `runs` row it can be
    tracked and, later, stopped through."""
    title = (ev.description or "").strip() or (ev.subagent_type or "sub-agent")
    child = store.create_session(
        title[:80],
        project_id=project_id,
        parent_id=parent_session_id,
        role=ev.subagent_type,
        spawned_at_turn=turn,
    )
    child_run = store.create_run(child["id"], cfg.model, None, cfg.max_turns, project_id=project_id)
    store.update_run(child_run["id"], "running")
    # Record the delegated task as the child's first message NOW, not when it finishes.
    # The child's transcript only replays on SubagentFinished, so until then its session
    # was completely empty: you could watch a child work for minutes with no way to see
    # what it had been asked to do — and therefore no way to judge whether the
    # coordinator had delegated the right thing while there was still time to stop it.
    # It is the same text the transcript will carry, so `_populate_subagent` skips the
    # leading user message on this path rather than storing it twice.
    if (ev.task or "").strip():
        store.add_message(child["id"], "user", ev.task.strip())
    return child, child_run["id"]


def _subagent_progress_payload(child_id: str, result_id: str, inner) -> dict | None:
    """Compact a child's inner `AgentEvent` (E1) into a live `subagent_progress` SSE for
    the browser. The child's steps are streamed for VISIBILITY only — they are not
    persisted here (the authoritative transcript still replays into the child session on
    finish); this is the live feed the coordinator's spawn box renders as the child works.
    Returns None for events with nothing to show live."""
    base = {"child_id": child_id, "result_id": result_id}
    if isinstance(inner, AssistantTextDelta):
        return {**base, "kind": "text", "text": inner.text}
    if isinstance(inner, TurnStarted):
        return {**base, "kind": "turn", "turn": inner.turn}
    if isinstance(inner, ToolCalled):
        return {**base, "kind": "tool", "tool": inner.name}
    if isinstance(inner, CheckResult):
        return {**base, "kind": "check", "status": inner.status}
    if isinstance(inner, Finished):
        return {**base, "kind": "finished", "reason": inner.reason}
    return None


def _child_deltas(broker, started: dict) -> "_DeltaStream":
    """The child's own text batcher, created on first use and kept on its `started`
    record. A child streams tokens onto its own broker exactly like the coordinator
    does, so it needs the same coalescing (P1)."""
    stream = started.get("deltas")
    if stream is None:
        stream = _DeltaStream(broker)
        started["deltas"] = stream
    return stream


def _flush_child_narration(broker, started: dict) -> None:
    """Commit the child's buffered narration as ONE assistant message on its broker, then
    clear the buffer. Mirrors the coordinator's `flush_narration`: a `message` whose
    content overlaps the live bubble REPLACES it (frontend), so each turn's narration lands
    as its own message instead of every turn concatenating into one run-together blob."""
    _child_deltas(broker, started).flush()  # P1: publish trailing streamed text first
    buf = started.get("narration") or []
    text = "".join(buf).strip()
    started["narration"] = []
    if not text:
        return
    n = started.get("msg_seq", 0) + 1
    started["msg_seq"] = n
    emit(broker, "message", {
        "id": f"sa-{started['run_id']}-{n}",
        "session_id": started["child_id"],
        "run_id": started["run_id"],
        "role": "assistant",
        "content": text,
        "created_at": store.utc_now(),
    })


def _forward_to_child_broker(broker, inner, started: dict) -> None:
    """Re-emit a child's inner `AgentEvent` onto its OWN run broker (E1 first-class) in the
    same SSE vocab `run_lea` uses for the coordinator, so the child's session view — which
    attaches to `/api/runs/<child_run_id>/events` like any run — renders it live with the
    exact same listeners. Narration streams as a live bubble and is COMMITTED to a discrete
    message on each turn/tool boundary (so turns don't run together). Ephemeral live view
    only; the durable transcript still replays into the child session on finish."""
    if isinstance(inner, AssistantTextDelta):
        started.setdefault("narration", []).append(inner.text)
        _child_deltas(broker, started).add(inner.text)
        return
    # Same ordering rule as the coordinator (P1): anything that is not streamed text
    # ends the batch, so a delta can't land after the step it preceded.
    _child_deltas(broker, started).flush()
    if isinstance(inner, TurnStarted):
        # A new turn began → the previous turn's narration is complete; commit it.
        _flush_child_narration(broker, started)
    elif isinstance(inner, ToolCalled):
        # The model finished narrating and is acting → commit, then show the tool step.
        _flush_child_narration(broker, started)
        emit(broker, "status", {"status": "tool_call", "message": f"Running {inner.name}", "turn": None})
    elif isinstance(inner, CheckResult):
        emit(broker, "status", {"status": "lean_check", "check_status": inner.status,
                                "check_detail": inner.detail})


def _subagent_error(ev: SubagentFinished) -> str | None:
    """The human error message when a child FAILED TO RUN (raised before returning a
    result — an API/config error, a crash), else None. This is the failure that was
    hiding behind the coordinator's paraphrase: the child has no candidate and no
    transcript, so nothing surfaced it. NOTE: a child whose *candidate* merely has Lean
    errors is NOT this — that is a normal outcome already shown as a red 'errors' badge;
    here we mean the child could not produce a result at all (`stop_reason == 'error'`)."""
    if ev.stop_reason != "error":
        return None
    # `_error_result` (prover) sets summary = "error — <exception>"; that carries the real
    # cause (e.g. the LiteLLM auth/config error). Fall back to check_detail, then a notice.
    msg = (ev.summary or "").strip()
    if not msg:
        msg = (ev.check_detail or "").strip()
    return msg or "The sub-agent failed before returning a result."


# D3: a child's terminal reason, for the reasons that are NOT an outright failure but
# are also not a clean finish. `_subagent_error` covers 'error' (the child never ran);
# these are children that ran and stopped short — and until now they rendered as a
# child that finished fine and happened to produce nothing. "Stopped: turn budget" and
# "explored and found nothing" call for completely different next moves by the user.
_SUBAGENT_STOP_NOTICES = {
    "max_turns": (
        "hit its turn or cost budget before finishing",
        "Raise this role's max turns or max cost on the Sub-agents page, or narrow the "
        "task you delegate to it.",
    ),
    "interrupted": ("was stopped before finishing", None),
}

# The 'assistant' terminal reason means the child's last turn was prose rather than a
# verified proof. That is NOT the same as "it produced nothing" — and saying so was
# wrong in the worst way: a child reported "ended without producing a candidate"
# directly above its own message describing the candidate it had just compiled cleanly.
#
# Two corrections. First, consult the RESULT: a child that handed back a candidate gets
# no notice at all. Second, when there is no candidate, describe what we RECORDED, not
# what the child did — "no candidate was recorded" is checkable; "it produced none" is
# a claim about someone else's work that we cannot actually make, and which was false
# here (the child wrote its proof outside its scratch dir, so the envelope lost it).
_NO_CANDIDATE_NOTICE = (
    "finished without a candidate being recorded — its own notes may still describe one",
    "Open the sub-agent to read what it did. If it reports a proof that was not "
    "captured, it wrote outside its scratch directory and the result could not be "
    "collected.",
)


def _subagent_stop_notice(ev: SubagentFinished) -> tuple[str, str | None] | None:
    """`(description, remedy)` when a child stopped short, else None.

    Never contradicts the envelope: a child that returned a candidate is not described
    as having produced nothing, whatever its terminal reason."""
    produced_candidate = bool(ev.candidate_path)
    if ev.stop_reason == "assistant":
        # Ending on prose is unremarkable when it delivered something.
        return None if produced_candidate else _NO_CANDIDATE_NOTICE
    notice = _SUBAGENT_STOP_NOTICES.get(ev.stop_reason)
    if notice and not produced_candidate and ev.stop_reason == "max_turns":
        # Both facts matter: it ran out of budget AND came back empty-handed.
        return (f"{notice[0]}, and no candidate was recorded", notice[1])
    return notice


def _populate_subagent(
    child_id: str,
    child_run_id: str | None,
    ev: SubagentFinished,
    *,
    turn: int,
    repo: Path,
    task_already_recorded: bool = False,
) -> None:
    """Fill a child session with the finished run's transcript + candidate (item 24).

    The transcript replays into the child's own `timeline` (so it renders read-only
    through the ordinary session view); the candidate file — read back from the child's
    scratch dir under the run's working tree — is stored as a code_step, so the child's
    *derived* status IS its lean_check verdict. The child transcript is NOT written onto
    the parent's timeline (it is the child's, not a coordinator code_step)."""
    pending = list(ev.transcript or [])
    # The delegated task is the transcript's FIRST user message. When the spawn path
    # already stored it (so a running child was readable), drop it here — otherwise the
    # child's thread opens with the same prompt twice.
    if task_already_recorded and pending and pending[0].get("role") == "user":
        pending = pending[1:]
    for msg in pending:
        text = _text_from_content(msg.get("content"))
        if text.strip():
            role = "user" if msg.get("role") == "user" else "assistant"
            store.add_message(child_id, role, text)
    # A child that FAILED TO RUN has no transcript — persist the error as its message so
    # the failure is visible in the child's own session (and its final_summary), not lost.
    err = _subagent_error(ev)
    if err:
        store.add_message(child_id, "assistant", err)
    if ev.candidate_path:
        cand = Path(ev.candidate_path)
        if not cand.is_absolute():
            cand = repo / cand
        try:
            content = cand.read_text()
        except OSError:
            content = None
        if content is not None:
            store.add_code_step(
                child_id, child_run_id, cand.name, content=content, author="agent", turn=turn,
                summary=(ev.summary or None),
                check_status=ev.check_status, check_detail=ev.check_detail,
                artifact_kind=_classify(content) if ev.check_status == "ok" else None,
            )


def _finalize_started_subagent(
    child_id: str,
    child_run_id: str,
    ev: SubagentFinished,
    *,
    turn: int,
    repo: Path,
) -> None:
    """D1 finish path: fill in a child created by `_start_subagent` and RETIRE its run
    row so its derived status flips from 'running' to the candidate's verdict."""
    # This path always went through `_start_subagent`, which recorded the delegated task.
    _populate_subagent(child_id, child_run_id, ev, turn=turn, repo=repo,
                       task_already_recorded=True)
    store.update_run(child_run_id, "error" if ev.stop_reason == "error" else "completed")


def _materialize_subagent(
    ev: SubagentFinished,
    *,
    parent_session_id: str,
    project_id: str | None,
    turn: int,
    repo: Path,
) -> dict:
    """Fallback for a `SubagentFinished` with no prior `SubagentStarted` (an older prover,
    or a dropped start event): create the child from the finished run in one shot, as
    before D1. No run row — it is already finished, so its status derives from the
    candidate's verdict directly."""
    child = store.create_session(
        _subagent_child_title(ev),
        project_id=project_id,
        parent_id=parent_session_id,
        role=ev.subagent_type,
        spawned_at_turn=turn,
    )
    _populate_subagent(child["id"], None, ev, turn=turn, repo=repo)
    return child


def _safeverify_file(path: str) -> str | None:
    """SafeVerify's verdict for a file: 'ok' | 'rejected' | 'error' | 'unavailable',
    or None if the audit could not be run at all.

    A crash here is not a rejection — it must not block a promotion, or an unbuilt
    SafeVerify would silently disable collation entirely."""
    try:
        return _safe_verify_file(path).status
    except Exception:
        logger.exception("SafeVerify could not audit %s; treating it as not run", path)
        return None


# Sentinel for "there was no file here", so it is distinguishable from "there was a
# file and it was empty" — restoring the two differently is the whole point.
_ABSENT = object()


def _snapshot_file(path: Path):
    """The file's current bytes, `_ABSENT` if it doesn't exist, or None if it exists but
    can't be read. None means "cannot restore", and callers must not delete on it."""
    if not path.exists():
        return _ABSENT
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        logger.warning("could not snapshot %s before overwriting it", path, exc_info=True)
        return None


def _restore_file(path: Path, previous) -> None:
    """Undo an overwrite guarded by :func:`_snapshot_file`. A snapshot that failed
    (None) leaves the file alone: we have nothing to put back, and deleting would turn
    a bad overwrite into data loss."""
    if previous is _ABSENT:
        path.unlink(missing_ok=True)
        return
    if previous is None:
        return
    try:
        path.write_text(previous)
    except OSError:
        logger.exception("could not restore %s after a failed promotion", path)


def _promote_winner(
    subagent_results: list[SubagentFinished],
    *,
    session_id: str,
    run_id: str,
    repo: Path,
    namespace: str | None,
    turn: int,
    events,
    formalization_id: str | None = None,
) -> dict | None:
    """Deterministic collation (item 25): promote the best *compiling* sub-agent candidate
    as the coordinator's proof, and record it as a code_step — the compiler decides, not
    the model. Returns the promoted step, or None if nothing was promoted.

    The ranking is `collation`'s (lean_check clean > SafeVerify-rejected > error, ties by
    sorry-free / shorter). Only a clean candidate is promotable. Two safety rails make this
    safe to run automatically:

      * the caller only invokes this when the coordinator produced NO clean proof itself
        (so a promotion fills a gap, never clobbers the coordinator's own answer);
      * the winner is **re-verified at the canonical path** before it's recorded — the
        child's lean_check ran in its scratch dir, so we don't trust that verdict at a new
        location; if it doesn't re-verify clean, nothing is promoted (the coordinator's
        result stands) rather than recording an unchecked "ok".
    """
    if not subagent_results:
        return None
    candidates = [collation.candidate_from_event(ev, base_dir=repo) for ev in subagent_results]
    # Best-first, and try the next one if the best fails a gate (AUDIT-2026-07-24 C5).
    # `rank` is total, so once a non-promotable candidate appears everything after it
    # is worse; there is nothing left to try.
    for winner in collation.rank(candidates):
        if not winner.is_promotable or not winner.candidate_path:
            return None
        step = _try_promote(
            winner, session_id=session_id, run_id=run_id, repo=repo,
            namespace=namespace, formalization_id=formalization_id,
            turn=turn, events=events,
        )
        if step is not None:
            return step
    return None


def _try_promote(
    winner, *, session_id, run_id, repo, namespace, formalization_id, turn, events
) -> dict | None:
    """Promote one candidate, or return None having left the tree as it was found."""
    # The session's canonical proofs dir: its namespace path (loose → Lea/Misc).
    ns_path = (namespace or "Lea.Misc").replace(".", "/")
    canonical = repo / ns_path / Path(winner.candidate_path).name
    # Whatever stands at the canonical path right now — very possibly a VERIFIED proof
    # from an earlier run. `promote` overwrites it, and the re-verification that decides
    # whether the candidate is worth keeping happens afterwards, so a candidate that
    # failed used to destroy the good proof it replaced and leave itself in its place
    # (AUDIT-2026-07-24 C3). Nothing recorded the change, either: the code_step is only
    # written on success, so the file on disk — which is what Lean compiles, what
    # /export zips, and what `git push` ships — silently diverged from the timeline.
    previous = _snapshot_file(canonical)
    try:
        collation.promote(winner, canonical)
    except ValueError as exc:
        # D2: this was a bare `return None` with no log at all, so a promotion that
        # could not even be attempted was indistinguishable from "no child produced
        # anything" — two very different facts for the user's next move.
        diagnose(events, session_id, run_id, "step_error", "subagent.promotion_failed",
                 f"The winning sub-agent candidate ({winner.result_id}) could not be "
                 f"written to {canonical.name}: {exc}",
                 turn=turn, child_result_id=winner.result_id)
        return None
    # Re-verify at the NEW path — the child checked a different location. Deliberately
    # AT the canonical path rather than at a temp one: Lake resolves modules by
    # location, so the path is part of what is being verified, and checking elsewhere
    # would verify something other than what we are about to keep. That is why the fix
    # for C3 is restore-on-failure rather than verify-then-move.
    verdict = _lean_check_file(str(canonical))
    if verdict.status != "ok":
        # D1: the re-verification is CORRECT and stays — recording an unchecked "ok"
        # would be worse. But it was a `logger.warning`, so the human watched N
        # children run for minutes and was shown: no proof, no promotion, no reason.
        # Restore first (C3), then report: the file on disk and the explanation of
        # what happened to it are two halves of the same recovery.
        _restore_file(canonical, previous)
        logger.warning(
            "sub-agent candidate %s did not re-verify at %s (%s); not promoting "
            "(restored the previous file)",
            winner.result_id, canonical, verdict.detail,
        )
        diagnose(events, session_id, run_id, "step_error", "subagent.promotion_rejected",
                 f"The best sub-agent candidate ({winner.result_id}) compiled in its own "
                 f"scratch directory but not at {canonical.name}: "
                 f"{verdict.detail or 'lean_check reported errors'}",
                 turn=turn, path=_relativize(str(canonical), repo),
                 child_result_id=winner.result_id)
        return None
    # SafeVerify gate (AUDIT-2026-07-24 C5). `collation` documents that a
    # SafeVerify-REJECTED candidate must never become the proof of record — "promoting
    # a cheat to the canonical file is exactly the failure SafeVerify exists to catch"
    # — but its `_TIER_SV_REJECTED` was unreachable, because nothing ever populated
    # `safeverify_status`. So the guarantee was documented and not enforced.
    #
    # Enforced here rather than by SafeVerifying every candidate before ranking: the
    # audit is a kernel replay, the ranking already put the compiler's verdict first,
    # and only the winner can become the proof of record — so one audit, at the moment
    # it decides something, instead of N that mostly inform an ordering.
    #
    # ONLY 'rejected' blocks. 'unavailable' (the binary isn't built) and 'error' leave
    # the candidate where lean_check put it, matching collation's stated degradation:
    # missing SafeVerify must never *mis*-rank, only fail to catch a cheat.
    audit = _safeverify_file(str(canonical))
    if audit == "rejected":
        _restore_file(canonical, previous)
        logger.warning(
            "sub-agent candidate %s compiles but SafeVerify rejected it at %s; not "
            "promoting (restored the previous file)",
            winner.result_id, canonical,
        )
        return None
    rel = _relativize(str(canonical), repo)
    step = store.add_code_step(
        session_id, run_id, rel, content=winner.text or "", author="agent", turn=turn,
        summary=f"Promoted the winning sub-agent candidate ({winner.result_id}).",
        check_status="ok", check_detail=None,
        artifact_kind=_classify(winner.text or ""),
        formalization_id=formalization_id,
        provenance={"promoted_from": winner.result_id},
    )
    emit(events, "code_step", step)
    emit(events, "status", {
        "status": "promoted",
        "message": f"Promoted the winning sub-agent candidate ({winner.result_id}).",
        "turn": turn,
    })
    return step


def _best_effort(what: str, run_id: str, action) -> None:
    """Run one piece of post-outcome bookkeeping, logging rather than raising.

    Used only for work that happens AFTER the run's result is durable, where failing
    loudly would be strictly worse than failing quietly: the result is already correct,
    and the alternative is discarding it (C6)."""
    try:
        action()
    except Exception:
        logger.exception("Could not persist the %s for run %s", what, run_id)


def _run_is_terminal(run_id: str) -> bool:
    """Whether the run row already holds a final status. A read failure answers False,
    so an unreachable database falls back to the old mark-it-failed behaviour rather
    than silently leaving a run looking live."""
    try:
        run = store.get_run(run_id)
    except Exception:
        logger.exception("Could not read run %s while handling a failure", run_id)
        return False
    return bool(run) and run["status"] not in {"pending", "running"}


def run_lea(context: RunnerContext) -> None:
    """Run one Lea activation, streaming normalized SSE events onto the queue.

    Always terminates the stream with a `done` event (the SSE endpoint breaks on
    it), even on failure — so the browser's EventSource never hangs.
    """
    events = context.events
    cfg = context.config
    # F2: set when the Sub-agents page's overrides could not be read, so the run can
    # report it once it has begun (reporting needs the run row to exist).
    subagent_override_error: str | None = None
    if context.autonomous:
        # Autonomous (D19): swap the interactive collaborator prompt for the
        # `default` autoformalizer so the run never pauses to present a plan and
        # wait for confirmation. LeaConfig is frozen, so build a copy. The gate is
        # disabled separately below.
        cfg = replace(cfg, prompt_variant="default")
    else:
        # Interactive coordinator (item 24): make `spawn_subagent` available so the
        # model can delegate parallel exploration to child sub-agents. The tool is
        # opt-in in the prover registry (off by default, so it never leaks into an
        # unfiltered toolset); the coordinator gets its normal default toolset PLUS
        # spawn_subagent, named explicitly. The model decides when to spawn; children
        # can't recurse (prover depth + toolset guards). Autonomous/Overleaf runs stay
        # single-agent for now.
        cfg, subagent_override_error = _with_subagents(cfg)
    session_id = context.session_id
    run_id = context.run_id

    # Admission (which run may start, and whether there's room) already happened in
    # the dispatcher before this thread was spawned. run_lea no longer acquires a
    # slot — it owns the one it was admitted into and releases it in
    # the finally. Crucially the whole body, INCLUDING the setup that used to sit
    # outside the try, now runs inside one guarded region: a throw in setup releases
    # the slot instead of leaking it forever (v2.3 items 4/9).

    # Register (or adopt) this run's cooperative stop flag — the interrupt endpoint
    # may have created+set it already if Stop was hit before we got here (D18).
    stop_event = _stop_events.setdefault(run_id, Event())

    # Per-run temp dir holding materialized skill .md files (W3/D48); None until
    # resolved inside the try. Declared before it so the `finally` can always clean up.
    skills_tempdir: str | None = None
    roles_tempdir: str | None = None

    narration: list[str] = []
    # Batches streamed text into ~one frame per subscriber poll (P1). `narration` still
    # accumulates every token for persistence — this only shapes what goes on the wire.
    deltas = _DeltaStream(events)
    current_turn = 0
    last_tool: str | None = None
    # The intent narration the model wrote just before its current tool call —
    # stamped onto the code step a write produces, so the UI can show "what this
    # write is trying to do" on the step card (M11).
    last_intent: str | None = None
    step_id_by_path: dict[str, str] = {}
    # The path of the most recent write_file/edit_file, captured at ToolCalled so the
    # ToolResulted handler can tell a project asset write (.lea/*.md) from a .lean
    # proof write (D33): the latter is a canvas snapshot via FileChanged; the former
    # is committed quietly and refreshes the project graph, never the canvas.
    last_write_path: str | None = None
    checked_artifact_kind = "unknown"
    usage = _UsageByTurn()
    # Finished sub-agents (item 24), in the order they completed — surfaced as child
    # sessions and kept for the collation pass (item 25) on the coordinator's Finished.
    subagent_results: list[SubagentFinished] = []
    # D1: children materialized at spawn (SubagentStarted), keyed by result_id, so the
    # matching SubagentFinished updates the SAME running child row instead of creating a
    # second. Value: the child session id, its run row id, and its start title.
    subagent_children: dict[str, dict] = {}
    # Whether the coordinator ITSELF produced a clean (non-scratch) proof this run. If it
    # did, the collation pass leaves it alone; if it didn't, the best compiling child
    # candidate is promoted to fill the gap (item 25).
    produced_clean = False
    last_persisted: str | None = None
    # Declared before the try so the crash handler can reference it even when setup
    # threw before the session's repo was resolved (a NameError inside the `except`
    # would replace the real failure with a spurious one).
    repo: Path | None = None
    # Declared before the try so the finally's `done` event always has them, even if
    # setup throws before the run reaches its Finished handler.
    final_status = "failed"
    final_result_kind: str | None = None
    final_result_detail: str | None = None
    focus_formalization_id: str | None = None
    focus_source_hash: str | None = None

    # Mid-run spend enforcement (PLAN-system-hardening 0.1): the cap used to be
    # checked only at POST /api/runs, so one run could overshoot it by its entire
    # cost. The active-cost overlay above keeps concurrent, not-yet-persisted run
    # spend visible. The agent halts at the next turn boundary, so one turn may
    # overshoot but a run can no longer run away.
    max_spend_usd = cfg.max_spend_usd
    spend_capped = False
    with _live_spend_lock:
        _live_run_costs[run_id] = 0.0

    def check_spend_cap() -> None:
        nonlocal spend_capped
        if spend_capped or max_spend_usd is None:
            return
        try:
            # A scalar aggregate over every run — NOT usage_stats()["global"], which
            # summed a 100-session page and so under-reported the very total this cap
            # is enforced against (AUDIT-2026-07-24 C1) — and memoized, because this
            # runs per usage event (P2).
            persisted = _persisted_spend_usd()
        except Exception:
            logger.exception("Could not read persisted spend; skipping this cap check")
            return
        with _live_spend_lock:
            live = sum(_live_run_costs.values())
        if persisted + live >= float(max_spend_usd):
            spend_capped = True
            stop_event.set()
            emit(events, "status", {
                "status": "max_spend",
                "message": "Max spend limit reached — stopping this run.",
                "turn": current_turn,
            })

    def persist_assistant(text: str) -> None:
        nonlocal last_persisted
        text = text.strip()
        if not text or text == last_persisted:
            return
        last_persisted = text
        emit(
            events,
            "message",
            store.add_message(
                session_id,
                "assistant",
                text,
                run_id,
                kind="assistant",
                formalization_id=focus_formalization_id,
            ),
        )

    def flush_narration() -> str:
        text = "".join(narration)
        narration.clear()
        persist_assistant(text)
        return text.strip()

    try:
        lea_root = cfg.lea_root or (Path(__file__).resolve().parents[2] / "prover")
        proofs_root = Path(lea_root) / "workspace" / "proofs"
        # Resolve the session's repo (D24): a project session writes the shared
        # proofs/Lea/<Project> repo; a loose session its own proofs/<session-id>. Root
        # the GitStore at the repo's parent and key by its dir name, so every
        # session-keyed primitive below operates on the right repo unchanged. The real
        # session_id still keys all DB rows.
        session = store.get_session(session_id)
        run_row = store.get_run(run_id)
        focus_formalization_id = (
            run_row.get("focus_formalization_id") if run_row else None
        )
        focus_source_hash = run_row.get("focus_source_hash") if run_row else None
        focused_formalization = (
            formalization_service.get(focus_formalization_id)
            if focus_formalization_id else None
        )
        project = (
            store.get_project(session["project_id"])
            if session and session.get("project_id") else None
        )
        repo = projects.repo_for_session(session or {"id": session_id}, proofs_root, project)
        gs = GitStore(repo.parent)
        repo_key = repo.name
        gs.init_repo(repo)  # idempotent; a project repo already exists from provisioning
        # Guard the Overleaf .tex mirror so the agent compiling the document mid-run can't
        # get its build artifacts (.pdf/.synctex.gz/.aux/...) swept in by commit-on-write.
        if project:
            uploads.ensure_overleaf_gitignore(project, proofs_root)
        # Project namespace for the prompt (D32): None → the default Lea.Misc block.
        namespace = project["namespace"] if project else None
        # Skill resolution (W3/D48): a project run picks up the skills that resolve for
        # it (global ∪ assigned, D47), materialized to per-run temp .md files fed to the
        # prover via cfg.skills. Loose sessions resolve to none (project is None), so
        # cfg.skills stays empty — no behavior change on the loose path.
        # E0e: resolution is now (global ∪ project-assigned) ± this SESSION's own diff, so
        # a loose session can use skills too — before, `project is None` meant no skills at
        # all and there was no way to opt one in.
        skill_paths, skills_tempdir = skills_catalog.materialize_run_skills(
            project["id"] if project else None, session_id, context.task
        )
        if skill_paths:
            # H7: the materialized tree is also a READ root, so a multi-file skill's
            # references are openable. Without it the agent is told they exist and then
            # refused when it tries to read one — worse than not advertising them.
            cfg = replace(cfg, skills=skill_paths, skills_root=skills_tempdir)
        for slug, count in skills_catalog.drain_skipped():
            # G5: the skill still loads; some of its reference material did not. Saying
            # so beats a `read_file` failing mid-proof for no visible reason.
            diagnose(
                events, session_id, run_id, "degraded", "skill.files_incomplete",
                f"{count} reference file(s) from the “{slug}” skill could not be prepared, "
                f"so Lea cannot open them in this run.",
                source="skill", skill=slug,
            )
        # MCP resolution (v2.5 A1) — the wire that was missing: the prover has had a
        # complete MCP implementation all along (`lea/mcp.py`, started by `agent.py`
        # whenever `cfg.mcp_servers` is non-empty), but nothing ever SET the field, so
        # servers were reachable only from the CLI with a hand-written YAML file.
        #
        # Resolution mirrors skills (global ∪ assigned, D47) with one deliberate
        # difference: a loose session still gets the GLOBAL servers, because a
        # machine-level tool the user switched on should work in a scratch session too.
        # Secrets are absent by construction — a spec carries `env_from` NAMES, and
        # `lea.mcp._child_env` reads their values at spawn (A7).
        mcp_specs = store.mcp_server_specs(project["id"] if project else None, session_id)
        if mcp_specs:
            cfg = replace(cfg, mcp_servers=mcp_specs)
        # B2: user-authored sub-agent roles, materialized to YAML the prover discovers via
        # `agent_dirs`. Without this the coordinator is only ever offered the two vendored
        # roles, so a role the user created would silently never run.
        # F2: declared HTTP tools resolve exactly like MCP servers.
        http_tools = store.custom_tool_specs(project["id"] if project else None)
        if http_tools:
            cfg = replace(cfg, http_tools=http_tools)
        roles_tempdir, skipped_roles = roles_catalog.materialize_roles()
        if roles_tempdir:
            cfg = replace(cfg, agent_dirs=[roles_tempdir])
        if skipped_roles:
            # G3: a role that could not be written is a role the coordinator is never
            # offered — an absence, with nothing raised anywhere. Discarding this signal
            # would leave the user with a role that exists in the Library and simply
            # never runs, which is the exact failure this phase is built to prevent.
            diagnose(
                events, session_id, run_id, "degraded", "subagent.role_unavailable",
                f"{len(skipped_roles)} sub-agent role(s) could not be prepared, so they "
                f"were not offered to the agent: {', '.join(sorted(skipped_roles))}.",
                source="subagent", roles=sorted(skipped_roles),
            )
        # Claim the row (C7). If the interrupt endpoint got here first the row is no
        # longer pending, and starting anyway would execute — and bill — a run the
        # client was already told was cancelled. This replaces the plain
        # `update_run(running)`: the claim IS the transition.
        if not store.claim_pending_run(run_id):
            current = store.get_run(run_id)
            final_status = (current or {}).get("status") or "cancelled"
            final_result_kind = (current or {}).get("result_kind")
            final_result_detail = (current or {}).get("result_detail")
            logger.info("Run %s was finalized before it started (%s); not running it",
                        run_id, final_status)
            return
        if subagent_override_error:
            # F2: reported AFTER the claim — a diagnostic needs a live run row to hang
            # off, and a run cancelled before it started has nothing to report against.
            diagnose(events, session_id, run_id, "degraded", "settings.overrides_unreadable",
                     f"Your sub-agent overrides could not be read ({subagent_override_error}); "
                     "every sub-agent in this run used its built-in defaults.")
        # Multi-turn (D16): replay the session's prior conversation so a follow-up
        # continues with full context — the prover is stateless, so the adapter
        # feeds it the faithful transcript (tool_call/tool_result parts intact) of
        # the last Finished run, then the new user turn. A cold first run gets [].
        prior = store.latest_transcript_for_session(session_id, exclude_run_id=run_id) or []
        # Diff-on-divergence (D12): if the human edited the proof outside a run since
        # the agent last acted, prepend their diff (+ notes) to the task so the agent
        # works from the current canvas, not its stale memory.
        task_content = context.task
        divergence = _divergence_context(
            session_id, repo_key, gs, focus_formalization_id
        )
        if divergence:
            task_content = f"{divergence}\n\n{task_content}"
        # Transcript gap (C10): `prior` is whatever run last stored a transcript, which
        # may not be the run that actually ran last — a crash mid-turn stores none and
        # vanishes from the replay. Say so rather than presenting a history with a
        # silent hole in it. Prepended last so it is the first thing the model reads.
        gap = _transcript_gap_context(session_id, run_id)
        if gap:
            task_content = f"{gap}\n\n{task_content}"
        # Project context (D25): prepend ONE composed message (instructions + memory +
        # blueprint + file inventory). Strip any stale copy from the replayed
        # transcript first, so exactly one — always current — leads the messages.
        # Loose runs: ctx is None and this is a no-op.
        ctx = projects.compose_context_message(project, repo) if project else None
        if ctx:
            prior = [m for m in prior if not projects.is_context_message(m)]
        focus_ctx = _formalization_context_message(focused_formalization)
        prior = [m for m in prior if not _is_formalization_context_message(m)]
        messages = (
            ([ctx] if ctx else [])
            + ([focus_ctx] if focus_ctx else [])
            + prior
            + [{"role": "user", "content": task_content}]
        )

        # Drive the generator manually (not `for`): the per-tool gate (D19) is a
        # two-way exchange — the prover yields ToolApprovalRequested and we feed the
        # human's decision back via gen.send(). A plain for-loop can't send.
        # Autonomous (D19): gate=None → no tool ever pauses for human approval, so
        # the run is fully unattended. Interactive UI runs keep the per-tool gate.
        gen = run_events(cfg, messages, namespace=namespace, session_id=session_id,
                         working_dir=str(repo), should_stop=stop_event.is_set,
                         gate=(None if context.autonomous else _make_gate(session_id)))
        to_send = None
        while True:
            try:
                ev = gen.send(to_send)
            except StopIteration:
                break
            to_send = None

            # Any event that is not streamed text ends the current text batch, so a
            # buffered delta can never be published after the event it preceded (P1).
            # One check here rather than a flush in every branch below: the ordering
            # rule then cannot be forgotten when a new event type is added.
            if not isinstance(ev, AssistantTextDelta):
                deltas.flush()

            if isinstance(ev, ToolApprovalRequested):
                to_send = _await_decision(run_id, session_id, ev, events, stop_event)
                continue

            if isinstance(ev, AssistantTextDelta):
                narration.append(ev.text)
                deltas.add(ev.text)

            elif isinstance(ev, TurnStarted):
                flush_narration()
                current_turn = ev.turn
                check_spend_cap()

            elif isinstance(ev, ToolCalled):
                intent = flush_narration()
                # Only a *short* narration is a per-write "what I'm doing" label
                # (M11). A long one — e.g. the natural-language proof sketch the
                # collaborator leads with — must stay as prose in the thread and
                # never be folded into a step card, so we don't stamp it.
                if intent:
                    last_intent = intent if len(intent) <= 280 else None
                last_tool = ev.name
                last_write_path = ev.args.get("path") if ev.name in ("write_file", "edit_file") else None
                emit(events, "status", {"status": "tool_call", "message": f"Running {ev.name}", "turn": current_turn})

            elif isinstance(ev, FileChanged):
                rel = _relativize(ev.path, repo)
                formalization_id = _attributable_formalization(
                    rel, focus_formalization_id
                )
                # The file on disk *is* the after-state — the prover has already
                # written it. Reading it here is what makes the stored content and
                # the streamed snapshot the same bytes by construction, rather than
                # two derivations of it that can disagree (the old path committed to
                # git, then asked git what it had committed).
                after = _read_after(ev.path)
                step = store.add_code_step(
                    session_id, run_id, rel, content=(after or ""),
                    author="agent", turn=current_turn, summary=last_intent,
                    content_lost=after is None,
                    formalization_id=formalization_id,
                )
                if formalization_id:
                    store.link_formalization_file(
                        formalization_id, rel, "generated"
                    )
                step_id_by_path[rel] = step["id"]
                emit(events, "code_step", step)  # already carries `code`
                if after is None:
                    # C2: the write happened but we could not capture what was
                    # written. Say so — the canvas would otherwise show an empty file
                    # with no indication that it is missing rather than empty.
                    diagnose(events, session_id, run_id, "step_error", "code.content_lost",
                             f"Lea wrote {rel}, but the file could not be read back to store it.",
                             turn=current_turn, path=rel, step_id=step["id"])

            elif isinstance(ev, CheckResult):
                rel = _relativize(ev.path, repo)
                formalization_id = _attributable_formalization(
                    rel, focus_formalization_id
                )
                step_id = step_id_by_path.get(rel)
                if step_id is None:
                    # A file this run never wrote through write_file/edit_file — a
                    # bash-written file, or one checked before its first write. It
                    # has no step from this run to back-fill, so the verdict had
                    # nowhere to go and was dropped. Record the check against the
                    # file's current content instead: a verdict with no step is a
                    # result the user never sees.
                    after = _read_after(ev.path)
                    step = store.add_code_step(
                        session_id, run_id, rel, content=(after or ""),
                        author="agent", turn=current_turn, summary=last_intent,
                        check_status=ev.status, check_detail=ev.detail,
                        artifact_kind=(_classify(after) if (ev.status == "ok" and after is not None) else None),
                        content_lost=after is None,
                        formalization_id=formalization_id,
                    )
                    if formalization_id:
                        store.link_formalization_file(
                            formalization_id, rel, "generated"
                        )
                    step_id_by_path[rel] = step["id"]
                    if after is None:
                        diagnose(events, session_id, run_id, "step_error", "code.content_lost",
                                 f"lean_check ran on {rel}, but the file could not be read back to store it.",
                                 turn=current_turn, path=rel, step_id=step["id"])
                    if ev.status == "ok":
                        checked_artifact_kind = step.get("artifact_kind")
                        if "scratch" not in rel.lower():
                            produced_clean = True
                    emit(events, "code_step", step)
                else:
                    artifact_kind = None
                    if ev.status == "ok":
                        current_step = store.latest_code_step_for_path(session_id, rel)
                        if current_step and current_step["id"] == step_id:
                            artifact_kind = _classify(current_step["code"])
                    updated = store.set_code_step_check(step_id, ev.status, ev.detail, artifact_kind=artifact_kind)
                    if updated:
                        if ev.status == "ok":
                            checked_artifact_kind = updated.get("artifact_kind") or _classify(updated["code"])
                            if "scratch" not in rel.lower():
                                produced_clean = True
                        emit(events, "code_step", updated)
                emit(events, "status", {
                    "status": "lean_check", "message": f"lean_check: {ev.status}",
                    "turn": current_turn, "check_status": ev.status, "check_detail": ev.detail,
                })

            elif isinstance(ev, UsageUpdated):
                usage.add(current_turn, ev.input_tokens, ev.output_tokens, ev.cost)
                with _live_spend_lock:
                    _live_run_costs[run_id] = (
                        _live_run_costs.get(run_id, 0.0) + float(ev.cost or 0.0)
                    )
                check_spend_cap()

            elif isinstance(ev, Compacted):
                # G1: the context condenser ran this turn — the coordinator's model-facing
                # history was pruned (and maybe summarized) to stay bounded on a long run.
                # Persist it as a durable `compaction` timeline message (same channel the
                # manual /compact marker rides) so the thread shows a "context compacted"
                # marker live AND on reload — live and reload can't disagree. It's a marker,
                # not proof content: the code_step record is untouched.
                flush_narration()
                _payload = json.dumps({
                    "manual": False,
                    "changed": True,
                    "pruned": ev.pruned,
                    "summarized": bool(ev.summarized),
                    "before_tokens": ev.before_tokens,
                    "after_tokens": ev.after_tokens,
                    "freed_tokens": max(0, ev.before_tokens - ev.after_tokens),
                    "referenced_files": [],
                })
                emit(events, "message",
                     store.add_message(
                         session_id, "assistant", _payload, run_id,
                         kind="compaction",
                         formalization_id=focus_formalization_id,
                     ))

            elif isinstance(ev, ToolResulted):
                # A project asset write (D33): a non-.lean write_file/edit_file in a
                # project (e.g. .lea/blueprint.md). The prover emits FileChanged only
                # for .lean (A2), so this never became a canvas snapshot. Commit it
                # quietly (git add -A covers any file) and emit a light graph-refresh
                # signal — no code_step, no canvas pollution. .lean writes are handled
                # by FileChanged above, so they're excluded here.
                if (
                    project
                    and ev.name in ("write_file", "edit_file")
                    and last_write_path
                    and not str(last_write_path).endswith(".lean")
                ):
                    asset_rel = _relativize(last_write_path, repo)
                    # Scoped to the asset this tool call wrote (X2): a concurrent run
                    # in the same project repo must not land in this commit.
                    sha = gs.commit_all(repo, f"agent {ev.name}: {asset_rel}", paths=[asset_rel])
                    emit(events, "project_updated", {
                        "project_id": project["id"], "path": asset_rel, "commit_sha": sha,
                    })
                last_write_path = None

            elif isinstance(ev, SubagentStarted):
                # D1: a child was just spawned and is about to run (this blocks the
                # coordinator's tool call for the child's whole life). Materialize it as
                # a RUNNING child session NOW — a running run row makes its derived status
                # 'running', so the sidebar's Sub-agents block and the parent's spawn node
                # show a live 'exploring…' child instead of nothing until it finishes. The
                # matching SubagentFinished (same result_id) fills it in and retires the run.
                child, child_run_id = _start_subagent(
                    ev,
                    parent_session_id=session_id,
                    project_id=(project["id"] if project else None),
                    turn=current_turn,
                    cfg=cfg,
                )
                subagent_children[ev.result_id] = {
                    "child_id": child["id"], "run_id": child_run_id, "title": child["title"],
                    # D4: every spawn in a turn is announced here, but the prover then
                    # queues them behind a semaphore (DEFAULT_MAX_CONCURRENT_CHILDREN,
                    # 5) — so children 6+ were displayed as "running" while actually
                    # waiting for a slot. A child that has emitted no event of its own
                    # has provably not started; its first SubagentProgress flips this.
                    # Deriving it from real events rather than mirroring the prover's
                    # cap means the two can't drift if that cap changes.
                    "started": False,
                }
                # E1 first-class child run: give the child run its OWN broker, keyed by its
                # run_id. The child's session view attaches to /api/runs/<child_run_id>/events
                # like any run; the events endpoint recognises a child and just TAILS this
                # broker (never admits/drives it — the coordinator drives it inline). So a
                # sub-agent's own session streams live, with no bespoke endpoint.
                runbroker.create(child_run_id)
                # D2: let the stop endpoint address this child by its session id.
                _child_session_to_result[child["id"]] = ev.result_id
                emit(events, "subagent_started", {
                    "child_id": child["id"],
                    "parent_id": session_id,
                    "run_id": run_id,
                    "result_id": ev.result_id,
                    "subagent_type": ev.subagent_type,
                    "role": ev.subagent_type,
                    "turn": current_turn,
                    "title": child["title"],
                    # D4: spawned, not yet executing. Flipped by `subagent_running`.
                    "state": "queued",
                    # What the coordinator actually asked for, live — the spawn box can
                    # show it immediately instead of a three-word title.
                    "task": ev.task or "",
                })

            elif isinstance(ev, SubagentProgress):
                # E1: a running child emitted one of its own events. Stream it to the
                # browser live (VISIBILITY only — not persisted; the authoritative
                # transcript still replays on finish) so the coordinator's spawn box shows
                # the child working in real time instead of a frozen 'exploring…'. Ignore
                # progress for a child we never saw start (defensive).
                started = subagent_children.get(ev.result_id)
                if started:
                    # D4: first event from this child ⇒ it cleared the concurrency
                    # semaphore and is genuinely running now.
                    if not started.get("started"):
                        started["started"] = True
                        emit(events, "subagent_running", {
                            "child_id": started["child_id"],
                            "result_id": ev.result_id,
                            "run_id": run_id,
                        })
                    # A child's own diagnostic belongs to the CHILD's session — that is
                    # where its transcript lives and where someone debugging it will
                    # look. Persisted there, and mirrored onto the coordinator's stream
                    # so a failure inside a child isn't only discoverable by opening it.
                    if isinstance(ev.event, Diagnostic):
                        _c = dict(ev.event.context or {})
                        _c.pop("turn", None)
                        _c["child_id"] = started["child_id"]
                        _c["child_result_id"] = ev.result_id
                        _row = diagnose(
                            events, started["child_id"], started["run_id"],
                            ev.event.severity, ev.event.code, ev.event.message,
                            source=ev.event.source, remedy=ev.event.remedy, **_c,
                        )
                        child_broker = runbroker.get(started["run_id"])
                        if child_broker is not None:
                            emit(child_broker, "diagnostic", _row)
                        continue
                    # (a) the coordinator's spawn box (a compact live line per child)
                    payload = _subagent_progress_payload(started["child_id"], ev.result_id, ev.event)
                    if payload:
                        emit(events, "subagent_progress", payload)
                    # (b) the child's OWN run stream (E1 first-class): re-emit the inner
                    # event in the normal SSE vocab onto the child's broker, so the child's
                    # session view renders it live with the same listeners as any run.
                    child_broker = runbroker.get(started["run_id"])
                    if child_broker is not None:
                        _forward_to_child_broker(child_broker, ev.event, started)

            elif isinstance(ev, SubagentFinished):
                # A child sub-agent finished (item 24). If it was materialized at spawn
                # (D1), FILL IN the same running child row and retire its run; otherwise
                # (older prover / dropped start event) create it now from the finished
                # result. Either way its transcript lands in its own read-only timeline and
                # its candidate becomes a code_step, so the child's derived status IS its
                # lean_check verdict. Keep the typed result for the collation pass on Finished.
                subagent_results.append(ev)
                started = subagent_children.pop(ev.result_id, None)
                if started:
                    _child_session_to_result.pop(started["child_id"], None)  # D2: child done
                    _finalize_started_subagent(
                        started["child_id"], started["run_id"], ev,
                        turn=current_turn, repo=repo,
                    )
                    # E1 first-class: close the child's own run stream — flush any trailing
                    # narration, then a `done` so any attached child-session view settles +
                    # reloads the durable transcript, then drop the broker. Idempotent if
                    # nothing ever attached.
                    child_broker = runbroker.get(started["run_id"])
                    if child_broker is not None:
                        _flush_child_narration(child_broker, started)
                        emit(child_broker, "done",
                             {"status": "error" if ev.check_status == "error" else "proved"})
                    runbroker.drop(started["run_id"])
                    child_id, child_title = started["child_id"], started["title"]
                else:
                    child = _materialize_subagent(
                        ev,
                        parent_session_id=session_id,
                        project_id=(project["id"] if project else None),
                        turn=current_turn,
                        repo=repo,
                    )
                    child_id, child_title = child["id"], child["title"]
                # D3: a child that ran but stopped short (turn/cost budget, stopped by
                # the user, ended without a candidate). Not an error — but not the
                # clean finish the UI used to render it as either.
                _stop = _subagent_stop_notice(ev)
                if _stop:
                    _desc, _remedy = _stop
                    diagnose(events, session_id, run_id, "notice", "subagent.stopped_early",
                             f"Sub-agent '{ev.subagent_type}' {_desc}.",
                             turn=current_turn, remedy=_remedy,
                             child_id=child_id, child_result_id=ev.result_id)
                emit(events, "subagent_finished", {
                    "child_id": child_id,
                    "parent_id": session_id,
                    "run_id": run_id,
                    "result_id": ev.result_id,
                    "subagent_type": ev.subagent_type,
                    "role": ev.subagent_type,
                    "turn": current_turn,
                    "title": child_title,
                    "check_status": ev.check_status,
                    "check_detail": ev.check_detail,
                    "stop_reason": ev.stop_reason,
                    # D3: the human reading of `stop_reason` for a child that stopped
                    # short, so the child row can say so without the frontend
                    # re-deriving it. None for a clean completion or an outright error.
                    "stop_notice": (_stop[0] if _stop else None),
                    "summary": ev.summary,
                    "candidate_path": ev.candidate_path,
                    # The failure to surface (E1): non-null when the child could not run at
                    # all — the UI shows it as a red "failed" child instead of hiding it.
                    "error": _subagent_error(ev),
                })

            elif isinstance(ev, Diagnostic):
                # A2: the prover reported a human-facing failure (a tool that raised,
                # a degraded capability). Its `context` already names the anchor —
                # tool, path, turn — so the UI renders it on that step rather than in
                # a banner. `turn` from the event's own context wins over the
                # adapter's counter: a diagnostic from a concurrent E3 worker knows
                # which turn it belongs to better than the loop does.
                _ctx = dict(ev.context or {})
                _turn = _ctx.pop("turn", None)
                _step = step_id_by_path.get(_relativize(_ctx["path"], repo)) if _ctx.get("path") else None
                if _ctx.get("path"):
                    _ctx["path"] = _relativize(_ctx["path"], repo)
                diagnose(events, session_id, run_id, ev.severity, ev.code, ev.message,
                         turn=_turn if _turn is not None else current_turn,
                         source=ev.source, remedy=ev.remedy, step_id=_step, **_ctx)

            elif isinstance(ev, Error):
                emit(events, "run_error", {"message": ev.message})
                diagnose(events, session_id, run_id, "fatal", "run.crashed", ev.message,
                         turn=current_turn, source="prover")

            elif isinstance(ev, Finished):
                flush_narration()
                # Deterministic collation (item 25): if the coordinator delegated but
                # produced no clean proof of its own, promote the best compiling child
                # candidate as the session's proof (re-verified at the canonical path).
                # Runs BEFORE the artifact-result classification so the promoted verdict
                # settles the session's outcome. Never clobbers a clean coordinator proof.
                if not produced_clean and subagent_results:
                    promoted = _promote_winner(
                        subagent_results, session_id=session_id, run_id=run_id,
                        repo=repo, namespace=namespace,
                        formalization_id=focus_formalization_id,
                        turn=current_turn, events=events,
                    )
                    if promoted:
                        checked_artifact_kind = promoted.get("artifact_kind") or checked_artifact_kind
                        step_id_by_path[promoted["path"]] = promoted["id"]
                        produced_clean = True
                final_text = _final_text_for_result(ev)
                persist_assistant(final_text)
                final_status, result_kind = _completed_artifact_result(ev, checked_artifact_kind)
                final_result_kind = result_kind
                final_result_detail = None if result_kind == "defined" else ev.result_detail
                if spend_capped and ev.reason != "completed":
                    # Our own cap-triggered stop, not a user cancel: label it. A
                    # run that completed anyway (finished the proof in the same
                    # turn the cap tripped) keeps its real result.
                    final_result_kind = "max_spend"
                    final_result_detail = _MAX_SPEND_DETAIL
                store.update_run(
                    run_id, final_status, final_text=final_text,
                    input_tokens=ev.usage.input_tokens, output_tokens=ev.usage.output_tokens,
                    cost_usd=ev.cost,
                    result_kind=final_result_kind, result_detail=final_result_detail,
                )
                # Everything past this point is BOOKKEEPING: the run's outcome is
                # already durable above. Each piece is guarded on its own so one
                # failure neither loses the others nor escapes to the handler below,
                # which used to rewrite a proved run to 'failed' over a locked DB or an
                # unserializable transcript (AUDIT-2026-07-24 C6).
                _best_effort("usage breakdown", run_id,
                             lambda: store.replace_run_usage_breakdown(run_id, usage.rows()))
                # Persist the faithful conversation for the next activation to replay
                # (multi-turn, D16). Only here, on Finished — an errored run stores none.
                _best_effort("transcript", run_id,
                             lambda: store.set_run_transcript(run_id, ev.transcript.get("messages", [])))
                # Structured artifact index (4.1): record which declarations this
                # run's checked files hold, keyed to the run's own FileChanged set.
                _best_effort("artifact index", run_id,
                             lambda: _record_run_artifacts(
                                 session_id, run_id, project, namespace,
                                 dict(step_id_by_path),
                                 focus_formalization_id=focus_formalization_id,
                                 source_hash=focus_source_hash))

    except Exception as exc:  # noqa: BLE001 — surface any failure as an error event, never hang the stream
        logger.exception("Lea run %s failed", run_id)
        flush_narration()
        # C3/B1: this handler has always had `current_turn`, `last_tool` and
        # `step_id_by_path` in scope and used none of them — it emitted the bare
        # exception, so the user got `AuthenticationError: ...` with no indication of
        # what the run was doing or what to do about it. Classify it into a coded
        # diagnostic (the catalog supplies the remedy) and anchor it.
        #
        # REDACTED, not raw. Provider errors quote the credential back at you — the
        # live DeepSeek failure read "Your api key: ... is invalid" — and a diagnostic
        # is persisted to the database and rendered in the UI, so classifying off the
        # raw exception would have leaked the key into both.
        error_detail = _public_error_detail(exc)
        # Whether a key is SAVED for this model's provider. It separates "you never
        # added a key" from "the key you added was rejected" — two different mistakes
        # that look identical in the exception. `None` when we can't tell, so the
        # message doesn't claim either: a key may also come from the environment,
        # which `configured_provider_keys` deliberately cannot see.
        key_configured: bool | None = None
        try:
            from . import settings as settings_service

            requirements = settings_service.model_requirements(cfg.model)
            required = requirements.get("required_keys") or []
            if required:
                key_configured = any(k.get("configured") for k in required)
        except Exception:  # noqa: BLE001 — never let the explainer break the report
            logger.debug("Could not resolve key requirements for %s", cfg.model, exc_info=True)
        analysis = analyze_exception(exc, model=cfg.model, key_configured=key_configured)
        # The classification reads the raw exception, but nothing user-facing may carry
        # it: re-redact whatever the analysis extracted from the provider's message.
        analysis["message"] = _redact(analysis["message"])
        if analysis.get("detail"):
            analysis["detail"] = _redact(analysis["detail"])
        # `run_error` stays for the Overleaf companion, which parses this frame as the
        # failure reason (companion/leaApiClient.mjs). The diagnostic is the UI's.
        emit(events, "run_error", {"message": error_detail})
        _crash_path = _relativize(last_write_path, repo) if (last_write_path and repo) else None
        diagnose(events, session_id, run_id, "fatal", analysis["code"], analysis["message"],
                 turn=current_turn or None, remedy=analysis["remedy"], tool=last_tool,
                 title=analysis["title"], detail=analysis["detail"],
                 actions=analysis["actions"], model=cfg.model,
                 path=_crash_path,
                 step_id=step_id_by_path.get(_crash_path) if _crash_path else None)
        # Never downgrade a run that already reached a terminal status (C6). The
        # Finished handler persists the outcome before doing anything else, so a
        # failure after that point is a bookkeeping problem, not a failed proof —
        # marking it 'failed' here discarded a real result AND, because the derived
        # session status reads the latest code step's run, made the session look broken
        # too. The `done` frame keeps the real outcome for the same reason.
        if _run_is_terminal(run_id):
            logger.warning(
                "Run %s already finished as %r; keeping that outcome despite the error",
                run_id, final_status,
            )
        else:
            try:
                store.update_run(
                    run_id,
                    "failed",
                    result_kind="failed",
                    result_detail=error_detail,
                )
            except Exception:
                logger.exception("Failed to mark run %s failed", run_id)
            final_status = "failed"
            final_result_kind = "failed"
            final_result_detail = error_detail
    finally:
        # Publish any trailing streamed text before the terminal `done` (P1) — a run
        # that ends mid-batch must not drop its last words.
        deltas.flush()
        skills_catalog.cleanup(skills_tempdir)
        roles_catalog.cleanup(roles_tempdir)
        # D1: retire any child whose SubagentStarted never saw its SubagentFinished —
        # the coordinator was interrupted or crashed mid-child. Left 'running', its run
        # row would count as an active run forever (an eternal 'exploring…' child); mark
        # it failed so the child's derived status settles. Best-effort.
        for _rid, started in subagent_children.items():
            try:
                store.update_run(started["run_id"], "failed")
            except Exception:
                logger.exception("Failed to retire orphan sub-agent run %s", started.get("run_id"))
            _child_session_to_result.pop(started["child_id"], None)  # D2: drop stop handle
            # E1: settle + drop any orphaned child stream so an attached view doesn't hang.
            orphan_broker = runbroker.get(started["run_id"])
            if orphan_broker is not None:
                emit(orphan_broker, "done", {"status": "failed"})
            runbroker.drop(started["run_id"])
        subagent_children.clear()
        _stop_events.pop(run_id, None)
        _pending_approvals.pop(run_id, None)
        with _live_spend_lock:
            _live_run_costs.pop(run_id, None)
        # Release the admission slot (paired with the dispatcher's try_admit). Idempotent,
        # so a run that reaches here unadmitted — e.g. a direct unit-test call to
        # run_lea, which never goes through the endpoint — is a harmless no-op.
        runregistry.registry.release(run_id)
        done_payload = {"status": final_status}
        if final_result_kind:
            done_payload["result_kind"] = final_result_kind
        if final_result_detail:
            done_payload["result_detail"] = final_result_detail
        emit(events, "done", done_payload)
        # The run has ended: retire its broker. Subscribers already draining hold
        # their own reference and exit on `done`; a late observer gets a synthesized
        # terminal event from the persisted run row. Idempotent, so a direct unit-test
        # call to run_lea (no broker) is a no-op.
        runbroker.drop(run_id)
