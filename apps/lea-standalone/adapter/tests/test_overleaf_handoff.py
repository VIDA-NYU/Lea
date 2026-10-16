"""Editor source handoff never admits stale evidence or changes chat approvals."""
import asyncio
import hashlib
from dataclasses import replace
from queue import Queue

import pytest
from fastapi import HTTPException
from app import bridge, db, lea_status, lea_status_store, overleaf_source, store, uploads
from app.config import LeaConfig
from app.routes import runs
from lea import agent
from lea.events import Finished
from lea.providers import Done, TextDelta, Usage
from lea.status_reporting import LeaStatusUpdateRequested
from test_lea_status import bundle, payload


@pytest.fixture
def target(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "handoff.sqlite3")
    db.init_db()
    cfg = LeaConfig(model="test", max_turns=3, lea_root=tmp_path, max_spend_usd=None)
    monkeypatch.setattr(runs, "load_config", lambda: cfg)
    monkeypatch.setattr(runs.bridge, "enqueue_run", lambda *_: None)
    from app import config
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    original = store.create_run_bundle(message="Formalize target", session_id=None, project_id=None,
        session_origin="overleaf", session_origin_url=None, model="test", provider=None,
        max_turns=3, autonomous=True, purpose="overleaf_solver", source_bundle=bundle(),
        new_formalization=dict(origin="overleaf", origin_key="doc:theorem:target",
                              display_title="target", declaration_name="target"))
    store.update_run(original["run"]["id"], "cancelled")
    return original, cfg


def refresh(original, source=None, **kwargs):
    reference = overleaf_source.new_request(original["formalization"]["id"])
    overleaf_source.respond(reference, source or bundle(), document_id="doc", **kwargs)
    return reference


def chat(original, reference=None, **kwargs):
    return runs.RunRequest(message="Inspect the missing case", session_id=original["session"]["id"],
        focus_formalization_id=original["formalization"]["id"], source_context_ref=reference, **kwargs)


def test_prepare_waits_for_matching_extension_response_without_a_run(target):
    original, _ = target
    async def drive():
        task = asyncio.create_task(overleaf_source.prepare(original["formalization"]["id"]))
        await asyncio.sleep(0)
        pending = overleaf_source.pending()
        assert pending == [dict(request_id=pending[0]["request_id"],
            formalization_id=original["formalization"]["id"], overleafProjectId="doc",
            targetKind="theorem", targetLabel="target")]
        overleaf_source.respond(pending[0]["request_id"], bundle(), document_id="doc")
        result = await task
        assert result["source_context_ref"] == pending[0]["request_id"]
        assert result["source_bundle_hash"] == bundle()["bundleHash"]
    asyncio.run(drive())
    with db.connect() as conn:
        assert conn.execute("select count(*) from runs").fetchone()[0] == 1


@pytest.mark.parametrize("problem", ["document", "target", "hash", "expired"])
def test_bad_response_never_becomes_source(target, problem):
    original, _ = target
    reference = overleaf_source.new_request(original["formalization"]["id"])
    source = bundle()
    if problem == "target":
        source["targetKey"] = "other"
        from app.source_context import source_hash
        source["bundleHash"] = source_hash(source)
        source["sourceIdentityHash"] = source_hash(source, identity=True)
    if problem == "hash": source["proof"] = "changed without hashing"
    if problem == "expired":
        with db.write() as conn:
            conn.execute("update overleaf_source_requests set expires_at = '2000' where id = ?", (reference,))
    with pytest.raises(ValueError):
        overleaf_source.respond(reference, source, document_id="other" if problem == "document" else "doc")
    with db.connect() as conn:
        assert conn.execute("select state from overleaf_source_requests where id = ?", (reference,)).fetchone()[0] == "pending"


def test_acquisition_error_and_timeout_leave_no_chat_run(target, monkeypatch):
    original, _ = target
    async def unavailable():
        task = asyncio.create_task(overleaf_source.prepare(original["formalization"]["id"]))
        await asyncio.sleep(0)
        request = overleaf_source.pending()[0]
        overleaf_source.respond(request["request_id"], None, "Target was deleted.", "doc")
        with pytest.raises(ValueError, match="deleted"): await task
    asyncio.run(unavailable())
    monkeypatch.setattr(overleaf_source, "REFRESH_SECONDS", 0.01)
    with pytest.raises(TimeoutError):
        asyncio.run(overleaf_source.prepare(original["formalization"]["id"]))
    assert overleaf_source.pending() == []


