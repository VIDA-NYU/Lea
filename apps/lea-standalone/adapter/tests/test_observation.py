"""Real SQLite boundaries: rollback, direct SQL, replay and stable partials."""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from queue import Queue
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, observation, runbroker, store
from app.bridge import _DeltaStream
from app.migration_limits import TEXT_CHUNK_BYTES
from app.routes import observation as routes
from app.routes.runs import _subscribe_durable


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "fixture.sqlite")
    db.init_db()
    session = store.create_session("Observation")
    run = store.create_run(session["id"], "fixture", None, 3)
    return session, run


def test_every_domain_table_has_all_transactional_triggers(fixture):
    domain, excluded = set(), set()
    versions = Path(__file__).parents[1] / "migrations/versions"
    for path in sorted(versions.glob("*.py")):
        if path.name[:4] < "0037":
            continue
        spec = importlib.util.spec_from_file_location(path.stem, path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        domain.update(getattr(migration, "DOMAIN_TABLES", ()))
        excluded.update(getattr(migration, "EXCLUDED_TABLES", ()))
        excluded.update(getattr(migration, "INTERNAL_TABLES", ()))
    with db.connect() as conn:
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")}
        assert tables == domain | excluded
        triggers = {r[0] for r in conn.execute("select name from sqlite_master where type='trigger'")}
    assert ({f"changes_{t}_{a}" for t in domain for a in ("insert", "update", "delete")}
            - {"changes_run_events_delete"}) <= triggers


def test_domain_and_changes_rollback_together_and_batch_direct_sql(fixture):
    session, _ = fixture
    before = observation.changes(0)["watermark"]
    with pytest.raises(RuntimeError):
        with db.write() as conn:
            conn.execute("update sessions set title='rolled back' where id=?", (session["id"],))
            raise RuntimeError("abort")
    assert observation.changes(before)["changes"] == []
    assert store.get_session(session["id"])["title"] == "Observation"
    with db.write() as conn:
        conn.execute("update sessions set title='one' where id=?", (session["id"],))
        conn.execute("update sessions set title='two' where id=?", (session["id"],))
    page = observation.changes(before)
    assert len(page["changes"]) == 2
    assert len({c["batch_id"] for c in page["changes"]}) == 1
    with db.connect() as conn:
        conn.execute("update sessions set title='three' where id=?", (session["id"],))
        conn.commit()
        conn.execute("update sessions set title='four' where id=?", (session["id"],))
    assert len({c["batch_id"] for c in observation.changes(page["cursor"])["changes"]}) == 2


def test_concurrent_commits_have_unique_monotonic_cursors(fixture):
    from concurrent.futures import ThreadPoolExecutor
    high = observation.changes(0)["watermark"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: store.create_session(f"writer {n}"), range(24)))
    page = observation.changes(high)
    assert len(page["changes"]) == 24
    ids = [c["id"] for c in page["changes"]]
    assert ids == sorted(set(ids))
    assert len({c["batch_id"] for c in page["changes"]}) == 24


def test_unregistered_external_writer_cannot_bypass_log(fixture):
    import sqlite3
    with sqlite3.connect(db.DB_PATH) as conn, pytest.raises(sqlite3.OperationalError, match="lea_change_batch"):
        conn.execute("update sessions set title='silent'")


def test_oversized_run_cursor_stays_within_sqlite_integer_range(fixture):
    from app.routes.runs import _request_cursor
    class Request:
        query_params = {"since": "9" * 5000}
        headers = {}
    assert _request_cursor(Request()) == 2**63 - 1


def test_scoped_tombstones_moves_and_pagination(fixture):
    session, _ = fixture
    first, second = store.create_project("first"), store.create_project("second")
    with db.write() as conn:
        conn.execute("update sessions set project_id=? where id=?", (first["id"], session["id"]))
    before = observation.changes(0)["watermark"]
    with db.write() as conn:
        conn.execute("update sessions set project_id=? where id=?", (second["id"], session["id"]))
    assert observation.changes(before, project_id=first["id"])["changes"][0]["action"] == "detach"
    assert observation.changes(before, project_id=second["id"])["changes"][0]["action"] == "update"
    with db.write() as conn:
        conn.execute("delete from sessions where id=?", (session["id"],))
    changes, cursor = [], before
    while True:
        page = observation.changes(cursor, session_id=session["id"], limit=1)
        changes.extend(page["changes"]); cursor = page["cursor"]
        if not page["has_more"]:
            break
    assert [c["id"] for c in changes] == sorted({c["id"] for c in changes})
    assert changes[-1]["action"] == "delete"
    assert changes[-1]["project_id"] == second["id"]


