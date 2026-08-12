"""Run admission registry (item 9) — the check-that-is-the-claim.

The bug this closes is a TOCTOU: on `main` the endpoint peeked one scalar and the
claim happened later inside the run thread, so two attaches could both peek `None`
and both spawn. The registry's fix is *structural* — it fuses check and claim into
one `try_admit` call, so there is no gap between them regardless of locking.

A note on the lock, measured rather than assumed: `try_admit`'s critical section is
pure dict ops, and under CPython's GIL that runs to completion without a thread
switch — so `test_try_admit_semantics_under_overlap` pins the capacity *accounting*
under overlap but would pass with or without the lock (the GIL already serializes
it). The lock is not decorative, though: the moment the section can yield the GIL —
any I/O added later, or free-threaded (PEP 703) Python — an unlocked check-then-set
over-admits wildly. `test_lock_serializes_a_yielding_critical_section` is the one
with teeth: it injects a yield mid-section and shows the real Lock holds at capacity
while a no-op lock admits everyone.

Semantics pinned here (order matters — see the module docstring):
  already_active (same run) < session_busy (same session, other run) < at_capacity.
Plus: release frees a slot, is idempotent, and clears the session map only if it
still points at the releasing run (so a superseding run, item 10, isn't clobbered).
"""

import contextlib
import threading
import time

from app.runregistry import (
    ADMITTED,
    ALREADY_ACTIVE,
    AT_CAPACITY,
    SESSION_BUSY,
    ActiveRun,
    Admission,
    RunRegistry,
    registry as process_registry,
)
from app import runregistry


def test_first_run_is_admitted_and_recorded():
    reg = RunRegistry(max_concurrent=1)
    a = reg.try_admit("run-1", "sess-1")
    assert a.outcome == ADMITTED and a.ok
    assert a.active == 1 and a.capacity == 1
    assert reg.is_active("run-1")
    assert reg.run_for_session("sess-1") == "run-1"
    assert reg.active_count() == 1


def test_second_distinct_run_at_capacity_one_is_rejected():
    reg = RunRegistry(max_concurrent=1)
    reg.try_admit("run-1", "sess-1")
    a = reg.try_admit("run-2", "sess-2")
    assert a.outcome == AT_CAPACITY and not a.ok
    assert a.active == 1 and a.capacity == 1
    assert not reg.is_active("run-2")


def test_same_run_id_twice_is_already_active_not_a_second_claim():
    """A second events connection for the *same* run (passive-viewer case) must not
    read as contention or claim a second slot."""
    reg = RunRegistry(max_concurrent=2)
    reg.try_admit("run-1", "sess-1")
    a = reg.try_admit("run-1", "sess-1")
    assert a.outcome == ALREADY_ACTIVE
    assert reg.active_count() == 1, "already_active must not consume a second slot"


def test_same_session_other_run_is_session_busy_with_incumbent():
    reg = RunRegistry(max_concurrent=2)
    reg.try_admit("run-1", "sess-1")
    a = reg.try_admit("run-2", "sess-1")
    assert a.outcome == SESSION_BUSY
    assert a.incumbent_run_id == "run-1"
    assert not reg.is_active("run-2")


def test_session_busy_beats_capacity_so_a_followup_can_supersede():
    """At capacity 1 with the session's own run holding the only slot, a new turn
    for that session must get session_busy (a supersede candidate), NOT at_capacity
    — otherwise a chat could never take over its own stuck run."""
    reg = RunRegistry(max_concurrent=1)
    reg.try_admit("run-1", "sess-1")
    a = reg.try_admit("run-2", "sess-1")
    assert a.outcome == SESSION_BUSY, "same-session follow-up must be supersede-able, not a 409"


def test_release_frees_the_slot():
    reg = RunRegistry(max_concurrent=1)
    reg.try_admit("run-1", "sess-1")
    assert reg.release("run-1") is True
    assert reg.active_count() == 0
    assert reg.run_for_session("sess-1") is None
    assert reg.try_admit("run-2", "sess-2").outcome == ADMITTED


def test_release_is_idempotent():
    reg = RunRegistry(max_concurrent=1)
    reg.try_admit("run-1", "sess-1")
    assert reg.release("run-1") is True
    assert reg.release("run-1") is False, "double release must be a no-op, not a fault"
    assert reg.release("never-admitted") is False


def test_stale_release_does_not_clobber_a_reassigned_session():
    """After a run releases and the session admits a new run, a *late* second
    release of the old run must not delete the new run's session ownership. This is
    the guard item 10's supersede depends on."""
    reg = RunRegistry(max_concurrent=1)
    reg.try_admit("run-1", "sess-1")
    reg.release("run-1")
    reg.try_admit("run-2", "sess-1")          # session now owned by run-2
    assert reg.release("run-1") is False       # stale/double release of the old run
    assert reg.run_for_session("sess-1") == "run-2", "stale release stole the session"