def test_chat_admits_frozen_context_and_replays_after_reference_expiry(target):
    original, _ = target
    reference = refresh(original)
    request = chat(original, reference, idempotency_key="chat-once")
    first = runs.create_run(request)
    row = store.get_run(first["run_id"])
    assert row["purpose"] == "overleaf_continuation" and not row["autonomous"]
    assert row["source_context_ref"] == reference
    from app.source_context import validate_source
    assert lea_status_store.context(row["id"])["source_bundle"] == validate_source(bundle())
    with db.write() as conn:
        conn.execute("update overleaf_source_requests set expires_at = '2000' where id = ?", (reference,))
    assert runs.create_run(request)["run_id"] == first["run_id"]


def test_newer_source_rejects_old_snapshot_at_admission_and_dispatch(target, monkeypatch):
    original, cfg = target
    reference = refresh(original)
    admitted = runs.create_run(chat(original, reference))
    refresh(original, bundle("The missing case is now justified."))
    with pytest.raises(HTTPException) as error:
        runs.create_run(chat(original, reference))
    assert error.value.status_code == 409
    with pytest.raises(ValueError, match="changed before execution"):
        overleaf_source.validate_run(admitted["run_id"], cfg.lea_root / "workspace" / "proofs")
    monkeypatch.setattr(bridge, "run_events", lambda *_args, **_kwargs: pytest.fail("Stale source must never reach the model"))
    bridge.run_lea(bridge.RunnerContext(session_id=admitted["session_id"], run_id=admitted["run_id"],
        task="Inspect", config=cfg, events=Queue(), purpose="overleaf_continuation"))
    row = store.get_run(admitted["run_id"])
    assert row["status"] == "failed" and row["stop_reason"] == "source_changed"
    assert row["cost_usd"] == 0 and row["usage_status"] == "final"
    assert lea_status_store.context(admitted["run_id"])["source_bundle"]["proof"] == "By triviality."


def test_mirror_change_rejects_snapshot_even_when_target_identity_is_same(target):
    original, cfg = target
    project = store.create_project("handoff-project", "Handoff project", "Lea.Handoff")
    with db.write() as conn:
        conn.execute("update formalizations set project_id = ?, loose_session_id = null where id = ?", (project["id"], original["formalization"]["id"]))
    files = {"main.tex": "Current statement\r\nand proof\r\n", "defs.tex": "dependency"}
    base = uploads.overleaf_dir(project, cfg.lea_root / "workspace" / "proofs")
    base.mkdir(parents=True)
    for path, content in files.items():
        (base / path).write_bytes(content.encode())
    source = bundle()
    source["mirror"] = dict(revision="revision", mirroredFileCount=2, verified=True)
    from app.source_context import source_hash
    source["bundleHash"] = source_hash(source)
    hashes = {path: hashlib.sha256(content.encode()).hexdigest() for path, content in files.items()}
    reference = refresh(original, source, mirrored_files=hashes)
    with db.connect() as conn:
        assert overleaf_source.resolve(conn, reference, original["formalization"]["id"])["bundleHash"] == source["bundleHash"]
    (base / "defs.tex").write_text("edited dependency")
    with db.connect() as conn, pytest.raises(ValueError, match="changed"):
        overleaf_source.resolve(conn, reference, original["formalization"]["id"])


def project_retry(target, monkeypatch):
    from app import projects, reformalizations as ops, runregistry, runtime
    from app.routes import projects as project_routes
    from lea import execution

    original, cfg = target
    proofs = cfg.lea_root / "workspace" / "proofs"
    project = projects.ensure_project("doc", proofs, title="Study", namespace="Lea.Study")
    monkeypatch.setattr(project_routes, "load_config", lambda: cfg)
    monkeypatch.setattr(execution, "capability", lambda: {"available": True})
    monkeypatch.setattr(runtime, "accepting_work", lambda: True)
    monkeypatch.setattr(runregistry, "registry", runregistry.RunRegistry(max_concurrent=2))
    with db.write() as conn:
        conn.execute("update formalizations set project_id=?,loose_session_id=null where id=?",
                     (project["id"], original["formalization"]["id"]))
        conn.execute("update sessions set project_id=? where id=?", (project["id"], original["session"]["id"]))
    repo = projects.project_repo_dir(project, proofs)
    (repo / "target.lean").write_text("namespace Lea.Study\ntheorem target : True := by trivial\nend Lea.Study\n")
    (repo / "dependent.lean").write_text("import Lea.Study.target\nnamespace Lea.Study\ntheorem dependent : True := target\nend Lea.Study\n")
    store.link_formalization_file(original["formalization"]["id"], "target.lean", "primary")
    uploads.sync_overleaf_tex(project, proofs, [{"path": "main.tex", "content": "Original source"}], commit=False)
    op = ops.enqueue(dict(message="Retry target", project_slug="doc", purpose="overleaf_solver",
        session_id=original["session"]["id"], focus_formalization_id=original["formalization"]["id"]), project["id"])
    return original, project, repo, op


