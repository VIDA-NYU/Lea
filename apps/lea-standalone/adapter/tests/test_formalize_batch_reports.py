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
        "disproved": 1, "failed": 1, "alreadyVerified": 1,
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
        assert created.json()["facts"]["counts"]["verified"] == 1
        assert client.post(root, json=payload()).status_code == 200
        listed = client.get(root).json()
        assert listed["reports"][0]["batch_id"] == "formalize-batch-1"
        detail = client.get(root + "/formalize-batch-1").json()
        assert detail["state"] == "ready"
        assert detail["narrative"].startswith("A verified proof")
        assert client.get("/api/projects/by-slug/project-two/formalize-batch-reports/formalize-batch-1").status_code == 404
