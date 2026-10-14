"""Live assessment publication and revision-aware projections. Never calls a model."""
from __future__ import annotations
import base64
import json
import os
from lea.status_reporting import attention, validate_payload
from . import lea_status_store as storage, store
from .artifact_snapshots import _snapshot, _dependency_hash


def admission_enabled():
    return os.environ.get("LEA_LIVE_STATUS_ENABLED", "1").lower() not in {"0", "false", "no"}


def seed(run_id):
    ctx = storage.context(run_id)
    if not ctx:
        return {}
    previous = storage.get(ctx["inherited_update_id"]) if ctx.get("inherited_update_id") else None
    return {"source_identity_hash": ctx["source_identity_hash"], "source_bundle": ctx["source_bundle"],
            "previous_assessment": previous["assessment"] if previous else None,
            "instruction": "Prior findings are context to reassess, not permission to change the theorem or proof approach."}


def publish(run_id, invocation_key, payload):
    payload = validate_payload(payload)
    ctx = storage.context(run_id)
    if not ctx:
        raise ValueError("No source-bound Lea Status context for this run")
    replay = storage.invocation(run_id, invocation_key)
    if replay:
        if replay["payload"] != payload:
            raise ValueError("Invocation identity already used with different content")
        return replay, event_payload(ctx, replay)
    captured = _snapshot(ctx["formalization_id"], run_id=run_id)
    snapshot = captured[0] if captured else {"revision_token": None, "files": []}
    if not snapshot["files"] and payload["scope"] != "source_only":
        raise ValueError("No captured artifact: use source_only scope")
    update = storage.append(run_id, invocation_key, payload, snapshot, _dependency_hash(snapshot))
    return update, event_payload(ctx, update)


def event_payload(ctx, update):
    return {"formalization_id": ctx["formalization_id"], "run_id": ctx["run_id"],
            "run_generation": ctx["run_generation"], "contract_version": 1,
            "latest_update": update, "assessment": update["assessment"],
            "sequence": update["sequence"], "attention": attention(update["assessment"]),
            "source_identity_hash": ctx["source_identity_hash"], "source_bundle_hash": ctx["source_bundle_hash"]}


def current(formalization_id, source_identity_hash=None, source_bundle_hash=None, *, source_bundle=None, coverage="partial"):
    if source_bundle is not None:
        from .source_context import compare_source, validate_source
        source_bundle = validate_source(source_bundle)
    if store.get_formalization(formalization_id) is None:
        raise LookupError("Formalization not found")
    ctx = storage.selected_context(formalization_id)
    if not ctx:
        return {"formalization_id": formalization_id, "assessment": None, "attention": "none",
                "freshness": "unknown", "activity": {"status": "idle"}, "latest_update": None}
    update = storage.latest(ctx["run_id"])
    run = store.get_run(ctx["run_id"]) or {}
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
        if snapshot["revision_token"] != update["artifact_snapshot"].get("revision_token"):
            result["freshness"] = "newer_artifact"
        elif _dependency_hash(snapshot) != update["dependency_hash"]:
            result["freshness"] = "dependency_changed"
    result["reporting_incomplete"] = run.get("status") not in {"pending", "running"} and (not update or update["kind"] != "final")
    result["previous_assessment"] = storage.get(ctx["inherited_update_id"]) if not update and ctx.get("inherited_update_id") else None
    return result


def history(formalization_id, after=None, limit=50):
    if store.get_formalization(formalization_id) is None:
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
    from . import alignment_store, alignment_checks
    legacy = [alignment_checks.public(row, current=False)["lea_check"] for row in alignment_store.history(formalization_id)] if not after else []
    return {"updates": updates, "next_cursor": cursor, "has_more": more, "legacy_reports": legacy}
