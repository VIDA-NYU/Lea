"""Capacity-aware FIFO dispatcher integration (main v2.3 + gen_repair Phase 2).

The events endpoint is now observer-only. Admission happens in the background
dispatcher: independent sessions may fill the configured capacity, later work
waits FIFO instead of receiving a 409, and a same-session follow-up cooperatively
supersedes only its own incumbent.
"""

import time
from threading import Event, Lock

from lea.interface import Finished, TurnStarted
from lea.providers import Usage

from app import bridge, db, runbroker, runregistry, store
from app.config import LeaConfig
from app.runregistry import RunRegistry
from app.routes import runs as runs_route
from app.routes.runs import RunRequest


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _setup(monkeypatch, tmp_path, capacity):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config = LeaConfig(model="m", max_turns=3, lea_root=tmp_path, max_spend_usd=None)
    monkeypatch.setattr(runs_route, "load_config", lambda: config)
    monkeypatch.setattr(bridge, "load_config", lambda: config)
    runbroker._brokers.clear()
    bridge._stop_events.clear()
    reg = RunRegistry(max_concurrent=capacity)
    monkeypatch.setattr(runregistry, "registry", reg)
    return reg


def _controlled_prover(monkeypatch, labels, *, ignore_stop=()):
    """Install a fake prover whose runs stay live until their release Event is set."""
    started = {label: Event() for label in labels}
    release = {label: Event() for label in labels}
    order = []
    guard = Lock()

    def fake(
        config,
        messages,
        *,
        namespace=None,
        session_id=None,
        working_dir=None,
        should_stop=None,
        gate=None,
    ):
        label = messages[-1]["content"]
        with guard:
            order.append(label)
        started[label].set()
        yield TurnStarted(1)
        while not release[label].wait(0.02):
            if label not in ignore_stop and should_stop and should_stop():
                yield Finished(
                    "interrupted", "stopped", 1, session_id, "m",
                    Usage(input_tokens=0, output_tokens=0), 0.0, {"messages": []},
                )
                return
        yield Finished(
            "completed", "done", 1, session_id, "m",
            Usage(input_tokens=0, output_tokens=0), 0.0, {"messages": []},
        )

    monkeypatch.setattr(bridge, "run_events", fake)
    return started, release, order


def _new_run(message, session_id=None):
    return runs_route.create_run(
        RunRequest(message=message, session_id=session_id, autonomous=True)
    )


def test_capacity_two_drives_two_independent_sessions_concurrently(tmp_path, monkeypatch):
    reg = _setup(monkeypatch, tmp_path, capacity=2)
    started, release, _ = _controlled_prover(monkeypatch, ("A", "B"))

    a = _new_run("A")
    b = _new_run("B")
    assert started["A"].wait(2)
    assert started["B"].wait(2)
    assert reg.active_count() == 2
    assert reg.is_active(a["run_id"]) and reg.is_active(b["run_id"])

    release["A"].set()
    release["B"].set()
    assert _wait_for(lambda: reg.active_count() == 0)


def test_full_capacity_queues_unrelated_session_without_stopping_it(tmp_path, monkeypatch):
    reg = _setup(monkeypatch, tmp_path, capacity=1)
    started, release, order = _controlled_prover(monkeypatch, ("A", "B"))

    a = _new_run("A")
    assert started["A"].wait(2)
    b = _new_run("B")
    time.sleep(0.15)

    assert not started["B"].is_set()
    assert store.get_run(b["run_id"])["status"] == "pending"
    assert not bridge._stop_events.get(a["run_id"], Event()).is_set()

    release["A"].set()
    assert started["B"].wait(2)
    assert order == ["A", "B"]
    release["B"].set()
    assert _wait_for(lambda: reg.active_count() == 0)


def test_same_session_followup_supersedes_only_its_incumbent(tmp_path, monkeypatch):
    reg = _setup(monkeypatch, tmp_path, capacity=1)
    started, release, _ = _controlled_prover(monkeypatch, ("first", "followup"))

    first = _new_run("first")
    assert started["first"].wait(2)
    followup = _new_run("followup", session_id=first["session_id"])

    assert started["followup"].wait(2)
    assert _wait_for(lambda: store.get_run(first["run_id"])["status"] == "cancelled")
    assert reg.is_active(followup["run_id"])
    release["followup"].set()
    assert _wait_for(lambda: reg.active_count() == 0)


def test_stubborn_same_session_incumbent_keeps_followup_queued_until_release(tmp_path, monkeypatch):
    reg = _setup(monkeypatch, tmp_path, capacity=1)
    started, release, _ = _controlled_prover(
        monkeypatch, ("stubborn", "followup"), ignore_stop=("stubborn",)
    )

    first = _new_run("stubborn")
    assert started["stubborn"].wait(2)
    followup = _new_run("followup", session_id=first["session_id"])

    assert _wait_for(lambda: bridge._stop_events[first["run_id"]].is_set())
    assert not started["followup"].is_set()
    assert store.get_run(followup["run_id"])["status"] == "pending"

    release["stubborn"].set()
    assert started["followup"].wait(2)
    release["followup"].set()
    assert _wait_for(lambda: reg.active_count() == 0)
