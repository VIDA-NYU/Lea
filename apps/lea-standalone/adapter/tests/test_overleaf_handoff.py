"""Editor source handoff never admits stale evidence or changes chat approvals."""
import asyncio
import hashlib
from dataclasses import replace
from queue import Queue
from published_source_fixtures import publish_for

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
    return publish_for(original['formalization']['id'],source or bundle(),**kwargs)


def chat(original, reference=None, **kwargs):
    return runs.RunRequest(message="Inspect the missing case", session_id=original["session"]["id"],
        focus_formalization_id=original["formalization"]["id"], source_context_ref=reference, **kwargs)


def test_prepare_looks_up_published_source_without_browser_or_run(target):
    original,_=target
    with pytest.raises(ValueError,match='No published source'):
        asyncio.run(overleaf_source.prepare(original['formalization']['id']))
    ref=refresh(original)
    assert asyncio.run(overleaf_source.prepare(original['formalization']['id']))['source_context_ref']==ref
    with db.connect() as conn:
        assert conn.execute('select count(*) from runs').fetchone()[0]==1
        assert conn.execute('select count(*) from overleaf_source_requests').fetchone()[0]==0


@pytest.mark.parametrize('problem',['document','target','hash','expired'])
def test_bad_publication_never_becomes_source(target,problem):
    from app import source_publications as source
    original,_=target
    value=bundle()
    source.publisher(source.PublisherRequest(document_id='doc',client_id='fixture'))
    document='other' if problem=='document' else 'doc'
    if problem=='target':
        value['targetKey']='other'
        from app.source_context import source_hash
        value['bundleHash']=source_hash(value)
        value['sourceIdentityHash']=source_hash(value,identity=True)
    if problem=='hash':value['proof']='changed without hashing'
    if problem=='expired':
        with db.write() as conn:conn.execute("update source_publishers set lease_until='2000'")
    with pytest.raises((HTTPException,ValueError)):
        source.publish(source.PublicationRequest(document_id=document,client_id='fixture',generation=1,sequence=1,
            formalization_id=original['formalization']['id'],source_bundle=value))
    with db.connect() as conn:assert conn.execute('select count(*) from source_publications').fetchone()[0]==0


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
        conn.execute("update source_publishers set lease_until='2000' where document_id='doc'")
    assert runs.create_run(request)["run_id"] == first["run_id"]


def test_newer_source_rejects_old_snapshot_at_admission_and_dispatch(target, monkeypatch):
    original, cfg = target
    reference = refresh(original)
    admitted = runs.create_run(chat(original, reference))
    refresh(original, bundle("The missing case is now justified."))
    with pytest.raises(HTTPException) as error:
        runs.create_run(chat(original, reference))
    assert error.value.status_code == 409
    with pytest.raises(ValueError, match="changed"):
        overleaf_source.validate_run(admitted["run_id"], cfg.lea_root / "workspace" / "proofs")
    monkeypatch.setattr(bridge, "run_events", lambda *_args, **_kwargs: pytest.fail("Stale source must never reach the model"))
    bridge.run_lea(bridge.RunnerContext(session_id=admitted["session_id"], run_id=admitted["run_id"],
        task="Inspect", config=cfg, events=Queue(), purpose="overleaf_continuation"))
    row = store.get_run(admitted["run_id"])
    assert row["status"] == "failed" and row["stop_reason"] == "source_changed"
    assert row["cost_usd"] == 0 and row["usage_status"] == "final"
    assert lea_status_store.context(admitted["run_id"])["source_bundle"]["proof"] == "By triviality."


def test_mirror_disk_change_does_not_affect_sql_source_admission(target):
    original,cfg=target
    ref=refresh(original,files={'main.tex':'published bytes'})
    (cfg.lea_root/'external.tex').write_text('external change')
    with db.connect() as conn:
        assert overleaf_source.resolve(conn,ref,original['formalization']['id'])['bundleHash']==bundle()['bundleHash']
    refresh(original,files={'main.tex':'changed published bytes'})
    with db.connect() as conn,pytest.raises(ValueError,match='changed'):
        overleaf_source.resolve(conn,ref,original['formalization']['id'])


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
    reference=refresh(original,files={'main.tex':'Original source'})
    op = ops.enqueue(dict(message="Retry target", project_slug="doc", purpose="overleaf_solver",source_context_ref=reference,
        session_id=original["session"]["id"], focus_formalization_id=original["formalization"]["id"]), project["id"])
    return original, project, repo, op


@pytest.mark.parametrize('phase',['active','applying'])
def test_source_archive_publication_remains_sql_only_during_reserved_work(target,monkeypatch,phase):
    from app import reformalizations as ops
    from app.routes import projects as project_routes
    original,project,repo,op=project_retry(target,monkeypatch)
    ops.update(op['id'],phase)
    before=(repo/'.lea/files/overleaf/main.tex').read_text()
    result=project_routes.mirror_overleaf_tex('doc',project_routes.MirrorRequest(files=[project_routes.MirrorFile(path='main.tex',content='Published while reserved')]))
    assert result['materialization']=='deferred_until_worker_binding'
    assert (repo/'.lea/files/overleaf/main.tex').read_text()==before
    reference=refresh(original,files={'main.tex':'New immutable source'})
    with db.connect() as conn:assert conn.execute('select id from source_publications where id=?',(reference,)).fetchone()
    assert ops.actor() is None
    ops.cancel(op['id'])


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
