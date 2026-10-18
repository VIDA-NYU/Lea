"""Exercise the production launcher with real HTTP streams and process signals."""
import http.client
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest
from fastapi import FastAPI

import run_api
from run_api import create_server, GRACEFUL_SHUTDOWN_SECONDS


def test_launcher_second_interrupt_bypasses_python_teardown(monkeypatch):
    exits = []
    monkeypatch.setattr(run_api.os, "_exit", exits.append)
    server = create_server(FastAPI(), port=0)
    assert server.config.timeout_graceful_shutdown == GRACEFUL_SHUTDOWN_SECONDS
    server.handle_exit(signal.SIGINT, None)
    assert server.should_exit
    assert server.config.app.state.shutting_down
    assert not server.force_exit
    server.handle_exit(signal.SIGINT, None)
    assert server.force_exit
    assert exits == [130]


def test_api_exit_never_drains_workspace_execution(monkeypatch):
    from app import runtime
    monkeypatch.setattr(runtime,'begin_drain',lambda **_:pytest.fail('API exit drained the worker'))
    server=create_server(FastAPI(),port=0)
    server.handle_exit(signal.SIGINT,None)
    assert server.should_exit and server.config.app.state.shutting_down


@pytest.fixture
def running_server(tmp_path):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    script = f"""
import asyncio
from pathlib import Path
from fastapi import FastAPI
from app import db
from app.routes.sessions import session_list_events
from run_api import create_server
db.DB_PATH = Path({str(tmp_path / 'test.sqlite3')!r})
db.init_db()
app = FastAPI()
app.add_api_route('/events', session_list_events)
@app.get('/health')
async def health():
    return {{'ok': True}}
@app.get('/held')
async def held():
    print('HELD REQUEST', flush=True)
    await asyncio.sleep(3600)
    return {{'ok': True}}
create_server(app, port={port}, log_level='info').run()
"""
    log_path = tmp_path / "server.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            cwd=Path(__file__).resolve().parents[1], stdout=log, stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, log_path.read_text()
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
                try:
                    connection.request("GET", "/health")
                    if connection.getresponse().status == 200:
                        break
                except OSError:
                    assert time.monotonic() < deadline, log_path.read_text()
                    time.sleep(0.05)
                finally:
                    connection.close()
            yield process, port, log_path
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def wait_for_log(path, marker):
    deadline = time.monotonic() + 3
    while marker not in path.read_text():
        assert time.monotonic() < deadline, path.read_text()
        time.sleep(0.02)


def test_open_session_stream_closes_on_first_interrupt(running_server):
    process, port, log = running_server
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", "/events")
        response = connection.getresponse()
        assert response.status == 200
        assert b"sessions_changed" in response.readline()
        started = time.monotonic()
        process.send_signal(signal.SIGINT)
        process.wait(timeout=3)
        assert time.monotonic() - started < 2.5, log.read_text()
        remaining=response.read().decode()
        assert 'id: 0\n' in remaining
        payload=next(line[6:] for line in remaining.splitlines() if line.startswith('data: '))
        assert json.loads(payload)=={'cursor':0,'reset':False}
        assert "timeout graceful shutdown exceeded" not in log.read_text()
    finally:
        connection.close()


@pytest.mark.parametrize("force", [False, True])
def test_stalled_request_has_a_deadline_and_second_interrupt_forces_exit(running_server, force):
    process, port, log = running_server
    with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
        connection.sendall(b"GET /held HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        wait_for_log(log, "HELD REQUEST")
        started = time.monotonic()
        process.send_signal(signal.SIGINT)
        wait_for_log(log, "Shutting down")
        if force:
            started = time.monotonic()
            process.send_signal(signal.SIGINT)
            process.wait(timeout=2)
            assert time.monotonic() - started < 1.5, log.read_text()
        else:
            process.wait(timeout=GRACEFUL_SHUTDOWN_SECONDS + 2)
            assert time.monotonic() - started < GRACEFUL_SHUTDOWN_SECONDS + 1.5, log.read_text()
            assert "timeout graceful shutdown exceeded" in log.read_text()
