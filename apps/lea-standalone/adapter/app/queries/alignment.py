import json
from .. import db
from . import core,usage

def history(fid):
    with db.connect() as conn:
        rows=conn.execute('select * from alignment_checks where formalization_id=? order by created_at desc',(fid,)).fetchall()
    items=[]
    for r in rows:
        row=usage.decode(dict(r))
        row['report']=json.loads(row['report_json']) if row.get('report_json') else None
        items.append(row)
    return items

@core.read_query
def current(fid,source_bundle_hash=None):
    from .status import _snapshot,_dependency_hash
    captured=_snapshot(fid)
    if not captured:return {'lea_check':{'status':'N/A','reason':'observation_unavailable','current':False}}
    snapshot=captured[0];dep=_dependency_hash(snapshot)
    match=next((r for r in history(fid) if r.get('artifact_hash')==snapshot.get('revision_token') and
        (not source_bundle_hash or r.get('source_bundle_hash')==source_bundle_hash) and r.get('dependency_hash')==dep),None)
    return public(match,current=True)

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
