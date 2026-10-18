"""SQL session, proof history and accounting projections. No workspace imports.

Extracted from the legacy store; worker observations supply freshness only.
"""
from __future__ import annotations

from .formatting import (_diagnostic_from_row, _duration_seconds, _message_from_row, _normalize_code_step, _normalize_project, _normalize_run_policy, _normalize_usage_breakdown_row, _normalize_usage_day, _normalize_usage_model, _normalize_usage_session, _optional_int)
import hashlib
import json
from contextlib import nullcontext
from typing import Any
from ..db import connect, row_to_dict
from . import usage as usage_metadata
from .core import read_query as read_projection, run_detail, approval_events_for_session, safe_verify as _safe_verify_summary


DEFAULT_SESSION_PAGE = 100

PROOF_EVIDENCE_COLUMNS = "e.evidence_json as proof_evidence_json, ep.namespace as evidence_namespace, es.parent_id as evidence_parent_id"

PROOF_EVIDENCE_JOINS = (
    " left join sessions es on es.id=t.session_id "
    "left join projects ep on ep.id=es.project_id "
    "left join proof_check_evidence e on e.id=t.check_evidence_id and e.blob_id=t.after_blob_id "
    "and e.session_id=t.session_id and e.path=t.path and e.project_id is es.project_id "
)

TIMELINE_WITH_BLOB = (
    f"select t.*, b.content as blob_content, {PROOF_EVIDENCE_COLUMNS} from timeline t "
    "left join artifact_blobs b on b.id = t.after_blob_id" + PROOF_EVIDENCE_JOINS
)

