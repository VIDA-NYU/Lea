"""A rejoinable, multi-subscriber event stream for one run (v2.3).

Why this exists
---------------
Before this, a run's live events flowed through a single in-memory ``Queue`` owned
by the *first* ``/api/runs/{id}/events`` connection — the one that got admitted and
spawned the driver thread. Any *second* connection to the same running run (a
reconnect after a dropped stream, or switching away from a running chat and back)
got a "passive view" that emitted nothing but a terminal ``done``. So a reattached
UI went silent until the run finished — approvals, code steps, and messages only
appeared after a full session-switch re-fetch. That is the "my click only registers
after I switch sessions" bug.

The broker fixes it structurally. The driver publishes events *to the broker*, not
to one connection's queue. The broker keeps the run's events in memory with a
monotonic ``seq``; any number of connections subscribe at any time, replay
everything after their cursor, then follow live events. The driver's lifecycle is
decoupled from any single connection: a reconnect rejoins the *live* stream instead
of a dead passive view.

Design notes
------------
* **Buffer for the run's (bounded) lifetime.** Runs are minutes long; holding their
  events in memory is cheap for this local/single-tenant adapter. The broker is
  dropped when the run ends (``bridge.run_lea``'s ``finally``); a late connection
  to an already-finished run gets a synthesized terminal event from the persisted
  run row.
* **``put`` is Queue-compatible** (takes ``{"type", "payload"}``) so the runner's
  existing ``emit()`` writes to it unchanged.
* **Cursor replay.** ``events_after(cursor)`` returns everything with ``seq >
  cursor``. A fresh subscriber passes ``cursor=0`` and replays the whole buffer; the
  browser's native reconnect passes ``Last-Event-ID`` so it resumes without
  re-replaying. Client event handlers already dedupe by id (messages, code steps,
  approvals) and rebuild the in-progress assistant bubble from replayed deltas, so a
  full replay is idempotent.
* **Thread-safe.** The driver thread calls ``put`` while the asyncio endpoint thread
  calls ``events_after``; both take ``_lock``.
"""

from __future__ import annotations

import threading
from typing import Any


class RunBroker:
    """An append-only, cursor-addressable event log for a single run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._events: list[dict[str, Any]] = []  # each: {"seq", "type", "payload"}
        self._lock = threading.Lock()
        self._closed = False

    def put(self, item: dict[str, Any]) -> None:
        """Publish one ``{"type", "payload"}`` event (runner-facing, Queue-compatible).

        Assigns the next ``seq``. A ``done`` event closes the broker; anything
        published after close is dropped (the run is over, nothing more can happen)."""
        with self._lock:
            if self._closed:
                return
            seq = len(self._events) + 1
            self._events.append({"seq": seq, "type": item["type"], "payload": item["payload"]})
            if item["type"] == "done":
                self._closed = True

    def events_after(self, cursor: int) -> list[dict[str, Any]]:
        """Every buffered event with ``seq > cursor`` (a snapshot copy, safe to iterate
        outside the lock).

        An index slice, not a filter (AUDIT-2026-07-24 P1). ``seq`` is assigned as
        ``len(self._events) + 1``, so it *is* the 1-based index and ``seq > cursor`` is
        exactly ``self._events[cursor:]``. The filter form re-examined every event in
        the buffer on every call — and the subscriber loop calls this every 80 ms, per
        connection, so a long run's cost grew with the square of its own length while
        returning nothing.

        A negative or over-large cursor is clamped rather than trusted: it arrives from
        a client-supplied ``Last-Event-ID``/``?since=`` (see ``routes/runs._request_cursor``),
        and a raw negative index would silently re-send the tail of the buffer.
        """
        with self._lock:
            start = min(max(cursor, 0), len(self._events))
            return self._events[start:]

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed


# ---- module-level registry: one broker per live run ----
_brokers: dict[str, RunBroker] = {}
_guard = threading.Lock()


def create(run_id: str) -> RunBroker:
    """Register a fresh broker for a run about to be driven (replaces any prior one
    for the same id, e.g. a superseded run)."""
    broker = RunBroker(run_id)
    with _guard:
        _brokers[run_id] = broker
    return broker


def get(run_id: str) -> RunBroker | None:
    with _guard:
        return _brokers.get(run_id)


def drop(run_id: str) -> None:
    """Remove a run's broker once the run has ended. Idempotent — a direct
    ``run_lea`` unit-test call that never registered one is a harmless no-op.
    Subscribers still mid-read hold their own reference, so they drain and exit
    cleanly; dropping only prevents *new* subscribers from attaching."""
    with _guard:
        _brokers.pop(run_id, None)
