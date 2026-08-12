"""Run admission registry (v2.3 concurrency, item 9).

The single source of truth for "may this run start, and is there room?" It replaces
the old split decision — the endpoint peeking a scalar (``bridge.current_active_run_id``)
while the *claim* happened later, inside the spawned thread (``active_run_lock.acquire``).
That gap was a **TOCTOU**: two attaches for two different runs both peeked and saw
``None``, both spawned a thread, and one lost the lock and had to emit ``done(failed)``.

Here the check *is* the claim: one lock guards two maps and one decision function,
``try_admit()``, called at the endpoint **before** the thread is spawned. A caller
that is admitted holds a slot until it calls ``release()``.

Capacity is ``LEA_MAX_CONCURRENT_RUNS`` (**default 4** as of v2.3 Phase 6). Every
seam concurrency needs — the admission registry, per-run brokers, run-scoped
filesystem context, the rejoinable stream — shipped and baked at cap 1, so the
default is now the first user-visible concurrency: four chats formalize at once.
Pin ``LEA_MAX_CONCURRENT_RUNS=1`` to fall back to the single-slot lock this
replaced (byte-for-byte the old behavior); raise it for a bigger host.

Two maps, not one:

  * ``_active: dict[run_id -> ActiveRun]`` — who is running (capacity is ``len``).
  * ``_by_session: dict[session_id -> run_id]`` — which run owns each chat, so a
    second turn for a *busy session* is distinguishable from a *new* session at
    capacity. The two get different answers (``session_busy`` vs ``at_capacity``):
    a session's own follow-up is a supersede candidate (item 10), a stranger at a
    full house is a plain 409.

Ordering of the checks matters and is deliberate:

  1. ``already_active`` — this exact ``run_id`` is already admitted. A second
     events connection for the *same* run (the passive-viewer case) must not be
     treated as contention.
  2. ``session_busy`` — a *different* run for this session holds a slot. Checked
     **before** capacity on purpose: at capacity 1 a same-session follow-up would
     otherwise get ``at_capacity`` and could never supersede its own incumbent.
  3. ``at_capacity`` — the house is full and none of it is yours.
  4. ``admitted`` — a slot is claimed and recorded.

``release()`` is idempotent: releasing an unknown/already-released run is a no-op,
so the run thread's ``finally`` can call it unconditionally without a second fault.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from threading import Lock

# Admission outcomes. Strings (not an Enum) so they serialize into log lines and
# test assertions without ceremony, and read the same in both.
ADMITTED = "admitted"
ALREADY_ACTIVE = "already_active"
SESSION_BUSY = "session_busy"
AT_CAPACITY = "at_capacity"


def _env_capacity(default: int = 4) -> int:
    """Read ``LEA_MAX_CONCURRENT_RUNS`` (default 4). A missing/blank/invalid value
    or anything < 1 falls back to the default rather than wedging admission at 0."""
    raw = os.environ.get("LEA_MAX_CONCURRENT_RUNS", "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


@dataclass(frozen=True)
class ActiveRun:
    """One admitted run. Minimal by design — the registry answers admission, not
    run state (that lives in the DB and the bridge's per-run maps)."""

    run_id: str
    session_id: str


@dataclass(frozen=True)
class Admission:
    """The verdict from ``try_admit``. ``ok`` is the fast path check the caller
    branches on; the rest is context the endpoint turns into an SSE/HTTP answer
    (passive view, 409 'at capacity (n/N)', or the item-10 supersede)."""

    outcome: str
    active: int          # slots in use *after* this call (admitted counts itself)
    capacity: int
    incumbent_run_id: str | None = None  # set only for session_busy

    @property
    def ok(self) -> bool:
        return self.outcome == ADMITTED


class RunRegistry:
    """Thread-safe admission for concurrent runs. One lock, two maps.

    Instantiate with an explicit ``max_concurrent`` in tests; the process singleton
    (:data:`registry`) reads the env knob once at import.
    """

    def __init__(self, max_concurrent: int | None = None) -> None:
        self._lock = Lock()
        self._active: dict[str, ActiveRun] = {}
        self._by_session: dict[str, str] = {}
        self._max = max_concurrent if max_concurrent is not None else _env_capacity()

    @property
    def capacity(self) -> int:
        return self._max

    def try_admit(self, run_id: str, session_id: str) -> Admission:
        """Atomically decide + claim. See the module docstring for the ordering.

        On ``admitted`` the slot is recorded and must be paired with a
        ``release(run_id)`` (the run thread's ``finally``). Every other outcome
        claims nothing — the caller does not release.
        """
        with self._lock:
            capacity = self._max
            if run_id in self._active:
                return Admission(ALREADY_ACTIVE, len(self._active), capacity)

            incumbent = self._by_session.get(session_id)
            if incumbent is not None and incumbent != run_id:
                return Admission(SESSION_BUSY, len(self._active), capacity,
                                 incumbent_run_id=incumbent)

            if len(self._active) >= capacity:
                return Admission(AT_CAPACITY, len(self._active), capacity)

            self._active[run_id] = ActiveRun(run_id=run_id, session_id=session_id)
            self._by_session[session_id] = run_id
            return Admission(ADMITTED, len(self._active), capacity)

    def release(self, run_id: str) -> bool:
        """Free the slot held by ``run_id``. Idempotent — returns True if it was
        holding one, False if there was nothing to release (unknown/double call).

        Clears the session map only if it still points at *this* run, so a
        superseding run that already reclaimed the session (item 10) isn't
        clobbered when the loser finally releases.
        """
        with self._lock:
            run = self._active.pop(run_id, None)
            if run is None:
                return False
            if self._by_session.get(run.session_id) == run_id:
                del self._by_session[run.session_id]
            return True

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._active

    def run_for_session(self, session_id: str) -> str | None:
        """The run_id currently holding a slot for this session, or None."""
        with self._lock:
            return self._by_session.get(session_id)

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def active_run_ids(self) -> list[str]:
        with self._lock:
            return list(self._active)


# Process singleton the endpoint + bridge share. Capacity is read from the env once
# here; tests construct their own RunRegistry(max_concurrent=...) instead.
registry = RunRegistry()