@pytest.mark.parametrize("changed_source", [False, True])
def test_reformalization_refresh_mirrors_source_before_starting_solver(target, monkeypatch, changed_source):
    import time
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import reformalizations as ops
    from app.routes import projects as project_routes, overleaf_source as source_routes
    from app.source_context import source_hash

    original, project, repo, op = project_retry(target, monkeypatch)
    app = FastAPI()
    app.include_router(project_routes.router)
    app.include_router(source_routes.router)
    client = TestClient(app)
    admitted = []
    monkeypatch.setattr(bridge, "enqueue_run", admitted.append)
    before = {p.name: p.read_bytes() for p in repo.glob("*.lean")}
    ops.dispatch(project["id"])
    try:
        deadline = time.monotonic() + 5
        while not overleaf_source.pending() and time.monotonic() < deadline:
            time.sleep(0.01)
        request = overleaf_source.pending()[0]
        assert ops.actor() is None  # The editor's HTTP request has no solver identity.
        assert ops.owner(project["id"])["id"] == op["id"]
        content = "Updated source" if changed_source else "Original source"
        mirror = client.post("/api/projects/by-slug/doc/mirror", json={
            "files": [{"path": "main.tex", "content": content}]})
        assert mirror.status_code == 200, mirror.text
        assert mirror.json()["changed"] is changed_source
        assert (repo / ".lea/files/overleaf/main.tex").read_text() == content
        # A mirror refresh must not grant access to canonical proof writes.
        blocked = client.put(f"/api/projects/{project['id']}/file",
            json={"path": "dependent.lean", "content": "overwritten"})
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["error"] == "project_busy"
        source = bundle("Updated proof." if changed_source else "By triviality.")
        source["mirror"] = dict(revision="a" * 64, mirroredFileCount=1, verified=True)
        source["bundleHash"] = source_hash(source)
        response = client.post(f"/api/overleaf/source-requests/{request['request_id']}", json={
            "document_id": "doc", "source_bundle": source,
            "mirrored_files": {"main.tex": hashlib.sha256(content.encode()).hexdigest()}})
        assert response.status_code == 200, response.text
        while not admitted and ops.get(op["id"])["state"] == "active" and time.monotonic() < deadline:
            time.sleep(0.01)
        current = ops.get(op["id"])
        assert current["state"] == "active", current.get("result")
        assert admitted == [current["root_run_id"]] and current["root_run_id"]
        run = store.get_run(current["root_run_id"])
        assert run["session_id"] == original["session"]["id"]
        assert run["focus_formalization_id"] == original["formalization"]["id"]
        assert run["reformalization_id"] == op["id"]
        assert lea_status_store.context(run["id"])["source_bundle"]["proof"] == source["proof"]
        assert {p.name: p.read_bytes() for p in repo.glob("*.lean")} == before
        # The exception ends when the pending acquisition is settled.
        assert client.post("/api/projects/by-slug/doc/mirror", json={
            "files": [{"path": "main.tex", "content": content}]}).status_code == 409
    finally:
        if overleaf_source.pending():
            overleaf_source.respond(overleaf_source.pending()[0]["request_id"], None,
                                    "Test ended source acquisition.", "doc")
        ops.cancel(op["id"])


@pytest.mark.parametrize("phase", ["no_request", "other_target", "expired", "solver", "continuation", "applying"])
def test_refresh_mirror_access_is_limited_to_pending_owner_acquisition(target, monkeypatch, phase):
    from fastapi import BackgroundTasks
    from app import reformalizations as ops
    from app.routes import projects as project_routes

    original, project, _, op = project_retry(target, monkeypatch)
    ops.update(op["id"], "applying" if phase == "applying" else "active")
    if phase != "no_request":
        fid = original["formalization"]["id"]
        if phase == "other_target":
            other = store.create_formalization(project_id=project["id"], loose_session_id=None,
                display_title="Other", origin="overleaf", origin_key="doc:theorem:other")
            with db.write() as conn:
                overleaf_source.bind(conn, other, {**bundle(), "targetKey": "other"})
            fid = other["id"]
        reference = overleaf_source.new_request(fid)
        with db.write() as conn:
            if phase == "expired":
                conn.execute("update overleaf_source_requests set expires_at='2000' where id=?", (reference,))
            if phase in {"solver", "continuation"}:
                conn.execute("update reformalization_operations set root_run_id=? where id=?", (original["run"]["id"], op["id"]))
        if phase == "continuation":
            ops.update(op["id"], result={"continuation": {"old_run_id": original["run"]["id"]}})
    request = project_routes.MirrorRequest(files=[project_routes.MirrorFile(path="main.tex", content="Original source")])
    if phase == "continuation":
        assert project_routes.mirror_overleaf_tex("doc", request, BackgroundTasks())["changed"] is False
    else:
        with pytest.raises(HTTPException) as error:
            project_routes.mirror_overleaf_tex("doc", request, BackgroundTasks())
        assert error.value.status_code == 409
    assert ops.actor() is None


