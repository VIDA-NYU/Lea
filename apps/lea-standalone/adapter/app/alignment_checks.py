"""Read-only access to historical Lea Check reports. Independent evaluation is retired."""
from . import alignment_store
from .artifact_snapshots import _snapshot, _dependency_hash


def current(formalization_id: str, source_bundle_hash: str | None = None) -> dict:
    captured = _snapshot(formalization_id)
    history = alignment_store.history(formalization_id)
    if captured is None:
        return {"lea_check": {"status": "N/A", "reason": "no_artifact", "current": True}}
    snapshot, _ = captured
    dependency_hash = _dependency_hash(snapshot)
    if source_bundle_hash:
        check = alignment_store.latest_for_snapshot(
            formalization_id, source_bundle_hash, snapshot["revision_token"], dependency_hash,
        )
    else:
        check = next((item for item in history if item["artifact_hash"] == snapshot["revision_token"]), None)
    if check:
        return public(check, current=True)
    return {
        "lea_check": {
            "status": "N/A",
            "reason": "superseded" if history else "not_evaluated",
            "current": True,
            "history_count": len(history),
        }
    }


def public(check: dict | None, *, current: bool) -> dict:
    if not check:
        return {"lea_check": {"status": "N/A", "reason": "not_evaluated", "current": current}}
    status = check["status"]
    value = {
        "pending": "in-progress", "running": "in-progress", "paused": "paused",
        "failed": "error", "completed": check.get("verdict") or "error",
    }.get(status, "error")
    phase = "queued" if status == "pending" else "evaluating" if status == "running" else "settled"
    report = check.get("report") if isinstance(check.get("report"), dict) else None
    findings = report.get("findings") if report and isinstance(report.get("findings"), list) else []
    warning_count = sum(1 for item in findings if isinstance(item, dict) and item.get("severity") == "warning")
    caveat_count = (
        sum(1 for item in findings if isinstance(item, dict) and item.get("severity") == "caveat")
        + (len(report.get("caveats")) if report and isinstance(report.get("caveats"), list) else 0)
    )
    return {
        "lea_check": {
            "id": check["id"],
            "status": value,
            "phase": phase,
            "current": current,
            "reason": check.get("stop_reason") or ("evaluation_failed" if status == "failed" else None),
            "report": report,
            "report_available": report is not None,
            "summary": report.get("summary") if report else None,
            "warning_count": warning_count,
            "caveat_count": caveat_count,
            "error": check.get("error"),
            "source_bundle_hash": check.get("source_bundle_hash"),
            "artifact_hash": check.get("artifact_hash"),
            "artifact_commit": check.get("artifact_commit"),
            "dependency_hash": check.get("dependency_hash"),
            "target_kind": check.get("target_kind"),
            "target_label": check.get("target_label"),
            "report_schema_version": check.get("report_schema_version", 1),
            "attempt": check.get("attempt"),
            "trigger": check.get("trigger"),
            "model": check.get("model"),
            "usage": {
                "input_tokens": check.get("input_tokens", 0),
                "output_tokens": check.get("output_tokens", 0),
                "cost_usd": check.get("cost_usd", 0.0),
                "usage_details": check.get("usage_details"),
            },
            "created_at": check.get("created_at"),
            "completed_at": check.get("completed_at"),
        }
    }
