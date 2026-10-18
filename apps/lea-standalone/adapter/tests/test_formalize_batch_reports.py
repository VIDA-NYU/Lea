from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import db, formalize_batch_reports as reports, store
from lea.providers import Done, TextDelta, Usage


def seed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "reports.sqlite3")
    db.init_db()
    with db.connect() as conn:
        for suffix in ("one", "two"):
            conn.execute("""insert into projects
                (id, slug, title, namespace, repo_path, created_at, updated_at)
                values (?, ?, ?, ?, ?, 't', 't')""",
                (f"p-{suffix}", f"project-{suffix}", suffix, f"Lea.{suffix}", f"proofs/{suffix}"))
        conn.execute("""insert into sessions (id, project_id, title, origin, created_at, updated_at)
            values ('s-one', 'p-one', 'Session', 'overleaf', 't', 't')""")
        conn.execute("""insert into formalizations
            (id, project_id, display_title, declaration_name, kind, origin, created_at, updated_at)
            values ('f-one', 'p-one', 'target', 'target', 'theorem', 'overleaf', 't', 't')""")
        conn.execute("""insert into runs
            (id, session_id, project_id, status, model, created_at, updated_at)
            values ('r-one', 's-one', 'p-one', 'completed', 'test-model', 't', 't')""")
        conn.execute("""insert into lea_status_run_contexts
            (run_id, formalization_id, session_id, project_id, operation, run_generation,
             source_bundle_json, source_identity_hash, source_bundle_hash, created_at)
            values ('r-one', 'f-one', 's-one', 'p-one', 'overleaf_solver', 1, ?, 'src', 'bundle', 't')""",
            (json.dumps({"targetKind": "theorem", "targetKey": "target"}),))
        conn.execute("""insert into lea_status_updates
            (id, run_id, formalization_id, sequence, invocation_key, kind,
             payload_json, assessment_json, artifact_snapshot_json, dependency_hash, created_at)
            values ('u-one', 'r-one', 'f-one', 1, 'key', 'final', '{}', ?, '{}', 'dep', 't')""",
            (json.dumps({"summary": "The source proof needs an extra lemma.", "confidence": "medium",
                         "recommended_next_action": {"kind": "continue", "detail": "Prove the finite cover lemma."},
                         "findings": [
                {"title": "Missing lemma", "severity": "warning", "detail": "Add a finite cover lemma."}
            ]}),))


def payload():
    return {"batchId": "formalize-batch-1", "startedAt": "2026-01-01T00:00:00Z",
            "finishedAt": "2026-01-01T00:01:00Z", "canceled": False, "items": [
                {"targetKind": "theorem", "targetLabel": "target", "state": "formalized",
                 "jobId": "job-1", "runId": "r-one", "formalizationId": "f-one"},
                {"targetKind": "theorem", "targetLabel": "dependent", "state": "skipped",
                 "reason": "depends_on_failed:other"},
                {"targetKind": "definition", "targetLabel": "counter", "state": "disproved"},
                {"targetKind": "theorem", "targetLabel": "cap", "state": "skipped", "reason": "max_spend"},
                {"targetKind": "theorem", "targetLabel": "prior", "state": "skipped", "reason": "existing_proof"},
                {"targetKind": "theorem", "targetLabel": "failed", "state": "failed"},
                {"targetKind": "theorem", "targetLabel": "stopped", "state": "canceled"}
            ]}