def get_session(session_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("select * from sessions where id = ?", (session_id,)).fetchone()
    return row_to_dict(row) if row else None

def list_sessions() -> list[dict]:
    """All sessions (loose + in-project), newest first. The sidebar uses
    `list_loose_sessions`; this stays the unfiltered view (usage stats, search)."""
    return _list_sessions()

def list_loose_sessions() -> list[dict]:
    """Loose sessions (`project_id IS NULL`) — the sidebar Chats group (D30). This
    INCLUDES sub-agent children (item 24): the tree is shipped whole and the frontend
    does the `roots = parent_id is null` / `childrenOf(id)` split (matching the design
    mock), because the contextual Sub-agents block needs the children in-store to
    render. In-project sessions are reached through the project window / search."""
    return _list_sessions("s.project_id is null")

def list_project_sessions(project_id: str) -> list[dict]:
    """Sessions belonging to one project — the project window's session list (D30).
    Includes children for the same reason as `list_loose_sessions`; the frontend splits
    roots from children."""
    return _list_sessions("s.project_id = ?", (project_id,))

def list_child_sessions(parent_id: str) -> list[dict]:
    """A coordinator's sub-agent children (item 24), newest first — a targeted read for
    callers that want just one coordinator's children (the frontend derives them from
    the full list, but the bridge/tests use this). Each carries its derived
    status/role/spawned_at_turn like any session row."""
    return _list_sessions("s.parent_id = ?", (parent_id,))

@read_projection
def _list_sessions(
    extra_where: str = "", params: tuple = (), limit: int = DEFAULT_SESSION_PAGE
) -> list[dict]:
    where_sql = f"where {extra_where}" if extra_where else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            select
                s.*,
                coalesce(sum(r.input_tokens), 0) as input_tokens,
                coalesce(sum(r.output_tokens), 0) as output_tokens,
                coalesce(sum(r.input_tokens + r.output_tokens), 0) as total_tokens,
                coalesce(sum(r.cost_usd), 0) as cost_usd,
                coalesce(sum(case when r.usage_status in ('pending', 'partial') then 1 else 0 end), 0) as incomplete_usage_runs,
                coalesce(sum(case when r.id is not null and (r.usage_status is null or r.usage_status = 'unknown') then 1 else 0 end), 0) as unconfirmed_usage_runs,
                count(distinct r.id) as run_count,
                (
                    select count(*)
                    from timeline m
                    where m.session_id = s.id and m.kind not in ('code', 'diagnostic')
                ) as message_count,
                (
                    select r2.model
                    from runs r2
                    where r2.session_id = s.id
                    order by r2.updated_at desc, r2.created_at desc
                    limit 1
                ) as primary_model,
                group_concat(distinct r.model) as models,
                p.id as project_id,
                p.slug as project_slug,
                p.title as project_title,
                p.namespace as project_namespace,
                p.repo_path as project_repo_path,
                s.created_at as started_at,
                s.updated_at as ended_at,
                (
                    select cs.check_status
                    from timeline cs
                    where cs.session_id = s.id and cs.kind = 'code'
                      and lower(cs.path) not like '%scratch%'
                    order by cs.id desc
                    limit 1
                ) as latest_check_status,
                (
                    select cs.artifact_kind
                    from timeline cs
                    where cs.session_id = s.id and cs.kind = 'code'
                      and lower(cs.path) not like '%scratch%'
                    order by cs.id desc
                    limit 1
                ) as latest_artifact_kind,
                coalesce(
                    (select cs.formalization_id from timeline cs
                     where cs.session_id = s.id and cs.kind = 'code'
                       and lower(cs.path) not like '%scratch%'
                     order by cs.id desc limit 1),
                    (select rcs.focus_formalization_id from runs rcs
                     where rcs.session_id = s.id
                       and rcs.result_kind in ('proved', 'defined', 'needs_review')
                     order by rcs.created_at desc, rcs.id desc limit 1)
                ) as latest_code_formalization_id,
                case when (
                    select rcs.result_kind from runs rcs
                    where rcs.session_id = s.id
                      and rcs.result_kind in ('proved', 'defined', 'needs_review')
                      and (
                        (select cs.formalization_id from timeline cs
                         where cs.session_id = s.id and cs.kind = 'code'
                           and lower(cs.path) not like '%scratch%'
                         order by cs.id desc limit 1) is null
                        or rcs.focus_formalization_id = (
                          select cs.formalization_id from timeline cs
                          where cs.session_id = s.id and cs.kind = 'code'
                            and lower(cs.path) not like '%scratch%'
                          order by cs.id desc limit 1)
                      )
                    order by rcs.created_at desc, rcs.id desc limit 1
                ) = 'needs_review' then 'needs_review' else (
                    select rcs.status from timeline cs
                    left join runs rcs on rcs.id = cs.run_id
                    where cs.session_id = s.id and cs.kind = 'code'
                      and lower(cs.path) not like '%scratch%'
                    order by cs.id desc limit 1
                ) end as latest_code_run_status,
                (
                    select count(*) from timeline cs
                    where cs.session_id = s.id and cs.kind = 'code'
                      and lower(cs.path) not like '%scratch%'
                ) as code_step_count,
                (
                    select count(*)
                    from runs r3
                    where r3.session_id = s.id and r3.status in ('pending', 'running')
                ) as active_run_count,
                (select rr.recovery_state from runs rr where rr.session_id = s.id
                 order by rr.created_at desc, rr.id desc limit 1) as latest_recovery_state,
                -- Sub-agents (bug-fix): a CHILD's final output — its last agent message —
                -- so the coordinator's spawn box can show a collapsed preview + expand
                -- without a second fetch. Gated on parent_id so a normal session's list
                -- row never carries a big prose blob it doesn't use.
                (
                    case when s.parent_id is not null then (
                        select tm.content
                        from timeline tm
                        where tm.session_id = s.id and tm.kind = 'message'
                          and tm.author = 'agent'
                        order by tm.id desc
                        limit 1
                    ) end
                ) as final_summary,
                -- The task the coordinator DELEGATED: a child's first user message,
                -- recorded at spawn. Same gating and rationale as final_summary — the
                -- spawn box can show what a child was asked to do without a second
                -- fetch, including for a child that is still running (which has no
                -- summary yet, and for which the task is the only judgeable content).
                (
                    case when s.parent_id is not null then (
                        select tm.content
                        from timeline tm
                        where tm.session_id = s.id and tm.kind = 'message'
                          and tm.author = 'user'
                        order by tm.id asc
                        limit 1
                    ) end
                ) as task,
                max(0, cast((julianday(s.updated_at) - julianday(s.created_at)) * 86400 as integer)) as duration_seconds
            from sessions s
            left join runs r on r.session_id = s.id
            left join projects p on p.id = s.project_id
            {where_sql}
            group by s.id
            order by s.updated_at desc
            limit ?
            """,
            (*params, int(limit)),
        ).fetchall()
    heads = {}
    if rows:
        with connect() as conn:
            ids = [row['id'] for row in rows]
            marks = ','.join('?' for _ in ids)
            latest = conn.execute(f"{TIMELINE_WITH_BLOB} where t.id in (select max(id) from timeline "
                f"where kind='code' and lower(path) not like '%scratch%' and session_id in ({marks}) group by session_id)", ids).fetchall()
        heads = {row['session_id']: _code_step_from_row(row) for row in latest}
    sessions = []
    status_targets = []
    for row in rows:
        data = row_to_dict(row)
        status_targets.append(data.pop("latest_code_formalization_id", None))
        # v2.3 item 13: keep the integer active-run count on the row (not just the
        # bool the derived status consumes). Derived status deliberately stays a
        # working-copy verdict (D14), so a session that already has code but is
        # re-running reads 'proved'/'ok', never 'running' — the sidebar needs this
        # separate signal to show a running dot for background runs.
        active_run_count = int(data.pop("active_run_count", 0) or 0)
        recorded_status = data.pop("latest_check_status", None)
        projected = heads.get(data['id'])
        recorded_status = recorded_status if projected and projected.get('check_current') else None
        data["status"] = _derive_session_status(
            recorded_status,
            data.pop("latest_artifact_kind", None),
            int(data.pop("code_step_count", 0) or 0),
            bool(active_run_count),
            data.pop("latest_code_run_status", None),
        )
        data["active_run_count"] = active_run_count
        sessions.append(_normalize_usage_session(data))
    if sessions:
        ids = [item["id"] for item in sessions]
        with connect() as conn:
            marks = ",".join("?" for _ in ids)
            detail_rows = conn.execute(f"select session_id,input_tokens,output_tokens,cost_usd,usage_details_json from runs where session_id in ({marks}) union all select session_id,input_tokens,output_tokens,cost_usd,usage_details_json from alignment_checks where session_id in ({marks})", (*ids, *ids)).fetchall()
        grouped = {}
        for r in detail_rows:
            grouped.setdefault(r["session_id"], []).append(r)
        for item in sessions:
            item["usage_details"] = usage_metadata.aggregate(grouped.get(item["id"], []))
    target_ids = list({fid for fid in status_targets if fid})
    if target_ids:
        from . import proofs as formalization_service
        with connect() as conn:
            marks = ",".join("?" for _ in target_ids)
            raw = [row_to_dict(row) for row in conn.execute(
                f"select * from formalizations where id in ({marks})", target_ids
            ).fetchall()]
        target_verdicts = {item['id']: item['validity_status'] for item in formalization_service.decorate(raw)
                           if item['validity_status'] in {'needs_review', 'disproved'} or item.get('review', {}).get('reason')}
        for session, fid in zip(sessions, status_targets):
            if fid in target_verdicts:
                session["status"] = target_verdicts[fid]
    return sessions

@read_projection
def session_detail(session_id: str) -> dict | None:
    session = get_session(session_id)
    if not session:
        return None
    with connect() as conn:
        usage_row = conn.execute(
            """
            select
                coalesce(sum(input_tokens), 0) as input_tokens,
                coalesce(sum(output_tokens), 0) as output_tokens,
                coalesce(sum(input_tokens + output_tokens), 0) as total_tokens,
                coalesce(sum(cost_usd), 0) as cost_usd,
                count(*) as run_count,
                (
                    select model
                    from runs
                    where session_id = ?
                    order by updated_at desc, created_at desc
                    limit 1
                ) as primary_model,
                group_concat(distinct model) as models
            from runs
            where session_id = ?
            """,
            (session_id, session_id),
        ).fetchone()
        # One table, one order (C4). These were two tables sharing a hand-rolled
        # counter so the frontend could merge them by a single key; now they're the
        # same rows, split apart on the way out only because the API shape predates
        # the merge. `id` is the order — nothing can disagree about it.
        # The blob rides along with the row (P3). This used to be a bare
        # `select * from timeline`, and the code steps were built AFTER the connection
        # closed — so every step then opened its own connection for its own blob. A
        # session with 200 steps opened 200 connections to render one thread.
        rows = conn.execute(
            f"{TIMELINE_WITH_BLOB} where t.session_id = ? order by t.id asc",
            (session_id,),
        ).fetchall()
        status_events = conn.execute(
            "select * from status_events where session_id = ? order by created_at asc",
            (session_id,),
        ).fetchall()
        active_run = conn.execute(
            """
            select *
            from runs
            where session_id = ? and status in ('pending', 'running')
            order by updated_at desc, created_at desc
            limit 1
            """,
            (session_id,),
        ).fetchone()
        latest_run = conn.execute(
            "select * from runs where session_id = ? order by created_at desc, id desc limit 1",
            (session_id,),
        ).fetchone()
        # Per-run outcomes (id + status), so the UI can place the "Proved"
        # milestone after the run that completed — live and on reload (M16).
        # Usage columns ride along for the Overleaf companion, whose
        # fetchApiRunUsage reads this run's tokens/cost off the persisted row
        # (they were missing here, so every companion job recorded $0 — caught
        # by the Phase 1 integration harness, PLAN-system-hardening).
        runs = conn.execute(
            "select id, status, result_kind, result_detail,"
            " input_tokens, output_tokens, cost_usd, usage_details_json, focus_formalization_id,"
            " focus_source_hash, attempt_id, recovery_state, recovery_reason, checkpoint_at, resumes_run_id,"
            " session_id, purpose, recoverable, stop_reason, created_at, client_request_id, autonomous, approval_mode"
            " from runs where session_id = ? order by created_at asc, id asc",
            (session_id,),
        ).fetchall()
        project = None
        if session.get("project_id"):
            project = conn.execute(
                "select * from projects where id = ?",
                (session["project_id"],),
            ).fetchone()
    from .core import recovery_action as available_action
    runs = [{**usage_metadata.decode(run_detail(dict(r))), "available_recovery_action": available_action(dict(r))} for r in runs]
    # Split back into the two lists the API exposes. Code rows carry their content
    # already — a read no longer needs a second store to be reachable, so there is
    # no separate hydrate step that can silently come back empty.
    # Three kinds out of one ordered read. A diagnostic must NOT fall into `messages`
    # — it would render as assistant speech and, worse, get replayed to the model as
    # conversation. It is the system reporting on itself (G1).
    messages = [_message_from_row(r) for r in rows if r["kind"] not in ("code", "diagnostic")]
    code_steps = [_code_step_from_row(r) for r in rows if r["kind"] == "code"]
    diagnostics_out = [_diagnostic_from_row(r) for r in rows if r["kind"] == "diagnostic"]
    usage = _normalize_usage_session(
        {
            **(row_to_dict(usage_row) if usage_row else {}),
            "message_count": len(messages),
            "started_at": session["created_at"],
            "ended_at": session["updated_at"],
            "duration_seconds": _duration_seconds(session["created_at"], session["updated_at"]),
        }
    )
    with connect() as conn:
        detail_rows = conn.execute("select input_tokens,output_tokens,cost_usd,usage_details_json from runs where session_id=? union all select input_tokens,output_tokens,cost_usd,usage_details_json from alignment_checks where session_id=?", (session_id, session_id)).fetchall()
    usage["usage_details"] = usage_metadata.aggregate(detail_rows)
    # working-copy verdict, derived from the latest *real* code_step (asc by seq).
    # Scratch/probe files (exact?/apply? scratchpads) are excluded so a session is
    # only 'ok' when an actual proof compiles, not when a throwaway probe does (M14).
    real_steps = [c for c in code_steps if "scratch" not in (c["path"] or "").lower()]
    latest_check_status = real_steps[-1]["check_status"] if real_steps and real_steps[-1].get("check_current") else None
    latest_artifact_kind = real_steps[-1]["artifact_kind"] if real_steps else None
    latest_code_run_status = None
    run_row = None
    if real_steps:
        with connect() as conn:
            run_row = conn.execute(
                """select result_kind, focus_formalization_id from runs where session_id = ?
                   and result_kind in ('proved', 'defined', 'needs_review')
                   and (? is null or focus_formalization_id = ?)
                   order by created_at desc, id desc limit 1""",
                (session_id, real_steps[-1].get("formalization_id"),
                 real_steps[-1].get("formalization_id")),
            ).fetchone()
            if run_row and run_row["result_kind"] == "needs_review":
                latest_code_run_status = "needs_review"
            elif real_steps[-1]["run_id"]:
                step_run = conn.execute(
                    "select status from runs where id = ?", (real_steps[-1]["run_id"],)
                ).fetchone()
                latest_code_run_status = step_run["status"] if step_run else None
    derived_status = _derive_session_status(
        latest_check_status, latest_artifact_kind, len(real_steps),
        active_run is not None, latest_code_run_status,
    )
    latest_formalization_id = (
        (real_steps[-1].get("formalization_id") if real_steps else None)
        or (run_row["focus_formalization_id"] if run_row else None)
    )
    if latest_formalization_id:
        from . import proofs as formalization_service
        target = formalization_service.get(latest_formalization_id)
        if target and (target['validity_status'] in {'needs_review', 'disproved'} or target.get('review', {}).get('reason')):
            derived_status = target["validity_status"]
    return {
        **session,
        **usage,
        "status": derived_status,
        "messages": messages,
        "code_steps": [_normalize_code_step(step) for step in code_steps],
        "diagnostics": diagnostics_out,
        "status_events": [row_to_dict(row) for row in status_events],
        "approval_events": approval_events_for_session(session_id),
        "review_events": review_events_for_session(session_id),
        "usage_breakdown": usage_breakdown_for_session(session_id),
        "active_run": _normalize_run(row_to_dict(active_run)) if active_run else None,
        "runs": [_normalize_run_policy(row_to_dict(r)) for r in runs],
        "safe_verify": _safe_verify_summary(row_to_dict(latest_run)) if latest_run else None,
        "project": _normalize_project(row_to_dict(project)) if project else None,
    }

@read_projection
def current_code_steps_for_formalization(
    formalization_id: str,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
) -> list[dict]:
    """Latest snapshot of every linked path for a formalization.

    With ``session_id`` this is the immutable conversation-local view. Without
    it, project formalizations resolve each path across every session in the
    shared project; loose formalizations resolve across their associated
    sessions. The query intentionally does not require the winning timeline row
    to carry this formalization id: two declarations may share one file, and a
    write attributed to either declaration changes the current bytes for both.
    """
    scope_clause = "t.session_id = ?"
    params: list[object] = [formalization_id]
    if session_id is not None:
        params.append(session_id)
    else:
        scope_clause = """
        (
          (f.project_id is not null and s.project_id = f.project_id)
          or
          (f.project_id is null and exists (
            select 1 from session_formalizations sf
            where sf.formalization_id = f.id and sf.session_id = t.session_id
          ))
        )
        """
    revision_clause = ""
    if run_id is not None:
        # A status run can assess its own writes and the revision it started from,
        # never a later write from another run sharing the same project file.
        revision_clause = "and (t.run_id = ? or t.created_at <= (select created_at from runs where id = ?))"
        params.extend([run_id, run_id])
    with connect() as conn:
        rows = conn.execute(
            f"""
            select * from (
              select
                t.*,
                b.content as blob_content, {PROOF_EVIDENCE_COLUMNS},
                b.sha256 as blob_sha256,
                ff.role as formalization_file_role,
                s.title as updating_session_title,
                row_number() over (
                  partition by ff.path
                  order by t.created_at desc, t.id desc
                ) as rn
              from formalization_files ff
              join formalizations f on f.id = ff.formalization_id
              join timeline t on t.kind = 'code' and t.path = ff.path
              join sessions s on s.id = t.session_id
              left join artifact_blobs b on b.id = t.after_blob_id {PROOF_EVIDENCE_JOINS}
              where ff.formalization_id = ? and {scope_clause} {revision_clause}
            )
            where rn = 1
            order by case formalization_file_role
                       when 'primary' then 0
                       when 'support' then 1
                       else 2
                     end,
                     path asc
            """,
            params,
        ).fetchall()
    result: list[dict] = []
    for row in rows:
        raw = row_to_dict(row)
        from .core import apply_step,scope_for
        step = apply_step(_code_step_from_row(row),scope_for(formalization_id=formalization_id))
        step.update(
            {
                "role": raw["formalization_file_role"],
                "blob_id": raw.get("after_blob_id"),
                "blob_sha256": raw.get("blob_sha256"),
                "updating_session_title": raw.get("updating_session_title"),
            }
        )
        result.append(step)
    return result

def current_proof_step(session_id: str, path: str, *, conn=None) -> dict | None:
    """Canonical path record with immutable blob identity, optionally in a write transaction."""
    with nullcontext(conn) if conn is not None else connect() as connection:
        session = connection.execute("select project_id from sessions where id=?", (session_id,)).fetchone()
        if not session:
            return None
        scope, scope_id = ("s.project_id", session["project_id"]) if session["project_id"] else ("t.session_id", session_id)
        row = connection.execute(
            f"{TIMELINE_WITH_BLOB} join sessions s on s.id=t.session_id "
            f"where t.kind='code' and t.path=? and {scope}=? order by t.created_at desc,t.id desc limit 1",
            (path, scope_id),
        ).fetchone()
        if not row:
            return None
        result = _code_step_from_row(row)
        result["blob_id"] = row["after_blob_id"]
        result["blob_sha256"] = hashlib.sha256(result["code"].encode()).hexdigest()
        return result

def blob_content(blob_id: str | None) -> str | None:
    """A blob's text, or None if absent (a `content_lost` row, or no blob)."""
    if not blob_id:
        return None
    with connect() as conn:
        row = conn.execute("select content from artifact_blobs where id = ?", (blob_id,)).fetchone()
    return row["content"] if row else None

def _code_step_from_row(row, *, code: str | None = None) -> dict:
    """A timeline code row in the shape the API has always returned.

    `code` is passed when the caller already has the bytes (it just wrote them).
    Otherwise it comes from the row's joined `blob_content` when the query used
    :data:`TIMELINE_WITH_BLOB`, and only failing that from a separate `blob_content()`
    lookup — the fallback that used to be the only path. A `content_lost` row yields
    `""`: the row survives to say a step happened, which is more honest than deleting
    history because its bytes are gone.
    """
    d = row_to_dict(row)
    if code is None:
        code = d.get("blob_content")
        if code is None:
            code = blob_content(d["after_blob_id"]) or ""
    d.pop("blob_content", None)
    result = {
        "id": str(d["id"]),
        "session_id": d["session_id"],
        "run_id": d["run_id"],
        "formalization_id": d.get("formalization_id"),
        "seq": d["id"],
        "turn": d["turn"],
        "author": d["author"],
        "path": d["path"],
        "summary": d["summary"],
        "check_status": d["check_status"],
        "check_detail": d["check_detail"],
        "artifact_kind": d["artifact_kind"],
        "content_lost": bool(d["content_lost"]),
        "created_at": d["created_at"],
        "code": code,
        "blob_id": d.get("after_blob_id"),
        "check_evidence_id": d.get("check_evidence_id"),
    }
    from .core import apply_step
    return apply_step(result)




def _derive_session_status(
    latest_check_status: str | None,
    latest_artifact_kind: str | None,
    code_step_count: int,
    has_active_run: bool = False,
    latest_code_run_status: str | None = None,
) -> str:
    """A session's status is its working-copy verdict (D14), derived — never stored.
    Once any code exists the verdict rules (latest step's check_status, or
    'unchecked' before it lands) — run lifecycle stays out of it, per D14. The one
    addition: a session with *no code yet* but an active run (pending/running) reads
    'running' instead of 'empty', so a freshly registered formalization — including
    an Overleaf-driven one whose first file hasn't been written yet — surfaces as
    in-progress in the session list and stats the moment it starts."""
    if code_step_count:
        if latest_code_run_status == "needs_review":
            return "needs_review"
        if latest_check_status == "ok":
            if latest_code_run_status == "disproved":
                return "disproved"
            if latest_artifact_kind == "definition":
                return "defined"
            if latest_artifact_kind in {"proof", "mixed"}:
                return "proved"
            return "ok"
        return latest_check_status or "unchecked"
    if has_active_run:
        return "running"
    return "empty"







def _normalize_run(row: dict) -> dict:
    row = _normalize_run_policy(row)
    row = usage_metadata.decode(run_detail(row))
    raw_pending = row.get("pending_approval")
    if isinstance(raw_pending, str) and raw_pending:
        try:
            row["pending_approval"] = json.loads(raw_pending)
        except json.JSONDecodeError:
            row["pending_approval"] = None
    else:
        row["pending_approval"] = None
    return row



def usage_breakdown_for_session(session_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            select *
            from run_usage_breakdown
            where session_id = ?
            order by run_number asc, ordinal asc, created_at asc
            """,
            (session_id,),
        ).fetchall()
    persisted = [_normalize_usage_breakdown_row(row_to_dict(row)) for row in rows]
    with connect() as conn:
        legacy=conn.execute('select usage_json from core_legacy_records where session_id=?',(session_id,)).fetchone()
    imported=json.loads(legacy[0]) if legacy else []
    represented={r['run_id'] for r in persisted}
    combined=[*persisted,*[r for r in imported if r.get('run_id') not in represented]]
    return sorted(combined,key=lambda r:(r.get('run_number',0),r.get('ordinal',0),r.get('created_at','')))

def review_events_for_session(session_id: str) -> list[dict]:
    with connect() as conn:
        return [dict(row) for row in conn.execute('''select id, formalization_id, run_id, action, outcome,
            approval_id, revision_token, created_at from formalization_review_events
            where session_id=? order by rowid''', (session_id,))]

@read_projection
def usage_stats(*, include_proof_status: bool = True) -> dict:
    # `sessions` stays the (100-row) list the Stats table renders — truncating a
    # rendered list is fine. `global` and `origins` are full-table aggregates, so
    # they no longer inherit that truncation.
    sessions = list_sessions() if include_proof_status else accounting_sessions()
    with connect() as conn:
        daily_rows = conn.execute(
            """
            select
                date(updated_at) as day,
                coalesce(sum(input_tokens), 0) as input_tokens,
                coalesce(sum(output_tokens), 0) as output_tokens,
                coalesce(sum(input_tokens + output_tokens), 0) as total_tokens,
                coalesce(sum(cost_usd), 0) as cost_usd,
                count(distinct id) as run_count,
                count(distinct session_id) as session_count
            from (
                select id, session_id, updated_at, input_tokens, output_tokens, cost_usd from runs
                union all
                select id, session_id, updated_at, input_tokens, output_tokens, cost_usd
                from alignment_checks
                union all
                select batch_id, null, updated_at, input_tokens, output_tokens, cost_usd
                from formalize_batch_reports where attempts > 0
            ) usage_events
            group by date(updated_at)
            order by day asc
            """
        ).fetchall()
        model_rows = conn.execute(
            """
            select
                model,
                coalesce(sum(input_tokens), 0) as input_tokens,
                coalesce(sum(output_tokens), 0) as output_tokens,
                coalesce(sum(input_tokens + output_tokens), 0) as total_tokens,
                coalesce(sum(cost_usd), 0) as cost_usd,
                count(*) as run_count,
                count(distinct session_id) as session_count
            from (
                select id, session_id, model, input_tokens, output_tokens, cost_usd from runs
                union all
                select id, session_id, model, input_tokens, output_tokens, cost_usd
                from alignment_checks
                union all
                select batch_id, null, model, input_tokens, output_tokens, cost_usd
                from formalize_batch_reports where attempts > 0
            ) usage_events
            group by model
            order by cost_usd desc, total_tokens desc
            """
        ).fetchall()

        detail_rows = conn.execute("select model,updated_at,input_tokens,output_tokens,cost_usd,usage_details_json from runs union all select model,updated_at,input_tokens,output_tokens,cost_usd,usage_details_json from alignment_checks union all select model,updated_at,input_tokens,output_tokens,cost_usd,usage_details_json from formalize_batch_reports where attempts>0").fetchall()
    by_model, by_day = {}, {}
    for r in detail_rows:
        by_model.setdefault(r["model"], []).append(r)
        by_day.setdefault(str(r["updated_at"])[:10], []).append(r)
    daily = [_normalize_usage_day(row_to_dict(row)) for row in daily_rows]
    models = [_normalize_usage_model(row_to_dict(row)) for row in model_rows]
    for row in daily:
        row["usage_details"] = usage_metadata.aggregate(by_day.get(row["day"], []))
    for row in models:
        row["usage_details"] = usage_metadata.aggregate(by_model.get(row["model"], []))

    return {
        "sessions": sessions,
        "origins": _origin_rollup(),
        "global": global_usage(),
        "alignment": alignment_usage(),
        "formalize_batch_reports": formalize_batch_report_usage(),
        "project_report_usage": project_report_usage(),
        "daily": daily,
        "models": models,
    }

def global_usage() -> dict:
    """The `global` block of `usage_stats` — every session and every run counted."""
    with connect() as conn:
        row = conn.execute(
            """
            select
                (select count(*) from sessions) as session_count,
                (select count(*) from timeline where kind != 'code') as message_count,
                (select coalesce(sum(input_tokens), 0) from runs)
                  + (select coalesce(sum(input_tokens), 0) from alignment_checks)
                  + (select coalesce(sum(input_tokens), 0) from formalize_batch_reports) as input_tokens,
                (select coalesce(sum(output_tokens), 0) from runs)
                  + (select coalesce(sum(output_tokens), 0) from alignment_checks)
                  + (select coalesce(sum(output_tokens), 0) from formalize_batch_reports) as output_tokens,
                (select coalesce(sum(cost_usd), 0) from runs)
                  + (select coalesce(sum(cost_usd), 0) from alignment_checks)
                  + (select coalesce(sum(cost_usd), 0) from formalize_batch_reports) as cost_usd,
                (select count(*) from runs where usage_status in ('pending', 'partial')) as incomplete_usage_runs,
                (select count(*) from runs where usage_status is null or usage_status = 'unknown') as unconfirmed_usage_runs
            """
        ).fetchone()
        details = usage_metadata.aggregate(conn.execute("select input_tokens,output_tokens,cost_usd,usage_details_json from runs union all select input_tokens,output_tokens,cost_usd,usage_details_json from alignment_checks union all select input_tokens,output_tokens,cost_usd,usage_details_json from formalize_batch_reports").fetchall())
    data = row_to_dict(row)
    session_count = int(data["session_count"] or 0)
    message_count = int(data["message_count"] or 0)
    input_tokens = int(data["input_tokens"] or 0)
    output_tokens = int(data["output_tokens"] or 0)
    total_tokens = input_tokens + output_tokens
    cost_usd = float(data["cost_usd"] or 0)
    return {
        "session_count": session_count,
        "message_count": message_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "usage_details": details,
        "incomplete_usage_runs": int(data["incomplete_usage_runs"] or 0),
        "unconfirmed_usage_runs": int(data["unconfirmed_usage_runs"] or 0),
        "average_tokens_per_session": round(total_tokens / session_count) if session_count else 0,
        "average_cost_per_session": cost_usd / session_count if session_count else 0,
        "average_messages_per_session": round(message_count / session_count) if session_count else 0,
    }

def accounting_sessions() -> list[dict]:
    """Full accounting rows, without source blobs or certification projections."""
    with connect() as conn:
        rows = conn.execute("""select s.id,p.slug as project_slug,
            coalesce(sum(r.input_tokens),0) as input_tokens,
            coalesce(sum(r.output_tokens),0) as output_tokens,
            coalesce(sum(r.cost_usd),0) as cost_usd,count(r.id) as run_count,
            coalesce(sum(r.usage_status in ('pending','partial')),0) as incomplete_usage_runs,
            coalesce(sum(r.id is not null and (r.usage_status is null or r.usage_status='unknown')),0)
                as unconfirmed_usage_runs
            from sessions s left join projects p on p.id=s.project_id
            left join runs r on r.session_id=s.id group by s.id order by s.updated_at desc""").fetchall()
        details = conn.execute("""select session_id,input_tokens,output_tokens,cost_usd,usage_details_json
            from runs union all select session_id,input_tokens,output_tokens,cost_usd,usage_details_json
            from alignment_checks""").fetchall()
    grouped = {}
    for row in details:
        grouped.setdefault(row['session_id'], []).append(row)
    return [{**dict(row), 'total_tokens': row['input_tokens'] + row['output_tokens'],
             'usage_details': usage_metadata.aggregate(grouped.get(row['id'], []))} for row in rows]

def alignment_usage() -> dict:
    """Evaluator-only usage, kept distinct while still included in global totals."""
    with connect() as conn:
        row = conn.execute(
            """
            select count(*) as run_count,
                   coalesce(sum(input_tokens), 0) as input_tokens,
                   coalesce(sum(output_tokens), 0) as output_tokens,
                   coalesce(sum(cost_usd), 0) as cost_usd
            from alignment_checks
            """
        ).fetchone()
    data = row_to_dict(row)
    input_tokens = int(data["input_tokens"] or 0)
    output_tokens = int(data["output_tokens"] or 0)
    return {
        "run_count": int(data["run_count"] or 0),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": float(data["cost_usd"] or 0),
    }

def formalize_batch_report_usage() -> dict:
    with connect() as conn:
        row = conn.execute("""select coalesce(sum(attempts), 0) as run_count,
            coalesce(sum(input_tokens), 0) as input_tokens,
            coalesce(sum(output_tokens), 0) as output_tokens,
            coalesce(sum(cost_usd), 0) as cost_usd from formalize_batch_reports""").fetchone()
    data = row_to_dict(row)
    return {"run_count": int(data["run_count"]), "input_tokens": int(data["input_tokens"]),
            "output_tokens": int(data["output_tokens"]), "cost_usd": float(data["cost_usd"])}

def project_report_usage() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("""select p.slug as project_slug,
            coalesce(sum(r.input_tokens), 0) as input_tokens,
            coalesce(sum(r.output_tokens), 0) as output_tokens,
            coalesce(sum(r.cost_usd), 0) as cost_usd,
            coalesce(sum(r.attempts), 0) as run_count
            from formalize_batch_reports r join projects p on p.id = r.project_id
            group by p.slug""").fetchall()
        metadata = conn.execute("select p.slug as project_slug,r.input_tokens,r.output_tokens,r.cost_usd,r.usage_details_json from formalize_batch_reports r join projects p on p.id=r.project_id").fetchall()
    results = [row_to_dict(row) for row in rows]
    for row in results:
        row["usage_details"] = usage_metadata.aggregate([r for r in metadata if r["project_slug"] == row["project_slug"]])
    return results

def _empty_origin_bucket(origin: str) -> dict:
    return {
        "origin": origin,
        "session_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    }

def _origin_rollup() -> list[dict]:
    """Per-origin usage rollup for the Stats "By origin" tab (Direct UI vs Overleaf).

    A full-table aggregate, like `global_usage` — its contract has always been that
    the two agree, and the way to keep that promise is for both to count everything.
    It used to fold the truncated `list_sessions()` page instead, which meant the
    per-origin totals and the global total were consistently wrong *together*
    (AUDIT-2026-07-24 C1).

    Both 'ui' and 'overleaf' rows are always emitted (zeros when absent) so the UI
    layout is stable. A NULL/blank origin falls back to 'ui'."""
    with connect() as conn:
        rows = conn.execute(
            """
            with usage_events as (
                select r.session_id, r.input_tokens, r.output_tokens, r.cost_usd
                from runs r
                union all
                select coalesce(ac.session_id, sr.session_id, f.loose_session_id) as session_id,
                       ac.input_tokens, ac.output_tokens, ac.cost_usd
                from alignment_checks ac
                left join runs sr on sr.id = ac.solver_run_id
                left join formalizations f on f.id = ac.formalization_id
            )
            select
                coalesce(nullif(trim(s.origin), ''), 'ui') as origin,
                count(distinct s.id) as session_count,
                coalesce(sum(u.input_tokens), 0) as input_tokens,
                coalesce(sum(u.output_tokens), 0) as output_tokens,
                coalesce(sum(u.cost_usd), 0) as cost_usd
            from sessions s
            left join usage_events u on u.session_id = s.id
            group by 1
            """
        ).fetchall()

    buckets: dict[str, dict] = {origin: _empty_origin_bucket(origin) for origin in ("ui", "overleaf")}
    for row in rows:
        data = row_to_dict(row)
        origin = str(data["origin"])
        bucket = buckets.setdefault(origin, _empty_origin_bucket(origin))
        bucket["session_count"] += int(data["session_count"] or 0)
        bucket["input_tokens"] += int(data["input_tokens"] or 0)
        bucket["output_tokens"] += int(data["output_tokens"] or 0)
        bucket["total_tokens"] += int(data["input_tokens"] or 0) + int(data["output_tokens"] or 0)
        bucket["cost_usd"] += float(data["cost_usd"] or 0)
    with connect() as conn:
        metadata_rows = conn.execute("select coalesce(nullif(trim(s.origin),''),'ui') as origin,r.input_tokens,r.output_tokens,r.cost_usd,r.usage_details_json from runs r join sessions s on s.id=r.session_id union all select coalesce(nullif(trim(s.origin),''),'ui'),a.input_tokens,a.output_tokens,a.cost_usd,a.usage_details_json from alignment_checks a left join runs sr on sr.id=a.solver_run_id left join formalizations f on f.id=a.formalization_id left join sessions s on s.id=coalesce(a.session_id,sr.session_id,f.loose_session_id)").fetchall()
        report_metadata = conn.execute("select input_tokens,output_tokens,cost_usd,usage_details_json from formalize_batch_reports").fetchall()
    for origin, bucket in buckets.items():
        bucket["usage_details"] = usage_metadata.aggregate([r for r in metadata_rows if r["origin"] == origin] + (report_metadata if origin == "overleaf" else []))
    report_usage = formalize_batch_report_usage()
    buckets["overleaf"]["input_tokens"] += report_usage["input_tokens"]
    buckets["overleaf"]["output_tokens"] += report_usage["output_tokens"]
    buckets["overleaf"]["total_tokens"] += report_usage["input_tokens"] + report_usage["output_tokens"]
    buckets["overleaf"]["cost_usd"] += report_usage["cost_usd"]
    # 'ui' and 'overleaf' first (stable UI order), then any unexpected origins.
    ordered = ["ui", "overleaf"] + [k for k in buckets if k not in ("ui", "overleaf")]
    return [buckets[k] for k in ordered]

def list_raw_project_formalizations(project_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            select * from formalizations
            where project_id = ?
            order by updated_at desc, display_title asc
            """,
            (project_id,),
        ).fetchall()
    return [row_to_dict(row) for row in rows]

def list_raw_session_formalizations(session_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            select f.*
            from formalizations f
            join session_formalizations sf on sf.formalization_id = f.id
            where sf.session_id = ?
            order by f.updated_at desc, f.display_title asc
            """,
            (session_id,),
        ).fetchall()
    return [row_to_dict(row) for row in rows]

def session_ids_for_formalization(formalization_id: str) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            """
            select sf.session_id
            from session_formalizations sf
            join sessions s on s.id = sf.session_id
            where sf.formalization_id = ?
            order by s.updated_at desc
            """,
            (formalization_id,),
        ).fetchall()
    return [str(row["session_id"]) for row in rows]

_SEARCH_FIELDS = (
    "id", "title", "status", "updated_at",
    "project_id", "project_title", "project_namespace",
)
def _escape_like(text: str) -> str:
    """Escape a user query for a LIKE pattern (so `%`/`_` are literal, not wildcards)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def search_sessions(query: str, limit: int = 30) -> list[dict]:
    """Sessions whose title — or whose project's title — matches `query`, newest first
    (D31/D41). Backs `GET /api/search`: the only way to reach a project session, which
    the sidebar hides. Case-insensitive SQLite LIKE (FTS5 is a later upgrade). Each hit
    carries its project tag so the overlay can section loose vs in-project. Blank → []."""
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{_escape_like(q)}%"
    # The limit goes into the QUERY, not a slice of the default page (C4). It used to
    # filter inside a query already truncated to the 100 most-recently-updated
    # sessions, so past that many a matching older session was simply unreachable —
    # and search is the ONLY path to an in-project session, which the sidebar hides.
    rows = _list_sessions(
        "(s.title like ? escape '\\' or p.title like ? escape '\\')",
        (like, like),
        limit=limit,
    )
    return [{field: row.get(field) for field in _SEARCH_FIELDS} for row in rows]

def list_projects() -> list[dict]:
    """All projects, newest first, each with a `session_count` (the sidebar shows
    it). Proof/node counts + status mix come from the live Lean state in later
    slices (the blueprint graph) — not stored here (DB-as-index, D4)."""
    with connect() as conn:
        rows = conn.execute(
            """
            select
                p.*,
                (select count(*) from sessions s where s.project_id = p.id) as session_count
            from projects p
            order by p.updated_at desc, p.title asc
            """
        ).fetchall()
    return [_normalize_project(row_to_dict(row)) for row in rows]