def test_snapshot_watermark_and_data_share_read_version(fixture):
    session, _ = fixture
    written = threading.Event()
    def update():
        with db.write() as conn:
            conn.execute("update sessions set title='new' where id=?", (session["id"],))
            written.set()  # commit waits for the reader in DELETE mode
    with db.read_snapshot() as conn:
        old_cursor = observation.watermark(conn)
        worker = threading.Thread(target=update)
        worker.start()
        assert written.wait(2)
        result = observation.session_snapshot(session["id"])
        assert result["cursor"] == old_cursor
        assert result["session"]["title"] == "Observation"
        with pytest.raises(RuntimeError, match="snapshot"):
            with db.write():
                pass
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert observation.changes(old_cursor)["changes"]


def test_retention_resets_without_cursor_reuse_or_pruning_history(fixture):
    session, run = fixture
    message = store.add_message(session["id"], "assistant", "keep", run["id"])
    high = observation.changes(0)["watermark"]
    floor = observation.compact_changes(now=datetime.now(timezone.utc) + timedelta(days=8))
    assert floor == high
    with pytest.raises(observation.CursorReset):
        observation.changes(0)
    assert observation.session_snapshot(session["id"])["timeline"][0]["content"] == "keep"
    store.add_message(session["id"], "assistant", "after compaction", run["id"])
    assert observation.changes(floor)["changes"][0]["id"] > floor


def test_partial_commits_before_delivery_and_finishes_same_identity(fixture):
    session, run = fixture
    class Observer(Queue):
        def put(self, event):
            message = event["payload"]["message"]
            row = observation.session_snapshot(session["id"])["timeline"][-1]
            assert str(row["id"]) == message["id"]
            assert row["content"].endswith(event["payload"]["text"])
            super().put(event)
    queue = Observer()
    stream = _DeltaStream(queue, session["id"], run["id"])
    text = "λ🙂" * 6000
    stream.add(text); stream.flush()
    events = list(queue.queue)
    assert all(len(e["payload"]["text"].encode()) <= TEXT_CHUNK_BYTES for e in events)
    assert "".join(e["payload"]["text"] for e in events) == text
    assert len({e["payload"]["message"]["id"] for e in events}) == 1
    final = stream.finish(text)
    assert final["id"] == events[0]["payload"]["message"]["id"]
    assert not final["live"]
    assert final["content"] == text
    assert len(observation.session_snapshot(session["id"])["timeline"]) == 1


def test_failed_partial_persistence_never_publishes(fixture, monkeypatch):
    session, run = fixture
    stream = _DeltaStream(Queue(), session["id"], run["id"])
    stream.add("uncommitted")
    def fail(*args):
        raise RuntimeError("DB unavailable")
    monkeypatch.setattr(store, "append_message_chunk", fail)
    with pytest.raises(RuntimeError):
        stream.flush()
    assert stream._events.empty()


def test_child_preview_uses_committed_batches_instead_of_per_token_writes(fixture):
    from app import bridge
    from lea.interface import AssistantTextDelta, ToolCalled
    session, run = fixture
    class Parent(Queue):
        def put(self, event):
            row = observation.session_snapshot(session["id"])["timeline"][-1]
            assert row["content"] == event["payload"]["text"]
            super().put(event)
    parent, child = Parent(), Queue()
    started = {"child_id": session["id"], "run_id": run["id"], "parent_events": parent,
               "result_id": "child-result"}
    for _ in range(100):
        bridge._forward_to_child_broker(child, AssistantTextDelta("x"), started)
    assert parent.empty()
    bridge._forward_to_child_broker(child, ToolCalled("read_file", {}), started)
    assert parent.qsize() == 1
    assert parent.get()["payload"]["text"] == "x" * 100


def test_child_transcript_preserves_repeated_narration_without_duplicates(fixture, tmp_path):
    from app import bridge
    from lea.interface import SubagentFinished
    session, run = fixture
    for _ in range(2):
        message = store.append_message_chunk(session["id"], run["id"], "same narration")
        store.finish_message_stream(message["id"], "same narration")
    event = SubagentFinished(result_id="child", subagent_type="explore", candidate_path=None,
        check_status=None, check_detail=None, stop_reason="completed", summary="done",
        transcript=[{"role": "assistant", "content": "same narration"}] * 2)
    bridge._populate_subagent(session["id"], run["id"], event, turn=1, repo=tmp_path)
    assert len(observation.session_snapshot(session["id"])["timeline"]) == 2