def test_captures_settled_facts_and_exact_run_status(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    report, created = reports.create("project-one", payload())
    assert created
    assert report["facts"]["counts"] == {
        "verified": 1, "verifiedProofs": 1, "verifiedDefinitions": 0,
        "disproved": 1, "incomplete": 0, "needsReview": 0, "failed": 1, "alreadyVerified": 1,
        "dependencySkipped": 1, "spendCapSkipped": 1, "otherSkipped": 0, "stopped": 1,
    }
    first = report["facts"]["items"][0]
    assert first["leaStatusUpdateId"] == "u-one"
    assert first["leaStatus"]["summary"] == "The source proof needs an extra lemma."
    assert first["reportingIncomplete"] is False
    with db.connect() as conn:
        conn.execute("update lea_status_updates set assessment_json = ? where id = 'u-one'",
                     (json.dumps({"summary": "Changed later"}),))
    assert reports.get("project-one", "formalize-batch-1")["facts"]["items"][0]["leaStatus"]["summary"] == "The source proof needs an extra lemma."
    assert reports.create("project-one", payload())[1] is False
    with pytest.raises(ValueError):
        reports.create("project-two", payload())
    altered = payload()
    altered["items"][0]["runId"] = "other-run"
    with pytest.raises(ValueError):
        reports.create("project-one", altered)
    altered = payload()
    altered["batchId"] = "formalize-batch-wrong-target"
    altered["items"][0]["targetLabel"] = "wrong-target"
    with pytest.raises(ValueError, match="different source target"):
        reports.create("project-one", altered)
    assert len(reports.list_reports("project-one")["reports"]) == 1
    assert reports.list_reports("project-two")["reports"] == []


def test_review_is_terminal_but_not_verified_in_historical_report(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    data = payload()
    data["batchId"] = "formalize-batch-review"
    data["items"][5]["state"] = "needs_review"
    data["items"][1]["reason"] = "depends_on_review:failed"
    report, created = reports.create("project-one", data)
    assert created is True
    assert report["facts"]["counts"]["needsReview"] == 1
    assert report["facts"]["counts"]["failed"] == 0
    assert report["facts"]["counts"]["verified"] == 1
    assert report["facts"]["counts"]["dependencySkipped"] == 1


def test_shared_results_do_not_inflate_document_totals_or_reprove_old_results(tmp_path, monkeypatch):
    from app import project_dependencies
    seed(tmp_path, monkeypatch)
    data = payload()
    data["dependencies"] = [{"id": "old", "started_here": False}, {"id": "new", "started_here": True}]
    with db.connect() as conn:
        conn.execute("insert into runs(id,session_id,project_id,status,model,created_at,updated_at) values ('r-old','s-one','p-one','completed','test','t','t')")
    monkeypatch.setattr(project_dependencies, "get", lambda key: dict(id=key, project_id="p-one", ready=True,
        state="ready", source_hash="source", path=f"Shared/{key}.lean", module_name=f"Lea.one.Shared.{key}",
        declaration_name=f"Lea.one.{key}", blocker=None, requesting_targets=["f-one"],
        attempts=[{"run_id": "worker", "state": "ready", "initiating_run_id": "r-one" if key == "new" else "r-old"}], specification={"name": key, "statement": "True", "sources": []}))
    report, _ = reports.create("project-one", data)
    assert report["facts"]["total"] == 7
    assert report["facts"]["counts"]["verifiedProofs"] == 1
    assert report["facts"]["dependencyCounts"] == {"newlyProved": 1, "reused": 1, "blocked": 0, "unknownOrigin": 0}
    assert report["facts"]["dependencies"][0]["newlyProved"] is False
    assert report["facts"]["dependencies"][0]["targets"] == ["target"]


def test_history_cursor_handles_same_completion_time(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    first = payload()
    second = payload()
    second["batchId"] = "formalize-batch-2"
    reports.create("project-one", first)
    reports.create("project-one", second)
    page = reports.list_reports("project-one", limit=1)
    assert page["reports"][0]["batch_id"] == "formalize-batch-2"
    assert page["nextCursor"]
    older = reports.list_reports("project-one", limit=1, before=page["nextCursor"])
    assert older["reports"][0]["batch_id"] == "formalize-batch-1"
    assert older["nextCursor"] is None


def test_incomplete_diagnostics_and_latest_outcomes_preserve_saved_facts(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    data = payload()
    data["items"][0].update(state="incomplete", reason="dependency_wait", detail="Awaiting final shared certification.", recoverable=True)
    data["items"][1]["reason"] = "depends_on_incomplete:target"
    first, _ = reports.create("project-one", data)
    assert first["facts"]["counts"]["incomplete"] == 1
    assert first["facts"]["counts"]["dependencySkipped"] == 1
    outcome = reports.latest_target_outcomes("p-one")[("theorem", "target")]
    assert outcome["detail"] == "Awaiting final shared certification."
    assert outcome["recoverable"] is True
    newer = payload()
    newer.update(batchId="formalize-batch-2", finishedAt="2026-01-02T00:01:00Z")
    reports.create("project-one", newer)
    assert reports.latest_target_outcomes("p-one")[("theorem", "target")]["state"] == "formalized"
    assert reports.get("project-one", data["batchId"])["facts"] == first["facts"]


def test_nested_and_continuation_provenance_and_unknown_legacy_origin(tmp_path, monkeypatch):
    from app import project_dependencies
    seed(tmp_path, monkeypatch)
    with db.connect() as conn:
        for run_id, parent, dependency in (("worker-one", None, "shared"), ("worker-two", "worker-one", "shared"),
                ("nested", None, "nested-shared"), ("document-continuation", "r-one", None), ("outside", None, None)):
            conn.execute("insert into runs(id,session_id,project_id,status,model,resumes_run_id,dependency_id,created_at,updated_at) values (?,'s-one','p-one','proved','test',?,?, 't','t')", (run_id, parent, dependency))
        for dependency, run_id, initiator in (("shared", "worker-two", "r-one"), ("nested-shared", "nested", "worker-one")):
            conn.execute("insert into project_dependency_attempts(dependency_id,generation,run_id,initiating_run_id,state,created_at,updated_at) values (?,1,?,?,'ready','t','t')", (dependency, run_id, initiator))
    assert reports._document_origin("document-continuation") == "r-one"
    assert reports._document_origin("nested") == "r-one"
    data = payload()
    # Transient flags deliberately disagree with the durable attempt records.
    data["dependencies"] = [{"id": "new", "started_here": False}, {"id": "external", "started_here": True}, {"id": "legacy", "started_here": True}]
    monkeypatch.setattr(project_dependencies, "get", lambda key: dict(id=key, project_id="p-one", ready=True,
        state="ready", source_hash="source", path=f"Shared/{key}.lean", module_name=f"Lea.one.Shared.{key}",
        declaration_name=f"Lea.one.{key}", blocker=None, requesting_targets=["f-one"],
        attempts=[{"state": "ready", "initiating_run_id": {"new": "nested", "external": "outside"}.get(key)}],
        specification={"name": key, "statement": "True", "sources": []}))
    report, _ = reports.create("project-one", data)
    assert report["facts"]["dependencyCounts"] == dict(newlyProved=1, reused=1, unknownOrigin=1, blocked=0)
    assert report["facts"]["dependencies"][2]["origin"] == "unknown"
    assert report["facts"]["dependencies"][2]["reused"] is False


def test_verified_proofs_and_definitions_are_separate():
    counts = reports._counts([
        {"targetKind": "theorem", "state": "formalized", "reason": None},
        {"targetKind": "definition", "state": "formalized", "reason": None},
    ])
    assert counts["verified"] == 2
    assert counts["verifiedProofs"] == 1
    assert counts["verifiedDefinitions"] == 1


def test_rejects_cross_project_run_and_marks_missing_final_assessment(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    bad = payload()
    bad["batchId"] = "formalize-batch-other"
    with pytest.raises(ValueError):
        reports.create("project-two", bad)
    with db.connect() as conn:
        conn.execute("delete from lea_status_updates where run_id = 'r-one'")
    report, _ = reports.create("project-one", payload())
    assert report["facts"]["items"][0]["leaStatus"] is None
    assert report["facts"]["items"][0]["reportingIncomplete"] is True


def test_synthesis_failure_keeps_facts_and_retry_accounts_for_spend(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    reports.create("project-one", payload())
    monkeypatch.setattr(reports, "load_config", lambda: SimpleNamespace(model="test-model", max_spend_usd=0))

    def no_credits(*args, **kwargs):
        raise RuntimeError("insufficient credits")
        yield  # generator shape

    monkeypatch.setattr(reports, "stream", no_credits)
    reports.generate("formalize-batch-1")
    failed = reports.get("project-one", "formalize-batch-1")
    assert failed["state"] == "narrative_unavailable"
    assert failed["facts"]["counts"]["verified"] == 1
    assert "insufficient credits" in failed["error"]

    def completion(*args, **kwargs):
        assert args[3] == []  # no tools
        assert '"lea_self_reported_confidence": "medium"' in args[2][0]["content"]
        assert 'Prove the finite cover lemma.' in args[2][0]["content"]
        yield TextDelta("One proof was verified; inspect the remaining targets.")
        yield Done(Usage(12, 8), 0.04)

    monkeypatch.setattr(reports, "stream", completion)
    reports.retry("project-one", "formalize-batch-1")
    reports.generate("formalize-batch-1")
    ready = reports.get("project-one", "formalize-batch-1")
    assert ready["state"] == "ready"
    assert ready["attempts"] == 2
    assert ready["cost_usd"] == 0.04
    assert store.total_spend_usd() == 0.04
    assert store.global_usage()["input_tokens"] == 12
    assert store.project_report_usage()[0]["project_slug"] == "project-one"
    assert store.project_report_usage()[0]["cost_usd"] == 0.04
    with pytest.raises(ValueError):
        reports.retry("project-one", "formalize-batch-1")


def test_restart_marks_unfinished_summary_retryable(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    reports.create("project-one", payload())
    reports.recover_interrupted()
    assert reports.get("project-one", "formalize-batch-1")["state"] == "narrative_unavailable"


def test_project_report_routes_persist_and_read_after_generation(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    seed(tmp_path, monkeypatch)
    monkeypatch.setattr(reports, "load_config", lambda: SimpleNamespace(model="test-model"))

    def completion(*args, **kwargs):
        yield TextDelta("A verified proof was produced; review the remaining targets.")
        yield Done(Usage(3, 4), 0.01)

    monkeypatch.setattr(reports, "stream", completion)
    root = "/api/projects/by-slug/project-one/formalize-batch-reports"
    with TestClient(app, base_url="http://127.0.0.1:8001") as client:
        created = client.post(root, json=payload())
        assert created.status_code == 200
        assert created.json()['command']['state']=='queued'
        # HTTP lifespan is SQL-only. Start the private executor explicitly in
        # this embedded domain fixture; real process separation is tested apart.
        from app.routes import formalize_batch_reports as private_report_routes
        from app import command_host
        command_host.start()
        import time
        command_id=created.json()['command']['id']
        deadline=time.monotonic()+15
        while True:
            result=client.get('/api/workspace/commands/'+command_id).json()['command']
            if result['state'] in {'succeeded','failed'}:break
            assert time.monotonic()<deadline,result
            time.sleep(.05)
        assert result['state']=='succeeded',result
        assert result['result']['facts']['counts']['verified']==1
        assert client.post(root, json=payload()).status_code == 200
        listed = client.get(root).json()
        assert listed["reports"][0]["batch_id"] == "formalize-batch-1"
        detail = client.get(root + "/formalize-batch-1").json()
        assert detail["state"] == "ready"
        assert detail["narrative"].startswith("A verified proof")
        assert client.get("/api/projects/by-slug/project-two/formalize-batch-reports/formalize-batch-1").status_code == 404
