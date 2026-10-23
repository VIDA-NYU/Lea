"""Pure response formatting shared by legacy workers and SQL queries."""
from __future__ import annotations
import json
from typing import Any
from ..db import row_to_dict
from . import usage as usage_metadata

def _message_from_row(row) -> dict:
    """A timeline message row in the shape the API has always returned.

    `role` is reconstructed from `author`: they were the same concept spelled
    twice, and the old `kind` column defaulted to 'assistant' — a *role* value used
    as a kind default — so it lied for every row nobody set explicitly. The new
    schema keeps `kind` for what a row *is* and `author` for who made it, which is
    the split OpenHands draws (`SourceType`) and opencode conflates.
    """
    d = row_to_dict(row)
    return {
        "id": str(d["id"]),
        "session_id": d["session_id"],
        "run_id": d["run_id"],
        "formalization_id": d.get("formalization_id"),
        "role": "user" if d["author"] == "user" else "assistant",
        "content": d["content"],
        "kind": d["kind"] if d["kind"] in ("edit_note", "compaction") else "assistant",
        "seq": d["id"],
        "created_at": d["created_at"],
        **({"live": d["stream_state"] == "streaming", "stream_state": d["stream_state"],
            "stream_attempt_id": d["stream_attempt_id"], "stream_sequence": d["stream_sequence"]}
           if d.get("stream_state") else {}),
    }

def _diagnostic_from_row(row) -> dict:
    """A timeline diagnostic row in the shape the API returns. The stored `data` JSON
    IS the payload that was streamed live, so a reloaded diagnostic and the one the
    user saw in real time cannot disagree (the same invariant the code rows hold)."""
    import json as _json

    d = row_to_dict(row)
    try:
        payload = _json.loads(d["data"]) if d["data"] else {}
    except (TypeError, ValueError):
        # A row whose JSON we can't parse still describes a real failure — degrade to
        # the prose we stored alongside it rather than dropping the row.
        payload = {}
    return {
        **payload,
        "id": str(d["id"]),
        "session_id": d["session_id"],
        "run_id": d["run_id"],
        "turn": d["turn"],
        "seq": d["id"],
        "message": payload.get("message") or d["content"] or "",
        "severity": payload.get("severity") or "notice",
        "code": payload.get("code") or "unknown",
        "context": payload.get("context") or {},
        "created_at": d["created_at"],
    }

def _normalize_project(row: dict) -> dict:
    row["overleaf_linked"] = bool(row.get("overleaf_linked"))
    return row

def _normalize_usage_session(row: dict) -> dict:
    models = [
        model for model in str(row.get("models") or "").split(",")
        if model
    ]
    row["models"] = models
    row["primary_model"] = row.get("primary_model") or (models[0] if models else None)
    for key in ("input_tokens", "output_tokens", "total_tokens", "message_count", "run_count", "duration_seconds"):
        row[key] = int(row.get(key) or 0)
    row["cost_usd"] = float(row.get("cost_usd") or 0)
    row["incomplete_usage_runs"] = int(row.get("incomplete_usage_runs") or 0)
    row["unconfirmed_usage_runs"] = int(row.get("unconfirmed_usage_runs") or 0)
    row["started_at"] = row.get("started_at")
    row["ended_at"] = row.get("ended_at")
    return row

def _normalize_code_step(row: dict) -> dict:
    # v2: a code_step is a plain pointer row (commit_sha + path + verdict); there
    # is nothing to decode. The JSON `used_project_formalizations` field was
    # dropped with the projects feature (deferred to v2.1). Kept as the single
    # read-side hook in case future presentation fields need shaping.
    return row

def _normalize_usage_day(row: dict) -> dict:
    for key in ("input_tokens", "output_tokens", "total_tokens", "run_count", "session_count"):
        row[key] = int(row.get(key) or 0)
    row["cost_usd"] = float(row.get("cost_usd") or 0)
    return row

def _normalize_usage_model(row: dict) -> dict:
    row["model"] = row.get("model") or "unknown"
    for key in ("input_tokens", "output_tokens", "total_tokens", "run_count", "session_count"):
        row[key] = int(row.get(key) or 0)
    row["cost_usd"] = float(row.get("cost_usd") or 0)
    return row

def _normalize_usage_breakdown_row(row: dict) -> dict:
    row = usage_metadata.decode(row)
    for key in ("run_number", "ordinal", "input_tokens", "output_tokens", "event_count"):
        row[key] = int(row.get(key) or 0)
    row["turn"] = _optional_int(row.get("turn"))
    row["candidate"] = _optional_int(row.get("candidate"))
    row["cost_usd"] = float(row.get("cost_usd") or 0)
    row["total_tokens"] = int(row["input_tokens"]) + int(row["output_tokens"])
    return row

def _normalize_run_policy(row: dict) -> dict:
    row["autonomous"] = bool(row.get("autonomous"))
    return row

def _duration_seconds(started_at: str | None, ended_at: str | None) -> int:
    if not started_at or not ended_at:
        return 0
    from datetime import datetime

    try:
        started = datetime.fromisoformat(started_at)
        ended = datetime.fromisoformat(ended_at)
    except ValueError:
        return 0
    return max(0, int((ended - started).total_seconds()))

def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) else None
