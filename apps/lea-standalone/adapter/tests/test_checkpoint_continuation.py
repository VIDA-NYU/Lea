"""Ordinary Resume restores execution state without replaying a formalization."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
import hashlib
from published_source_fixtures import publish_for
from queue import Queue
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi import HTTPException

from app import db, store, recovery, lea_status, lea_status_store, bridge, projects
from app.config import LeaConfig
from app.routes import runs
from app.source_context import source_hash
from lea import agent, registry
from lea.events import Finished, ToolCalled
from lea.providers import ToolCall, Done, Usage, TextDelta


def source(statement="True.", proof=""):
    value = dict(version=2, targetKey="target", targetKind="theorem", statement=statement, proof=proof,
        proofAssociation=dict(status="associated" if proof else "missing", method="adjacent" if proof else "none",
                              sourceFile="main.tex", proofHash=hashlib.sha256(proof.encode()).hexdigest() if proof else ""),
        statementLocation=dict(sourceFile="main.tex", sourceStartLine=1, sourceEndLine=4),
        uses=[], context="", relevantSource=[], mirror=None)
    value["bundleHash"] = source_hash(value)
    value["sourceIdentityHash"] = source_hash(value, identity=True)
    return value


@pytest.fixture
def paused(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "continuations.sqlite3")
    db.init_db()
    cfg = LeaConfig(model="test", max_turns=3, lea_root=tmp_path, max_spend_usd=None,
                    prompt_variant="overleaf_faithful", status_reporting=True, narrate_tool_steps=False,
                    extra_tools=["update_lea_status"])
    monkeypatch.setattr(runs, "load_config", lambda: cfg)
    monkeypatch.setattr(runs.bridge, "enqueue_run", lambda *_: None)
    monkeypatch.setattr(projects, "repo_for_session", lambda *_: tmp_path)
    bundle = store.create_run_bundle(message="Formalize target", session_id=None, project_id=None,
        session_origin="overleaf", session_origin_url=None, model="test", provider=None,
        max_turns=3, autonomous=True, purpose="overleaf_solver", source_bundle=source(),
        new_formalization=dict(origin="overleaf", origin_key="doc:theorem:target",
                              display_title="target", declaration_name="target"))
    old = bundle["run"]
    assert store.claim_pending_run(old["id"])
    attempt = recovery.start_attempt(old["id"])
    (tmp_path / "Target.lean").write_text("theorem target : True := by sorry\n")
    saved = dict(stage="turn_boundary", turn=2, max_turns=3, max_cost_usd=None,
        input_tokens=100, output_tokens=40, cost_usd=0.25, reporting_stop="source_obstruction",
        last_input_tokens=140, surfaced_domains=["algebra"], assistant_mode=False,
        messages=[{"role": "user", "content": "Formalize target"},
                  {"role": "assistant", "content": [{"type": "tool_use", "id": "write-1",
                      "name": "write_file", "input": {"path": "Target.lean", "content": "partial proof"}}]},
                  {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "write-1", "content": "Written"}]}],
        proof_state=dict(latest_proof_path=str(tmp_path / "Target.lean"), unchecked_write=False,
                         latest_check_path=str(tmp_path / "Target.lean"), latest_check_passed=True),
        status_context=lea_status.seed(old["id"]))
    update, _ = lea_status.publish(old["id"], "prior-finding", dict(kind="initial", confidence="medium",
        summary="Inspecting the source.", confidence_reason="A boundary case remains to be checked.", scope="source_only",
        finding_updates=[dict(key="boundary-case", severity="warning", category="source_gap",
            title="Boundary case", detail="The boundary case is omitted.", lean_resolution="planned",
            source_resolution="open", correction="Inspect the boundary case before completion.")]))
    saved["status_context"]["assessment"] = update["assessment"]
    recovery.Recorder(old["id"], attempt, recovery.manifest(run=old, config=cfg, repo=tmp_path))("checkpoint", saved)
    store.update_run(old["id"], "max_turns", recoverable=True, stop_reason="turn_cap",
                     input_tokens=100, output_tokens=40, cost_usd=0.25, usage_status="final")
    recovery.finish_attempt(old["id"], attempt, "max_turns")
    return old, cfg, tmp_path, saved


def request(**changes):
    return runs.ContinueRequest(idempotency_key="continue-once", source_context_ref=publish_for(None,source()),
                                timeout_seconds=90, **changes)


def test_linked_continuation_pins_state_and_grants_another_allowance(paused):
    old, cfg, repo, original = paused
    result = runs.continue_run(old["id"], request())
    new = store.get_run(result["run_id"])
    assert result["saved_turn"] == 2 and result["max_turns"] == 5
    assert result["session_id"] == old["session_id"]
    assert new["focus_formalization_id"] == old["focus_formalization_id"]
    assert new["resumes_run_id"] == old["id"] and new["resume_checkpoint_id"]
    assert datetime.fromisoformat(new["deadline_at"]) > datetime.now(timezone.utc)
    restored, changed = recovery.prepare_continuation(new, replace(cfg, max_turns=5), repo,
                                                     lea_status.seed(new["id"]), "Continue")
    assert not changed
    assert restored["messages"][:-1] == original["messages"]
    assert restored["proof_state"] == original["proof_state"]
    assert restored["turn"] == 2 and restored["last_input_tokens"] == 140
    assert restored["cost_usd"] == restored["input_tokens"] == restored["output_tokens"] == 0
    assert restored["reporting_stop"] is None
    assert restored["status_context"]["assessment"] == original["status_context"]["assessment"]
    assert store.get_run(old["id"])["cost_usd"] == 0.25
    assert recovery.continuation_checkpoint(old["id"])[0] == original


def test_prepared_source_continuation_replays_after_expiry_and_keeps_pause_policy(paused):
    from app import overleaf_source
    old, _, _, _ = paused
    assert recovery.available_action(store.get_run(old["id"])) == "continue"
    detail = store.session_detail(old["session_id"])
    assert detail["runs"][0]["available_recovery_action"] == "continue"
    with db.write() as conn:
        conn.execute("update runs set allow_source_pause = 1 where id = ?", (old["id"],))
    ref = publish_for(old["focus_formalization_id"],source())
    req = runs.ContinueRequest(idempotency_key="ui-continue-once", source_context_ref=ref)
    result = runs.continue_run(old["id"], req)
    new = store.get_run(result["run_id"])
    assert new["allow_source_pause"] and new["resume_checkpoint_id"] and new["autonomous"]
    assert new["source_context_ref"] == ref
    with db.write() as conn:
        conn.execute("update source_publishers set lease_until=\'2000\' where document_id=\'doc\'")
    assert runs.continue_run(old["id"], req)["run_id"] == result["run_id"]
    assert recovery.available_action(store.get_run(old["id"])) is None


def dependency_pause(paused, monkeypatch, **budget_changes):
    from app import project_dependencies
    old, cfg, repo, saved = paused
    budget = dict(max_turns=3, max_cost_usd=1, cost_used=0.25, remaining_seconds=45) | budget_changes
    with db.write() as conn:
        conn.execute("""update runs set status='cancelled', stop_reason='dependency_wait',
            waiting_dependencies_json='["verified-child"]', automation_budget_json=? where id=?""",
            (json.dumps(budget), old["id"]))
    from app.queries import core
    monkeypatch.setattr(core, "fact", lambda *_: dict(ready=True, result=dict(module="Lea.Shared.Child")))
    return store.get_run(old["id"]), cfg, repo, saved, budget


def test_dependency_continuation_checks_fresh_source_and_keeps_original_progress_and_allowance(paused, monkeypatch):
    from app import overleaf_source
    old, cfg, repo, saved, budget = dependency_pause(paused, monkeypatch)
    current = source()
    current["statementLocation"]["sourceStartLine"] = 4  # capture location only
    ref = publish_for(old["focus_formalization_id"],current)
    req = runs.ContinueRequest(idempotency_key="dependency-once", source_context_ref=ref, dependency_resume=True)
    result = runs.continue_run(old["id"], req)
    child = store.get_run(result["run_id"])
    assert child["max_turns"] == old["max_turns"] == 3
    assert child["model"] == old["model"]
    assert json.loads(child["automation_budget_json"]) == budget
    assert child["source_context_ref"] == ref
    restored, changed = recovery.prepare_continuation(child, cfg, repo, lea_status.seed(child["id"]), "Continue")
    assert not changed and restored["turn"] == saved["turn"]
    assert restored["messages"][:-1] == saved["messages"]
    assert restored["proof_state"] == saved["proof_state"]
    assert store.session_detail(old["session_id"])["runs"][-1]["client_request_id"] == "dependency-once"
    with db.write() as conn:
        conn.execute("update source_publishers set lease_until=\'2000\' where document_id=\'doc\'")
    assert runs.continue_run(old["id"], req)["run_id"] == child["id"]


@pytest.mark.parametrize("changes, reason", [
    ({"max_turns": 2}, "turn allowance"),
    ({"remaining_seconds": 0}, "execution time allowance"),
    ({"max_cost_usd": 0.25}, "cost allowance"),
])
def test_dependency_continuation_never_grants_more_allowance_automatically(paused, monkeypatch, changes, reason):
    old, *_ = dependency_pause(paused, monkeypatch, **changes)
    with pytest.raises(HTTPException) as error:
        runs.continue_run(old["id"], request(dependency_resume=True))
    assert error.value.status_code == 409 and reason in error.value.detail
    assert len(store.session_detail(old["session_id"])["runs"]) == 1


@pytest.mark.parametrize("current, reason", [(source("False."), "changed"), (source(proof="Changed proof."), "changed")])
def test_dependency_continuation_blocks_actual_source_changes(paused, monkeypatch, current, reason):
    from app import overleaf_source
    old, *_ = dependency_pause(paused, monkeypatch)
    ref = publish_for(old["focus_formalization_id"],current)
    req = runs.ContinueRequest(idempotency_key="changed-source", source_context_ref=ref, dependency_resume=True)
    with pytest.raises(HTTPException) as error:
        runs.continue_run(old["id"], req)
    assert error.value.status_code == 409 and reason in error.value.detail
    assert "Review the source" in error.value.detail
    assert len(store.session_detail(old["session_id"])["runs"]) == 1


def test_dependency_continuation_requires_the_latest_attempt_and_verified_results(paused, monkeypatch):
    from app import project_dependencies
    old, *_ = dependency_pause(paused, monkeypatch)
    from app.queries import core
    monkeypatch.setattr(core,"fact",lambda *_:dict(ready=False))
    with pytest.raises(HTTPException, match="not verified"):
        runs.continue_run(old["id"], request(dependency_resume=True))
    from app.queries import core
    monkeypatch.setattr(core, "fact", lambda *_: dict(ready=True, result=dict(module="Lea.Shared.Child")))
    store.create_run_bundle(message="A newer attempt", session_id=old["session_id"], project_id=None,
        session_origin="overleaf", session_origin_url=None, model="test", provider=None, max_turns=3,
        autonomous=True, purpose="overleaf_solver", source_bundle=source(),
        focus_formalization_id=old["focus_formalization_id"])
    with pytest.raises(HTTPException, match="newer formalization attempt"):
        runs.continue_run(old["id"], request(dependency_resume=True))


def test_resume_next_turn_does_not_repeat_the_saved_write_or_prior_usage(paused, monkeypatch):
    old, cfg, repo, _ = paused
    result = runs.continue_run(old["id"], request())
    new = store.get_run(result["run_id"])
    # One additional turn makes the next execution step unambiguous.
    cfg = replace(cfg, max_turns=3)
    restored, _ = recovery.prepare_continuation(new, cfg, repo, lea_status.seed(new["id"]), "Continue")
    calls = []
    def provider(*args, **kwargs):
        calls.append(args[2])
        yield ToolCall("read_file", {"path": "Target.lean"}, "read-next")
        yield Done(Usage(11, 4), 0.02)
    monkeypatch.setattr(agent, "_provider_stream", provider)
    events = list(agent.run_events(cfg, [], session_id=old["session_id"], working_dir=str(repo),
        resume_state=restored, checkpoint_callback=lambda *_: None))
    assert len(calls) == 1
    assert [event.name for event in events if isinstance(event, ToolCalled)] == ["read_file"]
    assert isinstance(events[-1], Finished) and events[-1].reason == "max_turns"
    assert events[-1].usage.input_tokens == 11 and events[-1].cost == 0.02
    assert (repo / "Target.lean").read_text().endswith("by sorry\n")


def test_changed_source_and_files_are_disclosed_and_invalidate_checks(paused):
    old, cfg, repo, _ = paused
    changed = request().model_copy(update={"source_context_ref":publish_for(None,source("False."))})
    result = runs.continue_run(old["id"], changed)
    new = store.get_run(result["run_id"])
    (repo / "Target.lean").write_text("theorem target : False := by sorry\n")
    restored, changed = recovery.prepare_continuation(new, replace(cfg, max_turns=5), repo,
        lea_status.seed(new["id"]), "Continue")
    assert changed and restored["proof_state"]["unchecked_write"]
    assert restored["proof_state"]["latest_check_passed"] is None
    notice = restored["messages"][-1]
    assert "Target.lean" in notice["content"] and '"before": "True."' in notice["content"]
    assert "False." in agent._latest_user_request(restored["messages"])
    assert restored["status_context"]["previous_assessment"]["findings"][0]["key"] == "boundary-case"
    assert lea_status_store.context(old["id"])["source_bundle"]["statement"] == "True."


def test_duplicate_and_concurrent_admission_create_one_successor(paused):
    old, _, _, _ = paused
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: runs.continue_run(old["id"], request()), range(4)))
    assert len({item["run_id"] for item in results}) == 1
    with db.connect() as conn:
        assert conn.execute("select count(*) from runs where resumes_run_id = ?", (old["id"],)).fetchone()[0] == 1
    changed = request().model_copy(update={"source_context_ref":publish_for(None,source("False."))})
    with pytest.raises(HTTPException) as error:
        runs.continue_run(old["id"], changed)
    assert error.value.status_code == 409


@pytest.mark.parametrize("problem", ["missing", "unreadable", "incompatible_format", "uncertain", "active", "completed", "review", "superseded", "wrong_target", "spend"])
def test_unavailable_continuation_never_starts_fresh_work(paused, monkeypatch, problem):
    old, _, _, _ = paused
    req = request()
    with db.write() as conn:
        if problem == "missing":
            conn.execute("delete from run_checkpoints")
        elif problem in {"unreadable", "incompatible_format"}:
            conn.execute("update run_checkpoints set payload = ?", ("{" if problem == "unreadable" else "[]",))
        elif problem == "uncertain":
            conn.execute("insert into run_invocations(id, run_id, attempt_id, kind, name, turn, input_json, started_at) values ('uncertain', ?, 'old', 'tool', 'write_file', 2, '{}', ?)",
                         (old["id"], db.utc_now()))
        elif problem in {"active", "completed", "review"}:
            conn.execute("update runs set status = ?, recovery_state = ? where id = ?",
                         ("running" if problem == "active" else "proved" if problem == "completed" else "cancelled",
                          "review" if problem == "review" else None, old["id"]))
    if problem == "superseded":
        runs.continue_run(old["id"], req)
        req = req.model_copy(update={"idempotency_key": "different"})
    elif problem == "wrong_target":
        other = source()
        other["targetKey"] = "other"
        other["bundleHash"] = source_hash(other)
        other["sourceIdentityHash"] = source_hash(other, identity=True)
        req = req.model_copy(update={"source_bundle": other})
    elif problem == "spend":
        monkeypatch.setattr(runs.settings_service, "spend_limit_reached", lambda *_: True)
    with pytest.raises(HTTPException) as error:
        runs.continue_run(old["id"], req)
    assert error.value.status_code == (402 if problem == "spend" else 409)


def test_runtime_change_rejects_restoration_before_paid_work(paused):
    old, cfg, repo, _ = paused
    result = runs.continue_run(old["id"], request())
    new = store.get_run(result["run_id"])
    with pytest.raises(ValueError, match="incompatible"):
        recovery.prepare_continuation(new, replace(cfg, model="different"), repo,
                                      lea_status.seed(new["id"]), "Continue")


def test_incompatible_runtime_is_rejected_by_worker_before_paid_work(paused, monkeypatch):
    old,cfg,repo,_=paused
    result=runs.continue_run(old['id'],request())
    with pytest.raises(ValueError,match='incompatible'):
        recovery.prepare_continuation(store.get_run(result['run_id']),replace(cfg,tools=['read_file']),repo,lea_status.seed(result['run_id']),'Continue')


def test_expired_pause_receives_fresh_time_and_preserves_model(paused, monkeypatch):
    old, cfg, _, _ = paused
    with db.write() as conn:
        conn.execute("update runs set deadline_at = '2020-01-01T00:00:00+00:00' where id = ?", (old["id"],))
    monkeypatch.setattr(runs, "load_config", lambda: replace(cfg, model="new-default"))
    result = runs.continue_run(old["id"], request())
    assert result["model"] == "test"
    assert (datetime.fromisoformat(result["deadline_at"]) - datetime.now(timezone.utc)).total_seconds() > 80


def test_distinct_concurrent_requests_create_at_most_one_successor(paused):
    old, _, _, _ = paused
    def submit(index):
        try:
            return runs.continue_run(old["id"], request().model_copy(update={"idempotency_key": f"click-{index}"}))
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(result == 409 for result in results) == 3


def test_compacted_history_classifier_uses_current_mathematical_source(paused, monkeypatch):
    _, cfg, _, _ = paused
    seen = []
    def classifier(*args, **kwargs):
        seen.append(args[2])
        yield TextDelta("PROVED")
        yield Done(Usage(), 0.0)
    monkeypatch.setattr(agent, "stream", classifier)
    result = agent._classify_final_result(model="test", messages=[{"role": "user", "content": "Compacted resume summary"}],
        final_text="Done", theorem_signature="theorem target : True",
        config=replace(cfg, status_context={"source_bundle": source("Current mathematical statement.")}))
    assert result[0] == "proved"
    assert "Current mathematical statement." in seen[0][0]["content"]
    assert "Compacted resume summary" not in seen[0][0]["content"]


def test_changed_dependency_source_requires_fresh_check(paused):
    old, cfg, repo, _ = paused
    bundle = source()
    bundle["relevantSource"] = [{"kind": "dependency", "path": "defs.tex", "startLine": 1,
                                 "endLine": 2, "content": "New definition", "truncated": False}]
    bundle["bundleHash"] = source_hash(bundle)
    assert bundle["sourceIdentityHash"] == source()["sourceIdentityHash"]
    row = runs.continue_run(old["id"], request().model_copy(update={"source_context_ref":publish_for(None,bundle)}))
    state, changed = recovery.prepare_continuation(store.get_run(row["run_id"]), cfg, repo,
                                                  lea_status.seed(row["run_id"]), "Continue")
    assert changed and state["proof_state"]["latest_check_passed"] is None
    assert "New definition" in state["messages"][-1]["content"]


def test_changed_dynamic_tool_schema_rejects_before_model_call(paused, monkeypatch):
    old, cfg, repo, _ = paused
    result = runs.continue_run(old["id"], request())
    state, _ = recovery.prepare_continuation(store.get_run(result["run_id"]), cfg, repo,
                                            lea_status.seed(result["run_id"]), "Continue")
    state["tool_schema_digest"] = "incompatible-tool-schema"
    calls = []
    monkeypatch.setattr(agent, "_provider_stream", lambda *args, **kwargs: calls.append(args) or iter(()))
    with pytest.raises(ValueError, match="Saved agent tools.*incompatible"):
        list(agent.run_events(cfg, [], working_dir=str(repo), resume_state=state))
    assert calls == []


def test_materialized_skill_paths_may_change_but_reference_contents_must_match(paused, monkeypatch):
    old, cfg, repo, _ = paused
    with TemporaryDirectory() as before, TemporaryDirectory() as after:
        paths = []
        for directory in (before, after):
            root = Path(directory) / "recipe"
            (root / "references").mkdir(parents=True)
            (root / "SKILL.md").write_text("Read references/details.md when needed.")
            (root / "references/details.md").write_text("A proof recipe.")
            paths.append(str(root / "SKILL.md"))
        original = replace(cfg, skills=[paths[0]], skills_root=before)
        current = replace(cfg, skills=[paths[1]], skills_root=after)
        with db.write() as conn:
            conn.execute("update run_checkpoints set manifest = ? where run_id = ?",
                         (json.dumps(recovery.manifest(run=old, config=original, repo=repo)), old["id"]))
        monkeypatch.setattr(runs, "load_config", lambda: current)
        result = runs.continue_run(old["id"], request())
        run = store.get_run(result["run_id"])
        state, changed = recovery.prepare_continuation(run, current, repo, lea_status.seed(run["id"]), "Continue")
        assert not changed and state["turn"] == 2
        (Path(after) / "recipe/references/details.md").write_text("A different proof recipe.")
        with pytest.raises(ValueError, match="incompatible"):
            recovery.prepare_continuation(run, current, repo, lea_status.seed(run["id"]), "Continue")


def test_best_effort_requires_explicit_proofless_source_pause(paused):
    old, _, _, _ = paused
    with pytest.raises(HTTPException):
        runs.continue_run(old["id"], request(best_effort=True))
    with db.write() as conn:
        conn.execute("update runs set stop_reason = 'source_obstruction' where id = ?", (old["id"],))
    result = runs.continue_run(old["id"], request(best_effort=True))
    assert store.get_run(result["run_id"])["continuation_best_effort"] == 1
    message = store.get_run_submission("continue-once")["message"]["content"]
    assert "Author-authorized best-effort continuation" in message


def test_bridge_executes_successive_continuations_with_separate_usage(paused, monkeypatch):
    old, cfg, repo, _ = paused
    monkeypatch.setattr(projects, "repo_for_session", lambda *_: repo)
    calls = []
    def provider(*args, **kwargs):
        calls.append(args[2])
        yield ToolCall("read_file", {"path": "Target.lean"}, f"next-{len(calls)}")
        yield Done(Usage(11, 4), 0.02)
    monkeypatch.setattr(agent, "_provider_stream", provider)
    predecessor = old["id"]
    for index in range(2):
        req = request().model_copy(update={"idempotency_key": f"segment-{index}"})
        result = runs.continue_run(predecessor, req)
        row = store.get_run(result["run_id"])
        queue = Queue()
        task = store.get_run_submission(f"segment-{index}")["message"]["content"]
        bridge.run_lea(bridge.RunnerContext(session_id=old["session_id"], run_id=row["id"],
            task=task, config=replace(cfg, max_turns=row["max_turns"]),
            events=queue, autonomous=True, purpose="overleaf_solver"))
        finished = store.get_run(row["id"])
        assert finished["status"] == "max_turns", finished.get("result_detail")
        assert finished["input_tokens"] == 33
        assert finished["cost_usd"] == pytest.approx(0.06)
        assert recovery.load_checkpoint(row["id"])[0]["turn"] == 5 + index * 3
        predecessor = row["id"]
    assert len(calls) == 6
    with db.connect() as conn:
        total = conn.execute("select sum(cost_usd), sum(input_tokens) from runs").fetchone()
    assert total[0] == pytest.approx(0.37) and total[1] == 166


def test_automatic_recovery_uses_the_continuations_own_checkpoint(paused, monkeypatch):
    old, cfg, repo, _ = paused
    result = runs.continue_run(old["id"], request())
    row = store.get_run(result["run_id"])
    cfg = replace(cfg, max_turns=5)
    state, _ = recovery.prepare_continuation(row, cfg, repo, lea_status.seed(row["id"]), "Continue")
    state.update(turn=3, input_tokens=11, output_tokens=4, cost_usd=0.02)
    state["messages"].append({"role": "assistant", "content": "Own continuation checkpoint"})
    assert store.claim_pending_run(row["id"])
    attempt = recovery.start_attempt(row["id"])
    recovery.Recorder(row["id"], attempt, recovery.manifest(run=row, config=cfg, repo=repo))("checkpoint", state)
    store.checkpoint_run_usage(row["id"], 11, 4, 0.02, 1)
    assert recovery.recover_running()["resumed"] == 1
    monkeypatch.setattr(projects, "repo_for_session", lambda *_: repo)
    seen = []
    def provider(*args, **kwargs):
        seen.append(args[2])
        yield ToolCall("read_file", {"path": "Target.lean"}, "after-crash")
        yield Done(Usage(11, 4), 0.02)
    monkeypatch.setattr(agent, "_provider_stream", provider)
    bridge.run_lea(bridge.RunnerContext(session_id=old["session_id"], run_id=row["id"], task="Continue",
        config=cfg, events=Queue(), autonomous=True, purpose="overleaf_solver"))
    finished = store.get_run(row["id"])
    assert finished["status"] == "max_turns", finished.get("result_detail")
    assert len(seen) == 2
    assert any(m.get("content") == "Own continuation checkpoint" for m in seen[0])
    assert finished["cost_usd"] == pytest.approx(0.06) and finished["input_tokens"] == 33


@pytest.mark.parametrize("revision", ["unchanged", "source", "dependencies", "restart"])
def test_completion_reuses_only_current_saved_verification(paused, monkeypatch, revision):
    old, cfg, repo, saved = paused
    content = "theorem target : True := by trivial\n"
    (repo / "Target.lean").write_text(content)
    (repo / "lakefile.toml").write_text('name = "test"\n')
    (repo / "lean-toolchain").write_text("leanprover/lean4:v4.19.0\n")
    store.link_formalization_file(old["focus_formalization_id"], "Target.lean", "primary")
    store.add_code_step(old["session_id"], old["id"], "Target.lean", content=content,
        check_status="ok", artifact_kind="proof", formalization_id=old["focus_formalization_id"])
    # Arrange a pause after a successful check, before the final response.
    with db.write() as conn:
        conn.execute("update run_checkpoints set manifest = ? where run_id = ?",
                     (json.dumps(recovery.manifest(run=old, config=cfg, repo=repo)), old["id"]))
    current_source = source("True, with an added clarification.") if revision == "source" else source()
    if revision == "dependencies":
        (repo / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n")
    result = runs.continue_run(old["id"], request().model_copy(update={"source_context_ref":publish_for(None,current_source)}))
    row = store.get_run(result["run_id"])
    cfg = replace(cfg, max_turns=row["max_turns"])
    if revision == "restart":
        state, _ = recovery.prepare_continuation(row, cfg, repo, lea_status.seed(row["id"]), "Continue")
        assert store.claim_pending_run(row["id"])
        attempt = recovery.start_attempt(row["id"])
        recovery.Recorder(row["id"], attempt, recovery.manifest(run=row, config=cfg, repo=repo))("checkpoint", state)
        assert recovery.recover_running()["resumed"] == 1
    calls = []
    checks = []
    def provider(*args, **kwargs):
        calls.append(args[2])
        yield TextDelta("The proof is complete.")
        yield Done(Usage(9, 3), 0.01)
    monkeypatch.setattr(agent, "_provider_stream", provider)
    monkeypatch.setattr(agent, "_classify_final_result", lambda **_: ("proved", None, Usage(), 0.0))
    monkeypatch.setitem(registry.REGISTRY, "lean_check", replace(registry.REGISTRY["lean_check"],
        handler=lambda args: checks.append(args) or CheckText("Lean check passed.", mock_execution(args["path"]), Path(args["path"]).read_text())))
    queue = Queue()
    bridge.run_lea(bridge.RunnerContext(session_id=old["session_id"], run_id=row["id"], task="Continue",
        config=cfg, events=queue, autonomous=True, purpose="overleaf_solver"))
    finished = store.get_run(row["id"])
    assert finished["status"] == "proved", finished.get("result_detail")
    assert len(calls) == 1
    assert len(checks) == 1  # Legacy checkpoints have no compiler-input evidence.
    assert finished["cost_usd"] == pytest.approx(0.01)


def install_supported_predecessor(old, cfg, repo, release_index=0):
    release = json.loads(Path(recovery.__file__).with_name("checkpoint_compatibility.json").read_text())[release_index]
    current = recovery.manifest(run=old, config=cfg, repo=repo)
    assert current["tool_code_digests"] == release["destination"], "Compatibility must pin the reviewed destination runtime"
    state, manifest, checkpoint_id = recovery.continuation_checkpoint(old["id"])
    import hashlib
    state["tool_schema_digest"] = hashlib.sha256(json.dumps(registry.build_toolset(cfg.tools, cfg.extra_tools)[0], sort_keys=True).encode()).hexdigest()
    manifest["tool_code_digests"] = release["predecessor"]
    with db.write() as conn:
        conn.execute("update run_checkpoints set payload=?,manifest=? where id=?", (json.dumps(state), json.dumps(manifest), checkpoint_id))
    return state, manifest, checkpoint_id


@pytest.mark.parametrize("release_index", range(1, len(json.loads(
    Path(recovery.__file__).with_name("checkpoint_compatibility.json").read_text()))))
def test_reviewed_provider_change_requires_a_settled_exact_runtime(paused, release_index):
    old, cfg, repo, _ = paused
    state, manifest, _ = install_supported_predecessor(old, cfg, repo, release_index)
    current = recovery.manifest(run=old, config=cfg, repo=repo)
    assert recovery.compatible_crash_manifest(manifest, current, state)
    uncertain = {**state, "stage": "tools"}
    assert not recovery.compatible_runtime(manifest, current, uncertain)
    changed = {**current, "tool_code_digests": [*current["tool_code_digests"]]}
    changed["tool_code_digests"][5] = "an-unreviewed-provider-change"
    assert not recovery.compatible_runtime(manifest, changed, state)


def test_known_settled_predecessor_migrates_without_replay_or_recount(paused, monkeypatch):
    old, cfg, repo, original = paused
    saved, manifest, _ = install_supported_predecessor(old, cfg, repo)
    result = runs.continue_run(old["id"], request())
    new = store.get_run(result["run_id"])
    restored, changed = recovery.prepare_continuation(new, replace(cfg, max_turns=5), repo, lea_status.seed(new["id"]), "Continue")
    assert restored["turn"] == original["turn"]
    assert restored["messages"][:3] == original["messages"]
    assert restored["proof_state"] == original["proof_state"]
    assert restored["status_context"]["assessment"] == original["status_context"]["assessment"]
    assert restored["cost_usd"] == 0 and restored["input_tokens"] == 0
    assert restored["usage_details"] is None and restored["stall_detector"] == {}
    assert store.get_run(old["id"])["cost_usd"] == .25
    assert (repo / "Target.lean").read_text() == "theorem target : True := by sorry\n"
    assert not changed
    # Exercise the migrated state through the driver with paid calls mocked.
    # The old write is retained in history, never executed again. A fresh failed
    # verification of the partial artifact pauses within the new bound.
    with db.write() as conn:
        saved["proof_state"]["unchecked_write"] = True
        conn.execute("update run_checkpoints set payload=? where id=?",
                     (json.dumps(saved), new["resume_checkpoint_id"]))
    calls, checks = [], []
    def provider(*args, **kwargs):
        calls.append(args[2])
        yield TextDelta("Acknowledged.")
        yield Done(Usage(9, 3), 0.01)
    monkeypatch.setattr(agent, "_provider_stream", provider)
    monkeypatch.setitem(registry.REGISTRY, "lean_check", replace(registry.REGISTRY["lean_check"],
        handler=lambda args: checks.append(args) or "Error: proof contains sorry."))
    bridge.run_lea(bridge.RunnerContext(session_id=old["session_id"], run_id=new["id"],
        task="Continue", config=replace(cfg, max_turns=5), events=Queue(),
        autonomous=True, purpose="overleaf_solver"))
    finished = store.get_run(new["id"])
    assert finished["stop_reason"] == "stalled", finished.get("result_detail")
    assert finished["recoverable"] and finished["status"] == "cancelled"
    assert len(calls) == 3 and checks
    assert finished["cost_usd"] == pytest.approx(.03)
    assert store.get_run(old["id"])["cost_usd"] == .25
    assert (repo / "Target.lean").read_text() == "theorem target : True := by sorry\n"


def test_compatible_crash_migration_preserves_stall_state_and_accounting(paused):
    old, cfg, repo, _ = paused
    state, old_manifest, _ = install_supported_predecessor(old, cfg, repo)
    current = recovery.manifest(run=old, config=cfg, repo=repo)
    state["stall_detector"] = {"failures": 2, "evidence_revision": 0, "seen_evidence": []}
    state["usage_details"] = {"cost_basis": "legacy_estimate"}
    assert recovery.compatible_crash_manifest(old_manifest, current, state)
    assert state["stall_detector"]["failures"] == 2
    assert state["cost_usd"] == .25
    current["source_hash"] = "new-source"
    assert not recovery.compatible_crash_manifest(old_manifest, current, state)


@pytest.mark.parametrize("problem", ["unknown_predecessor", "different_tool", "uncertain", "corrupt_stall", "corrupt_usage"])
def test_migration_rejects_unreviewed_or_uncertain_state(paused, problem):
    old, cfg, repo, _ = paused
    state, manifest, checkpoint_id = install_supported_predecessor(old, cfg, repo)
    if problem == "unknown_predecessor":
        manifest["tool_code_digests"][0] = "unknown"
    elif problem == "different_tool":
        manifest["tool_code_digests"][1] = "different"
    elif problem == "corrupt_stall":
        state["stall_detector"] = {"failures": "three"}
    elif problem == "corrupt_usage":
        state["usage_details"] = ["invalid"]
    else:
        with db.write() as conn:
            conn.execute("insert into run_invocations(id,run_id,attempt_id,kind,name,turn,started_at) values ('uncertain',?,?,'model','stream',3,?)", (old["id"], "legacy-attempt", db.utc_now()))
    with db.write() as conn:
        conn.execute("update run_checkpoints set payload=?,manifest=? where id=?", (json.dumps(state), json.dumps(manifest), checkpoint_id))
    if problem in {'unknown_predecessor','different_tool'}:
        result=runs.continue_run(old['id'],request())
        with pytest.raises(ValueError,match='incompatible'):
            recovery.prepare_continuation(store.get_run(result['run_id']),cfg,repo,lea_status.seed(result['run_id']),'Continue')
    else:
        with pytest.raises(HTTPException):runs.continue_run(old['id'],request())
        with db.connect() as conn:
            assert conn.execute('select count(*) from runs where resumes_run_id=?',(old['id'],)).fetchone()[0]==0



# This module mocks compiler behavior; bind those mock verdicts to their inputs.
pytestmark = pytest.mark.usefixtures("certified_unit_inputs")

from lea.check_runtime import CheckText
from evidence_fixtures import mock_execution
