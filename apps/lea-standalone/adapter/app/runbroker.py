"""Worker-local publisher registry; production observation lives only in SQLite.

Production create().put() commits before delivery and retains no memory replay.
Direct construction keeps an isolated in-memory sink for event-mapping tests.
"""

from __future__ import annotations

import threading
from typing import Any


class RunBroker:
    """An append-only, cursor-addressable event log for a single run."""

    def __init__(self, run_id: str, *, persistent: bool = False) -> None:
        self.run_id = run_id
        self.persistent = persistent
        self._events: list[dict[str, Any]] = []  # each: {"seq", "type", "payload"}
        self._lock = threading.Lock()
        self._closed = False

    def put(self, item: dict[str, Any]) -> None:
        """Publish one ``{"type", "payload"}`` event (runner-facing, Queue-compatible).

        Assigns a durable global cursor in production (a local seq in pure mode).
        A ``done`` event closes the broker; later publication is dropped."""
        with self._lock:
            if self._closed:
                return
            if self.persistent:
                from .observation import append_run_event
                append_run_event(self.run_id, item["type"], item["payload"])
            else:
                event = {"seq": len(self._events) + 1, "type": item["type"], "payload": item["payload"]}
                self._events.append(event)
            if item["type"] == "done":
                self._closed = True

    def events_after(self, cursor: int) -> list[dict[str, Any]]:
        """Replay after a cursor: bounded SQL pages in production, a copy in tests.

        Pure mode uses an index slice (AUDIT-2026-07-24 P1); production uses
        indexed SQL pages of committed global event IDs, which can have gaps. Neither
        rescans the whole log on every poll. Pure mode clamps hostile cursors;
        production returns an explicit reset outside retained history.
        """
        with self._lock:
            if self.persistent:
                from . import db, observation
                with db.connect() as conn:
                    run = conn.execute('select attempt_id from runs where id=?', (self.run_id,)).fetchone()
                return observation.run_events(self.run_id, run[0] if run else None, max(cursor, 0))
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
    broker = RunBroker(run_id, persistent=True)
    with _guard:
        _brokers[run_id] = broker
    return broker


def get(run_id: str) -> RunBroker | None:
    with _guard:
        return _brokers.get(run_id)


def drop(run_id: str) -> None:
    """Remove a run's broker once the run has ended. Idempotent — a direct
    ``run_lea`` unit-test call that never registered one is a harmless no-op.
    SQL subscribers remain independent of this worker-local registry."""
    with _guard:
        _brokers.pop(run_id, None)
