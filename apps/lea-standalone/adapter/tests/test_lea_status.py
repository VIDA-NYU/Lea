"""Durable live status: admission, ordering, replay, revisions and interruptions."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import pytest
from app import db, store, lea_status, lea_status_store
from app.source_context import source_hash
from lea.status_reporting import TOOL_SCHEMA, validate_payload, merge_assessment, attention


def test_version_2_source_hash_matches_browser_golden_fixture():
    fixture_path = Path(__file__).resolve().parents[3] / "overleaf-extension/tests/fixtures/sourceIdentityV2.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    source = fixture["bundle"]
    assert source_hash(source) == fixture["bundleHash"]
    assert source_hash(source, identity=True) == fixture["sourceIdentityHash"]


def bundle(proof="By triviality."):
    value = dict(version=2, targetKey="target", targetKind="theorem", statement="True.", proof=proof,
        proofAssociation=dict(status="associated", method="adjacent", sourceFile="main.tex", proofHash=""),
        statementLocation=dict(sourceFile="main.tex", sourceStartLine=1, sourceEndLine=4),
        uses=[], context="", relevantSource=[], mirror=None)
    value["bundleHash"] = source_hash(value)
    value["sourceIdentityHash"] = source_hash(value, identity=True)
    return value


def payload(**changes):
    return dict(kind="initial", confidence="medium", summary="Examining the supplied argument.",
        scope="source_only", confidence_reason="The statement is clear; the argument remains unchecked.", **{}) | changes


def finding(**changes):
    return dict(key="gap", severity="warning", category="source_gap", title="Missing case",
        detail="The boundary case is omitted.", lean_resolution="planned", source_resolution="open",
        correction="Prove the boundary case explicitly.") | changes


def test_tool_contract_reserves_blocking_for_semantic_changes():
    description = TOOL_SCHEMA["description"]
    assert "missing source proof" in description
    assert "is not blocking" in description
    assert "pauses only when this run's pause policy allows it" in description
    assert "Reserve blocking findings for a required semantic change" in description
    assert "Author-authorized best-effort continuation" in description
    assert "non-blocking warning instead of blocking again" in description


def admit(source=None, form=None, session=None, purpose="overleaf_solver", project=None, allow_source_pause=False):
    return store.create_run_bundle(message="Formalize target", session_id=session, project_id=project,
        session_origin="overleaf", session_origin_url=None, model="test", provider=None,
        max_turns=3, autonomous=True, purpose=purpose, source_bundle=source or bundle(),
        allow_source_pause=allow_source_pause,
        focus_formalization_id=form,
        new_formalization=None if form else dict(origin="overleaf", origin_key="doc:theorem:target", display_title="target", declaration_name="target"))


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "status.sqlite3")
    db.init_db()
    return admit()


def ids(fresh):
    return fresh["run"]["id"], fresh["formalization"]["id"], fresh["session"]["id"]


def test_source_only_publication_is_durable_and_idempotent(fresh):
    run, form, _ = ids(fresh)
    first, event = lea_status.publish(run, "call", payload())
    second, _ = lea_status.publish(run, "call", payload())
    assert first == second
    assert first["artifact_snapshot"]["files"] == []
    assert event["sequence"] == 1
    assert lea_status.current(form, bundle()["sourceIdentityHash"], bundle()["bundleHash"])["freshness"] == "current"
    assert lea_status_store.context(run)["source_bundle"]["proof"] == "By triviality."
    db.init_db()
    assert lea_status_store.get(first["id"]) == first


def test_invalid_payload_consumes_no_sequence(fresh):
    run, _, _ = ids(fresh)
    for changes in (dict(run_id="spoof"), dict(summary="x" * 501), dict(confidence="approved"), dict(kind="final")):
        with pytest.raises(ValueError):
            lea_status.publish(run, "bad", payload(**changes))
    assert lea_status.publish(run, "good", payload())[0]["sequence"] == 1


def test_concurrent_writes_and_paginated_history(fresh):
    run, form, _ = ids(fresh)
    with ThreadPoolExecutor(max_workers=6) as pool:
        updates = list(pool.map(lambda i: lea_status.publish(run, str(i), payload(summary=str(i)))[0], range(12)))
    assert sorted(u["sequence"] for u in updates) == list(range(1, 13))
    page = lea_status.history(form, limit=5)
    assert page["has_more"] and len(page["updates"]) == 5
    assert lea_status.history(form, page["next_cursor"], 5)["updates"][0]["sequence"] == 6
    with pytest.raises(ValueError):
        lea_status.history(form, "invalid")


def test_new_generation_beats_late_old_updates(fresh):
    run, form, session = ids(fresh)
    lea_status.publish(run, "one", payload())
    newer = admit(form=form, session=session)
    lea_status.publish(run, "late", payload(summary="Old run"))
    assert lea_status.current(form)["run_id"] == newer["run"]["id"]
    assert lea_status.current(form)["assessment"] is None
    assert lea_status.current(form)["previous_assessment"] is not None


def test_revision_freshness_and_terminal_retention(fresh):
    run, form, session = ids(fresh)
    source = bundle()
    lea_status.publish(run, "one", payload())
    assert lea_status.current(form, bundle("New proof")["sourceIdentityHash"])["freshness"] == "source_changed"
    store.link_formalization_file(form, "target.lean", "primary")
    store.add_code_step(session, run, "target.lean", content="theorem target : True := by trivial", formalization_id=form)
    assert lea_status.current(form, source["sourceIdentityHash"])["freshness"] == "newer_artifact"
    updated, _ = lea_status.publish(run, "two", payload(scope="partial_artifact"))
    assert updated["artifact_snapshot"]["files"][0]["blob_id"]
    store.update_run(run, "cancelled", stop_reason="user_stop", recoverable=True)
    current = lea_status.current(form, source["sourceIdentityHash"])
    assert current["reporting_incomplete"] and current["activity"]["recoverable"]
    assert current["latest_update"]["id"] == updated["id"]
    assert lea_status.publish(run, "two", payload(scope="partial_artifact"))[0]["id"] == updated["id"]
    with pytest.raises(ValueError):
        lea_status.publish(run, "late", payload())


def test_wrong_source_target_rolls_back_admission(fresh):
    source = bundle()
    source["targetKey"] = "other"
    source["bundleHash"] = source_hash(source)
    source["sourceIdentityHash"] = source_hash(source, identity=True)
    with db.connect() as conn:
        before = conn.execute("select count(*) from runs").fetchone()[0]
    with pytest.raises(ValueError, match="different target"):
        admit(source)
    with db.connect() as conn:
        assert conn.execute("select count(*) from runs").fetchone()[0] == before


def test_material_findings_survive_progress_and_lean_repairs():
    initial = merge_assessment(None, payload(finding_updates=[finding()]), "old")
    repaired = merge_assessment(initial, payload(finding_updates=[finding(lean_resolution="applied")]), "old")
    assert attention(repaired) == "source_issue"
    assert merge_assessment(repaired, payload(), "old")["findings"] == repaired["findings"]
    resolution = finding(lean_resolution="applied", source_resolution="resolved", resolution_explanation="Author added the missing case.")
    with pytest.raises(ValueError, match="earlier source revision"):
        merge_assessment(repaired, payload(finding_updates=[resolution]), "old")
    assert attention(merge_assessment(repaired, payload(finding_updates=[resolution]), "new")) == "none"
    assert attention(merge_assessment(initial, payload(finding_updates=[finding(severity="blocking")]), "old")) == "needs_author_input"


def test_legacy_missing_proof_blocker_can_be_reassessed_as_non_blocking():
    blocked = merge_assessment(None, payload(finding_updates=[finding(severity="blocking")]), "old")
    reassessed = merge_assessment(blocked, payload(finding_updates=[finding(
        severity="warning",
        resolution_explanation="A missing proof alone is non-blocking because the statement is precise.",
    )]), "old")
    assert attention(reassessed) == "source_issue"


def test_bridge_acknowledges_committed_update_before_next_effect(fresh, tmp_path, monkeypatch):
    from pathlib import Path
    from queue import Queue
    from app import bridge
    from app.config import LeaConfig
    from lea.interface import FileChanged, Finished, TurnStarted
    from lea.providers import Usage
    from lea.status_reporting import LeaStatusUpdateRequested, LeaStatusUpdateAck
    run, form, session = ids(fresh)
    events = Queue()
    context = bridge.RunnerContext(session_id=session, run_id=run, task="Formalize target",
        config=LeaConfig(model="test", lea_root=tmp_path, max_turns=3), events=events,
        autonomous=True, purpose="overleaf_solver")
    observed = []
    def fake(config, messages, **kwargs):
        assert config.status_reporting and "update_lea_status" in config.extra_tools
        assert config.status_context["source_bundle"]["targetKey"] == "target"
        yield TurnStarted(1)
        first = yield LeaStatusUpdateRequested(payload(), "initial")
        assert isinstance(first, LeaStatusUpdateAck) and first.accepted
        assert lea_status_store.get(first.update_id)
        assert any(e["type"] == "lea_status_updated" for e in list(events.queue))
        path = Path(kwargs["working_dir"]) / "target.lean"
        path.write_text("theorem target : True := by trivial")
        yield FileChanged(str(path))
        second = yield LeaStatusUpdateRequested(payload(scope="partial_artifact"), "partial")
        observed.append(second)
        assert second.accepted
        row = lea_status_store.get(second.update_id)
        assert row["artifact_snapshot"]["files"][0]["blob_id"]
        yield Finished("interrupted", "Paused", 1, session, "test", Usage(1, 1), 0, {})
    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(context)
    assert len(observed) == 1
    assert len(lea_status.history(form)["updates"]) == 2


def test_status_retires_deleted_support_without_losing_the_target(fresh, tmp_path, monkeypatch):
    from queue import Queue
    from app import bridge
    from app.config import LeaConfig
    from lea.interface import Finished
    from lea.providers import Usage
    from lea.status_reporting import LeaStatusUpdateRequested

    run, form, session = ids(fresh)
    observed = []

    def fake(config, messages, **kwargs):
        repo = Path(kwargs["working_dir"])
        primary = repo / "target.lean"
        primary.write_text("theorem target : True := by trivial")
        store.link_formalization_file(form, primary.name, "primary")
        store.add_code_step(session, run, primary.name, content=primary.read_text(), formalization_id=form)
        store.link_formalization_file(form, "retired.lean", "support")
        store.add_code_step(session, run, "retired.lean", content="def oldHelper := 1", formalization_id=form)
        ack = yield LeaStatusUpdateRequested(payload(scope="statement_and_proof"), "after-retirement")
        observed.append(ack)
        yield Finished("interrupted", "Done", 1, session, "test", Usage(), 0, {})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(bridge.RunnerContext(
        session_id=session, run_id=run, task="Formalize target",
        config=LeaConfig(model="test", lea_root=tmp_path, max_turns=3), events=Queue(),
        autonomous=True, purpose="overleaf_solver"))

    assert observed[0].accepted
    assert [row["path"] for row in store.list_formalization_files(form)] == ["target.lean"]
    update = lea_status_store.latest(run)
    assert [item["path"] for item in update["artifact_snapshot"]["files"]] == ["target.lean"]


def test_source_obstruction_pause_is_opt_in(fresh, tmp_path, monkeypatch):
    from queue import Queue
    from app import bridge
    from app.config import LeaConfig
    from lea.interface import Finished
    from lea.providers import Usage
    from lea.status_reporting import LeaStatusUpdateRequested

    initial_run, form, session = ids(fresh)
    observed = []

    def fake(config, messages, **kwargs):
        ack = yield LeaStatusUpdateRequested(payload(finding_updates=[finding(severity="blocking")]), "blocker")
        observed.append(ack)
        yield Finished("interrupted", "Stopped", 1, session, "test", Usage(), 0, {})

    monkeypatch.setattr(bridge, "run_events", fake)
    for run_id, allowed in ((initial_run, False),
                            (admit(form=form, session=session, allow_source_pause=True)["run"]["id"], True)):
        bridge.run_lea(bridge.RunnerContext(
            session_id=session, run_id=run_id, task="Formalize target",
            config=LeaConfig(model="test", lea_root=tmp_path, max_turns=3), events=Queue(),
            autonomous=True, purpose="overleaf_solver", allow_source_pause=allowed))
    assert observed[0].accepted and observed[0].stop_reason is None
    assert observed[1].accepted and observed[1].stop_reason == "source_obstruction"
    assert lea_status.history(form)["updates"][-1]["assessment"]["findings"][0]["severity"] == "blocking"


def test_continuation_activates_only_when_it_reports_or_edits(fresh):
    run, form, session = ids(fresh)
    lea_status.publish(run, "one", payload())
    continuation = admit(form=form, session=session, purpose="overleaf_continuation")["run"]["id"]
    assert lea_status.current(form)["run_id"] == run
    lea_status.publish(continuation, "two", payload())
    assert lea_status.current(form)["run_id"] == continuation


def test_material_finding_can_be_reassessed_then_resolved_on_changed_source():
    initial = merge_assessment(None, payload(finding_updates=[finding()]), "old")
    reassessed = merge_assessment(initial, payload(finding_updates=[finding()]), "new")
    resolved = merge_assessment(reassessed, payload(finding_updates=[finding(source_resolution="resolved", resolution_explanation="Verified author's new case.")]), "new")
    assert attention(resolved) == "none"


@pytest.mark.parametrize("fail_delivery", [False, True])
def test_bridge_storage_failure_pauses_material_work_but_delivery_failure_is_accepted(fresh, tmp_path, monkeypatch, fail_delivery):
    from queue import Queue
    from app import bridge
    from app.config import LeaConfig
    from lea.interface import Finished
    from lea.providers import Usage
    from lea.status_reporting import LeaStatusUpdateRequested
    run, form, session = ids(fresh)
    ctx = bridge.RunnerContext(session_id=session, run_id=run, task="Formalize target",
        config=LeaConfig(model="test", lea_root=tmp_path, max_turns=3), events=Queue(),
        autonomous=True, purpose="overleaf_solver")
    seen = []
    attempts = []
    if fail_delivery:
        original_emit = bridge.emit
        def emit(events, kind, body):
            if kind == "lea_status_updated":
                raise OSError("simulated disconnected stream")
            return original_emit(events, kind, body)
        monkeypatch.setattr(bridge, "emit", emit)
    else:
        def publish(*args):
            attempts.append(args[1])
            raise OSError("simulated full disk")
        monkeypatch.setattr(lea_status, "publish", publish)
    def fake(*args, **kwargs):
        ack = yield LeaStatusUpdateRequested(payload(finding_updates=[finding()]), "material")
        seen.append(ack)
        yield Finished(ack.stop_reason or "interrupted", "Paused", 1, session, "test", Usage(), 0, {})
    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)
    assert len(seen) == 1
    assert seen[0].accepted is fail_delivery
    if fail_delivery:
        assert len(lea_status.history(form)["updates"]) == 1
    else:
        assert attempts == ["material", "material"]
        assert seen[0].stop_reason == "status_reporting_failed"
        assert store.get_run(run)["recoverable"]
        assert lea_status.history(form)["updates"] == []


def test_concurrent_run_assesses_its_own_revision_not_another_sessions_bytes(fresh):
    run, form, session = ids(fresh)
    store.link_formalization_file(form, "target.lean", "primary")
    own = store.add_code_step(session, run, "target.lean", content="theorem target : True := by trivial", formalization_id=form)
    other = store.create_session("Other conversation", origin="overleaf")
    store.link_session_formalization(other["id"], form)
    store.add_code_step(other["id"], None, "target.lean", content="theorem target : True := by\n  trivial", formalization_id=form)
    update, _ = lea_status.publish(run, "own", payload(scope="partial_artifact"))
    assert update["artifact_snapshot"]["files"][0]["code_step_id"] == str(own["id"])
    assert lea_status.current(form, bundle()["sourceIdentityHash"], bundle()["bundleHash"])["freshness"] == "newer_artifact"


def test_dependency_freshness_and_deliberate_project_cleanup(fresh):
    project = store.create_project("status-project")
    linked = admit(project=project["id"])
    run, form, session = ids(linked)
    dependency = store.create_formalization(project_id=project["id"], loose_session_id=None, display_title="dependency")
    store.link_formalization_file(dependency["id"], "Dep.lean", "primary")
    store.add_code_step(session, None, "Dep.lean", content="theorem dep : True := by trivial", formalization_id=dependency["id"])
    store.upsert_artifact(project_id=project["id"], session_id=session, run_id=None, declaration_name="dep", kind="theorem",
        path="Dep.lean", module_name="Lea.StatusProject.Dep", formalization_id=dependency["id"])
    store.link_formalization_file(form, "target.lean", "primary")
    store.add_code_step(session, run, "target.lean", content="import Lea.StatusProject.Dep\ntheorem target : True := by exact dep", formalization_id=form)
    update, _ = lea_status.publish(run, "first", payload(scope="partial_artifact"))
    assert any(f["role"] == "dependency" for f in update["artifact_snapshot"]["files"])
    store.add_code_step(session, None, "Dep.lean", content="theorem dep : True := by\n  trivial", formalization_id=dependency["id"])
    assert lea_status.current(form, bundle()["sourceIdentityHash"], bundle()["bundleHash"])["freshness"] == "dependency_changed"
    assert store.delete_project_cascade(project["id"])
    assert lea_status_store.context(run) is None
    assert lea_status_store.get(update["id"]) is None
    assert lea_status_store.context(ids(fresh)[0]) is not None
