"""First-class formalization read model.

Persistence lives in ``store``; this module batch-loads ledger evidence and
derives validity/activity without caching either on the formalization row.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from .artifacts import contains_sorry_marker
from .target_completion import checked_target, target_declaration
from .db import connect, row_to_dict
from . import store


def _validity(
    formalization: dict,
    *,
    primary: dict | None,
    artifact: dict | None,
    latest_step: dict | None,
    latest_run: dict | None,
    steps: list[dict] | None = None,
) -> tuple[str, str, bool, str | None]:
    """Current verdict, explanation, check freshness and declaration kind."""
    result_kind = (latest_run or {}).get("result_kind")
    if result_kind == "disproved":
        return "disproved", "A verified disproof was reported.", False, None
    current_steps = steps or []
    name = formalization.get("declaration_name")
    declaration = next(
        (decl for step in current_steps
         if (decl := target_declaration(step.get("code"), name, formalization.get("kind") or "theorem"))),
        None,
    )
    match, reason = checked_target(formalization, current_steps)
    check_current = any(step.get("check_status") == "ok" for step in current_steps)
    kind = declaration.kind if declaration else None
    # A review result is a verdict about this target. No later chat, file
    # classification or manual check may promote it.
    if result_kind == "needs_review":
        return "needs_review", (latest_run or {}).get("result_detail") or reason or "Lea requested review.", check_current, kind
    if not current_steps:
        if result_kind in {"proved", "defined"}:
            return "needs_review", "No current checked artifact contains the requested declaration.", False, kind
        return ("draft" if not name and primary is None else "planned"), "No current artifact is linked to this target.", False, kind
    if not declaration:
        return "needs_review", f"The artifact does not declare the requested {name or 'target'}.", check_current, None
    if any(step.get("check_status") == "error" for step in current_steps):
        return "failing", "The current artifact failed its Lean check.", False, kind
    if contains_sorry_marker(next((step.get("code") or "" for step in current_steps if target_declaration(step.get("code"), name, formalization.get("kind") or "theorem")), "")):
        return "failing", "The requested declaration still contains sorry or admit.", False, kind
    current_hash = formalization.get("source_hash")
    artifact_hash = (artifact or {}).get("source_hash")
    if current_hash and artifact_hash and current_hash != artifact_hash:
        return "stale", "The source statement changed after this artifact was recorded.", False, kind
    if not check_current:
        if "ambiguous" in reason:
            return "needs_review", reason, False, kind
        return "unchecked", reason, False, kind
    if match is None:
        if "ambiguous" in reason:
            return "needs_review", reason, check_current, kind
        return "unchecked", reason, check_current, kind
    if result_kind not in {"proved", "defined"}:
        return "unchecked", "No successful target run has certified this artifact.", True, kind
    if formalization.get("kind") != "definition" and result_kind != "proved":
        return "needs_review", "The run reported a definition rather than proving this theorem.", True, kind
    if match.get("run_id") != latest_run.get("id"):
        # A manual check of identical bytes may become the latest timeline
        # row. Verify the successful run's own checked snapshot by content,
        # so the later check neither creates nor erases certification.
        with connect() as conn:
            rows = conn.execute(
                """select t.*, b.content as blob_content from timeline t
                   left join artifact_blobs b on b.id = t.after_blob_id
                   where t.kind = 'code' and t.run_id = ?
                   order by t.id desc""",
                (latest_run["id"],),
            ).fetchall()
        seen_paths = set()
        run_steps = []
        for row in rows:
            if row["path"] not in seen_paths:
                seen_paths.add(row["path"])
                run_steps.append(store._code_step_from_row(row))
        certified, _ = checked_target(formalization, run_steps)
        if not certified or certified.get("path") != match.get("path") or certified.get("code") != match.get("code"):
            return "needs_review", "The successful run did not check this target revision.", True, kind
    return ("defined" if formalization.get("kind") == "definition" else "proved"), "The requested declaration passed Lean in a successful target run.", True, kind


def _activity(run: dict | None) -> dict:
    if not run:
        return {"status": "idle", "run_id": None}
    if run.get("status") == "pending":
        status = "queued"
    elif run.get("pending_approval"):
        status = "waiting_for_approval"
    else:
        status = "running"
    return {"status": status, "run_id": run.get("id")}


def _rows_for_ids(conn, sql: str, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(sql.format(marks=marks), ids).fetchall()
    return [row_to_dict(row) for row in rows]


def decorate(rows: list[dict]) -> list[dict]:
    ids = [str(row["id"]) for row in rows]
    if not ids:
        return []
    with connect() as conn:
        files = _rows_for_ids(
            conn,
            """
            select * from formalization_files
            where formalization_id in ({marks})
            order by case role when 'primary' then 0 when 'support' then 1 else 2 end,
                     path asc
            """,
            ids,
        )
        artifacts = _rows_for_ids(
            conn,
            """
            select * from artifacts
            where formalization_id in ({marks})
            order by updated_at desc, id desc
            """,
            ids,
        )
        latest_steps = _rows_for_ids(
            conn,
            """
            select * from (
                select t.*, b.content as blob_content,
                       f.id as resolved_formalization_id,
                       row_number() over (
                           partition by f.id
                           order by
                             case ff.role
                               when 'primary' then 0
                               when 'support' then 1
                               else 2
                             end,
                             t.created_at desc,
                             t.id desc
                       ) as rn
                from formalizations f
                join formalization_files ff on ff.formalization_id = f.id
                join timeline t on t.kind = 'code' and t.path = ff.path
                join sessions s on s.id = t.session_id
                left join artifact_blobs b on b.id = t.after_blob_id
                where f.id in ({marks})
                  and (
                    (f.project_id is not null and s.project_id = f.project_id)
                    or
                    (f.project_id is null and exists (
                      select 1 from session_formalizations sf
                      where sf.formalization_id = f.id
                        and sf.session_id = t.session_id
                    ))
                  )
            ) where rn = 1
            """,
            ids,
        )
        active_runs = _rows_for_ids(
            conn,
            """
            select * from (
                select r.*,
                       row_number() over (
                           partition by r.focus_formalization_id
                           order by r.created_at desc, r.id desc
                       ) as rn
                from runs r
                where r.focus_formalization_id in ({marks})
                  and r.status in ('pending', 'running')
            ) where rn = 1
            """,
            ids,
        )
        latest_runs = _rows_for_ids(
            conn,
            """
            select * from (
                select r.*,
                       row_number() over (
                           partition by r.focus_formalization_id
                           order by r.created_at desc, r.id desc
                       ) as rn
                from runs r
                where r.focus_formalization_id in ({marks})
            ) where rn = 1
            """,
            ids,
        )
        completion_runs = _rows_for_ids(
            conn,
            """
            select * from (
                select r.*, row_number() over (
                    partition by r.focus_formalization_id
                    order by r.created_at desc, r.id desc
                ) as rn
                from runs r
                where r.focus_formalization_id in ({marks})
                  and r.result_kind in ('proved', 'defined', 'needs_review', 'disproved')
            ) where rn = 1
            """,
            ids,
        )
        verifications = _rows_for_ids(
            conn,
            """
            select * from (
                select v.*,
                       row_number() over (
                           partition by v.formalization_id
                           order by v.created_at desc, v.id desc
                       ) as rn
                from verification_events v
                where v.formalization_id in ({marks})
            ) where rn = 1
            """,
            ids,
        )
        sessions = _rows_for_ids(
            conn,
            """
            select sf.formalization_id, s.id, s.title, s.updated_at
            from session_formalizations sf
            join sessions s on s.id = sf.session_id
            where sf.formalization_id in ({marks})
              -- ROOTS only. A sub-agent is a session row whose `parent_id` is the
              -- coordinator that spawned it. The UI uses `sessions[0]` as the row's
              -- click target, and a child session opens READ-ONLY with a provenance
              -- bar — so a formalization could send you to an internal child instead
              -- of the conversation you actually had. A child is meaningful only from
              -- inside its coordinator's thread, which is where it stays reachable.
              and s.parent_id is null
            order by s.updated_at desc
            """,
            ids,
        )

    by_files: dict[str, list[dict]] = {}
    by_sessions: dict[str, list[dict]] = {}
    for item in files:
        by_files.setdefault(str(item["formalization_id"]), []).append(item)
    for item in sessions:
        by_sessions.setdefault(str(item["formalization_id"]), []).append(
            {
                "id": item["id"],
                "title": item["title"],
                "updated_at": item["updated_at"],
            }
        )
    first_artifact: dict[str, dict] = {}
    for item in artifacts:
        first_artifact.setdefault(str(item["formalization_id"]), item)
    step_by_id = {
        str(item["resolved_formalization_id"]): item for item in latest_steps
    }
    active_by_id = {str(item["focus_formalization_id"]): item for item in active_runs}
    run_by_id = {str(item["focus_formalization_id"]): item for item in latest_runs}
    completion_by_id = {str(item["focus_formalization_id"]): item for item in completion_runs}
    verify_by_id = {str(item["formalization_id"]): item for item in verifications}

    result = []
    for raw in rows:
        item = dict(raw)
        fid = str(item["id"])
        item_files = by_files.get(fid, [])
        primary = next((f for f in item_files if f["role"] == "primary"), None)
        artifact = first_artifact.get(fid)
        step = step_by_id.get(fid)
        active = active_by_id.get(fid)
        latest_run = run_by_id.get(fid)
        completion_run = completion_by_id.get(fid)
        current_steps = store.current_code_steps_for_formalization(fid)
        # A partial run may only have support files and no artifact-index entry.
        # Its own snapshots still provide evidence for Needs review.
        if not current_steps and completion_run:
            with connect() as conn:
                raw_steps = conn.execute(
                    """select t.*, b.content as blob_content from timeline t
                       left join artifact_blobs b on b.id = t.after_blob_id
                       where t.kind = 'code' and t.run_id = ?
                       order by t.created_at desc, t.id desc""",
                    (completion_run["id"],),
                ).fetchall()
            seen_paths = set()
            for raw_step in raw_steps:
                if raw_step["path"] not in seen_paths:
                    seen_paths.add(raw_step["path"])
                    current_steps.append(store._code_step_from_row(raw_step))
        validity, validity_reason, check_current, declaration_kind = _validity(
            item, primary=primary, artifact=artifact,
            latest_step=step, latest_run=completion_run, steps=current_steps,
        )
        verification = verify_by_id.get(fid)
        if active and isinstance(active.get("pending_approval"), str):
            try:
                active["pending_approval"] = json.loads(active["pending_approval"])
            except json.JSONDecodeError:
                active["pending_approval"] = None
        safe_verify = None
        if verification:
            safe_verify = {
                "id": verification["id"],
                "status": verification["status"],
                "detail": verification.get("detail"),
                "path": verification["path"],
                "code_step_id": (
                    str(verification["code_step_id"])
                    if verification.get("code_step_id") is not None else None
                ),
                "current": bool(
                    step
                    and verification.get("code_step_id") is not None
                    and str(verification["code_step_id"]) == str(step["id"])
                ),
                "created_at": verification["created_at"],
            }
        item.update(
            {
                "validity_status": validity,
                "validity_reason": validity_reason,
                "completion_run_id": completion_run["id"] if completion_run else None,
                "declaration_kind": declaration_kind,
                "check_current": check_current,
                "activity": _activity(active),
                "primary_path": (
                    primary["path"] if primary else (artifact or {}).get("path")
                ),
                "files": item_files,
                "artifact": artifact,
                "latest_check": (
                    {
                        "code_step_id": str(step["id"]),
                        "path": step["path"],
                        "status": step["check_status"],
                        "detail": step["check_detail"],
                        "created_at": step["created_at"],
                    }
                    if step else None
                ),
                "safe_verify": safe_verify,
                "sessions": by_sessions.get(fid, []),
                "latest_run": (
                    {
                        "id": latest_run["id"],
                        "status": latest_run["status"],
                        "result_kind": latest_run.get("result_kind"),
                        "result_detail": latest_run.get("result_detail"),
                    }
                    if latest_run else None
                ),
            }
        )
        result.append(item)
    return result


def get(formalization_id: str) -> dict | None:
    row = store.get_formalization(formalization_id)
    if not row:
        return None
    return decorate([row])[0]


def _revision_token(steps: list[dict]) -> str | None:
    parts = [
        f"{step['path']}:{step.get('blob_sha256') or step.get('blob_id') or step['id']}"
        for step in sorted(steps, key=lambda item: item["path"])
    ]
    if not parts:
        return None
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def current_snapshot(
    formalization_id: str,
    *,
    conversation_session_id: str | None = None,
) -> dict | None:
    """Canonical project view plus the optional open conversation's revision."""
    formalization = get(formalization_id)
    if formalization is None:
        return None
    current_files = store.current_code_steps_for_formalization(formalization_id)
    conversation_files = (
        store.current_code_steps_for_formalization(
            formalization_id, session_id=conversation_session_id
        )
        if conversation_session_id else []
    )
    current_revision = _revision_token(current_files)
    conversation_revision = _revision_token(conversation_files)
    newest = max(
        current_files,
        key=lambda item: (item.get("created_at") or "", int(item["id"])),
        default=None,
    )
    conversation_newest = max(
        conversation_files,
        key=lambda item: (item.get("created_at") or "", int(item["id"])),
        default=None,
    )
    return {
        "formalization_id": formalization_id,
        "project_id": formalization.get("project_id"),
        "revision_token": current_revision,
        "files": current_files,
        "last_updated_session": (
            {
                "id": newest["session_id"],
                "title": newest.get("updating_session_title") or "Conversation",
            }
            if newest else None
        ),
        "last_updated_at": newest.get("created_at") if newest else None,
        "conversation": (
            {
                "session_id": conversation_session_id,
                "revision_token": conversation_revision,
                "files": conversation_files,
                "last_updated_at": (
                    conversation_newest.get("created_at")
                    if conversation_newest else None
                ),
                "is_current": bool(
                    current_revision
                    and conversation_revision
                    and current_revision == conversation_revision
                ),
            }
            if conversation_session_id else None
        ),
        "validity_status": formalization["validity_status"],
        "safe_verify": formalization.get("safe_verify"),
    }


