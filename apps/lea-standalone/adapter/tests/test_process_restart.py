"""Small real-process probe for drain ordering and the data-directory lock."""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest


def _port():
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
    except PermissionError:
        pytest.skip("Loopback socket binding is unavailable in this test sandbox")


SERVER = """
import sys, threading, time
from pathlib import Path
import uvicorn
from app import db, runtime, runregistry
db.DB_PATH = Path(sys.argv[1])
from app import main
from app.config import LeaConfig
main.load_config = lambda: LeaConfig(model='fake/model', max_turns=3, lea_root=Path(sys.argv[3]))
if sys.argv[4] == 'observe-recovery':
    main.bridge.recover_runs_at_startup = lambda: None
if sys.argv[4] == 'hold':
    @main.app.on_event('startup')
    def hold_one_run():
        runregistry.registry.try_admit('held-run', 'held-session')
        marker = Path(sys.argv[3]) / 'release-held-run'
        def release_when_requested():
            while not marker.exists():
                time.sleep(0.05)
            runregistry.registry.release('held-run')
        threading.Thread(target=release_when_requested, daemon=True).start()
config = uvicorn.Config(main.app, host='127.0.0.1', port=int(sys.argv[2]), log_level='error')
server = uvicorn.Server(config)
runtime.bind_server(server)
server.run()
if not server.started:
    raise RuntimeError('Adapter startup failed')
"""


def _launch(database: Path, port: int, root: Path, mode="idle"):
    return subprocess.Popen(
        [sys.executable, "-u", "-c", SERVER, str(database), str(port), str(root), mode],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _json(url, body=None):
    request = Request(url, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json"} if body is not None else {},
                      method="POST" if body is not None else "GET")
    with urlopen(request, timeout=2) as response:
        return json.load(response)


def _wait_health(port: int, process, timeout=8):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if process.poll() is not None:
            raise AssertionError(f"Adapter exited before health: {process.stderr.read()}")
        try:
            return _json(f"http://127.0.0.1:{port}/api/health")
        except (OSError, URLError):
            time.sleep(0.05)
    raise AssertionError("Adapter did not become healthy")


def test_process_drain_preserves_health_and_rejects_duplicate_owner(tmp_path):
    database = tmp_path / "data" / "test.sqlite3"
    port = _port()
    duplicate_port = _port()
    first = _launch(database, port, tmp_path, "hold")
    second = None
    try:
        health = _wait_health(port, first)
        assert health["active_runs"] == 1
        second = _launch(database, duplicate_port, tmp_path)
        _, second_error = second.communicate(timeout=8)
        assert second.returncode != 0
        assert "Another adapter owns data directory" in second_error
        assert first.poll() is None

        draining = _json(f"http://127.0.0.1:{port}/api/runtime/restart", {"shutdown": True})
        assert draining["lifecycle_state"] == "draining"
        assert _json(f"http://127.0.0.1:{port}/api/health")["lifecycle_state"] == "draining"
        with pytest.raises(HTTPError) as rejected:
            _json(f"http://127.0.0.1:{port}/api/runs", {"message": "new work"})
        assert rejected.value.code == 503
        assert rejected.value.headers["Retry-After"] == "5"
        (tmp_path / "release-held-run").write_text("release")
        first.wait(timeout=8)
        assert first.returncode == 0
    finally:
        for process in (second, first):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_process_startup_recovers_safe_boundary_and_pauses_uncertain_model(tmp_path, monkeypatch):
    from app import db, recovery, store

    database = tmp_path / "data" / "test.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", database)
    db.init_db()
    run_ids = []
    for name in ("safe", "uncertain"):
        session = store.create_session(name)
        run = store.create_run(session["id"], "fake/model", None, 3)
        assert store.claim_pending_run(run["id"])
        attempt = recovery.start_attempt(run["id"])
        recorder = recovery.Recorder(run["id"], attempt, {"schema": 1})
        recorder("checkpoint", {"stage": "turn_boundary", "turn": 0,
                                "messages": [{"role": "user", "content": name}],
                                "cost_usd": 0.0, "max_cost_usd": None})
        if name == "uncertain":
            recorder("invocation_start", {"id": uuid4().hex, "kind": "model",
                                          "name": "stream", "turn": 1, "input": {}})
        run_ids.append(run["id"])

    port = _port()
    process = _launch(database, port, tmp_path, "observe-recovery")
    try:
        _wait_health(port, process)
        safe = _json(f"http://127.0.0.1:{port}/api/runs/{run_ids[0]}")
        uncertain = _json(f"http://127.0.0.1:{port}/api/runs/{run_ids[1]}")
        assert safe["status"] == "pending"
        assert safe["recovery_state"] == "recovering"
        assert uncertain["status"] == "cancelled"
        assert uncertain["recovery_state"] == "review"
        assert "model stream" in uncertain["recovery_reason"]
        _json(f"http://127.0.0.1:{port}/api/runtime/restart", {"shutdown": True})
        process.wait(timeout=8)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
