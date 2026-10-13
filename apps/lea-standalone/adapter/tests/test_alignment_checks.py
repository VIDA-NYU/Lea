"""Retired evaluator endpoints retain read-only historical reports."""
import hashlib
import json
import pytest
from fastapi import HTTPException
from app import alignment_checks, alignment_store, db, store, lea_status
from app.alignment_schemas import SourceBundle
from app.routes import alignment_checks as routes
from app.artifact_snapshots import _snapshot, _dependency_hash

def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Overleaf theorem", origin="overleaf")
    formalization = store.create_formalization(
        project_id=None,
        loose_session_id=session["id"],
        display_title="target",
        declaration_name="target",
        origin="overleaf",
        source_hash="a" * 64,
    )
    store.link_session_formalization(session["id"], formalization["id"])
    store.link_formalization_file(formalization["id"], "target.lean", "primary")
    store.add_code_step(
        session["id"], None, "target.lean",
        content="theorem target : True := by trivial",
        check_status="ok", formalization_id=formalization["id"],
    )
    return session, formalization


def _bundle(*, proof="", status="missing", target_kind="theorem", digest=None):
    payload = {
        "version": 2,
        "targetKey": "target",
        "targetKind": target_kind,
        "statement": "True.",
        "proof": proof,
        "proofAssociation": {
            "status": status,
            "method": "adjacent" if status == "associated" else "none",
            "sourceFile": "main.tex",
            "sourceStartLine": 5,
            "sourceEndLine": 7,
            "proofHash": "",
        },
        "statementLocation": {
            "sourceFile": "main.tex", "sourceStartLine": 1, "sourceEndLine": 4,
        },
        "uses": [], "context": "", "relevantSource": [], "mirror": None,
    }
    identity = {
        "version": payload["version"],
        "targetKey": payload["targetKey"],
        "targetKind": payload["targetKind"],
        "statement": payload["statement"],
        "proof": payload["proof"],
        "proofAssociation": {
            key: payload["proofAssociation"][key]
            for key in ("status", "method", "sourceFile", "proofHash")
        },
        "uses": payload["uses"],
        "context": payload["context"],
        "relevantSource": payload["relevantSource"],
        "mirror": payload["mirror"],
    }
    payload["bundleHash"] = digest or hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return SourceBundle.model_validate(payload)



def test_start_and_retry_are_gone():
    for call in (lambda: routes.start_alignment_check("any", {}), lambda: routes.retry_alignment_check("any")):
        with pytest.raises(HTTPException) as error:
            call()
        assert error.value.status_code == 410
        assert error.value.detail["code"] == "lea_check_retired"


def test_historical_report_survives_restart_and_appears_in_status_history(tmp_path, monkeypatch):
    session, form = _fresh(tmp_path, monkeypatch)
    snapshot, _ = _snapshot(form["id"])
    row, _ = alignment_store.create(formalization_id=form["id"], project_id=None,
        session_id=session["id"], solver_run_id=None, trigger="manual", source_bundle=_bundle().model_dump(),
        artifact_snapshot=snapshot, artifact_hash=snapshot["revision_token"], artifact_commit=None,
        dependency_hash=_dependency_hash(snapshot), prompt_version="legacy", model="test", provider=None)
    alignment_store.complete(row["id"], report=dict(verdict="warning", summary="Historical source gap."), input_tokens=1, output_tokens=1, cost_usd=0)
    db.init_db()
    assert routes.alignment_check_history(form["id"])["alignment_checks"][0]["report"]["summary"] == "Historical source gap."
    assert lea_status.history(form["id"])["legacy_reports"][0]["id"] == row["id"]
    assert lea_status.current(form["id"])["assessment"] is None