def test_capacity_two_admits_two_distinct_sessions_then_rejects():
    reg = RunRegistry(max_concurrent=2)
    assert reg.try_admit("run-1", "sess-1").outcome == ADMITTED
    assert reg.try_admit("run-2", "sess-2").outcome == ADMITTED
    third = reg.try_admit("run-3", "sess-3")
    assert third.outcome == AT_CAPACITY
    assert third.active == 2 and third.capacity == 2


def _race_admits(reg, contenders):
    """Fire `contenders` `try_admit` calls (distinct run/session each) at once behind
    a barrier and return the list of outcomes."""
    barrier = threading.Barrier(contenders)
    results: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait(timeout=5)  # force all try_admit calls to overlap
        outcome = reg.try_admit(f"run-{i}", f"sess-{i}").outcome
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(contenders)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return results


def test_try_admit_semantics_under_overlap():
    """Capacity accounting stays exact when many distinct runs overlap: at capacity
    K exactly K are admitted and the rest are at_capacity.

    NOTE: this pins the *semantics*, not the lock. `try_admit`'s section is pure dict
    ops, which the GIL runs atomically, so this passes with or without the lock. The
    lock's teeth are in `test_lock_serializes_a_yielding_critical_section`.
    """
    capacity, contenders = 3, 40
    reg = RunRegistry(max_concurrent=capacity)
    results = _race_admits(reg, contenders)
    assert results.count(ADMITTED) == capacity, f"admitted {results.count(ADMITTED)}, want {capacity}"
    assert results.count(AT_CAPACITY) == contenders - capacity
    assert reg.active_count() == capacity, "registry over-admitted under the race"


class _YieldingRegistry(RunRegistry):
    """A registry whose critical section yields the GIL between the capacity check
    and the claim — standing in for any future I/O in the section, or free-threaded
    (nogil) Python. The injectable `_lock` lets a test compare real-lock vs no-op."""

    def try_admit(self, run_id, session_id):
        with self._lock:
            cap = self._max
            if run_id in self._active:
                return Admission(ALREADY_ACTIVE, len(self._active), cap)
            incumbent = self._by_session.get(session_id)
            if incumbent is not None and incumbent != run_id:
                return Admission(SESSION_BUSY, len(self._active), cap, incumbent_run_id=incumbent)
            full = len(self._active) >= cap
            time.sleep(0.001)  # the yield the real registry's pure section never makes
            if full:
                return Admission(AT_CAPACITY, len(self._active), cap)
            self._active[run_id] = ActiveRun(run_id, session_id)
            self._by_session[session_id] = run_id
            return Admission(ADMITTED, len(self._active), cap)


def test_lock_serializes_a_yielding_critical_section():
    """Teeth for the lock: with a GIL-yielding section, the real Lock still admits
    exactly capacity, while a no-op lock admits everyone. Proves the lock — not just
    the GIL — is what bounds admission once the section can yield."""
    capacity, contenders = 3, 40

    locked = _YieldingRegistry(max_concurrent=capacity)
    locked._lock = threading.Lock()
    assert _race_admits(locked, contenders).count(ADMITTED) == capacity
    assert locked.active_count() == capacity

    unlocked = _YieldingRegistry(max_concurrent=capacity)
    unlocked._lock = contextlib.nullcontext()
    admitted_without_lock = _race_admits(unlocked, contenders).count(ADMITTED)
    assert admitted_without_lock > capacity, (
        f"expected the no-op lock to over-admit (>{capacity}); got {admitted_without_lock}. "
        "If this fails the yield isn't exposing the race, so the locked case proves nothing."
    )


def test_env_knob_sets_default_capacity(monkeypatch):
    monkeypatch.setenv("LEA_MAX_CONCURRENT_RUNS", "5")
    assert RunRegistry().capacity == 5


def test_env_knob_invalid_or_below_one_falls_back_to_default(monkeypatch):
    # Default is 4 as of v2.3 Phase 6; an unusable value must not wedge admission.
    monkeypatch.setenv("LEA_MAX_CONCURRENT_RUNS", "0")
    assert RunRegistry().capacity == 4
    monkeypatch.setenv("LEA_MAX_CONCURRENT_RUNS", "not-a-number")
    assert RunRegistry().capacity == 4
    monkeypatch.delenv("LEA_MAX_CONCURRENT_RUNS", raising=False)
    assert RunRegistry().capacity == 4


def test_default_capacity_is_four(monkeypatch):
    """Phase 6 flips the shipped default from single-slot to four concurrent runs.
    Pinning the knob to 1 restores the old single-slot behavior exactly."""
    monkeypatch.delenv("LEA_MAX_CONCURRENT_RUNS", raising=False)
    assert RunRegistry().capacity == 4
    monkeypatch.setenv("LEA_MAX_CONCURRENT_RUNS", "1")
    assert RunRegistry().capacity == 1


def test_process_singleton_exists_and_is_a_registry():
    """The endpoint + bridge share one registry."""
    assert isinstance(process_registry, RunRegistry)
    assert process_registry is runregistry.registry