def test_partial_recovery_retains_identity_and_fences_attempt(fixture):
    session, run = fixture
    partial = store.append_message_chunk(session["id"], run["id"], "survives restart")
    with db.write() as conn:
        conn.execute("update runs set attempt_id='new-attempt' where id=?", (run["id"],))
    with pytest.raises(ValueError, match="Stale"):
        store.append_message_chunk(session["id"], run["id"], "stale", partial["id"])
    store.interrupt_message_streams()
    rows = observation.session_snapshot(session["id"])["timeline"]
    assert str(rows[0]["id"]) == partial["id"]
    assert rows[0]["stream_state"] == "interrupted"
    assert rows[0]["content"] == "survives restart"


def test_durable_replay_survives_broker_drop(fixture):
    _, run = fixture
    broker = runbroker.create(run["id"])
    broker.put({"type": "status", "payload": {"status": "working"}})
    broker.put({"type": "done", "payload": {"status": "answered"}})
    runbroker.drop(run["id"])
    async def read():
        return [frame async for frame in _subscribe_durable(run["id"], 0, None)]
    frames = asyncio.run(read())
    assert "event: status" in frames[0]
    assert "event: done" in frames[-1]
    stored = observation.run_events(run["id"], None, 0)
    assert len(stored) == 2
    assert observation.run_events(run["id"], None, stored[0]["seq"]) == stored[1:]
    assert len([c for c in observation.changes(0, run_id=run["id"])["changes"] if c["entity_type"] == "run_events"]) == 2


def test_failed_event_commit_is_not_visible_to_compatibility_broker(fixture, monkeypatch):
    _, run = fixture
    broker = runbroker.create(run["id"])
    def fail(*args):
        raise RuntimeError("commit failed")
    monkeypatch.setattr(observation, "append_run_event", fail)
    with pytest.raises(RuntimeError):
        broker.put({"type": "status", "payload": {"status": "not committed"}})
    assert broker.events_after(0) == []


def test_compatibility_replay_cannot_mutate_after_commit(fixture):
    _, run = fixture
    broker = runbroker.create(run["id"])
    payload = {"status": "committed"}
    broker.put({"type": "status", "payload": payload})
    payload["status"] = "not committed"
    assert broker.events_after(0)[0]["payload"]["status"] == "committed"
    assert observation.run_events(run["id"], None, 0)[0]["payload"]["status"] == "committed"


def test_run_event_replay_pages_bound_payload_bytes(fixture):
    _, run = fixture
    for _ in range(3):
        observation.append_run_event(run["id"], "status", {"text": "x" * 600_000})
    cursor, ids = 0, []
    for _ in range(3):
        page = observation.run_events(run["id"], None, cursor)
        assert len(page) == 1
        cursor = page[0]["seq"]
        ids.append(cursor)
    assert len(set(ids)) == 3


def test_deleting_run_removes_replay_payloads_with_scoped_tombstones(fixture):
    session, run = fixture
    observation.append_run_event(run["id"], "status", {"text": "deleted content"})
    high = observation.changes(0)["watermark"]
    with db.write() as conn:
        conn.execute("delete from runs where id=?", (run["id"],))
    assert observation.run_events(run["id"], None, 0) == []
    changes = observation.changes(high, session_id=session["id"])["changes"]
    assert any(c["entity_type"] == "run_observation_records" and c["action"] == "delete" for c in changes)


def test_slow_change_subscriber_resets_after_retention(fixture):
    class Request:
        headers = {}
        async def is_disconnected(self):
            return False
    async def run():
        response = await routes.changes(Request(), after=0, limit=1, stream=True)
        iterator = response.body_iterator
        first = await anext(iterator)
        assert "event: changes" in first
        observation.compact_changes(now=datetime.now(timezone.utc)+timedelta(days=8))
        assert "event: resync" in await anext(iterator)
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
    asyncio.run(run())


def test_snapshot_and_change_api_resets_and_filters(fixture):
    session, _ = fixture
    app = FastAPI(); app.include_router(routes.router)
    with TestClient(app) as client:
        snapshot = client.get(f"/api/snapshots/sessions/{session['id']}").json()
        assert snapshot["version"] == 1
        assert client.get("/api/changes?limit=1001").status_code == 422
        assert client.get("/api/changes?after=-1").status_code == 422
        assert client.get("/api/changes?stream=true", headers={"last-event-id": "invalid"}).status_code == 400
        unrelated = client.get("/api/changes?session_id=missing").json()
        assert unrelated["changes"] == []
        assert unrelated["cursor"] == snapshot["cursor"]
        observation.compact_changes(now=datetime.now(timezone.utc)+timedelta(days=8))
        response = client.get("/api/changes")
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "snapshot_required"
