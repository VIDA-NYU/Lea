"""First-class formalization read model.

Persistence lives in ``store``; this module batch-loads ledger evidence and
derives validity/activity without caching either on the formalization row.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from .artifacts import contains_sorry_marker
from .target_completion import checked_target, target_declaration, compiler_result
from .db import connect, row_to_dict
from . import store
from .public_errors import run_detail
from .read_projections import read_projection


def _certified_namespace_revision(formalization: dict, certified: dict, current: dict, _depth=0) -> bool:
    """Accept only recorded, checked namespace replacements of certified bytes.

    Manual checks of identical bytes and successive renames are allowed. A
    semantic edit, unverified migration, or unrelated project cannot inherit a
    successful run's certification.
    """
    from .projects import rewrite_namespace_text

    if _depth > 32:
        return False
    if certified.get("path") != current.get("path"):
        with connect() as conn:
            receipt = conn.execute('select data from timeline where id=?', (current['id'],)).fetchone()
            try:
                data = json.loads(receipt['data'] or '{}')
            except (TypeError, ValueError):
                return False
            accepted = conn.execute("select state from reformalization_applications where id=?", (data.get('reformalization_application'),)).fetchone()
            if not accepted or accepted['state'] != 'published':
                return False
            for move in (data.get('preservation') or {}).get('relocations', []):
                named = target_declaration(current.get('code'), formalization.get('declaration_name'), formalization.get('kind') or 'theorem')
                if (not named or named.full_name != move['name'] or move['after_path'] != current['path']
                        or (data.get('preservation', {}).get('source_update') and move['name'] not in data['preservation'].get('stable_protected', []))):
                    continue
                parent = conn.execute(store.TIMELINE_WITH_BLOB + ' where t.id=? and t.id<?', (move.get('from_step_id'), int(current['id']))).fetchone()
                if parent and parent['after_blob_id'] == move['before_blob_id']:
                    previous = store._code_step_from_row(parent)
                    return _certified_namespace_revision(formalization, certified, previous, _depth + 1)
        return False
    if certified.get("code") == current.get("code"):
        return True
    project_id = formalization.get("project_id")
    if not project_id:
        return False
    with connect() as conn:
        rows = conn.execute(
            """select t.*, b.content as blob_content from timeline t
               join sessions s on s.id = t.session_id
               left join artifact_blobs b on b.id = t.after_blob_id
               where s.project_id = ? and t.kind = 'code' and t.path = ?
                 and t.id <= ? order by t.id desc""",
            (project_id, current["path"], current["id"]),
        ).fetchall()
    by_id = {str(row["id"]): row for row in rows}
    expected = current.get("code")
    for row in rows:
        if row["blob_content"] != expected or row["content_lost"]:
            continue
        try:
            data = json.loads(row['data'] or '{}')
            preserved = data.get('preservation') or {}
            original_decl = target_declaration(certified.get('code'), formalization.get('declaration_name'), formalization.get('kind') or 'theorem')
            protected_name = original_decl.full_name if original_decl else formalization.get('declaration_name')
            if (protected_name in preserved.get('protected', [])
                    and (not preserved.get('source_update') or protected_name in preserved.get('stable_protected', [])) and row['check_status'] == 'ok'):
                with connect() as conn:
                    accepted = conn.execute("select state from reformalization_applications where id=?", (data.get('reformalization_application'),)).fetchone()
                parent = by_id.get(str(preserved.get('from_step_id')))
                if (accepted and accepted['state'] == 'published' and parent and parent['id'] < row['id']
                        and parent['after_blob_id'] == preserved.get('before_blob_id')):
                    expected = parent['blob_content']
                    if expected == certified.get('code'):
                        return True
                    continue
        except (ValueError, TypeError):
            pass
        if row["author"] != "environment" or row["run_id"] is not None or row["check_status"] != "ok":
            continue
        try:
            migration = json.loads(row["data"] or "{}").get("namespace_migration") or {}
            old = store.validate_project_namespace(migration["old_namespace"])
            new = store.validate_project_namespace(migration["new_namespace"])
            parent = by_id.get(str(migration["from_step_id"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not parent or parent["id"] >= row["id"] or parent["content_lost"]:
            continue
        if rewrite_namespace_text(parent["blob_content"] or "", old, new) != expected:
            continue
        expected = parent["blob_content"]
        if expected == certified.get("code"):
            return True
    return False


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
    classifier_detail = (latest_run or {}).get('result_detail')
    if result_kind in {'proved', 'disproved'} and classifier_detail and classifier_detail.strip().upper() != result_kind.upper():
        return 'needs_review', 'The earlier completion classification needs review.', False, None
    if result_kind == "disproved":
        current_steps = steps or []
        if any(s.get('check_current') and s.get('check_status') == 'error' for s in current_steps):
            return 'failing', 'The current disproof artifact failed its Lean check.', False, None
        fresh = [s for s in current_steps if s.get('check_current') and s.get('check_status') == 'ok']
        if not fresh:
            return 'unchecked', 'The disproof needs a current Lean check.', False, None
        if formalization.get('source_hash') and (artifact or {}).get('source_hash') and formalization['source_hash'] != artifact['source_hash']:
            return 'stale', 'The source statement changed after this disproof was recorded.', False, None
        with connect() as conn:
            rows = conn.execute('''select t.*, b.content as blob_content from timeline t
                left join artifact_blobs b on b.id=t.after_blob_id where t.kind='code'
                and t.run_id=? order by t.id desc''', (latest_run['id'],)).fetchall()
        seen = set()
        for row in rows:
            if row['path'] in seen:
                continue
            seen.add(row['path'])
            actual = (artifact or {}).get('declaration_name') or formalization.get('declaration_name')
            scoped = compiler_result(dict(row), actual, 'theorem') if actual else None
            if row['check_status'] != 'ok' or scoped is False or (scoped is None and contains_sorry_marker(row['blob_content'] or '')):
                continue
            certified = {**dict(row), 'code': row['blob_content'] or ''}
            if any(_certified_namespace_revision(formalization, certified, current) for current in fresh):
                return 'disproved', 'A verified disproof was reported.', True, None
        return 'needs_review', 'The successful disproof run did not check this artifact revision.', True, None
    current_steps = steps or []
    name = formalization.get("declaration_name")
    declaration = next(
        (decl for step in current_steps
         if (decl := target_declaration(step.get("code"), name, formalization.get("kind") or "theorem"))),
        None,
    )
    match, reason = checked_target(formalization, current_steps)
    check_current = any(step.get("check_status") == "ok" and step.get("check_current") for step in current_steps)
    kind = declaration.kind if declaration else None
    # Review decisions are projected below, independently of run history.
    if result_kind == "needs_review":
        return "needs_review", (latest_run or {}).get("result_detail") or reason or "Lea requested review.", check_current, kind
    if not current_steps:
        if result_kind in {"proved", "defined"}:
            return "planned", "No current checked artifact contains the requested declaration.", False, kind
        return ("draft" if not name and primary is None else "planned"), "No current artifact is linked to this target.", False, kind
    if not declaration:
        return "unchecked", f"The artifact does not declare the requested {name or 'target'}.", check_current, None
    if any(step.get("check_status") == "error" and step.get("check_current") for step in current_steps):
        return "failing", "The current artifact failed its Lean check.", False, kind
    target_step = next((step for step in current_steps if target_declaration(step.get('code'), name, formalization.get('kind') or 'theorem')), None)
    if target_step and compiler_result(target_step, name, formalization.get('kind') or 'theorem') is None and contains_sorry_marker(target_step.get('code') or ''):
        return "failing", "The requested declaration still contains sorry or admit.", False, kind
    current_hash = formalization.get("source_hash")
    artifact_hash = (artifact or {}).get("source_hash")
    if current_hash and artifact_hash and current_hash != artifact_hash:
        return "stale", "The source statement changed after this artifact was recorded.", False, kind
    if not check_current:
        if "ambiguous" in reason:
            return "unchecked", reason, False, kind
        return "unchecked", reason, False, kind
    if match is None:
        if "ambiguous" in reason:
            return "unchecked", reason, check_current, kind
        return "unchecked", reason, check_current, kind
    if result_kind not in {"proved", "defined"}:
        return "unchecked", "No successful target run has certified this artifact.", True, kind
    if formalization.get("kind") != "definition" and result_kind != "proved":
        return "unchecked", "The run reported a definition rather than proving this theorem.", True, kind
    if match.get("run_id") != latest_run.get("id"):
        # A manual check of identical bytes may become the latest timeline
        # row. Verify the successful run's own checked snapshot by content (or
        # a checked mechanical namespace migration), so a later check neither
        # creates nor erases certification.
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
        certified, _ = checked_target(formalization, run_steps, require_current=False)
        if not certified and '.' in (formalization.get('declaration_name') or ''):
            certified, _ = checked_target({**formalization, 'declaration_name': formalization['declaration_name'].rsplit('.', 1)[-1]},
                                         run_steps, require_current=False)
        if not certified or not _certified_namespace_revision(formalization, certified, match):
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


@read_projection
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
                  and (r.reformalization_id is null or exists (
                    select 1 from reformalization_operations ro
                    where ro.id=r.reformalization_id and ro.state='settled'))
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
        retries = _rows_for_ids(conn, """select * from (
            select ro.*, row_number() over(partition by formalization_id order by rowid desc) as rn,
            exists(select 1 from reformalization_drafts d where d.operation_id=ro.id) as has_drafts
            from reformalization_operations ro where formalization_id in ({marks})) where rn=1""", ids)
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
    run_by_id = {str(item["focus_formalization_id"]): run_detail(item) for item in latest_runs}
    completion_by_id = {str(item["focus_formalization_id"]): run_detail(item) for item in completion_runs}
    verify_by_id = {str(item["formalization_id"]): item for item in verifications}
    retry_by_id = {str(item['formalization_id']): item for item in retries}

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
                    store.TIMELINE_WITH_BLOB + " where t.kind = 'code' and t.run_id = ? order by t.created_at desc, t.id desc",
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
        from . import formalization_reviews
        review, _, _, technical = formalization_reviews.project(
            {**item, 'files': item_files}, current_steps, primary, artifact, completion_run, active,
            requested=validity == 'needs_review')
        if technical is not None:
            validity, validity_reason, check_current, declaration_kind = technical
        from .reformalization_review import published_approval
        accepted_review = published_approval(item, current_steps)
        if accepted_review and check_current:
            review = {**review, 'approval': accepted_review, 'eligible': False,
                'run_id': (completion_run or {}).get('id'), 'reason': 'Approved draft published after current canonical checks.'}
            validity, validity_reason = accepted_review['outcome'], 'Approved by user.'
        certification = None
        if review.get('approval'):
            certification = {'source': 'user_review', 'outcome': review['approval']['outcome'],
                'review_event_id': review['approval']['id'], 'run_id': review.get('run_id')}
        elif validity in {'proved', 'defined', 'disproved'}:
            certification = {'source': 'run', 'outcome': validity, 'run_id': (completion_run or {}).get('id')}
        verification = verify_by_id.get(fid)
        retry = retry_by_id.get(fid)
        retry_result = json.loads(retry.get('result_json') or '{}') if retry else {}
        if item.get("origin") == "project_dependency":
            from . import project_dependencies
            with connect() as conn:
                dependency = conn.execute("select id from project_dependencies where formalization_id=?", (fid,)).fetchone()
            shared = project_dependencies.get(dependency[0], validate=False) if dependency else None
            if shared and shared.get("ready"):
                validity = "defined" if item.get("kind") == "definition" else "proved"
                validity_reason = "The pinned shared declaration passed its current module build and kernel audit."
                check_current = True
        if active and isinstance(active.get("pending_approval"), str):
            try:
                active["pending_approval"] = json.loads(active["pending_approval"])
            except json.JSONDecodeError:
                active["pending_approval"] = None
        safe_verify = None
        if verification:
            from .proof_evidence import verification_current
            safe_verify = {
                "id": verification["id"],
                "status": verification["status"],
                "detail": verification.get("detail"),
                "path": verification["path"],
                "code_step_id": (
                    str(verification["code_step_id"])
                    if verification.get("code_step_id") is not None else None
                ),
                "current": verification_current(verification, step),
                "created_at": verification["created_at"],
            }
        item.update(
            {
                "validity_status": validity,
                "validity_reason": validity_reason,
                "completion_run_id": completion_run["id"] if completion_run else None,
                "declaration_kind": declaration_kind,
                "check_current": check_current,
                "review": review,
                'reformalization': ({'id': retry['id'], 'state': retry['state'],
                    'has_drafts': bool(retry['has_drafts']), 'detail': retry_result.get('detail'),
                    'run_id': retry['root_run_id'], 'repair_needed': retry_result.get('repair_needed', [])}
                    if retry else None),
                "certification": certification,
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
        "review": formalization.get('review'),
        "certification": formalization.get('certification'),
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