def for_project(project_id: str) -> list[dict]:
    return decorate(store.list_raw_project_formalizations(project_id))


def for_session(session_id: str) -> list[dict]:
    return decorate(store.list_raw_session_formalizations(session_id))


def search(query: str, limit: int = 30) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{store._escape_like(q)}%"
    with connect() as conn:
        rows = conn.execute(
            """
            select f.*, p.title as project_title, p.namespace as project_namespace
            from formalizations f
            left join projects p on p.id = f.project_id
            where f.display_title like ? escape '\\'
               or f.declaration_name like ? escape '\\'
               or f.statement like ? escape '\\'
               or exists (
                   select 1 from formalization_files ff
                   where ff.formalization_id = f.id
                     and ff.path like ? escape '\\'
               )
            order by f.updated_at desc
            limit ?
            """,
            (like, like, like, like, limit),
        ).fetchall()
    raw = [row_to_dict(row) for row in rows]
    project_tags = {
        str(item["id"]): {
            "project_title": item.pop("project_title", None),
            "project_namespace": item.pop("project_namespace", None),
        }
        for item in raw
    }
    items = decorate(raw)
    for item in items:
        item.update(project_tags[str(item["id"])])
    return items


def summary(items: list[dict]) -> dict:
    counts = Counter(str(item.get("validity_status") or "unknown") for item in items)
    return {
        "formalization_count": len(items),
        **dict(counts),
        "active_run_count": sum(
            1 for item in items
            if (item.get("activity") or {}).get("status") != "idle"
        ),
    }
