"""Planned restarts keep admission closed until existing work finishes."""

import time
from types import SimpleNamespace

from app import db, runtime


class _Registry:
    def __init__(self):
        self.active = 1

    def active_count(self):
        return self.active

    def active_run_ids(self):
        return ["active-run"] if self.active else []


def test_graceful_drain_waits_and_can_be_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runtime.sqlite3")
    db.init_db()
    registry = _Registry()
    monkeypatch.setattr(runtime.runregistry, "registry", registry)
    server = SimpleNamespace(should_exit=False, config=SimpleNamespace(timeout_graceful_shutdown=None))
    runtime.bind_server(server)
    runtime.set_state("ready")
    try:
        snapshot = runtime.begin_drain(restart=True)
        assert snapshot["lifecycle_state"] == "draining"
        assert not runtime.accepting_work()
        time.sleep(0.15)
        assert server.should_exit is False
        assert runtime.cancel_drain()["lifecycle_state"] == "ready"
        assert server.should_exit is False

        runtime.begin_drain(restart=False)
        registry.active = 0
        deadline = time.monotonic() + 2
        while not server.should_exit and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.should_exit is True
        assert runtime.state() == "stopping"
    finally:
        runtime.set_state("ready")
        runtime.bind_server(None)


def test_drain_waits_for_manual_check_operation(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runtime.sqlite3")
    db.init_db()
    registry = _Registry()
    registry.active = 0
    monkeypatch.setattr(runtime.runregistry, "registry", registry)
    server = SimpleNamespace(should_exit=False, config=SimpleNamespace(timeout_graceful_shutdown=None))
    runtime.bind_server(server)
    runtime.set_state("ready")
    try:
        with runtime.operation():
            assert runtime.snapshot()["active_operations"] == 1
            runtime.begin_drain(restart=False)
            time.sleep(0.15)
            assert server.should_exit is False
        deadline = time.monotonic() + 2
        while not server.should_exit and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.should_exit is True
    finally:
        runtime.set_state("ready")
        runtime.bind_server(None)


def test_force_escalates_an_existing_graceful_drain(tmp_path, monkeypatch):
    from app import bridge

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runtime.sqlite3")
    db.init_db()
    registry = _Registry()
    monkeypatch.setattr(runtime.runregistry, "registry", registry)
    stops = []
    monkeypatch.setattr(bridge, "request_stop", lambda *args, **kwargs: stops.append((args, kwargs)))
    server = SimpleNamespace(should_exit=False, config=SimpleNamespace(timeout_graceful_shutdown=None))
    runtime.bind_server(server)
    runtime.set_state("ready")
    try:
        runtime.begin_drain(restart=False)
        assert runtime.begin_drain(force=True, restart=False)["lifecycle_state"] == "draining"
        deadline = time.monotonic() + 2
        while not stops and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stops == [(("active-run",), {"reason": "adapter_force", "source": "adapter"})]
        registry.active = 0
        while not server.should_exit and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.should_exit is True
        assert server.config.timeout_graceful_shutdown == 5
    finally:
        runtime.set_state("ready")
        runtime.bind_server(None)
