"""Crash decisions use the durable journal, never a process-local guess."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app import db, recovery, store


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "recovery.sqlite3")
    db.init_db()
    session = store.create_session("Prove the target")
    row = store.create_run(session["id"], "fake/model", None, 4)
    assert store.claim_pending_run(row["id"])
    attempt = recovery.start_attempt(row["id"])
    return row["id"], attempt


def _position(recorder, stage="turn_boundary", **extra):
    recorder("checkpoint", {
        "stage": stage, "messages": [{"role": "user", "content": "Prove the target"}],
        "turn": 1, "cost_usd": 0.25, "max_cost_usd": 1.0,
        **extra,
    })


def _pending(recorder, kind, name, call_index=None):
    action = uuid4().hex
    recorder("invocation_start", {"id": action, "kind": kind, "name": name,
                                  "turn": 1, "call_index": call_index,
                                  "input": {"path": "Target.lean"}})
    return action


def test_safe_boundary_resumes_once_with_same_logical_run(run):
    rid, attempt = run
    from app import runtime
    assert recovery.runtime_id() == runtime.IDENTITY
    _position(recovery.Recorder(rid, attempt, {"schema": 1}))
    assert recovery.recover_running() == {"resumed": 1, "paused": 0, "stopped": 0}
    row = store.get_run_status(rid)
    assert row["status"] == "pending"
    assert row["recovery_state"] == "recovering"
    assert row["recovery_count"] == 1
    with db.connect() as conn:
        assert conn.execute("select count(*) from timeline where run_id = ? and kind = 'diagnostic'",
                            (rid,)).fetchone()[0] == 1
    assert recovery.recover_running() == {"resumed": 0, "paused": 0, "stopped": 0}


@pytest.mark.parametrize("kind,name", [
    ("model", "stream"), ("tool", "write_file"),
    ("tool", "bash"), ("tool", "spawn_subagent"),
])
def test_uncertain_action_requires_review(run, kind, name):
    rid, attempt = run
    recorder = recovery.Recorder(rid, attempt, {"schema": 1})
    _position(recorder, "model_complete")
    _pending(recorder, kind, name, 0)
    assert recovery.recover_running()["paused"] == 1
    row = store.get_run_status(rid)
    assert row["status"] == "cancelled"
    assert row["recovery_state"] == "review"
    assert name in row["recovery_reason"]
    assert row["usage_status"] == "unknown"
    with db.connect() as conn:
        assert conn.execute("select count(*) from timeline where run_id = ? and kind = 'diagnostic'",
                            (rid,)).fetchone()[0] == 1


def test_pending_child_started_before_newer_checkpoint_still_requires_review(run):
    rid, attempt = run
    recorder = recovery.Recorder(rid, attempt, {"schema": 1})
    _position(recorder, "model_complete")
    _pending(recorder, "tool", "spawn_subagent", 1)
    # Another tool may finish while the child remains active.
    _position(recorder, "model_complete", completed_tools={"0": "read result"})
    assert recovery.recover_running()["paused"] == 1
    assert "spawn_subagent" in store.get_run_status(rid)["recovery_reason"]


@pytest.mark.parametrize("name", ["read_file", "search_mathlib", "lean_check"])
def test_audited_read_only_action_can_retry(run, name):
    rid, attempt = run
    recorder = recovery.Recorder(rid, attempt, {"schema": 1})
    _position(recorder, "model_complete")
    _pending(recorder, "tool", name, 0)
    assert recovery.recover_running()["resumed"] == 1
    assert store.get_run_status(rid)["status"] == "pending"


def test_completed_tool_result_is_loaded_from_checkpoint(run):
    rid, attempt = run
    recorder = recovery.Recorder(rid, attempt, {"schema": 1})
    _position(recorder, "model_complete", completed_tools={"0": "written"})
    assert recovery.recover_running()["resumed"] == 1
    payload, manifest = recovery.load_checkpoint(rid)
    assert payload["completed_tools"] == {"0": "written"}
    assert manifest == {"schema": 1}


def test_lost_model_result_is_not_automatically_reissued(run):
    rid, attempt = run
    recorder = recovery.Recorder(rid, attempt, {"schema": 1})
    _position(recorder)
    action = _pending(recorder, "model", "stream")
    recorder("invocation_result", {"id": action, "result": {"completed": True}})
    assert recovery.recover_running()["paused"] == 1
    assert "after the last saved" in store.get_run_status(rid)["recovery_reason"]


def test_original_stop_and_deadline_win_over_recovery(run):
    rid, attempt = run
    _position(recovery.Recorder(rid, attempt, {"schema": 1}))
    with db.write() as conn:
        conn.execute("update runs set stop_requested_reason = 'user_stop' where id = ?", (rid,))
    assert recovery.recover_running()["stopped"] == 1
    assert store.get_run_status(rid)["stop_reason"] == "user_stop"


def test_expired_original_deadline_does_not_restart(run):
    rid, attempt = run
    _position(recovery.Recorder(rid, attempt, {"schema": 1}))
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with db.write() as conn:
        conn.execute("update runs set deadline_at = ? where id = ?", (past, rid))
    assert recovery.recover_running()["stopped"] == 1
    assert store.get_run_status(rid)["stop_reason"] == "timeout"


def test_only_two_complete_payloads_are_retained(run):
    rid, attempt = run
    recorder = recovery.Recorder(rid, attempt, {"schema": 1})
    for turn in range(4):
        _position(recorder, turn=turn)
    with db.connect() as conn:
        rows = conn.execute("select payload from run_checkpoints where run_id = ? order by id", (rid,)).fetchall()
    assert len(rows) == 2
    assert '"turn": 2' in rows[0]["payload"]
    assert '"turn": 3' in rows[1]["payload"]


def test_obsolete_attempt_is_fenced_from_checkpoint_writes(run):
    rid, attempt = run
    recorder = recovery.Recorder(rid, attempt, {"schema": 1})
    with db.write() as conn:
        conn.execute("update runs set attempt_id = 'replacement' where id = ?", (rid,))
    with pytest.raises(recovery.CheckpointWriteError, match="Obsolete"):
        _position(recorder)


def test_prover_resumes_saved_model_result_without_repeating_request(tmp_path, monkeypatch):
    from lea import agent
    from lea.config import LeaConfig
    from lea.events import Finished
    from lea.providers import Done, TextDelta, Usage

    calls = []
    saved = {}

    def provider(*args, **kwargs):
        calls.append("request")
        yield TextDelta("Here is the requested explanation.")
        yield Done(Usage(7, 3), 0.02)

    def crash_after_model(event, payload):
        if event == "checkpoint" and payload["stage"] == "model_complete":
            saved.update(payload)
            raise recovery.CheckpointWriteError("simulated crash after model response")

    monkeypatch.setattr(agent, "_provider_stream", provider)
    monkeypatch.setattr(agent, "load_system_prompt", lambda *a, **k: "system")
    config = LeaConfig(model="fake/model", prompt_variant="interactive", max_turns=3,
                       lea_root=tmp_path, mcp_servers={})
    messages = [{"role": "user", "content": "Explain this proof."}]

    with pytest.raises(recovery.CheckpointWriteError):
        list(agent.run_events(config, messages, session_id="test", working_dir=str(tmp_path),
                              checkpoint_callback=crash_after_model))
    assert saved["stage"] == "model_complete"
    assert len(calls) == 1

    events = list(agent.run_events(config, messages, session_id="test", working_dir=str(tmp_path),
                                   resume_state=saved, checkpoint_callback=lambda *_: None))
    assert len(calls) == 1
    assert isinstance(events[-1], Finished)
    assert events[-1].reason == "assistant"
    assert events[-1].usage.input_tokens == 7


@pytest.mark.parametrize('evidence', ['legacy', 'current', 'changed'])
def test_prover_finishes_saved_final_classification_without_second_model_call(tmp_path, monkeypatch, evidence):
    from lea import agent
    from lea.config import LeaConfig
    from lea.events import Finished

    monkeypatch.setattr(agent, "_provider_stream", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("provider must not be called")))
    monkeypatch.setattr(agent, "load_system_prompt", lambda *a, **k: "system")
    config = LeaConfig(model="fake/model", prompt_variant="default", max_turns=3,
                       lea_root=tmp_path, mcp_servers={})
    saved = {
        "stage": "final_result", "messages": [{"role": "user", "content": "Target"}],
        "turn": 1, "input_tokens": 11, "output_tokens": 4, "cost_usd": 0.03,
        "final_result": {"text": "Finished proof", "kind": "proved", "detail": "Target checked"},
    }
    if evidence != 'legacy':
        from evidence_fixtures import mock_execution
        path = tmp_path / 'Target.lean'
        path.write_text('theorem target : True := by trivial\n')
        saved['proof_state'] = dict(latest_proof_path=str(path), latest_check_path=str(path),
            latest_check_passed=True, latest_check_output='OK', latest_check_content=path.read_text(),
            latest_check_execution=mock_execution(path), unchecked_write=False)
        if evidence == 'changed':
            path.write_text('theorem target : False := by trivial\n')
    events = list(agent.run_events(config, saved["messages"], session_id="test",
                                   working_dir=str(tmp_path), resume_state=saved,
                                   checkpoint_callback=lambda *_: None))
    assert len(events) == 1
    assert isinstance(events[0], Finished)
    assert events[0].result_kind == ('proved' if evidence == 'current' else 'needs_review')
    assert events[0].cost == 0.03


def test_data_directory_lock_refuses_second_worker(tmp_path, monkeypatch):
    import subprocess
    import sys
    from app import runtime

    database = tmp_path / "exclusive.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", database)
    runtime.acquire_data_lock()
    try:
        script = (
            "from pathlib import Path\n"
            "from app import db, runtime\n"
            f"db.DB_PATH = Path({str(database)!r})\n"
            "runtime.acquire_data_lock()\n"
        )
        child = subprocess.run([sys.executable, "-c", script], capture_output=True,
                               text=True, timeout=10)
        assert child.returncode != 0
        assert "Another workspace worker owns data directory" in child.stderr
    finally:
        runtime.release_data_lock()


def test_shared_data_configuration_controls_database_and_lock_root(tmp_path):
    import os
    import subprocess
    import sys

    data_dir = tmp_path / "shared"
    env = {**os.environ, "LEA_SHARED_DATA_DIR": str(data_dir)}
    child = subprocess.run([sys.executable, "-c",
                            "from app import db, runtime; print(db.DB_PATH); print(runtime.owned_process_dir())"],
                           capture_output=True, text=True, timeout=10, env=env)
    assert child.returncode == 0
    assert child.stdout.splitlines() == [str(data_dir / "lea-interface.sqlite3"),
                                         str(data_dir / "owned-processes")]


def test_stale_lean_process_receipt_is_reaped_only_with_matching_identity(tmp_path, monkeypatch):
    import json
    import os
    import subprocess
    import sys
    import threading
    from lea import process_ownership

    owned = tmp_path / "owned"
    monkeypatch.setenv("LEA_OWNED_PROCESS_DIR", str(owned))
    lease = process_ownership.prepare()
    assert lease is not None
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            start_new_session=True, pass_fds=(lease.fd,))
    try:
        process_ownership.register(proc.pid, lease)
        process_ownership.release_parent(lease, spawned=True)
        receipt = owned / f"{proc.pid}.json"
        assert receipt.exists()
        saved = json.loads(receipt.read_text())
        saved["owner_pid"] = 0  # simulate a previous adapter process
        real_lease = saved["lease"]
        saved["lease"] = "wrong.lease"
        receipt.write_text(json.dumps(saved))
        with pytest.raises(RuntimeError, match="Could not verify cleanup"):
            process_ownership.reap_previous()
        assert proc.poll() is None
        saved["lease"] = real_lease
        receipt.write_text(json.dumps(saved))
        with monkeypatch.context() as scoped:
            scoped.setattr(process_ownership.os, "getpgid", lambda _pid: -1)
            with pytest.raises(RuntimeError, match="Could not verify cleanup"):
                process_ownership.reap_previous()
        assert proc.poll() is None
        waiter = threading.Thread(target=proc.wait, daemon=True)
        waiter.start()
        assert process_ownership.reap_previous() == [proc.pid]
        waiter.join(timeout=5)
        assert proc.poll() is not None
        assert not receipt.exists()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, 9)
            proc.wait(timeout=5)


def test_lean_transport_starts_only_after_ownership_is_recorded(tmp_path, monkeypatch):
    import json
    import sys
    import time
    from lea.lsp_daemon import _Transport

    owned = tmp_path / "owned"
    marker = tmp_path / "started"
    monkeypatch.setenv("LEA_OWNED_PROCESS_DIR", str(owned))
    command = [sys.executable, "-c",
               "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(30)",
               str(marker)]
    transport = _Transport(command, str(tmp_path))
    try:
        assert transport.start(lambda _message: None)
        receipt = owned / f"{transport._proc.pid}.json"
        assert receipt.exists()
        assert json.loads(receipt.read_text())["pid"] == transport._proc.pid
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.01)
        assert marker.read_text() == "ready"
    finally:
        transport.close()
    assert not receipt.exists()


def test_lean_transport_does_not_start_if_ownership_record_fails(tmp_path, monkeypatch):
    import sys
    from lea import process_ownership
    from lea.lsp_daemon import _Transport

    monkeypatch.setenv("LEA_OWNED_PROCESS_DIR", str(tmp_path / "owned"))
    marker = tmp_path / "started"
    command = [sys.executable, "-c",
               "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started')",
               str(marker)]
    monkeypatch.setattr(process_ownership, "register",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("receipt failed")))
    transport = _Transport(command, str(tmp_path))
    assert not transport.start(lambda _message: None)
    assert "receipt failed" in str(transport.failure)
    assert not marker.exists()
    assert transport._proc is None
    assert not list((tmp_path / "owned").glob("*.json"))


def test_manifest_detects_manual_proof_and_configuration_changes(tmp_path, monkeypatch):
    from app.config import LeaConfig

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "recovery.sqlite3")
    db.init_db()
    repo = tmp_path / "proofs"
    repo.mkdir()
    source = repo / "Target.lean"
    source.write_text("theorem target : True := by trivial\n")
    run = {"session_id": "session", "focus_formalization_id": "target",
           "focus_source_hash": "source", "model": "fake/model"}
    config = LeaConfig(model="fake/model", lea_root=tmp_path, max_turns=4)
    original = recovery.manifest(run=run, config=config, repo=repo)
    source.write_text("theorem target : True := by simp\n")
    assert recovery.manifest(run=run, config=config, repo=repo) != original
    source.write_text("theorem target : True := by trivial\n")
    changed = LeaConfig(model="fake/model", lea_root=tmp_path, max_turns=5)
    assert recovery.manifest(run=run, config=changed, repo=repo) != original