def test_refresh_background_commit_keeps_owner_after_snapshot_is_ready(target, monkeypatch):
    from fastapi import BackgroundTasks
    from app import reformalizations as ops
    from app.routes import projects as project_routes

    original, project, repo, op = project_retry(target, monkeypatch)
    ops.update(op["id"], "active")
    reference = overleaf_source.new_request(original["formalization"]["id"])
    tasks = BackgroundTasks()
    project_routes.mirror_overleaf_tex("doc", project_routes.MirrorRequest(
        files=[project_routes.MirrorFile(path="main.tex", content="Updated source")]), tasks)
    overleaf_source.respond(reference, bundle(), document_id="doc")
    with db.write() as conn:
        conn.execute("update reformalization_operations set root_run_id=? where id=?",
                     (original["run"]["id"], op["id"]))
    assert ops.source_refresh_owner(project["id"]) is None
    assert ops.actor() is None
    asyncio.run(tasks())
    import subprocess
    assert subprocess.check_output(["git", "-C", str(repo), "show", "HEAD:.lea/files/overleaf/main.tex"],
                                   text=True) == "Updated source"
    assert ops.actor() is None


def test_interactive_tool_publication_is_durable_and_approval_gate_is_preserved(target, monkeypatch):
    original, cfg = target
    result = runs.create_run(chat(original, refresh(original)))
    events = Queue()
    def fake(config, messages, **options):
        assert config.prompt_variant == "interactive_overleaf"
        assert config.status_reporting and "update_lea_status" in config.extra_tools
        assert "safe_verify" in config.extra_tools
        assert options["gate"] is not None
        ack = yield LeaStatusUpdateRequested(payload(), "chat-status")
        assert ack.accepted and lea_status_store.get(ack.update_id)
        yield Finished("assistant", "The boundary case needs justification.", 1,
            result["session_id"], "test", Usage(1, 1), 0, {})
    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(bridge.RunnerContext(session_id=result["session_id"], run_id=result["run_id"],
        task="Inspect the missing case", config=cfg, events=events, purpose="overleaf_continuation"))
    assert len(lea_status.history(original["formalization"]["id"])["updates"]) == 1
    assert any(event["type"] == "lea_status_updated" for event in list(events.queue))


def test_general_chat_does_not_inherit_reporting_from_overleaf_history(target, monkeypatch):
    original, cfg = target
    result = runs.create_run(chat(original))
    def fake(config, messages, **options):
        assert not config.status_reporting and "update_lea_status" not in config.extra_tools
        assert options["gate"] is not None
        yield Finished("assistant", "An explanation.", 1, result["session_id"], "test", Usage(1, 1), 0, {})
    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(bridge.RunnerContext(session_id=result["session_id"], run_id=result["run_id"],
        task="Explain", config=cfg, events=Queue()))
    assert lea_status_store.context(result["run_id"]) is None
    assert lea_status.history(original["formalization"]["id"])["updates"] == []


def test_discussion_turn_keeps_status_tool_available_without_publishing(target, monkeypatch):
    original, cfg = target
    captured = []
    def provider(*args, **kwargs):
        captured.extend(tool["name"] for tool in args[3])
        yield TextDelta("Here is why the boundary case matters.")
        yield Done(Usage(2, 2), 0)
    monkeypatch.setattr(agent, "_provider_stream", provider)
    monkeypatch.setattr(agent, "_classify_intent", lambda *_: ("ASSISTANT", Usage(0, 0), 0))
    config = replace(cfg, prompt_variant="interactive_overleaf", status_reporting=True,
                     status_context={"source_bundle": bundle()}, extra_tools=["update_lea_status"])
    events = list(agent.run_events(config, [dict(role="assistant", content="Previous proof work."),
        dict(role="user", content="Explain the boundary case.")], working_dir=str(cfg.lea_root)))
    assert "update_lea_status" in captured and "read_file" in captured
    assert events[-1].reason == "assistant"
    assert not any(isinstance(event, LeaStatusUpdateRequested) for event in events)
