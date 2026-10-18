import base64
import hashlib
import json
from . import sessions as store, status_storage as storage, core
from .proofs import get as get_formalization

def _snapshot(fid):
    value=core.fact(core.scope_for(formalization_id=fid),'artifact_snapshot',fid)
    return (value,[]) if value else None

def _dependency_hash(snapshot):
    return hashlib.sha256('\n'.join(sorted(f"{i['path']}:{i.get('sha256') or i.get('blob_id')}" for i in snapshot['files'] if i.get('role')=='dependency')).encode()).hexdigest()

def _get_run(run_id):
    from .. import db
    with db.connect() as conn:
        row=conn.execute('select * from runs where id=?',(run_id,)).fetchone()
    return dict(row) if row else None

def attention(assessment: dict | None) -> str:
    findings = (assessment or {}).get("findings", [])
    active = [f for f in findings if f.get("source_resolution") not in {"resolved", "retracted"}
              and (f.get("source_resolution") == "open" or f.get("lean_resolution") in {"open", "planned"})]
    if any(f["severity"] == "blocking" for f in active):
        return "needs_author_input"
    return "source_issue" if any(f["severity"] == "warning" for f in active) else "none"

def event_payload(ctx, update):
    return {"formalization_id": ctx["formalization_id"], "run_id": ctx["run_id"],
            "run_generation": ctx["run_generation"], "contract_version": 1,
            "latest_update": update, "assessment": update["assessment"],
            "sequence": update["sequence"], "attention": attention(update["assessment"]),
            "source_identity_hash": ctx["source_identity_hash"], "source_bundle_hash": ctx["source_bundle_hash"]}

@core.read_query
def current(formalization_id, source_identity_hash=None, source_bundle_hash=None, *, source_bundle=None, coverage="partial"):
    if source_bundle is not None:
        from ..source_context import compare_source, validate_source
        source_bundle = validate_source(source_bundle)
    if get_formalization(formalization_id) is None:
        raise LookupError("Formalization not found")
    ctx = storage.selected_context(formalization_id)
    if not ctx:
        return {"formalization_id": formalization_id, "assessment": None, "attention": "none",
                "freshness": "unknown", "activity": {"status": "idle"}, "latest_update": None}
    update = storage.latest(ctx["run_id"])
    run = _get_run(ctx["run_id"]) or {}
    result = event_payload(ctx, update) if update else {
        "formalization_id": formalization_id, "run_id": ctx["run_id"], "run_generation": ctx["run_generation"],
        "assessment": None, "latest_update": None, "attention": "none", "sequence": 0,
        "source_identity_hash": ctx["source_identity_hash"], "source_bundle_hash": ctx["source_bundle_hash"],
    }
    result["activity"] = {"status": run.get("status", "unknown"), "stop_reason": run.get("stop_reason"),
                          "recoverable": bool(run.get("recoverable"))}
    result["waiting_dependencies"] = json.loads(run.get("waiting_dependencies_json") or "[]")
    result["freshness"] = "current" if source_identity_hash and source_bundle_hash else "unknown"
    if source_identity_hash and ctx["source_identity_hash"] != source_identity_hash:
        result["freshness"] = "source_changed"
    elif source_bundle_hash and ctx["source_bundle_hash"] != source_bundle_hash:
        result["freshness"] = "unknown"
        result["freshness_detail"] = "The evidence snapshots could not be compared; source freshness is unavailable."
    if source_bundle is not None:
        result["freshness"], detail = compare_source(ctx["source_bundle"], source_bundle, coverage)
        result.pop("freshness_detail", None)
        if detail:
            result["freshness_detail"] = detail
    if update and result["freshness"] not in {"source_changed", "evidence_changed"}:
        captured = _snapshot(formalization_id)
        snapshot = captured[0] if captured else {"revision_token": None, "files": []}
        if not captured:
            result["freshness"] = "unknown"
        elif snapshot["revision_token"] != update["artifact_snapshot"].get("revision_token"):
            result["freshness"] = "newer_artifact"
        elif _dependency_hash(snapshot) != update["dependency_hash"]:
            result["freshness"] = "dependency_changed"
    result["reporting_incomplete"] = run.get("status") not in {"pending", "running"} and (not update or update["kind"] != "final")
    result["previous_assessment"] = storage.get(ctx["inherited_update_id"]) if not update and ctx.get("inherited_update_id") else None
    result["projection"]=core.metadata(core.scope_for(formalization_id=formalization_id))
    if result["projection"]["state"]!="observed" and result["freshness"] not in {"source_changed","evidence_changed"}: result["freshness"]="unknown"
    return result

@core.read_query
def history(formalization_id, after=None, limit=50):
    if get_formalization(formalization_id) is None:
        raise LookupError("Formalization not found")
    sequence = 0
    if after:
        try:
            target, sequence = json.loads(base64.urlsafe_b64decode(after.encode()))
            if target != formalization_id or type(sequence) is not int or sequence < 0:
                raise ValueError()
        except Exception as exc:
            raise ValueError("Invalid or cross-target history cursor") from exc
    if not 1 <= limit <= 100:
        raise ValueError("History limit must be between 1 and 100")
    updates, more = storage.history(formalization_id, sequence, limit)
    end = updates[-1]["sequence"] if updates else sequence
    cursor = base64.urlsafe_b64encode(json.dumps([formalization_id, end]).encode()).decode()
    from . import alignment as alignment_checks
    alignment_store=alignment_checks
    legacy = [alignment_checks.public(row, current=False)["lea_check"] for row in alignment_store.history(formalization_id)] if not after else []
    return {"updates": updates, "next_cursor": cursor, "has_more": more, "legacy_reports": legacy}
