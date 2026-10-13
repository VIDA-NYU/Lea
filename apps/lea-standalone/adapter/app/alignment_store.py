"""SQLite persistence for immutable, revision-bound Lea alignment reports."""

from __future__ import annotations

import json
from uuid import uuid4

from .db import connect, row_to_dict, utc_now, write
from . import usage_details as usage_metadata


def _decode(row: dict | None) -> dict | None:
    if row is None:
        return None
    item = usage_metadata.decode(dict(row))
    for key in ("source_bundle_json", "artifact_snapshot_json", "report_json"):
        raw = item.pop(key, None)
        public_key = key.removesuffix("_json")
        item[public_key] = json.loads(raw) if raw else None
    return item


def get(check_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("select * from alignment_checks where id = ?", (check_id,)).fetchone()
    return _decode(row_to_dict(row)) if row else None


def create(
    *,
    formalization_id: str,
    project_id: str | None,
    session_id: str | None,
    solver_run_id: str | None,
    trigger: str,
    source_bundle: dict,
    artifact_snapshot: dict,
    artifact_hash: str,
    artifact_commit: str | None,
    dependency_hash: str,
    prompt_version: str,
    model: str | None,
    provider: str | None,
    retry_of: str | None = None,
) -> tuple[dict, bool]:
    now = utc_now()
    with write() as conn:
        attempt_row = conn.execute(
            """
            select coalesce(max(attempt), 0) as attempt
            from alignment_checks
            where formalization_id = ? and source_bundle_hash = ?
              and artifact_hash = ? and dependency_hash = ? and prompt_version = ?
            """,
            (
                formalization_id, source_bundle["bundleHash"], artifact_hash,
                dependency_hash, prompt_version,
            ),
        ).fetchone()
        latest_attempt = int(attempt_row["attempt"] or 0)
        if retry_of is None and latest_attempt:
            row = conn.execute(
                """
                select * from alignment_checks
                where formalization_id = ? and source_bundle_hash = ?
                  and artifact_hash = ? and dependency_hash = ? and prompt_version = ?
                order by attempt desc limit 1
                """,
                (
                    formalization_id, source_bundle["bundleHash"], artifact_hash,
                    dependency_hash, prompt_version,
                ),
            ).fetchone()
            return _decode(row_to_dict(row)), False
        check_id = str(uuid4())
        attempt = latest_attempt + 1
        conn.execute(
            """
            insert into alignment_checks (
                id, formalization_id, project_id, session_id, solver_run_id,
                evaluator_run_id, target_kind, target_label, status, trigger,
                source_bundle_hash, artifact_hash, artifact_commit, dependency_hash,
                source_bundle_json, artifact_snapshot_json, prompt_version,
                attempt, retry_of, model, provider, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                check_id, formalization_id, project_id, session_id, solver_run_id,
                check_id, source_bundle.get("targetKind") or "theorem",
                source_bundle.get("targetKey") or "", trigger,
                source_bundle["bundleHash"], artifact_hash, artifact_commit, dependency_hash,
                json.dumps(source_bundle, ensure_ascii=False),
                json.dumps(artifact_snapshot, ensure_ascii=False), prompt_version,
                attempt, retry_of, model, provider, now, now,
            ),
        )
        row = conn.execute("select * from alignment_checks where id = ?", (check_id,)).fetchone()
    return _decode(row_to_dict(row)), True


def mark_running(check_id: str) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            "update alignment_checks set status = 'running', started_at = ?, updated_at = ? where id = ?",
            (now, now, check_id),
        )


def complete(check_id: str, *, report: dict, input_tokens: int, output_tokens: int, cost_usd: float, usage_details: dict | None = None) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            update alignment_checks
            set status = 'completed', verdict = ?, report_json = ?,
                input_tokens = ?, output_tokens = ?, cost_usd = ?, usage_details_json = ?,
                completed_at = ?, updated_at = ?, error = null, stop_reason = null
            where id = ?
            """,
            (
                report.get("verdict"), json.dumps(report, ensure_ascii=False),
                input_tokens, output_tokens, cost_usd, usage_metadata.encode(usage_details), now, now, check_id,
            ),
        )


def fail(
    check_id: str,
    *,
    message: str,
    report: dict | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    usage_details: dict | None = None,
) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            update alignment_checks
            set status = 'failed', verdict = 'error', report_json = ?, error = ?,
                input_tokens = ?, output_tokens = ?, cost_usd = ?, usage_details_json = ?,
                completed_at = ?, updated_at = ? where id = ?
            """,
            (
                json.dumps(report, ensure_ascii=False) if report else None,
                message, input_tokens, output_tokens, cost_usd, usage_metadata.encode(usage_details), now, now, check_id,
            ),
        )


def pause(check_id: str, *, reason: str, report: dict | None = None) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            update alignment_checks
            set status = 'paused', report_json = ?, stop_reason = ?, paused_at = ?, updated_at = ? where id = ?
            """,
            (json.dumps(report, ensure_ascii=False) if report else None, reason, now, now, check_id),
        )


def recover_interrupted() -> int:
    """Pause checks whose in-memory evaluator disappeared during a restart."""
    now = utc_now()
    report = {
        "schema_version": 1,
        "verdict": "error",
        "scope": "partial_artifact",
        "summary": "Lea Check was interrupted when the adapter restarted.",
        "confidence": "low",
        "statement_match": "uncertain",
        "approach_match": "insufficient_evidence",
        "findings": [],
        "caveats": [],
        "remaining_obligations": ["Retry Lea Check to evaluate the same frozen inputs."],
        "limitations": ["The interrupted evaluator did not produce a trustworthy semantic verdict."],
        "recommended_next_action": "Retry Lea Check against the retained source and artifact snapshot.",
    }
    with connect() as conn:
        cursor = conn.execute(
            """
            update alignment_checks
            set status = 'paused', report_json = ?, stop_reason = 'adapter_restart',
                paused_at = ?, updated_at = ?
            where status in ('pending', 'running')
            """,
            (json.dumps(report, ensure_ascii=False), now, now),
        )
        return cursor.rowcount


def history(formalization_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "select * from alignment_checks where formalization_id = ? order by created_at desc, attempt desc",
            (formalization_id,),
        ).fetchall()
    return [_decode(row_to_dict(row)) for row in rows]


def latest_for_snapshot(
    formalization_id: str, source_hash: str, artifact_hash: str, dependency_hash: str,
) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """
            select * from alignment_checks
            where formalization_id = ? and source_bundle_hash = ?
              and artifact_hash = ? and dependency_hash = ?
            order by created_at desc, attempt desc limit 1
            """,
            (formalization_id, source_hash, artifact_hash, dependency_hash),
        ).fetchone()
    return _decode(row_to_dict(row)) if row else None
