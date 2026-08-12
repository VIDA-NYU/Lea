from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections import Counter
from uuid import uuid4

from . import authoring as _authoring

from typing import Any

from .db import ROOT, connect, row_to_dict, utc_now, write


RAW_EVENT_LOG_DIR = ROOT / "data" / "lea-api-events"

# In-process "something about the session list changed" counter (AUDIT-2026-07-24 P4).
# `/api/sessions/events` polled `sessions_digest()` — a real query — once a second per
# connected client, forever, against the single-writer database the runs are writing
# to. Every write that can move the list bumps this instead, so an idle client costs
# an integer comparison. The SQL digest stays as a slow backstop: this counter only
# sees writes from THIS process, which is all of them today, and the backstop means a
# wrong assumption there degrades to the old latency rather than to silence.
_change_lock = threading.Lock()
_change_token = 0


def _bump_sessions_changed() -> None:
    global _change_token
    with _change_lock:
        _change_token += 1


def sessions_change_token() -> int:
    with _change_lock:
        return _change_token
PROJECT_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
PROJECT_NAMESPACE_RE = re.compile(r"^Lea\.[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*)*$")
# A skill slug is the stable id AND the materialized filename stem the prover reads
# as `## Skill: <slug>` (D45) — lower-kebab, letter/digit-initial, ≤80 chars.
SKILL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def create_session(
    title: str,
    project_id: str | None = None,
    origin: str = "ui",
    origin_url: str | None = None,
    parent_id: str | None = None,
    role: str | None = None,
    spawned_at_turn: int | None = None,
) -> dict:
    """Create a session. `origin` records providence ('ui' | 'overleaf'); for an
    Overleaf-spawned session `origin_url` is the canonical Overleaf document URL so
    the UI can open/focus the source document. Both default to the interactive-UI
    case so the existing path is unchanged.

    `parent_id`/`role`/`spawned_at_turn` (item 24) make this a sub-agent CHILD of the
    coordinator that spawned it: a child is a real session excluded from the root list
    (`parent_id is null`), tagged with its `role` (subagent_type) and the coordinator
    `turn` it was delegated on. All three default to None, so a root session is
    unchanged."""
    now = utc_now()
    session_id = str(uuid4())
    origin_value = (origin or "ui").strip() or "ui"
    with connect() as conn:
        conn.execute(
            "insert into sessions "
            "(id, project_id, title, origin, origin_url, parent_id, role, spawned_at_turn, created_at, updated_at) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, project_id, title[:120] or "Untitled theorem", origin_value, origin_url,
             parent_id, role, spawned_at_turn, now, now),
        )
        row = conn.execute("select * from sessions where id = ?", (session_id,)).fetchone()
    _bump_sessions_changed()
    return row_to_dict(row)


def touch_session(session_id: str) -> None:
    """Bump a session's updated_at. There is no stored status to set — a session's
    status is its working-copy verdict, derived from the latest code_step on read
    (D14). Run lifecycle is tracked on runs.status, not here."""
    now = utc_now()
    with connect() as conn:
        conn.execute("update sessions set updated_at = ? where id = ?", (now, session_id))
    _bump_sessions_changed()


def update_session_title(session_id: str, title: str) -> dict | None:
    with connect() as conn:
        conn.execute(
            "update sessions set title = ?, updated_at = ? where id = ?",
            (title[:120], utc_now(), session_id),
        )
        row = conn.execute(
            "select * from sessions where id = ?", (session_id,)
        ).fetchone()
    if row:
        _bump_sessions_changed()
        return row_to_dict(row)
    return None


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


# Fields the search endpoint returns per hit — the session plus its project tag. A
# light projection of the full `_list_sessions` dict (the overlay needs no usage rollups).
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


# The default page for the sidebar and the stats table. It is a RENDERING cap, not a
# fact about the data — `global_usage` and `_origin_rollup` deliberately do not use
# this query (AUDIT-2026-07-24 C1), and `search_sessions` passes its own (C4).
DEFAULT_SESSION_PAGE = 100


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
                (
                    select rcs.status
                    from timeline cs
                    left join runs rcs on rcs.id = cs.run_id
                    where cs.session_id = s.id and cs.kind = 'code'
                      and lower(cs.path) not like '%scratch%'
                    order by cs.id desc
                    limit 1
                ) as latest_code_run_status,
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
    sessions = []
    for row in rows:
        data = row_to_dict(row)
        # v2.3 item 13: keep the integer active-run count on the row (not just the
        # bool the derived status consumes). Derived status deliberately stays a
        # working-copy verdict (D14), so a session that already has code but is
        # re-running reads 'proved'/'ok', never 'running' — the sidebar needs this
        # separate signal to show a running dot for background runs.
        active_run_count = int(data.pop("active_run_count", 0) or 0)
        data["status"] = _derive_session_status(
            data.pop("latest_check_status", None),
            data.pop("latest_artifact_kind", None),
            int(data.pop("code_step_count", 0) or 0),
            bool(active_run_count),
            data.pop("latest_code_run_status", None),
        )
        data["active_run_count"] = active_run_count
        sessions.append(_normalize_usage_session(data))
    return sessions


def sessions_digest() -> str:
    """A cheap fingerprint of the session-list state, for the `/api/sessions/events`
    SSE feed to poll. Changes whenever a session is created or touched (max
    updated_at + count) or a run enters/leaves the active set (so a status flip from
    'running' → 'ok'/'error' is also detected). Deliberately avoids running the full
    `list_sessions` aggregate on every tick — that query only fires when this digest
    moves."""
    with connect() as conn:
        row = conn.execute(
            """
            select
                (select count(*) from sessions) as session_count,
                (select coalesce(max(updated_at), '') from sessions) as max_updated_at,
                (select count(*) from runs where status in ('pending', 'running')) as active_runs,
                (select coalesce(max(updated_at), '') from runs) as max_run_updated_at
            """
        ).fetchone()
    data = row_to_dict(row)
    return "|".join(
        str(data.get(key, ""))
        for key in ("session_count", "max_updated_at", "active_runs", "max_run_updated_at")
    )


def create_run(
    session_id: str,
    model: str,
    provider: str | None,
    max_turns: int | None,
    project_id: str | None = None,
    autonomous: bool = False,
    focus_formalization_id: str | None = None,
    focus_source_hash: str | None = None,
) -> dict:
    now = utc_now()
    run_id = str(uuid4())
    with connect() as conn:
        conn.execute(
            """
            insert into runs (
                id, session_id, project_id, status, autonomous, model, provider,
                max_turns, focus_formalization_id, focus_source_hash, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, session_id, project_id, "pending",
                1 if autonomous else 0, model, provider, max_turns,
                focus_formalization_id, focus_source_hash, now, now,
            ),
        )
        row = conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
    _bump_sessions_changed()
    return row_to_dict(row)


FORMALIZATION_KINDS = {
    "theorem", "lemma", "definition", "counterexample", "disproof", "other",
}
FORMALIZATION_FILE_ROLES = {"primary", "support", "generated"}


def _normalize_formalization_kind(kind: str | None) -> str:
    value = str(kind or "theorem").strip().lower()
    if value == "proof":
        value = "theorem"
    if value not in FORMALIZATION_KINDS:
        raise ValueError(f"unsupported formalization kind: {value}")
    return value


def _formalization_from_conn(conn, formalization_id: str) -> dict | None:
    row = conn.execute(
        "select * from formalizations where id = ?", (formalization_id,)
    ).fetchone()
    return row_to_dict(row) if row else None


def create_formalization(
    *,
    project_id: str | None,
    loose_session_id: str | None,
    display_title: str,
    kind: str = "theorem",
    declaration_name: str | None = None,
    statement: str | None = None,
    origin: str = "ui",
    origin_key: str | None = None,
    source_hash: str | None = None,
) -> dict:
    now = utc_now()
    formalization_id = str(uuid4())
    # Capped at the same 160 the UPDATE path uses. A formalization created from a chat
    # message inherits the whole message as its title, so without this a paragraph-long
    # prompt becomes the label everywhere it is shown — and the two paths disagreeing
    # meant the same title was legal on create and truncated on edit.
    title = str(display_title or declaration_name or "Untitled formalization").strip()[:160]
    with write() as conn:
        conn.execute(
            """
            insert into formalizations (
                id, project_id, loose_session_id, display_title, declaration_name,
                kind, statement, origin, origin_key, source_hash, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                formalization_id, project_id, loose_session_id, title[:160],
                (declaration_name or "").strip() or None,
                _normalize_formalization_kind(kind), statement,
                (origin or "ui").strip() or "ui",
                (origin_key or "").strip() or None,
                (source_hash or "").strip() or None,
                now, now,
            ),
        )
        result = _formalization_from_conn(conn, formalization_id)
    assert result is not None
    return result


def get_formalization(formalization_id: str) -> dict | None:
    with connect() as conn:
        return _formalization_from_conn(conn, formalization_id)


def find_formalization_by_origin(
    project_id: str, origin: str, origin_key: str
) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """
            select * from formalizations
            where project_id = ? and origin = ? and origin_key = ?
            """,
            (project_id, origin, origin_key),
        ).fetchone()
    return row_to_dict(row) if row else None


def find_formalization_by_declaration(
    *,
    project_id: str | None,
    loose_session_id: str | None,
    declaration_name: str,
) -> dict | None:
    """Resolve a stable target by declaration within exactly one scope."""
    if bool(project_id) == bool(loose_session_id):
        raise ValueError("provide exactly one formalization scope")
    scope_column = "project_id" if project_id else "loose_session_id"
    scope_value = project_id or loose_session_id
    with connect() as conn:
        row = conn.execute(
            f"""
            select * from formalizations
            where {scope_column} = ? and declaration_name = ?
            """,
            (scope_value, declaration_name),
        ).fetchone()
    return row_to_dict(row) if row else None


def update_formalization(
    formalization_id: str,
    *,
    display_title: str | None = None,
    declaration_name: str | None = None,
    statement: str | None = None,
    kind: str | None = None,
    source_hash: str | None = None,
) -> dict | None:
    with write() as conn:
        current = _formalization_from_conn(conn, formalization_id)
        if not current:
            return None
        if declaration_name is not None and declaration_name != current.get("declaration_name"):
            artifact = conn.execute(
                "select 1 from artifacts where formalization_id = ? limit 1",
                (formalization_id,),
            ).fetchone()
            if artifact:
                raise ValueError("a checked formalization's declaration cannot be renamed here")
        conn.execute(
            """
            update formalizations
            set display_title = ?, declaration_name = ?, statement = ?, kind = ?,
                source_hash = ?, updated_at = ?
            where id = ?
            """,
            (
                (
                    str(display_title).strip()[:160]
                    if display_title is not None
                    else current["display_title"]
                ) or current["display_title"],
                (
                    str(declaration_name).strip() or None
                    if declaration_name is not None
                    else current["declaration_name"]
                ),
                statement if statement is not None else current["statement"],
                _normalize_formalization_kind(kind) if kind is not None else current["kind"],
                (
                    str(source_hash).strip() or None
                    if source_hash is not None
                    else current["source_hash"]
                ),
                utc_now(),
                formalization_id,
            ),
        )
        return _formalization_from_conn(conn, formalization_id)


def link_session_formalization(session_id: str, formalization_id: str) -> None:
    with write() as conn:
        conn.execute(
            """
            insert or ignore into session_formalizations (
                session_id, formalization_id, created_at
            ) values (?, ?, ?)
            """,
            (session_id, formalization_id, utc_now()),
        )


def link_formalization_file(
    formalization_id: str, path: str, role: str = "generated"
) -> dict:
    role_value = str(role or "generated").lower()
    if role_value not in FORMALIZATION_FILE_ROLES:
        raise ValueError(f"unsupported formalization file role: {role_value}")
    now = utc_now()
    with write() as conn:
        if role_value == "primary":
            conn.execute(
                """
                update formalization_files set role = 'support', updated_at = ?
                where formalization_id = ? and role = 'primary' and path <> ?
                """,
                (now, formalization_id, path),
            )
        conn.execute(
            """
            insert into formalization_files (
                formalization_id, path, role, created_at, updated_at
            ) values (?, ?, ?, ?, ?)
            on conflict(formalization_id, path)
            do update set role = excluded.role, updated_at = excluded.updated_at
            """,
            (formalization_id, path, role_value, now, now),
        )
        row = conn.execute(
            """
            select * from formalization_files
            where formalization_id = ? and path = ?
            """,
            (formalization_id, path),
        ).fetchone()
    return row_to_dict(row)


def list_formalization_files(formalization_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            select * from formalization_files
            where formalization_id = ?
            order by case role when 'primary' then 0 when 'support' then 1 else 2 end,
                     path asc
            """,
            (formalization_id,),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


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


def create_run_bundle(
    *,
    message: str,
    session_id: str | None,
    project_id: str | None,
    session_origin: str,
    session_origin_url: str | None,
    model: str,
    provider: str | None,
    max_turns: int | None,
    autonomous: bool,
    focus_formalization_id: str | None = None,
    focus_source_hash: str | None = None,
    new_formalization: dict | None = None,
) -> dict:
    """Atomically create/resolve the conversation scope, run, and user message."""
    if focus_formalization_id and new_formalization:
        raise ValueError("choose an existing focus or a new formalization, not both")
    now = utc_now()
    with write() as conn:
        if session_id:
            session_row = conn.execute(
                "select * from sessions where id = ?", (session_id,)
            ).fetchone()
            if not session_row:
                raise LookupError("session not found")
            session = row_to_dict(session_row)
            if project_id and not session.get("project_id"):
                conn.execute(
                    "update sessions set project_id = ?, updated_at = ? where id = ?",
                    (project_id, now, session_id),
                )
                session["project_id"] = project_id
            elif project_id is None and session.get("project_id"):
                project_id = session["project_id"]
            elif project_id and session.get("project_id") != project_id:
                raise ValueError("session belongs to a different project")
        else:
            session_id = str(uuid4())
            conn.execute(
                """
                insert into sessions (
                    id, project_id, title, origin, origin_url, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, project_id, message[:120] or "Untitled conversation",
                    (session_origin or "ui").strip() or "ui",
                    session_origin_url, now, now,
                ),
            )
            session = row_to_dict(
                conn.execute(
                    "select * from sessions where id = ?", (session_id,)
                ).fetchone()
            )

        formalization = None
        if new_formalization:
            origin = str(new_formalization.get("origin") or "ui").strip() or "ui"
            origin_key = str(new_formalization.get("origin_key") or "").strip() or None
            if project_id and origin_key:
                existing = conn.execute(
                    """
                    select * from formalizations
                    where project_id = ? and origin = ? and origin_key = ?
                    """,
                    (project_id, origin, origin_key),
                ).fetchone()
                if existing:
                    formalization = row_to_dict(existing)
            if formalization is None:
                focus_formalization_id = str(uuid4())
                title = str(
                    new_formalization.get("display_title")
                    or new_formalization.get("declaration_name")
                    or message[:120]
                    or "Untitled formalization"
                ).strip()
                conn.execute(
                    """
                    insert into formalizations (
                        id, project_id, loose_session_id, display_title,
                        declaration_name, kind, statement, origin, origin_key,
                        source_hash, created_at, updated_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        focus_formalization_id, project_id,
                        None if project_id else session_id, title[:160],
                        str(new_formalization.get("declaration_name") or "").strip() or None,
                        _normalize_formalization_kind(new_formalization.get("kind")),
                        new_formalization.get("statement"), origin, origin_key,
                        str(new_formalization.get("source_hash") or "").strip() or None,
                        now, now,
                    ),
                )
                formalization = _formalization_from_conn(conn, focus_formalization_id)
            else:
                focus_formalization_id = formalization["id"]
                requested_decl = str(
                    new_formalization.get("declaration_name") or ""
                ).strip() or None
                if (
                    requested_decl
                    and formalization.get("declaration_name")
                    and requested_decl != formalization["declaration_name"]
                ):
                    raise ValueError("origin key resolves to a conflicting declaration")
                requested_kind = _normalize_formalization_kind(
                    new_formalization.get("kind")
                )
                if requested_kind != formalization.get("kind"):
                    raise ValueError("origin key resolves to a conflicting kind")
        elif focus_formalization_id:
            formalization = _formalization_from_conn(conn, focus_formalization_id)
            if not formalization:
                raise LookupError("formalization not found")

        if formalization:
            if project_id:
                if formalization.get("project_id") != project_id:
                    raise ValueError("formalization belongs to a different project")
            elif formalization.get("loose_session_id") != session_id:
                raise ValueError("loose formalization belongs to a different session")
            conn.execute(
                """
                insert or ignore into session_formalizations (
                    session_id, formalization_id, created_at
                ) values (?, ?, ?)
                """,
                (session_id, formalization["id"], now),
            )
            source_hash = str(focus_source_hash or "").strip() or None
            if source_hash:
                conn.execute(
                    """
                    update formalizations
                    set source_hash = ?, updated_at = ?
                    where id = ?
                    """,
                    (source_hash, now, formalization["id"]),
                )
                formalization["source_hash"] = source_hash

        run_id = str(uuid4())
        conn.execute(
            """
            insert into runs (
                id, session_id, project_id, status, autonomous, model, provider,
                max_turns, focus_formalization_id, focus_source_hash,
                created_at, updated_at
            ) values (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, session_id, project_id, 1 if autonomous else 0,
                model, provider, max_turns, focus_formalization_id,
                str(focus_source_hash or "").strip() or None, now, now,
            ),
        )
        message_cursor = conn.execute(
            """
            insert into timeline (
                session_id, run_id, kind, author, content, formalization_id, created_at
            ) values (?, ?, 'message', 'user', ?, ?, ?)
            """,
            (session_id, run_id, message, focus_formalization_id, now),
        )
        conn.execute(
            "update sessions set updated_at = ? where id = ?", (now, session_id)
        )
        run = row_to_dict(
            conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
        )
        user_message = _message_from_row(
            conn.execute(
                "select * from timeline where id = ?", (message_cursor.lastrowid,)
            ).fetchone()
        )
        session = row_to_dict(
            conn.execute("select * from sessions where id = ?", (session_id,)).fetchone()
        )
    _bump_sessions_changed()
    return {
        "session": session,
        "formalization": formalization,
        "run": run,
        "message": user_message,
    }


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
    return [row_to_dict(row) for row in rows]


def get_project(project_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("select * from projects where id = ?", (project_id,)).fetchone()
    return row_to_dict(row) if row else None


def get_project_by_slug(slug: str) -> dict | None:
    value = validate_project_slug(slug)
    with connect() as conn:
        row = conn.execute("select * from projects where slug = ?", (value,)).fetchone()
    return row_to_dict(row) if row else None


def get_project_by_namespace(namespace: str) -> dict | None:
    value = validate_project_namespace(namespace)
    with connect() as conn:
        row = conn.execute("select * from projects where namespace = ?", (value,)).fetchone()
    return row_to_dict(row) if row else None


def validate_project_namespace(namespace: str) -> str:
    value = str(namespace or "").strip()
    if not PROJECT_NAMESPACE_RE.fullmatch(value):
        raise ValueError("project namespace must be under Lea. with Lean identifier segments")
    return value


def project_namespace_for_slug(slug: str) -> str:
    """Derive a fallback Lean namespace `Lea.<Project>` from a slug. The slug is the
    immutable Overleaf/project binding; the namespace is cached and can migrate only
    through the explicit project-identity rename flow."""
    parts = re.split(r"[-_\s]+", str(slug or "").strip())
    camel = "".join(p[:1].upper() + p[1:] for p in parts if p)
    camel = re.sub(r"[^A-Za-z0-9]", "", camel)
    if not camel or not camel[0].isalpha():
        camel = "P" + camel  # Lean segments can't start with a digit
    return f"Lea.{camel}"


def repo_path_for_namespace(namespace: str) -> str:
    """The shared dir / git repo for a namespace: `Lea.Foo` → `proofs/Lea/Foo` (D22)."""
    value = validate_project_namespace(namespace)
    return "proofs/" + value.replace(".", "/")


def get_or_create_project(slug: str, title: str | None = None) -> dict:
    """Return the project with this slug, creating it on first use. Used by the
    Overleaf path to tag runs with the document namespace so per-document usage can
    be aggregated. `slug` is unique, so a concurrent create just re-reads the
    winner."""
    existing = get_project_by_slug(slug)
    if existing:
        return existing
    try:
        return create_project(slug, title=title)
    except sqlite3.IntegrityError:
        # Lost a create race on the unique slug — read back the winner.
        winner = get_project_by_slug(slug)
        if winner:
            return winner
        raise


def create_project(
    slug: str,
    title: str | None = None,
    description: str | None = None,
    namespace: str | None = None,
    repo_path: str | None = None,
    remote_url: str | None = None,
) -> dict:
    """Insert a project index row (D21/D30). `namespace`/`repo_path` default to the
    slug-derived values so the Overleaf tag-only path keeps working; P2's project
    service passes them explicitly when it provisions the real on-disk repo."""
    slug = validate_project_slug(slug)
    now = utc_now()
    project_id = str(uuid4())
    project_title = (title or slug).strip() or slug
    ns = validate_project_namespace(namespace) if namespace else project_namespace_for_slug(slug)
    repo = repo_path or repo_path_for_namespace(ns)
    with connect() as conn:
        conn.execute(
            """
            insert into projects
                (id, slug, title, description, namespace, repo_path, remote_url, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, slug, project_title, description, ns, repo, remote_url, now, now),
        )
        row = conn.execute("select * from projects where id = ?", (project_id,)).fetchone()
    return row_to_dict(row)


def update_project(
    project_id: str,
    title: str | None = None,
    description: str | None = None,
    remote_url: str | None = None,
) -> dict | None:
    """Update project metadata only (D31): title, description, GitHub remote. The
    slug → namespace → repo_path chain is immutable (D22), so those never change
    here. Pass a field as None to leave it untouched."""
    now = utc_now()
    with connect() as conn:
        row = conn.execute("select * from projects where id = ?", (project_id,)).fetchone()
        if not row:
            return None
        current = row_to_dict(row)
        conn.execute(
            """
            update projects
            set title = ?, description = ?, remote_url = ?, updated_at = ?
            where id = ?
            """,
            (
                (title if title is not None else current["title"]).strip() or current["title"],
                description if description is not None else current["description"],
                remote_url if remote_url is not None else current["remote_url"],
                now,
                project_id,
            ),
        )
        updated = conn.execute("select * from projects where id = ?", (project_id,)).fetchone()
    return row_to_dict(updated)


def update_project_identity(
    project_id: str,
    *,
    title: str,
    namespace: str,
    repo_path: str,
) -> dict | None:
    """Update the mutable project identity fields. `slug` remains immutable; this is
    reserved for the explicit namespace-migration path, not ordinary metadata edits."""
    ns = validate_project_namespace(namespace)
    now = utc_now()
    with connect() as conn:
        row = conn.execute("select * from projects where id = ?", (project_id,)).fetchone()
        if not row:
            return None
        conn.execute(
            """
            update projects
            set title = ?, namespace = ?, repo_path = ?, updated_at = ?
            where id = ?
            """,
            ((title or "").strip() or row["title"], ns, repo_path, now, project_id),
        )
        updated = conn.execute("select * from projects where id = ?", (project_id,)).fetchone()
    return row_to_dict(updated)


def create_project_file(
    project_id: str,
    filename: str,
    stored_path: str,
    mime: str | None = None,
    kind: str = "upload",
    extracted_path: str | None = None,
) -> dict:
    """Index a project file (D27). The bytes live in the project repo under
    `.lea/files/` (git-canonical); this row is the pointer + extraction metadata."""
    now = utc_now()
    file_id = str(uuid4())
    with connect() as conn:
        conn.execute(
            """
            insert into project_files
                (id, project_id, filename, stored_path, mime, kind, extracted_path, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (file_id, project_id, filename, stored_path, mime, kind, extracted_path, now),
        )
        row = conn.execute("select * from project_files where id = ?", (file_id,)).fetchone()
    return row_to_dict(row)


def list_project_files(project_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "select * from project_files where project_id = ? order by created_at asc, filename asc",
            (project_id,),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def get_project_file(file_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("select * from project_files where id = ?", (file_id,)).fetchone()
    return row_to_dict(row) if row else None


def list_project_files_by_kind(project_id: str, kind: str) -> list[dict]:
    """Project files of one ``kind`` (e.g. ``"overleaf"`` for the mirrored .tex), used
    by the Overleaf mirror reconcile to diff the desired set against what's indexed."""
    with connect() as conn:
        rows = conn.execute(
            "select * from project_files where project_id = ? and kind = ? "
            "order by stored_path asc",
            (project_id, kind),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def get_project_file_by_path(project_id: str, stored_path: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "select * from project_files where project_id = ? and stored_path = ?",
            (project_id, stored_path),
        ).fetchone()
    return row_to_dict(row) if row else None


def delete_project_file(file_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute("delete from project_files where id = ?", (file_id,))
    return cur.rowcount > 0


def delete_project_cascade(project_id: str) -> bool:
    """Delete a project and every DB row that references it. SQLite foreign keys are
    not enforced here (no `PRAGMA foreign_keys=ON`), so the cascade is explicit: the
    project's sessions and all their dependent rows go first, then project_files,
    then the project. The on-disk repo (`rm -rf`) is the caller's job (the project
    service) — this is the index half of delete (D31). Returns False if absent."""
    with connect() as conn:
        if not conn.execute("select 1 from projects where id = ?", (project_id,)).fetchone():
            return False
        import_ids = [
            row["id"] for row in conn.execute(
                "select id from github_imports where project_id = ?", (project_id,)
            ).fetchall()
        ]
        if import_ids:
            import_marks = ",".join("?" for _ in import_ids)
            conn.execute(
                f"delete from github_import_declarations where import_id in ({import_marks})",
                import_ids,
            )
            conn.execute(
                f"delete from github_import_files where import_id in ({import_marks})",
                import_ids,
            )
            conn.execute(
                f"delete from github_imports where id in ({import_marks})", import_ids
            )
        session_ids = [
            r["id"] for r in conn.execute(
                "select id from sessions where project_id = ?", (project_id,)
            ).fetchall()
        ]
        formalization_ids = [
            r["id"] for r in conn.execute(
                "select id from formalizations where project_id = ?", (project_id,)
            ).fetchall()
        ]
        if session_ids:
            marks = ",".join("?" for _ in session_ids)
            formalization_ids.extend(
                r["id"] for r in conn.execute(
                    f"select id from formalizations where loose_session_id in ({marks})",
                    session_ids,
                ).fetchall()
            )
            conn.execute(
                f"delete from verification_events where session_id in ({marks})",
                session_ids,
            )
            conn.execute(
                f"delete from session_formalizations where session_id in ({marks})",
                session_ids,
            )
        if formalization_ids:
            form_marks = ",".join("?" for _ in formalization_ids)
            conn.execute(
                f"delete from verification_events where formalization_id in ({form_marks})",
                formalization_ids,
            )
            conn.execute(
                f"delete from session_formalizations where formalization_id in ({form_marks})",
                formalization_ids,
            )
            conn.execute(
                f"delete from formalization_files where formalization_id in ({form_marks})",
                formalization_ids,
            )
            conn.execute(
                f"update artifacts set formalization_id = null where formalization_id in ({form_marks})",
                formalization_ids,
            )
        if session_ids:
            marks = ",".join("?" for _ in session_ids)
            # `messages`/`code_steps` are pre-cutover rows kept until the contract
            # step drops them; they're still cleared so a delete doesn't leave half
            # a session behind in tables that are still readable.
            for table in ("timeline", "messages", "code_steps", "status_events",
                          "run_usage_breakdown", "runs"):
                conn.execute(f"delete from {table} where session_id in ({marks})", session_ids)
            conn.execute(f"delete from sessions where id in ({marks})", session_ids)
            # Blobs are content-addressed and therefore *shared* — the same file
            # content in another project is the same row. So they can't be deleted
            # by session; drop only the ones nothing points at any more. Deleting
            # eagerly here would silently blank another project's history.
            conn.execute(
                "delete from artifact_blobs where id not in "
                "(select after_blob_id from timeline where after_blob_id is not null)"
            )
        # The artifact index is scoped by project OR by session (`scope` is whichever
        # applies), and was left behind entirely (AUDIT-2026-07-24 C9). A stale row
        # survives a re-created slug and makes `_ensure_artifacts_backfilled` think the
        # fresh project is already indexed, so its real proofs never get imported.
        conn.execute("delete from artifacts where project_id = ? or scope = ?",
                     (project_id, project_id))
        if session_ids:
            conn.execute(
                f"delete from artifacts where session_id in ({marks}) or scope in ({marks})",
                (*session_ids, *session_ids),
            )
        conn.execute("delete from project_files where project_id = ?", (project_id,))
        if formalization_ids:
            form_marks = ",".join("?" for _ in formalization_ids)
            conn.execute(
                f"delete from formalizations where id in ({form_marks})",
                formalization_ids,
            )
        # Drop any skill assignments pointing at this project (D47) — the skills
        # themselves survive (they may be global or assigned elsewhere).
        conn.execute("delete from skill_projects where project_id = ?", (project_id,))
        conn.execute("delete from projects where id = ?", (project_id,))
    return True


def assign_session_project(session_id: str, project_id: str | None) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            "update sessions set project_id = ?, updated_at = ? where id = ?",
            (project_id, now, session_id),
        )


# NOTE (D8): `sessions_with_latest_code_path` and `record_project_unassignment`
# lived here — both were project-only (v2.1) AND broken against the v2 schema
# (they referenced the dropped `code`/`kind`/`step_number`/`used_project_formalizations`
# columns). Removed rather than left as landmines; the projects feature rewrites its
# store layer against the git-backed code_steps when it returns. The dormant project
# CRUD below (projects table) stays as the v2.1 foundation.


def validate_project_slug(slug: str) -> str:
    value = str(slug or "").strip()
    if not PROJECT_SLUG_RE.fullmatch(value):
        raise ValueError("Project slug must be 1-80 characters using letters, numbers, '_' or '-'.")
    return value


# --- Skills (Skill Factory, v2.1.1 W1) ------------------------------------------
# A skill is a DB row (markdown `body` in a column), not a git file (D45). The
# scope model (D47): `is_global` → every project; else the `skill_projects` join;
# loose (project-less) sessions resolve to none. These queries are the store half
# of Slice 8 — CRUD + assignment + the project-resolution read; the routes (W2),
# run-time materialization (W3), and GitHub import (W4) build on top.


def slugify_skill(value: str) -> str:
    """Derive a skill slug from a name: lower-kebab, alphanumeric, ≤80 chars (D45).
    Runs of non-alphanumerics collapse to a single '-'; a leading non-letter/digit
    is dropped (slugs must be letter/digit-initial). Empty input → 'skill'."""
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = text[:80].rstrip("-")
    return text or "skill"


def validate_skill_slug(slug: str) -> str:
    value = str(slug or "").strip()
    if not SKILL_SLUG_RE.fullmatch(value):
        raise ValueError(
            "Skill slug must be 1-80 characters of lowercase letters, numbers or '-', "
            "starting with a letter or number."
        )
    return value


def _unique_skill_slug(conn, base: str, exclude_id: str | None = None) -> str:
    """A slug not already taken by another skill, appending -2, -3, … on collision.
    `exclude_id` lets an update keep its own slug. Bounded retry so the unique
    constraint is the real backstop, not this loop."""
    base = validate_skill_slug(base)
    candidate = base
    suffix = 2
    while True:
        row = conn.execute(
            "select id from skills where slug = ?", (candidate,)
        ).fetchone()
        if row is None or row["id"] == exclude_id:
            return candidate
        candidate = validate_skill_slug(f"{base[:74]}-{suffix}")
        suffix += 1


def set_skill_files(skill_id: str, files: list[tuple[str, str]]) -> None:
    """Replace a skill's reference files (H2). Wholesale, so a re-import can't leave
    orphans from the previous version."""
    now = utc_now()
    with connect() as conn:
        conn.execute("delete from skill_files where skill_id = ?", (skill_id,))
        for path, content in files or []:
            conn.execute(
                "insert or replace into skill_files (skill_id, path, content, created_at) "
                "values (?, ?, ?, ?)",
                (skill_id, str(path), str(content), now),
            )


def skill_files(skill_id: str) -> list[dict]:
    with connect() as conn:
        return [row_to_dict(r) for r in conn.execute(
            "select path, content from skill_files where skill_id = ? order by path",
            (skill_id,)).fetchall()]


def _skill_row(conn, skill_id: str) -> dict | None:
    row = conn.execute("select * from skills where id = ?", (skill_id,)).fetchone()
    if not row:
        return None
    data = _normalize_skill(row_to_dict(row))
    data["project_ids"] = [
        r["project_id"]
        for r in conn.execute(
            "select project_id from skill_projects where skill_id = ? order by project_id",
            (skill_id,),
        ).fetchall()
    ]
    # H2: paths only. The contents can be large (a real skill's references run to
    # hundreds of KB), and no caller that lists skills wants them.
    data["file_paths"] = [
        r["path"] for r in conn.execute(
            "select path from skill_files where skill_id = ? order by path", (skill_id,)
        ).fetchall()
    ]
    return data


def create_skill(
    name: str,
    body: str,
    is_global: bool = False,
    source_url: str | None = None,
    source_ref: str | None = None,
    slug: str | None = None,
    authoring: dict | None = None,
    description: str | None = None,
    triggers: list[str] | None = None,
) -> dict:
    """Insert a skill row (D45). `slug` defaults to a unique slugify(name); when
    given explicitly it is validated and uniquified. The created row carries its
    (empty) `project_ids` so callers get the full assignment shape back."""
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Skill name is required.")
    # C2: when the guided form was used, the compiled text IS the body — so every
    # consumer keeps reading `body` and learns nothing new.
    if not _authoring.is_empty(authoring):
        body = _authoring.compile_text(authoring)
    base_slug = validate_skill_slug(slug) if slug else slugify_skill(clean_name)
    now = utc_now()
    skill_id = str(uuid4())
    with connect() as conn:
        final_slug = _unique_skill_slug(conn, base_slug)
        conn.execute(
            """
            insert into skills
                (id, name, slug, body, is_global, source_url, source_ref, authoring,
                 description, triggers, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_id,
                clean_name,
                final_slug,
                str(body or ""),
                1 if is_global else 0,
                source_url,
                source_ref,
                _authoring.dumps(authoring),
                (description or "").strip() or None,
                json.dumps([t.strip() for t in triggers if t.strip()]) if triggers else None,
                now,
                now,
            ),
        )
        return _skill_row(conn, skill_id)


def get_skill(skill_id: str) -> dict | None:
    with connect() as conn:
        return _skill_row(conn, skill_id)


def get_skill_by_slug(slug: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("select id from skills where slug = ?", (slug,)).fetchone()
        return _skill_row(conn, row["id"]) if row else None


def list_skills() -> list[dict]:
    """All skills, newest first, each with its `project_ids` assignment list. The
    factory catalog (F11) renders global/▣-projects badges from this."""
    with connect() as conn:
        ids = [
            row["id"]
            for row in conn.execute(
                "select id from skills order by updated_at desc, name asc"
            ).fetchall()
        ]
        return [_skill_row(conn, skill_id) for skill_id in ids]


def update_skill(
    skill_id: str,
    name: str | None = None,
    body: str | None = None,
    source_url: str | None = None,
    source_ref: str | None = None,
    authoring: dict | None = None,
) -> dict | None:
    """Update a skill's editable fields (name/body/provenance). The slug is the
    stable identifier (D45) and is NOT changed here. Pass a field as None to leave
    it untouched. Returns the updated row, or None if the id is unknown.

    C2: passing `authoring` recompiles `body` from the fields, so the two can never drift
    — the fields are the source, the body is what the model reads."""
    if authoring is not None and not _authoring.is_empty(authoring):
        body = _authoring.compile_text(authoring)
    now = utc_now()
    with connect() as conn:
        current = conn.execute("select * from skills where id = ?", (skill_id,)).fetchone()
        if not current:
            return None
        cur = row_to_dict(current)
        new_name = cur["name"] if name is None else (str(name).strip() or cur["name"])
        conn.execute(
            """
            update skills
            set name = ?, body = ?, source_url = ?, source_ref = ?, authoring = ?,
                updated_at = ?
            where id = ?
            """,
            (
                new_name,
                cur["body"] if body is None else str(body),
                cur["source_url"] if source_url is None else source_url,
                cur["source_ref"] if source_ref is None else source_ref,
                cur["authoring"] if authoring is None else _authoring.dumps(authoring),
                now,
                skill_id,
            ),
        )
        return _skill_row(conn, skill_id)


def set_skill_assignment(
    skill_id: str,
    is_global: bool,
    project_ids: list[str] | None = None,
) -> dict | None:
    """Set a skill's scope (D47): `is_global` plus the explicit per-project join.
    Replaces the join wholesale with `project_ids` (deduped, unknown ids rejected).
    When `is_global` is True the join is still stored but unused at resolution time
    — kept so toggling global off restores the prior per-project set is the caller's
    job; here global simply wins. Returns the updated row, or None if unknown."""
    ids = list(dict.fromkeys(project_ids or []))
    now = utc_now()
    with connect() as conn:
        if not conn.execute("select 1 from skills where id = ?", (skill_id,)).fetchone():
            return None
        if ids:
            marks = ",".join("?" for _ in ids)
            known = {
                r["id"]
                for r in conn.execute(
                    f"select id from projects where id in ({marks})", ids
                ).fetchall()
            }
            missing = [pid for pid in ids if pid not in known]
            if missing:
                raise ValueError(f"Unknown project id(s): {', '.join(missing)}")
        conn.execute("delete from skill_projects where skill_id = ?", (skill_id,))
        for project_id in ids:
            conn.execute(
                "insert into skill_projects (skill_id, project_id) values (?, ?)",
                (skill_id, project_id),
            )
        conn.execute(
            "update skills set is_global = ?, updated_at = ? where id = ?",
            (1 if is_global else 0, now, skill_id),
        )
        return _skill_row(conn, skill_id)


def delete_skill(skill_id: str) -> bool:
    """Delete a skill and cascade its `skill_projects` rows. Returns False if absent."""
    with connect() as conn:
        if not conn.execute("select 1 from skills where id = ?", (skill_id,)).fetchone():
            return False
        conn.execute("delete from skill_projects where skill_id = ?", (skill_id,))
        conn.execute("delete from skill_files where skill_id = ?", (skill_id,))
        conn.execute("delete from skills where id = ?", (skill_id,))
    return True


def skills_for_project(project_id: str) -> list[dict]:
    """The skills that resolve for a project: global ∪ assigned (D47), newest first.
    This is the run-time resolution read W3 materializes to `cfg.skills`. A loose
    (project-less) session never calls this — it resolves to [] by definition."""
    with connect() as conn:
        ids = [
            row["id"]
            for row in conn.execute(
                """
                select id from skills
                where is_global = 1
                   or id in (select skill_id from skill_projects where project_id = ?)
                order by updated_at desc, name asc
                """,
                (project_id,),
            ).fetchall()
        ]
        return [_skill_row(conn, skill_id) for skill_id in ids]


def _normalize_skill(row: dict) -> dict:
    row["is_global"] = bool(row.get("is_global"))
    row["authoring"] = _authoring.loads(row.get("authoring"))
    try:
        row["triggers"] = json.loads(row.get("triggers")) if row.get("triggers") else []
    except (TypeError, ValueError):
        row["triggers"] = []
    return row


# --- MCP servers (v2.5 E0) -----------------------------------------------------
# Deliberately the same shape as skills above: same slug rules, same `is_global` ∪
# join scoping (D47), same row helpers. An MCP server and a skill are both library
# items a project selects, and H8 will have a skill declare its own servers — so the
# two must not drift apart.
#
# SECRETS (A7): `env` holds non-secret literals only; a credential is NAMED in
# `env_from` (stdio) or `api_key_name` (remote) and its value read from the
# environment at spawn. No row here ever contains a secret.

MCP_TRANSPORTS = ("stdio", "sse", "http")


def _unique_mcp_slug(conn, base: str, exclude_id: str | None = None) -> str:
    base = validate_skill_slug(base)
    candidate, suffix = base, 2
    while True:
        row = conn.execute("select id from mcp_servers where slug = ?", (candidate,)).fetchone()
        if row is None or row["id"] == exclude_id:
            return candidate
        candidate = validate_skill_slug(f"{base[:74]}-{suffix}")
        suffix += 1


def _normalize_mcp_server(row: dict) -> dict:
    row["is_global"] = bool(row.get("is_global"))
    row["enabled"] = bool(row.get("enabled"))
    for key, empty in (("args", []), ("env_from", []), ("env", {})):
        try:
            row[key] = json.loads(row.get(key) or "null")
        except (TypeError, ValueError):
            row[key] = None
        if row[key] is None:
            row[key] = empty
    return row


def _mcp_server_row(conn, server_id: str) -> dict | None:
    row = conn.execute("select * from mcp_servers where id = ?", (server_id,)).fetchone()
    if not row:
        return None
    data = _normalize_mcp_server(row_to_dict(row))
    data["project_ids"] = [
        r["project_id"]
        for r in conn.execute(
            "select project_id from mcp_server_projects where mcp_server_id = ? order by project_id",
            (server_id,),
        ).fetchall()
    ]
    return data


# A stored `env` value is persisted in plain text and travels on `LeaConfig`, which
# promises to be "safe to log or serialize". A credential therefore belongs in
# `env_from` (a NAME, resolved from the environment at spawn), never here.
SECRET_ENV_NAME_RE = re.compile(r"_(API_KEY|KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)$", re.I)


def _validate_mcp_env(env: dict | None) -> None:
    """Refuse secret-shaped `env` entries at SAVE time (A7), while the user is still
    looking at the field — rather than at run time, or never."""
    for key in (env or {}):
        if SECRET_ENV_NAME_RE.search(str(key)):
            raise ValueError(
                f"{key} looks like a credential, so it can't be stored here. "
                f"Save the value under Settings → API keys and list the NAME "
                f"'{key}' in 'Pass through from environment' instead."
            )


def _validate_mcp_fields(transport: str, command: str | None, url: str | None) -> None:
    """Shape rules shared by create and update. Mirrors the UI's own validation so a
    direct API call can't store a server the form would have rejected."""
    if transport not in MCP_TRANSPORTS:
        raise ValueError(f"Transport must be one of: {', '.join(MCP_TRANSPORTS)}.")
    if transport == "stdio":
        if not (command or "").strip():
            raise ValueError("A stdio server needs a command.")
        if " " in command.strip():
            raise ValueError(
                "Command must be a single executable — put parameters in Arguments."
            )
    elif not (url or "").strip():
        raise ValueError(f"A {transport} server needs a URL.")


def create_mcp_server(
    name: str,
    transport: str = "stdio",
    command: str | None = None,
    args: list[str] | None = None,
    env: dict | None = None,
    env_from: list[str] | None = None,
    url: str | None = None,
    api_key_name: str | None = None,
    enabled: bool = True,
) -> dict:
    """Insert an MCP server row. Raises ValueError on any shape problem (the route
    turns it into a 400) — a malformed server must fail at save, while the user is
    still looking at the field, not at run time."""
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Server name is required.")
    _validate_mcp_fields(transport, command, url)
    _validate_mcp_env(env)
    now, server_id = utc_now(), str(uuid4())
    with connect() as conn:
        slug = _unique_mcp_slug(conn, slugify_skill(clean_name))
        conn.execute(
            """
            insert into mcp_servers
                (id, name, slug, transport, command, args, env, env_from, url,
                 api_key_name, enabled, is_global, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                server_id, clean_name, slug, transport,
                (command or "").strip() or None,
                json.dumps(list(args or [])),
                json.dumps(dict(env or {})),
                json.dumps(list(env_from or [])),
                (url or "").strip() or None,
                (api_key_name or "").strip() or None,
                1 if enabled else 0,
                now, now,
            ),
        )
        return _mcp_server_row(conn, server_id)


def get_mcp_server(server_id: str) -> dict | None:
    with connect() as conn:
        return _mcp_server_row(conn, server_id)


def list_mcp_servers() -> list[dict]:
    with connect() as conn:
        ids = [
            r["id"] for r in conn.execute(
                "select id from mcp_servers order by updated_at desc, name asc"
            ).fetchall()
        ]
        return [_mcp_server_row(conn, sid) for sid in ids]


def update_mcp_server(server_id: str, **fields) -> dict | None:
    """Update editable fields; pass a field as None to leave it untouched. The slug is
    stable and never changes. Returns the updated row, or None if the id is unknown."""
    with connect() as conn:
        current = _mcp_server_row(conn, server_id)
        if current is None:
            return None
        merged = {k: (fields[k] if fields.get(k) is not None else current[k])
                  for k in ("name", "transport", "command", "args", "env", "env_from",
                            "url", "api_key_name")}
        enabled = fields.get("enabled")
        merged["enabled"] = current["enabled"] if enabled is None else bool(enabled)
        if not str(merged["name"] or "").strip():
            raise ValueError("Server name is required.")
        _validate_mcp_fields(merged["transport"], merged["command"], merged["url"])
        _validate_mcp_env(merged["env"])
        conn.execute(
            """
            update mcp_servers
               set name = ?, transport = ?, command = ?, args = ?, env = ?, env_from = ?,
                   url = ?, api_key_name = ?, enabled = ?, updated_at = ?
             where id = ?
            """,
            (
                str(merged["name"]).strip(), merged["transport"],
                (merged["command"] or "").strip() or None,
                json.dumps(list(merged["args"] or [])),
                json.dumps(dict(merged["env"] or {})),
                json.dumps(list(merged["env_from"] or [])),
                (merged["url"] or "").strip() or None,
                (merged["api_key_name"] or "").strip() or None,
                1 if merged["enabled"] else 0,
                utc_now(), server_id,
            ),
        )
        return _mcp_server_row(conn, server_id)


def set_mcp_server_assignment(
    server_id: str, is_global: bool, project_ids: list[str] | None = None
) -> dict | None:
    """Set a server's scope (D47) — the exact counterpart of `set_skill_assignment`."""
    ids = list(dict.fromkeys(project_ids or []))
    with connect() as conn:
        if not conn.execute("select 1 from mcp_servers where id = ?", (server_id,)).fetchone():
            return None
        if ids:
            marks = ",".join("?" for _ in ids)
            known = {
                r["id"] for r in conn.execute(
                    f"select id from projects where id in ({marks})", ids
                ).fetchall()
            }
            missing = [pid for pid in ids if pid not in known]
            if missing:
                raise ValueError(f"Unknown project id(s): {', '.join(missing)}")
        conn.execute("delete from mcp_server_projects where mcp_server_id = ?", (server_id,))
        for project_id in ids:
            conn.execute(
                "insert into mcp_server_projects (mcp_server_id, project_id) values (?, ?)",
                (server_id, project_id),
            )
        conn.execute(
            "update mcp_servers set is_global = ?, updated_at = ? where id = ?",
            (1 if is_global else 0, utc_now(), server_id),
        )
        return _mcp_server_row(conn, server_id)


def delete_mcp_server(server_id: str) -> bool:
    with connect() as conn:
        if not conn.execute("select 1 from mcp_servers where id = ?", (server_id,)).fetchone():
            return False
        conn.execute("delete from mcp_server_projects where mcp_server_id = ?", (server_id,))
        conn.execute("delete from mcp_servers where id = ?", (server_id,))
    return True


def mcp_servers_for_project(project_id: str | None) -> list[dict]:
    """The ENABLED servers that resolve for a run: global ∪ assigned (D47).

    A loose (project-less) session resolves to the global ones only — deliberately
    unlike skills, which resolve to nothing without a project. A machine-level server
    the user turned on should work in a scratch session too; E0e's `/mcp` is what will
    let a session refine this.
    """
    with connect() as conn:
        if project_id is None:
            rows = conn.execute(
                "select id from mcp_servers where enabled = 1 and is_global = 1 "
                "order by updated_at desc, name asc"
            ).fetchall()
        else:
            rows = conn.execute(
                """
                select id from mcp_servers
                 where enabled = 1
                   and (is_global = 1
                        or id in (select mcp_server_id from mcp_server_projects
                                   where project_id = ?))
                 order by updated_at desc, name asc
                """,
                (project_id,),
            ).fetchall()
        return [_mcp_server_row(conn, r["id"]) for r in rows]


# --- user-authored sub-agent roles (v2.5 B2) -----------------------------------
# The Sub-agents page could previously only retune the two roles vendored inside the
# prover. These rows are the user's own, materialized to YAML at run start and handed
# to the prover as a directory (`LeaConfig.agent_dirs`) — the same "rows in, files out"
# shape skills already use, which is what keeps the prover ignorant of the database.
#
# Global by design: a role is a way of working, not a resource a project owns.


def create_agent_role(
    name: str,
    system_prompt: str,
    description: str | None = None,
    model: str | None = None,
    tools: list[str] | None = None,
    max_turns: int | None = None,
    reserved_names: set[str] | None = None,
    authoring: dict | None = None,
) -> dict:
    """Insert a user role. `reserved_names` are the vendored role names — a collision is
    refused rather than allowed to shadow, because two roles answering to one name makes
    "which one ran?" unanswerable."""
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Role name is required.")
    # C2: the guided fields compile into the role head, and `when_to_use` becomes the
    # description B1 lists in the coordinator's enum — so writing "when to use this" and
    # making the coordinator choose correctly are one act.
    if not _authoring.is_empty(authoring):
        system_prompt = _authoring.compile_text(authoring)
        description = _authoring.short_description(authoring, description)
    if not str(system_prompt or "").strip():
        raise ValueError("A role needs instructions — that is what makes it a role.")
    if max_turns is not None and (not isinstance(max_turns, int) or max_turns < 1):
        raise ValueError("Max turns must be a positive whole number.")
    slug = slugify_skill(clean_name)
    if reserved_names and slug in reserved_names:
        raise ValueError(
            f"“{slug}” is a built-in role. Pick a different name — you can retune the "
            f"built-in one instead."
        )
    now, role_id = utc_now(), str(uuid4())
    with connect() as conn:
        if conn.execute("select 1 from agent_roles where slug = ?", (slug,)).fetchone():
            raise ValueError(f"A role called “{slug}” already exists.")
        conn.execute(
            """
            insert into agent_roles
                (id, name, slug, description, system_prompt, model, tools, max_turns,
                 authoring, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (role_id, clean_name, slug, (description or "").strip() or None,
             str(system_prompt).strip(), (model or "").strip() or None,
             json.dumps(list(tools)) if tools else None, max_turns,
             _authoring.dumps(authoring), now, now),
        )
        return _agent_role_row(conn, role_id)


def _agent_role_row(conn, role_id: str) -> dict | None:
    row = conn.execute("select * from agent_roles where id = ?", (role_id,)).fetchone()
    if not row:
        return None
    data = row_to_dict(row)
    try:
        data["tools"] = json.loads(data["tools"]) if data["tools"] else None
    except (TypeError, ValueError):
        data["tools"] = None
    data["authoring_raw"] = data.get("authoring")
    data["authoring"] = _authoring.loads(data.get("authoring"))
    return data


def get_agent_role(role_id: str) -> dict | None:
    with connect() as conn:
        return _agent_role_row(conn, role_id)


def list_agent_roles() -> list[dict]:
    with connect() as conn:
        ids = [r["id"] for r in conn.execute(
            "select id from agent_roles order by updated_at desc, name asc").fetchall()]
        return [_agent_role_row(conn, rid) for rid in ids]


def update_agent_role(role_id: str, **fields) -> dict | None:
    """Update a user role. The slug is stable — renaming the display name must not change
    the identity the coordinator was offered mid-conversation."""
    with connect() as conn:
        current = _agent_role_row(conn, role_id)
        if current is None:
            return None
        merged = {k: (fields[k] if fields.get(k) is not None else current[k])
                  for k in ("name", "description", "system_prompt", "model", "tools",
                            "max_turns")}
        authoring = fields.get("authoring")
        if authoring is not None and not _authoring.is_empty(authoring):
            merged["system_prompt"] = _authoring.compile_text(authoring)
            merged["description"] = _authoring.short_description(
                authoring, merged.get("description"))
        if not str(merged["name"] or "").strip():
            raise ValueError("Role name is required.")
        if not str(merged["system_prompt"] or "").strip():
            raise ValueError("A role needs instructions — that is what makes it a role.")
        mt = merged["max_turns"]
        if mt is not None and (not isinstance(mt, int) or mt < 1):
            raise ValueError("Max turns must be a positive whole number.")
        conn.execute(
            """
            update agent_roles
               set name = ?, description = ?, system_prompt = ?, model = ?, tools = ?,
                   max_turns = ?, authoring = ?, updated_at = ?
             where id = ?
            """,
            (str(merged["name"]).strip(), (merged["description"] or "").strip() or None,
             str(merged["system_prompt"]).strip(), (merged["model"] or "").strip() or None,
             json.dumps(list(merged["tools"])) if merged["tools"] else None,
             mt,
             current["authoring_raw"] if authoring is None else _authoring.dumps(authoring),
             utc_now(), role_id),
        )
        return _agent_role_row(conn, role_id)


def delete_agent_role(role_id: str) -> bool:
    with connect() as conn:
        if not conn.execute("select 1 from agent_roles where id = ?", (role_id,)).fetchone():
            return False
        conn.execute("delete from agent_roles where id = ?", (role_id,))
    return True


# --- declarative HTTP tools (v2.5 F1) ------------------------------------------
# A REST endpoint as a tool. Same library shape as skills and MCP servers, and the same
# secret rule: `auth_key_name` NAMES a key, the value is read at call time.

CUSTOM_TOOL_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _normalize_custom_tool(row: dict) -> dict:
    row["is_global"] = bool(row.get("is_global"))
    row["enabled"] = bool(row.get("enabled"))
    row["authoring"] = _authoring.loads(row.get("authoring"))
    for key, empty in (("params", {}), ("headers", {})):
        try:
            row[key] = json.loads(row.get(key) or "null") or empty
        except (TypeError, ValueError):
            row[key] = empty
    return row


def _custom_tool_row(conn, tool_id: str) -> dict | None:
    row = conn.execute("select * from custom_tools where id = ?", (tool_id,)).fetchone()
    if not row:
        return None
    data = _normalize_custom_tool(row_to_dict(row))
    data["project_ids"] = [
        r["project_id"] for r in conn.execute(
            "select project_id from custom_tool_projects where custom_tool_id = ? "
            "order by project_id", (tool_id,)).fetchall()
    ]
    return data


def _validate_custom_tool(name: str, url: str, method: str) -> None:
    """Save-time validation (G6). The URL rules mirror the prover's own `check_url`, so a
    tool cannot be SAVED pointing somewhere it would be refused at call time — failing
    here, while the user is looking at the field, beats failing mid-proof."""
    if not str(name or "").strip():
        raise ValueError("The tool needs a name.")
    if method not in CUSTOM_TOOL_METHODS:
        raise ValueError(f"Method must be one of: {', '.join(CUSTOM_TOOL_METHODS)}.")
    from lea.http_tools import UrlRefused, check_url

    # Placeholders are substituted at call time; check the template with them removed.
    probe = re.sub(r"\{[^}]*\}", "x", str(url or ""))
    try:
        check_url(probe)
    except UrlRefused as exc:
        raise ValueError(str(exc)) from None


def create_custom_tool(name: str, url: str, description: str = "", method: str = "GET",
                       params: dict | None = None, headers: dict | None = None,
                       auth_key_name: str | None = None, auth_header: str | None = None,
                       timeout: int | None = None, enabled: bool = True,
                       authoring: dict | None = None) -> dict:
    method = str(method or "GET").upper()
    if not _authoring.is_empty(authoring):
        description = _authoring.compile_text(authoring)
    _validate_custom_tool(name, url, method)
    _validate_mcp_env(headers)          # a header must not carry a secret either
    now, tool_id = utc_now(), str(uuid4())
    with connect() as conn:
        slug = slugify_skill(str(name).strip())
        if conn.execute("select 1 from custom_tools where slug = ?", (slug,)).fetchone():
            raise ValueError(f"A tool called “{slug}” already exists.")
        conn.execute(
            """
            insert into custom_tools
                (id, name, slug, description, authoring, method, url, params, headers,
                 auth_key_name, auth_header, timeout, enabled, is_global,
                 created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (tool_id, str(name).strip(), slug, str(description or ""),
             _authoring.dumps(authoring), method, str(url).strip(),
             json.dumps(params or {}), json.dumps(headers or {}),
             (auth_key_name or "").strip() or None, (auth_header or "").strip() or None,
             timeout, 1 if enabled else 0, now, now),
        )
        return _custom_tool_row(conn, tool_id)


def list_custom_tools() -> list[dict]:
    with connect() as conn:
        ids = [r["id"] for r in conn.execute(
            "select id from custom_tools order by updated_at desc, name asc").fetchall()]
        return [_custom_tool_row(conn, t) for t in ids]


def get_custom_tool(tool_id: str) -> dict | None:
    with connect() as conn:
        return _custom_tool_row(conn, tool_id)


def set_custom_tool_assignment(tool_id: str, is_global: bool,
                               project_ids: list[str] | None = None) -> dict | None:
    ids = list(dict.fromkeys(project_ids or []))
    with connect() as conn:
        if not conn.execute("select 1 from custom_tools where id = ?", (tool_id,)).fetchone():
            return None
        if ids:
            marks = ",".join("?" for _ in ids)
            known = {r["id"] for r in conn.execute(
                f"select id from projects where id in ({marks})", ids).fetchall()}
            missing = [p for p in ids if p not in known]
            if missing:
                raise ValueError(f"Unknown project id(s): {', '.join(missing)}")
        conn.execute("delete from custom_tool_projects where custom_tool_id = ?", (tool_id,))
        for project_id in ids:
            conn.execute("insert into custom_tool_projects (custom_tool_id, project_id) "
                         "values (?, ?)", (tool_id, project_id))
        conn.execute("update custom_tools set is_global = ?, updated_at = ? where id = ?",
                     (1 if is_global else 0, utc_now(), tool_id))
        return _custom_tool_row(conn, tool_id)


def delete_custom_tool(tool_id: str) -> bool:
    with connect() as conn:
        if not conn.execute("select 1 from custom_tools where id = ?", (tool_id,)).fetchone():
            return False
        conn.execute("delete from custom_tool_projects where custom_tool_id = ?", (tool_id,))
        conn.execute("delete from custom_tools where id = ?", (tool_id,))
    return True


def custom_tool_specs(project_id: str | None) -> list[dict]:
    """Resolved tools as the prover's `cfg.http_tools` list. Same global ∪ assigned rule
    as everything else; secrets absent by construction (`auth_key_name` is a NAME)."""
    with connect() as conn:
        if project_id is None:
            rows = conn.execute("select id from custom_tools where enabled = 1 and "
                                "is_global = 1 order by name").fetchall()
        else:
            rows = conn.execute(
                "select id from custom_tools where enabled = 1 and (is_global = 1 or id in "
                "(select custom_tool_id from custom_tool_projects where project_id = ?)) "
                "order by name", (project_id,)).fetchall()
        out = []
        for r in rows:
            t = _custom_tool_row(conn, r["id"])
            out.append({
                "name": t["slug"], "description": t["description"], "method": t["method"],
                "url": t["url"], "input_schema": t["params"] or {"type": "object", "properties": {}},
                "headers": t["headers"], "auth_key_name": t["auth_key_name"],
                "auth_header": t["auth_header"], "timeout": t["timeout"],
            })
        return out


# --- per-session skill / MCP overrides (v2.5 E0e) ------------------------------
# The session tier. A project picks skills and MCP servers for all its sessions; a
# session may then add or drop either for itself. What is STORED is the diff (`add` / `remove`), so a
# later project-level change still reaches existing sessions — see the 0012 revision.

SKILL_MCP_KINDS = ("skill", "mcp_server")


def set_session_skill_mcp(
    session_id: str, kind: str, item_id: str, action: str | None
) -> None:
    """Record (or clear) one session-level override.

    `action` is 'add', 'remove', or None to delete the override entirely — which is what
    "put it back the way the project has it" means, and why this is a diff rather than a
    stored list.
    """
    if kind not in SKILL_MCP_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(SKILL_MCP_KINDS)}.")
    if action not in ("add", "remove", None):
        raise ValueError("action must be 'add', 'remove', or null.")
    with connect() as conn:
        if not conn.execute("select 1 from sessions where id = ?", (session_id,)).fetchone():
            raise ValueError("Unknown session.")
        conn.execute(
            "delete from session_skill_mcp_overrides where session_id = ? and kind = ? "
            "and item_id = ?",
            (session_id, kind, item_id),
        )
        if action is not None:
            conn.execute(
                "insert into session_skill_mcp_overrides "
                "(session_id, kind, item_id, action, created_at) values (?, ?, ?, ?, ?)",
                (session_id, kind, item_id, action, utc_now()),
            )


def session_skill_mcp_overrides(session_id: str | None, kind: str) -> dict[str, str]:
    """`{item_id: 'add' | 'remove'}` for one session and kind. Empty when no session."""
    if not session_id:
        return {}
    with connect() as conn:
        return {
            r["item_id"]: r["action"]
            for r in conn.execute(
                "select item_id, action from session_skill_mcp_overrides "
                "where session_id = ? and kind = ?",
                (session_id, kind),
            ).fetchall()
        }


def _apply_overrides(base: list[dict], everything: list[dict], overrides: dict[str, str]) -> list[dict]:
    """base ± the session's diff. An override naming an item that no longer exists is
    silently dropped (it was deleted from the library) rather than raising."""
    by_id = {item["id"]: item for item in everything}
    kept = [item for item in base if overrides.get(item["id"]) != "remove"]
    have = {item["id"] for item in kept}
    for item_id, action in overrides.items():
        if action == "add" and item_id not in have and item_id in by_id:
            kept.append(by_id[item_id])
    return kept


def _global_skills() -> list[dict]:
    with connect() as conn:
        ids = [r["id"] for r in conn.execute(
            "select id from skills where is_global = 1 order by updated_at desc, name asc"
        ).fetchall()]
        return [_skill_row(conn, sid) for sid in ids]


def matches_triggers(skill: dict, text: str | None) -> bool:
    """True when a skill should apply to a run with this message (H9).

    No triggers → always on, which is every existing skill. With triggers, a whole-word
    match against the task text. Substring matching would fire "ring" on "bringing";
    whole-word keeps a keyword list something a mathematician can reason about.
    """
    triggers = skill.get("triggers") or []
    if not triggers:
        return True
    haystack = (text or "").lower()
    return any(re.search(rf"\b{re.escape(str(t).lower())}\b", haystack) for t in triggers)


def skills_for_run(project_id: str | None, session_id: str | None = None,
                   task: str | None = None) -> list[dict]:
    """The skills a run actually gets: (global ∪ project-assigned) ± the session's diff.

    A LOOSE session gets the GLOBAL skills — "global" has to mean global, or the word is
    a lie. D47 originally resolved a project-less session to nothing at all, which made a
    skill unusable outside a project and left no way to opt one in; E0e's diff added the
    opt-in, and this makes the inherited half consistent with how MCP servers already
    resolve.
    """
    base = skills_for_project(project_id) if project_id else _global_skills()
    overrides = session_skill_mcp_overrides(session_id, "skill")
    resolved = base if not overrides else _apply_overrides(base, list_skills(), overrides)
    # A skill a session opted into explicitly is wanted regardless of keywords — the user
    # asking for it is a stronger signal than any trigger list.
    forced = {k for k, v in overrides.items() if v == "add"}
    return [s for s in resolved if s["id"] in forced or matches_triggers(s, task)]


def mcp_servers_for_run(project_id: str | None, session_id: str | None = None) -> list[dict]:
    """The MCP servers a run actually gets: resolved set ± the session's diff. A session
    can only add a server that is ENABLED — turning one off in the Library is a global
    "stop using this", which a per-session opt-in must not quietly override."""
    base = mcp_servers_for_project(project_id)
    overrides = session_skill_mcp_overrides(session_id, "mcp_server")
    if not overrides:
        return base
    enabled = [s for s in list_mcp_servers() if s["enabled"]]
    return _apply_overrides(base, enabled, overrides)


def mcp_key_requirements() -> dict[str, list[str]]:
    """`{ENV_VAR_NAME: [server slug, ...]}` — which saved keys each server depends on
    (v2.5 D1).

    Nothing new is declared: `env_from` (stdio) and `api_key_name` (remote) already NAME
    the credentials a server needs, precisely so the value never has to be stored. This
    just reads that declaration back, which is what lets the UI say "this needs a key you
    haven't saved" before enabling, and "clearing this breaks 2 servers" before deleting.
    """
    needs: dict[str, list[str]] = {}
    for row in list_mcp_servers():
        declared = list(row.get("env_from") or [])
        if row.get("api_key_name"):
            declared.append(row["api_key_name"])
        for name in declared:
            needs.setdefault(str(name), []).append(row["slug"])
    return needs


def mcp_server_specs(project_id: str | None, session_id: str | None = None) -> dict[str, dict]:
    """Resolved servers as the prover's `cfg.mcp_servers` mapping (A1).

    Resolution is (global ∪ project-assigned) ± the session's own diff (E0e), so this is
    the single place the two tiers combine. Emits only what `lea.mcp` reads, keyed by slug
    so a rename can't change a running server's identity. Secrets are absent by construction: `env` carries literals and
    `env_from` carries NAMES whose values `_child_env` reads at spawn.
    """
    specs: dict[str, dict] = {}
    for row in mcp_servers_for_run(project_id, session_id):
        if row["transport"] == "stdio":
            spec = {"command": row["command"], "args": row["args"]}
            if row["env"]:
                spec["env"] = row["env"]
            if row["env_from"]:
                spec["env_from"] = row["env_from"]
        else:
            spec = {"url": row["url"]}
            if row["transport"] == "sse":
                spec["transport"] = "sse"
            if row["api_key_name"]:
                # The NAME travels; `bridge` resolves the value into a header at spawn.
                spec["api_key_name"] = row["api_key_name"]
        specs[row["slug"]] = spec
    return specs


def update_run(
    run_id: str,
    status: str,
    final_text: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    result_kind: str | None = None,
    result_detail: str | None = None,
) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            update runs
            set status = ?,
                final_text = coalesce(?, final_text),
                result_kind = coalesce(?, result_kind),
                result_detail = coalesce(?, result_detail),
                input_tokens = coalesce(?, input_tokens),
                output_tokens = coalesce(?, output_tokens),
                cost_usd = coalesce(?, cost_usd),
                updated_at = ?
            where id = ?
            """,
            (status, final_text, result_kind, result_detail, input_tokens, output_tokens, cost_usd, now, run_id),
        )
    _bump_sessions_changed()


def fail_pending_run(run_id: str, detail: str) -> bool:
    """Atomically move a run from `pending` to `failed`; True if THIS caller did it.

    The interrupt endpoint used to read the status, ask the registry whether the run
    was active, and then write — three steps the dispatcher could interleave with
    (AUDIT-2026-07-24 C7). One conditional UPDATE makes the check and the claim the
    same operation, so exactly one of "interrupted before it started" and "started"
    can win."""
    with connect() as conn:
        cursor = conn.execute(
            "update runs set status = 'failed', result_kind = coalesce(result_kind, 'failed'),"
            " result_detail = coalesce(result_detail, ?), updated_at = ?"
            " where id = ? and status = 'pending'",
            (detail, utc_now(), run_id),
        )
    _bump_sessions_changed()
    return cursor.rowcount > 0


def claim_pending_run(run_id: str) -> bool:
    """Atomically move a run from `pending` to `running`; True if THIS caller did it.

    The other half of the same race (C7): `run_lea` used to set `running`
    unconditionally, so an interrupt that landed between admission and start was
    overwritten and the run executed anyway — after the endpoint had already told the
    client it was interrupted."""
    with connect() as conn:
        cursor = conn.execute(
            "update runs set status = 'running', updated_at = ? where id = ? and status = 'pending'",
            (utc_now(), run_id),
        )
    _bump_sessions_changed()
    return cursor.rowcount > 0


def fail_stale_active_runs() -> int:
    """Crash recovery, called once at startup: a run still `running` in the DB
    has no live worker after a restart, so mark it failed. `pending` runs are
    NOT reaped anymore (Phase 2): they are honest queue entries that
    bridge.recover_runs_at_startup re-enqueues, so queued work survives a
    restart instead of being stranded. Returns the count reaped."""
    now = utc_now()
    detail = "Run did not finish: the adapter restarted before it completed."
    with connect() as conn:
        cursor = conn.execute(
            """
            update runs
            set status = 'failed',
                result_kind = coalesce(result_kind, 'failed'),
                result_detail = coalesce(result_detail, ?),
                updated_at = ?
            where status = 'running'
            """,
            (detail, now),
        )
        return cursor.rowcount


def set_run_api_run_id(run_id: str, api_run_id: str) -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            "update runs set api_run_id = ?, updated_at = ? where id = ?",
            (api_run_id, now, run_id),
        )


def set_run_pending_approval(run_id: str, pending_approval: dict | None) -> None:
    now = utc_now()
    value = json.dumps(pending_approval) if pending_approval is not None else None
    with connect() as conn:
        conn.execute(
            "update runs set pending_approval = ?, updated_at = ? where id = ?",
            (value, now, run_id),
        )


def set_session_safe_verify(session_id: str, status: str, detail: str | None) -> None:
    """Persist a standalone /verify verdict on the session's latest run, so it
    survives reload (the endpoint is run-less; the latest run is the proof run,
    and session_detail surfaces it as `safe_verify`)."""
    with connect() as conn:
        row = conn.execute(
            "select id from runs where session_id = ? order by created_at desc, id desc limit 1",
            (session_id,),
        ).fetchone()
        if not row:
            return
        conn.execute(
            "update runs set safe_verify_status = ?, safe_verify_detail = ?, updated_at = ? where id = ?",
            (status, detail, utc_now(), row["id"]),
        )


def record_verification_event(
    *,
    session_id: str,
    formalization_id: str | None,
    path: str,
    status: str,
    detail: str | None,
    code_step_id: str | int | None,
    run_id: str | None = None,
) -> dict:
    event_id = str(uuid4())
    with write() as conn:
        conn.execute(
            """
            insert into verification_events (
                id, formalization_id, session_id, run_id, code_step_id,
                path, status, detail, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, formalization_id, session_id, run_id,
                int(code_step_id) if code_step_id is not None else None,
                path, status, detail, utc_now(),
            ),
        )
        row = conn.execute(
            "select * from verification_events where id = ?", (event_id,)
        ).fetchone()
    return row_to_dict(row)


def latest_code_step_for_path(session_id: str, path: str) -> dict | None:
    """The most recent code step for a file in a session (newest id wins).

    The standalone lean-check / verify endpoints use this to back-fill the verdict
    onto the current working step (the canvas's latest snapshot of that file)."""
    with connect() as conn:
        row = conn.execute(
            f"{TIMELINE_WITH_BLOB} where t.session_id = ? and t.kind = 'code' and t.path = ? "
            "order by t.id desc limit 1",
            (session_id, path),
        ).fetchone()
    return _code_step_from_row(row) if row else None


def current_code_steps_for_formalization(
    formalization_id: str,
    *,
    session_id: str | None = None,
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
    with connect() as conn:
        rows = conn.execute(
            f"""
            select * from (
              select
                t.*,
                b.content as blob_content,
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
              left join artifact_blobs b on b.id = t.after_blob_id
              where ff.formalization_id = ? and {scope_clause}
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
        step = _code_step_from_row(row)
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


def code_steps_for_project_path(
    project_id: str, path: str, *, include_content: bool = True
) -> list[dict]:
    """Every code step for a file across a project's sessions, newest first — the raw
    material for a blueprint node's status + session attribution (D29). Joins on the
    session's project_id so loose sessions never leak in. Ordered by `created_at`
    (cross-session recency; `id` only orders within one session), so the first row is
    the latest verdict and the distinct session order is newest-touched-first.

    `include_content=False` returns the rows with `code=""` and skips the blob join
    entirely (AUDIT-2026-07-24 P3). The blueprint graph calls this once per node and
    reads only `check_status`/`session_id`/`created_at`, so hydrating every historical
    revision of every file — each formerly its own connection and its own full copy of
    the proof — was work whose result was discarded."""
    content_join = "left join artifact_blobs b on b.id = c.after_blob_id" if include_content else ""
    content_column = "b.content as blob_content" if include_content else "'' as blob_content"
    with connect() as conn:
        rows = conn.execute(
            f"select c.*, {content_column} from timeline c "
            f"join sessions s on s.id = c.session_id {content_join} "
            "where s.project_id = ? and c.kind = 'code' and c.path = ? "
            "order by c.created_at desc, c.id desc",
            (project_id, path),
        ).fetchall()
    return [_code_step_from_row(r) for r in rows]


def safe_verify_ok_sessions(project_id: str) -> set[str]:
    """Project session ids whose *latest* run holds a passing SafeVerify verdict.

    The verdict is stored on the session's newest run (`set_session_safe_verify`);
    a human edit clears it (routes/sessions.py) and a fresh agent run supersedes it
    (the new latest run carries no verdict), so 'ok' here means the session's
    current working file was audited — not a stale earlier state. Feeds the blueprint
    graph's `verified` flag: a node is SafeVerify-audited iff the session that owns
    its file's latest code_step is in this set."""
    with connect() as conn:
        rows = conn.execute(
            "select s.id from sessions s "
            "join runs r on r.id = ("
            "  select id from runs where session_id = s.id order by created_at desc, id desc limit 1"
            ") "
            "where s.project_id = ? and r.safe_verify_status = 'ok'",
            (project_id,),
        ).fetchall()
    return {row["id"] for row in rows}


def latest_agent_code_step(session_id: str) -> dict | None:
    """The most recent agent-authored code step — the proof state the agent last
    'knew' (D12). Its content vs. the file's current content reveals human edits."""
    with connect() as conn:
        row = conn.execute(
            f"{TIMELINE_WITH_BLOB} where t.session_id = ? and t.kind = 'code' and t.author = 'agent' "
            "order by t.id desc limit 1",
            (session_id,),
        ).fetchone()
    return _code_step_from_row(row) if row else None


def latest_agent_code_step_for_formalization(
    session_id: str, formalization_id: str
) -> dict | None:
    """The latest agent snapshot attributed to one formalization."""
    with connect() as conn:
        row = conn.execute(
            f"{TIMELINE_WITH_BLOB} where t.session_id = ? and t.kind = 'code' "
            "and t.author = 'agent' and t.formalization_id = ? "
            "order by t.id desc limit 1",
            (session_id, formalization_id),
        ).fetchone()
    return _code_step_from_row(row) if row else None


def latest_agent_code_step_for_path(session_id: str, path: str) -> dict | None:
    """As above, but for one file — the per-file 'before' the agent last saw (D12).

    Divergence is a property of a *file*, not a repo: `git diff <sha> HEAD` compared
    whole trees, so an edit to any file in a shared project repo (D24) reported every
    other session's file as diverged too. Keying on the path is what scopes it."""
    with connect() as conn:
        row = conn.execute(
            f"{TIMELINE_WITH_BLOB} where t.session_id = ? and t.kind = 'code' "
            "and t.author = 'agent' and t.path = ? order by t.id desc limit 1",
            (session_id, path),
        ).fetchone()
    return _code_step_from_row(row) if row else None


def edit_notes_since(session_id: str, seq: int) -> list[str]:
    """Edit-note explanations (D11) recorded after a given timeline position —
    the human's words about edits made since the agent last acted (D12)."""
    with connect() as conn:
        rows = conn.execute(
            "select content from timeline where session_id = ? and kind = 'edit_note' and id > ? "
            "order by id asc",
            (session_id, seq),
        ).fetchall()
    return [row["content"] for row in rows]


def get_run(run_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
    return _normalize_run(row_to_dict(row)) if row else None


def get_run_status(run_id: str) -> dict | None:
    """The cheap run-row read (v2.3 item 16): just the four outcome columns a
    poller needs — id + lifecycle status + terminal kind/detail. Deliberately
    NOT ``get_run`` (which pulls ``select *``, including the potentially large
    ``transcript`` blob) and emphatically not ``session_detail`` (messages +
    code_steps + status_events + usage). The Overleaf companion hits this every
    ~3s while waiting for a slot or after a dropped stream; paying a full session
    detail there was a self-inflicted DB-contention source under concurrency."""
    with connect() as conn:
        row = conn.execute(
            "select id, status, result_kind, result_detail from runs where id = ?",
            (run_id,),
        ).fetchone()
    return row_to_dict(row) if row else None


def list_runs_by_status(status: str) -> list[dict]:
    """Runs with a status in stable FIFO order, used for startup recovery."""
    with connect() as conn:
        rows = conn.execute(
            "select * from runs where status = ? order by created_at asc, id asc",
            (status,),
        ).fetchall()
    return [_normalize_run(row_to_dict(row)) for row in rows]


# --- Structured artifact index (PLAN-system-hardening 4.1) -------------------
# One row per (scope, declaration): "declaration X currently lives at path Y".
# Written by the run finalizer; read by the Overleaf companion instead of
# reverse-engineering artifacts from registry-markdown diffs.

def upsert_artifact(
    *,
    project_id: str | None,
    session_id: str | None,
    run_id: str | None,
    declaration_name: str,
    kind: str | None,
    path: str,
    module_name: str | None,
    formalization_id: str | None = None,
    source_hash: str | None = None,
) -> dict:
    scope = project_id or session_id
    if not scope:
        raise ValueError("an artifact needs a project or a session scope")
    now = utc_now()
    # Concurrent runs can finish in the same project, so serialize the
    # read-then-upsert and keep one stable row id per (scope, declaration).
    with write() as conn:
        existing = conn.execute(
            "select id from artifacts where scope = ? and declaration_name = ?",
            (scope, declaration_name),
        ).fetchone()
        if existing:
            conn.execute(
                "update artifacts set project_id = ?, session_id = ?, run_id = ?,"
                " kind = ?, path = ?, module_name = ?, formalization_id = coalesce(?, formalization_id),"
                " source_hash = coalesce(?, source_hash), updated_at = ? where id = ?",
                (
                    project_id, session_id, run_id, kind, path, module_name,
                    formalization_id, source_hash, now, existing["id"],
                ),
            )
            artifact_id = existing["id"]
        else:
            artifact_id = str(uuid4())
            conn.execute(
                "insert into artifacts (id, scope, project_id, session_id, run_id,"
                " declaration_name, kind, path, module_name, formalization_id,"
                " source_hash, created_at, updated_at)"
                " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (artifact_id, scope, project_id, session_id, run_id,
                 declaration_name, kind, path, module_name, formalization_id,
                 source_hash, now, now),
            )
        row = conn.execute("select * from artifacts where id = ?", (artifact_id,)).fetchone()
    return row_to_dict(row)


def latest_check_for_project_path(project_id: str, path: str) -> dict | None:
    """The newest recorded check verdict for a repo-relative path across ALL of
    a project's sessions (they share one repo, D24). One of the ledger facts
    the target-status endpoint serves (PLAN 4.4): agent runs, manual edits,
    and cascade re-checks all land in the unified timeline."""
    with connect() as conn:
        row = conn.execute(
            """
            select t.check_status, t.check_detail, t.author, t.data, t.created_at
            from timeline t
            join sessions s on s.id = t.session_id
            where s.project_id = ? and t.kind = 'code' and t.path = ?
              and t.check_status is not null
            order by t.created_at desc, t.id desc
            limit 1
            """,
            (project_id, path),
        ).fetchone()
    if not row:
        return None
    result = row_to_dict(row)
    if result.get("data"):
        try:
            result["author"] = json.loads(result["data"]).get("reason") or result["author"]
        except (TypeError, ValueError):
            pass
    result.pop("data", None)
    return result


def list_artifacts_for_scope(scope: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "select * from artifacts where scope = ? order by declaration_name asc",
            (scope,),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def rebase_project_artifact_modules(
    project_id: str,
    *,
    old_namespace: str,
    new_namespace: str,
) -> int:
    """Rebase cached artifact module names after an explicit project rename.

    Artifact ``path`` values are relative to the project repo and therefore do
    not change when the repo moves. ``module_name`` is namespace-qualified,
    however, and is returned by the target-status ledger to the Overleaf pane.
    Only exact namespace matches (or dot-delimited descendants) are rewritten
    so similarly-prefixed namespaces cannot be changed accidentally.
    """
    old_ns = validate_project_namespace(old_namespace)
    new_ns = validate_project_namespace(new_namespace)
    if old_ns == new_ns:
        return 0
    now = utc_now()
    changed = 0
    with write() as conn:
        rows = conn.execute(
            "select id, module_name from artifacts where project_id = ?",
            (project_id,),
        ).fetchall()
        for row in rows:
            module_name = str(row["module_name"] or "")
            if module_name == old_ns:
                rebased = new_ns
            elif module_name.startswith(f"{old_ns}."):
                rebased = f"{new_ns}{module_name[len(old_ns):]}"
            else:
                continue
            conn.execute(
                "update artifacts set module_name = ?, updated_at = ? where id = ?",
                (rebased, now, row["id"]),
            )
            changed += 1
    return changed


def queue_position(run_id: str) -> int | None:
    """How many pending runs precede this pending run (0 = next up). None when
    the run is not pending. Derived, never stored — invariant 2."""
    with connect() as conn:
        row = conn.execute(
            "select created_at, id, status from runs where id = ?", (run_id,)
        ).fetchone()
        if not row or row["status"] != "pending":
            return None
        ahead = conn.execute(
            "select count(*) as n from runs where status = 'pending'"
            " and (created_at < ? or (created_at = ? and id < ?))",
            (row["created_at"], row["created_at"], row["id"]),
        ).fetchone()
    return int(ahead["n"])


def set_run_transcript(run_id: str, messages: list) -> None:
    """Persist the faithful prover conversation at this run's end (D16/multi-turn).

    `messages` is the prover's `Finished.transcript["messages"]` — the structured
    model-replay conversation (tool_call/tool_result parts intact, raw_part already
    stripped). Stored as JSON; the next activation in the session replays it as the
    base. Only called on a Finished run, so an errored run leaves this NULL.
    """
    with connect() as conn:
        conn.execute(
            "update runs set transcript = ?, updated_at = ? where id = ?",
            (json.dumps(messages), utc_now(), run_id),
        )


def latest_transcript_for_session(session_id: str, exclude_run_id: str | None = None) -> list | None:
    """The most recent stored transcript in the session — the base for the next run.

    Each activation receives the prior transcript and returns the full updated one,
    so the latest run that has a transcript holds the whole conversation so far.
    `exclude_run_id` skips the current (just-created, transcript-less) run. Returns
    None when the session has no prior Finished run (a cold first activation).
    """
    with connect() as conn:
        row = conn.execute(
            """
            select transcript from runs
            where session_id = ? and transcript is not null and id != ?
            order by created_at desc, id desc
            limit 1
            """,
            (session_id, exclude_run_id or ""),
        ).fetchone()
    if not row or row["transcript"] is None:
        return None
    return json.loads(row["transcript"])


def transcript_gap_for_session(session_id: str, exclude_run_id: str | None = None) -> list[dict]:
    """Finished runs that left no transcript and are NEWER than the one being replayed.

    `latest_transcript_for_session` silently falls back to the newest run that *has* a
    transcript. A run that crashed mid-turn never reaches `Finished`, so it stores
    none — and simply disappears from the replayed history (AUDIT-2026-07-24 C10). The
    user watched that turn happen; the next one replays a conversation in which it
    never did, and the agent redoes the work.

    This names what is missing so the caller can say so out loud. Only *terminal* runs
    count: a pending or running one is not a gap, it is a run.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            select id, status, result_kind, result_detail, created_at
            from runs
            where session_id = ?
              and id != ?
              and transcript is null
              and status not in ('pending', 'running')
              and created_at > coalesce((
                  select max(created_at) from runs
                  where session_id = ? and transcript is not null and id != ?
              ), '')
            order by created_at asc, id asc
            """,
            (session_id, exclude_run_id or "", session_id, exclude_run_id or ""),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def latest_transcript_run_for_session(session_id: str) -> dict | None:
    """The run that holds the session's latest transcript — id, model, and messages.

    The manual `/compact` path (G3) needs the run_id (to overwrite the condensed
    transcript back onto the SAME row that seeds the next activation) and the model
    (to run the summary call with the session's own model). Returns None when the
    session has no Finished run yet (nothing to compact)."""
    with connect() as conn:
        row = conn.execute(
            """
            select id, model, transcript from runs
            where session_id = ? and transcript is not null
            order by created_at desc, id desc
            limit 1
            """,
            (session_id,),
        ).fetchone()
    if not row or row["transcript"] is None:
        return None
    return {"run_id": row["id"], "model": row["model"], "messages": json.loads(row["transcript"])}


# ---------------------------------------------------------------------------
# timeline (C4) — one table, one counter
#
# `messages` and `code_steps` were two tables sharing one hand-rolled `seq`
# counter, which is what made a session's thread an ORDER BY merge. That counter
# was a read-modify-write across both tables, and under concurrent writers it
# silently issued duplicate seqs (measured: ~110/200 collisions; see db.write()).
#
# Merging the tables retires the counter: `timeline.id` is an autoincrement
# primary key, so ordering is assigned by SQLite under the write lock. The race
# isn't fixed, it's unrepresentable — there is no read-then-write to lose. The
# `seq` key below is that id, kept so callers and the frontend read unchanged.
#
# Content lives in `artifact_blobs` (D7 inverted): a code row points at a blob by
# id, and the schema CHECKs that a code row has one (or is explicitly marked
# `content_lost`). Git was an unverified pointer into a second store — the 0004
# backfill found a code_step naming a commit whose tree never held the file.
# ---------------------------------------------------------------------------


def _put_blob(conn, content: str) -> str:
    """Insert-or-find a blob by content hash. Dedup is the schema's job (`sha256`
    is UNIQUE), so this stays a dumb upsert. Identical content across steps — a
    revert, a cascade re-check, an unchanged save — costs one row, not a copy.

    Must run inside a `write()`: this is a read-then-insert, and the UNIQUE index
    is what makes a concurrent duplicate an error rather than a silent second copy.
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    row = conn.execute("select id from artifact_blobs where sha256 = ?", (digest,)).fetchone()
    if row:
        return row["id"]
    blob_id = str(uuid4())
    conn.execute(
        "insert into artifact_blobs (id, sha256, content, created_at) values (?, ?, ?, ?)",
        (blob_id, digest, content, utc_now()),
    )
    return blob_id


def blob_content(blob_id: str | None) -> str | None:
    """A blob's text, or None if absent (a `content_lost` row, or no blob)."""
    if not blob_id:
        return None
    with connect() as conn:
        row = conn.execute("select content from artifact_blobs where id = ?", (blob_id,)).fetchone()
    return row["content"] if row else None


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
    }


# Every read that turns timeline rows into code steps selects through this, so the
# blob arrives WITH the row instead of costing a second query — and a whole extra
# SQLite connection — per step (AUDIT-2026-07-24 P3). `session_detail` on a session
# with 200 steps opened 200 connections; `graph.build_graph` did it per revision of
# per file. Aliased to `blob_content` because `timeline.content` already exists (it
# holds message text), so `b.content` would collide on the way out.
TIMELINE_WITH_BLOB = (
    "select t.*, b.content as blob_content from timeline t "
    "left join artifact_blobs b on b.id = t.after_blob_id"
)


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
    return {
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
    }


def add_message(
    session_id: str,
    role: str,
    content: str,
    run_id: str | None = None,
    kind: str = "assistant",
    commit_sha: str | None = None,
    formalization_id: str | None = None,
) -> dict:
    """Append a transcript message. A user's edit explanation (D11) is just this
    with `kind='edit_note'` — no bespoke channel; it rides the same path that feeds
    context to the prover.

    `commit_sha` is accepted and ignored: git no longer stores content, so there is
    no commit to point at. The parameter stays only so callers can be moved off it
    one at a time.
    """
    with write() as conn:
        cur = conn.execute(
            """
            insert into timeline (
                session_id, run_id, kind, author, content, formalization_id, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                run_id,
                kind if kind in ("edit_note", "compaction") else "message",
                "user" if role == "user" else "agent",
                content,
                formalization_id,
                utc_now(),
            ),
        )
        row = conn.execute("select * from timeline where id = ?", (cur.lastrowid,)).fetchone()
    touch_session(session_id)
    return _message_from_row(row)


def add_diagnostic(
    session_id: str,
    run_id: str | None,
    payload: dict,
    *,
    turn: int | None = None,
) -> dict:
    """Persist one diagnostic as a timeline row (v2.4).

    `payload` is `diagnostics.resolve(...)` output — severity/code/title/message/
    remedy/source/context. The human message goes in `content` (so the schema's
    "prose rows must have prose" CHECK holds and a diagnostic is greppable in the DB
    without JSON extraction); the structured rest goes in `data`.

    Persisting is the point: the old model was a mutable client-side string, so a
    second failure erased the first and a reload erased both. A stored row means
    "what went wrong in yesterday's run" is answerable from the UI instead of from
    an adapter stderr that no longer exists.

    `author='environment'` — a diagnostic is neither the user's nor the agent's
    speech; it is the system reporting on itself.
    """
    import json as _json

    with write() as conn:
        cur = conn.execute(
            """
            insert into timeline (session_id, run_id, kind, author, content, turn, data, created_at)
            values (?, ?, 'diagnostic', 'environment', ?, ?, ?, ?)
            """,
            (
                session_id,
                run_id,
                payload.get("message") or payload.get("title") or payload.get("code", ""),
                turn,
                _json.dumps(payload),
                utc_now(),
            ),
        )
        row = conn.execute("select * from timeline where id = ?", (cur.lastrowid,)).fetchone()
    touch_session(session_id)
    return _diagnostic_from_row(row)


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


def diagnostics_for_session(session_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "select * from timeline where session_id = ? and kind = 'diagnostic' order by id asc",
            (session_id,),
        ).fetchall()
    return [_diagnostic_from_row(r) for r in rows]


def add_code_step(
    session_id: str,
    run_id: str | None,
    path: str,
    *,
    content: str,
    author: str = "agent",
    summary: str | None = None,
    turn: int | None = None,
    check_status: str | None = None,
    check_detail: str | None = None,
    artifact_kind: str | None = None,
    provenance: dict | None = None,
    content_lost: bool = False,
    formalization_id: str | None = None,
) -> dict:
    """Record a timeline step holding a file's full contents after a write.

    `content` is the file's bytes and is keyword-only and required — it replaces
    the old `commit_sha` pointer, so a stale caller still passing a sha fails
    loudly rather than storing a 40-char sha as if it were a proof.

    `content_lost=True` (C2) records that the bytes could NOT be captured — the file
    was unreadable when the adapter went to read its after-state. The step still
    happened, so the row is still written; what changes is that it no longer claims
    to know the contents. Previously the unreadable case stored `""` through the
    normal path, which the canvas rendered as "the agent wrote an empty file" — a
    silent, confident lie about proof content. The schema has carried the flag for
    exactly this since 0003; nothing had ever set it.

    `run_id` is NULL for user edits made outside a run (D9); `turn` is NULL for
    user edits. The verdict (`check_status`/`check_detail`) is recorded here, not
    in a commit message (D6), and may be back-filled once `lean_check` returns.

    `author` is constrained by the schema to 'user' | 'agent' | 'environment'.
    Note 'cascade' — a re-verification of an *unchanged* file — is NOT an author:
    it's a *reason*, and it was only ever in this column because the old schema had
    nowhere else to put it. It rides in `data` instead; the file is still the
    agent's work regardless of what prompted the re-check.

    `provenance` (item 25) is merged into the same `data` JSON — e.g.
    `{"promoted_from": "<result_id>"}` links a promoted sub-agent candidate back to
    the child run that produced it, so "which attempt won" stays answerable.
    """
    now = utc_now()
    reason = None if author in ("user", "agent", "environment") else author
    data_obj: dict = {}
    if reason:
        data_obj["reason"] = reason
    if provenance:
        data_obj.update(provenance)
    data_json = json.dumps(data_obj) if data_obj else None
    with write() as conn:
        # A lost-content row points at no blob: the CHECK
        # `kind <> 'code' or after_blob_id is not null or content_lost = 1` is what
        # keeps "I don't have the bytes" and "the bytes are empty" distinguishable at
        # the schema level rather than by convention.
        blob_id = None if content_lost else _put_blob(conn, content)
        cur = conn.execute(
            """
            insert into timeline (
                session_id, run_id, kind, author, turn, path, after_blob_id,
                summary, check_status, check_detail, artifact_kind, data,
                formalization_id, created_at, content_lost
            )
            values (?, ?, 'code', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                run_id,
                "agent" if reason else author,
                turn,
                path,
                blob_id,
                summary,
                check_status,
                check_detail,
                artifact_kind if check_status == "ok" else None,
                data_json,
                formalization_id,
                now,
                1 if content_lost else 0,
            ),
        )
        row = conn.execute("select * from timeline where id = ?", (cur.lastrowid,)).fetchone()
    touch_session(session_id)
    return _code_step_from_row(row, code=("" if content_lost else content))


def project_has_active_run(project_id: str) -> bool:
    """True if any session in the project has a pending/running run.

    A real query, not a scan of derived session status (AUDIT-2026-07-24 C2). A
    session's status is its working-copy *verdict* (D14): once it has any code step
    the verdict wins and it reads 'proved'/'ok'/'error' — `_derive_session_status`
    only ever returns 'running' for a session with **no code yet**. So a caller that
    tested `status == "running"` could see a live run only in a session that had
    never written a file, which is the opposite of the sessions worth protecting.

    Joined through `sessions.project_id` rather than `runs.project_id` on purpose:
    the session's project tag is what `repo_for_session` uses to pick the on-disk
    repo, so it is the link that decides whose working tree a run is writing to —
    which is exactly what this interlock exists to protect."""
    with connect() as conn:
        row = conn.execute(
            "select 1 from runs r join sessions s on s.id = r.session_id "
            "where s.project_id = ? and r.status in ('pending', 'running') limit 1",
            (project_id,),
        ).fetchone()
    return row is not None


# --- Additive GitHub project imports ---------------------------------------

GITHUB_IMPORT_STATUSES = {
    "applying", "checking", "complete", "complete_with_issues", "failed",
}
GITHUB_IMPORT_DISPOSITIONS = {
    "add", "already_present", "path_conflict", "declaration_conflict",
    "unsupported_module_layout", "excluded",
}


def create_github_import(
    *,
    project_id: str,
    source_url: str,
    source_commit_sha: str,
    destination_namespace: str,
    source_ref: str | None = None,
    source_namespace: str | None = None,
    destination_snapshot: str | None = None,
    import_id: str | None = None,
) -> dict:
    now = utc_now()
    row_id = import_id or str(uuid4())
    with write() as conn:
        conn.execute(
            """
            insert or ignore into github_imports (
                id, project_id, source_url, source_ref, source_commit_sha,
                source_namespace, destination_namespace, status,
                destination_snapshot, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, 'applying', ?, ?, ?)
            """,
            (
                row_id, project_id, source_url, source_ref, source_commit_sha,
                source_namespace, destination_namespace, destination_snapshot, now, now,
            ),
        )
        row = conn.execute(
            "select * from github_imports where id = ?", (row_id,)
        ).fetchone()
    if row is None:
        raise ValueError("could not create GitHub import")
    return row_to_dict(row)


def get_github_import(import_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "select * from github_imports where id = ?", (import_id,)
        ).fetchone()
    return row_to_dict(row) if row else None


def list_project_github_imports(project_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "select * from github_imports where project_id = ? "
            "order by created_at desc, id desc",
            (project_id,),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def set_github_import_status(
    import_id: str,
    status: str,
    *,
    session_id: str | None = None,
    commit_sha: str | None = None,
    error_detail: str | None = None,
) -> dict | None:
    if status not in GITHUB_IMPORT_STATUSES:
        raise ValueError(f"unsupported GitHub import status: {status}")
    with write() as conn:
        conn.execute(
            """
            update github_imports
            set status = ?, session_id = coalesce(?, session_id),
                commit_sha = coalesce(?, commit_sha),
                error_detail = coalesce(?, error_detail), updated_at = ?
            where id = ?
            """,
            (status, session_id, commit_sha, error_detail, utc_now(), import_id),
        )
        row = conn.execute(
            "select * from github_imports where id = ?", (import_id,)
        ).fetchone()
    return row_to_dict(row) if row else None


def upsert_github_import_file(
    *,
    import_id: str,
    source_path: str,
    destination_path: str | None,
    disposition: str,
    reason: str | None = None,
    content_sha256: str | None = None,
    code_step_id: int | None = None,
    check_status: str | None = None,
    check_detail: str | None = None,
) -> dict:
    if disposition not in GITHUB_IMPORT_DISPOSITIONS:
        raise ValueError(f"unsupported GitHub import disposition: {disposition}")
    now = utc_now()
    with write() as conn:
        conn.execute(
            """
            insert into github_import_files (
                import_id, source_path, destination_path, disposition, reason,
                content_sha256, code_step_id, check_status, check_detail,
                created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(import_id, source_path) do update set
                destination_path = excluded.destination_path,
                disposition = excluded.disposition,
                reason = excluded.reason,
                content_sha256 = excluded.content_sha256,
                code_step_id = coalesce(excluded.code_step_id, github_import_files.code_step_id),
                check_status = coalesce(excluded.check_status, github_import_files.check_status),
                check_detail = coalesce(excluded.check_detail, github_import_files.check_detail),
                updated_at = excluded.updated_at
            """,
            (
                import_id, source_path, destination_path, disposition, reason,
                content_sha256, code_step_id, check_status, check_detail, now, now,
            ),
        )
        row = conn.execute(
            "select * from github_import_files where import_id = ? and source_path = ?",
            (import_id, source_path),
        ).fetchone()
    return row_to_dict(row)


def set_github_import_file_check(
    import_id: str,
    source_path: str,
    check_status: str,
    check_detail: str | None = None,
) -> dict | None:
    if check_status not in {"pending", "ok", "error"}:
        raise ValueError(f"unsupported GitHub import check status: {check_status}")
    with write() as conn:
        conn.execute(
            """
            update github_import_files
            set check_status = ?, check_detail = ?, updated_at = ?
            where import_id = ? and source_path = ?
            """,
            (check_status, check_detail, utc_now(), import_id, source_path),
        )
        row = conn.execute(
            "select * from github_import_files where import_id = ? and source_path = ?",
            (import_id, source_path),
        ).fetchone()
    return row_to_dict(row) if row else None


def list_github_import_files(import_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "select * from github_import_files where import_id = ? order by source_path",
            (import_id,),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def upsert_github_import_declaration(
    *,
    import_id: str,
    project_id: str,
    destination_path: str,
    declaration_name: str,
    full_name: str,
    kind: str,
    module_name: str,
    formalization_id: str | None = None,
    source_hash_at_match: str | None = None,
) -> dict:
    now = utc_now()
    with write() as conn:
        existing = conn.execute(
            """
            select id from github_import_declarations
            where project_id = ? and destination_path = ? and declaration_name = ?
            """,
            (project_id, destination_path, declaration_name),
        ).fetchone()
        declaration_id = existing["id"] if existing else str(uuid4())
        if existing:
            conn.execute(
                """
                update github_import_declarations
                set import_id = ?, full_name = ?, kind = ?, module_name = ?,
                    formalization_id = coalesce(?, formalization_id),
                    source_hash_at_match = coalesce(?, source_hash_at_match), updated_at = ?
                where id = ?
                """,
                (
                    import_id, full_name, kind, module_name, formalization_id,
                    source_hash_at_match, now, declaration_id,
                ),
            )
        else:
            conn.execute(
                """
                insert into github_import_declarations (
                    id, import_id, project_id, destination_path, declaration_name,
                    full_name, kind, module_name, formalization_id,
                    source_hash_at_match, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    declaration_id, import_id, project_id, destination_path,
                    declaration_name, full_name, kind, module_name, formalization_id,
                    source_hash_at_match, now, now,
                ),
            )
        row = conn.execute(
            "select * from github_import_declarations where id = ?", (declaration_id,)
        ).fetchone()
    return row_to_dict(row)


def bind_github_import_declaration(
    declaration_id: str, formalization_id: str, source_hash: str | None
) -> dict | None:
    with write() as conn:
        conn.execute(
            """
            update github_import_declarations
            set formalization_id = ?, source_hash_at_match = ?, updated_at = ?
            where id = ?
            """,
            (formalization_id, source_hash, utc_now(), declaration_id),
        )
        row = conn.execute(
            "select * from github_import_declarations where id = ?", (declaration_id,)
        ).fetchone()
    return row_to_dict(row) if row else None


def find_unbound_imported_declarations(
    project_id: str, declaration_name: str
) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            select d.*, i.session_id, f.code_step_id, f.check_status
            from github_import_declarations d
            join github_imports i on i.id = d.import_id
            join github_import_files f
              on f.import_id = d.import_id and f.destination_path = d.destination_path
            where d.project_id = ? and d.formalization_id is null
              and (d.declaration_name = ? or d.full_name = ?)
            order by d.updated_at desc, d.id desc
            """,
            (project_id, declaration_name, declaration_name),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def list_github_import_declarations(import_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "select * from github_import_declarations where import_id = ? "
            "order by destination_path, full_name",
            (import_id,),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def list_recoverable_github_imports() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "select * from github_imports where status in ('applying', 'checking') "
            "order by created_at, id"
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def project_has_active_import(project_id: str, *, exclude_import_id: str | None = None) -> bool:
    query = (
        "select 1 from github_imports where project_id = ? "
        "and status in ('applying', 'checking')"
    )
    params: list[Any] = [project_id]
    if exclude_import_id:
        query += " and id != ?"
        params.append(exclude_import_id)
    query += " limit 1"
    with connect() as conn:
        row = conn.execute(query, params).fetchone()
    return row is not None


def github_import_progress(import_id: str) -> dict | None:
    imported = get_github_import(import_id)
    if not imported:
        return None
    files = list_github_import_files(import_id)
    declarations = list_github_import_declarations(import_id)
    disposition_counts = Counter(row["disposition"] for row in files)
    check_counts = Counter(row["check_status"] or "unstarted" for row in files)
    return {
        **imported,
        "files": files,
        "declarations": declarations,
        "counts": {
            "dispositions": dict(disposition_counts),
            "checks": dict(check_counts),
            "matched_declarations": sum(
                1 for row in declarations if row.get("formalization_id")
            ),
            "reusable_declarations": sum(
                1 for row in declarations if not row.get("formalization_id")
            ),
        },
    }


def has_active_run(session_id: str) -> bool:
    """True if the session has a pending/running agent run — the modal lock (D62):
    a user write is refused while the agent is mid-run so the two never race on the
    same file. Same status set the derived session status uses for `active_run_count`."""
    with connect() as conn:
        row = conn.execute(
            "select 1 from runs where session_id = ? and status in ('pending', 'running') limit 1",
            (session_id,),
        ).fetchone()
    return row is not None


def upsert_user_code_step(
    session_id: str,
    path: str,
    *,
    content: str,
    formalization_id: str | None = None,
) -> dict:
    """Record a user edit, coalescing rapid successive edits into one timeline step.

    Auto-save (v2.2) saves on every debounced keystroke-pause, which would spray the
    History stepper with one 'your edit' step per save. So if the file's latest step
    is already an *uncommitted-to-a-run* user edit (author='user', run_id NULL), we
    repoint that step at the new content and clear its stale verdict — one step that
    tracks the newest bytes — instead of inserting a new row. A step authored by the
    agent (or a user edit for a different file) still starts a fresh step, so the
    human/agent boundary in the timeline is preserved.

    Coalescing now drops the superseded *content*, where git kept every commit. The
    dropped versions are debounced keystroke states of the file the user is actively
    looking at, so the editor — not history — is their undo. Blobs are content-
    addressed, so an intermediate state that recurs anywhere else is still reachable."""
    latest = latest_code_step_for_path(session_id, path)
    if (
        latest
        and latest.get("author") == "user"
        and latest.get("run_id") is None
        and latest.get("formalization_id") == formalization_id
    ):
        with write() as conn:
            blob_id = _put_blob(conn, content)
            conn.execute(
                "update timeline set after_blob_id = ?, content_lost = 0, check_status = NULL, "
                "check_detail = NULL, artifact_kind = NULL where id = ?",
                (blob_id, int(latest["id"])),
            )
            row = conn.execute("select * from timeline where id = ?", (int(latest["id"]),)).fetchone()
        touch_session(session_id)
        return _code_step_from_row(row, code=content)
    return add_code_step(
        session_id, None, path, content=content, author="user",
        formalization_id=formalization_id,
    )


def set_code_step_check(
    step_id: str,
    check_status: str,
    check_detail: str | None = None,
    artifact_kind: str | None = None,
) -> dict | None:
    """Back-fill a code step's verdict once `lean_check` returns (D6).

    The write's row is inserted *before* the check runs (FileChanged precedes
    CheckResult), so the verdict lands here, on the existing row, rather than in a
    commit message. Returns the updated row, or None if the id is unknown.
    """
    with connect() as conn:
        conn.execute(
            "update timeline set check_status = ?, check_detail = ?, artifact_kind = ? "
            "where id = ? and kind = 'code'",
            (check_status, check_detail, artifact_kind if check_status == "ok" else None, int(step_id)),
        )
        row = conn.execute(f"{TIMELINE_WITH_BLOB} where t.id = ?", (int(step_id),)).fetchone()
    return _code_step_from_row(row) if row else None


def add_status_event(
    session_id: str,
    run_id: str,
    message: str,
    status: str | None = None,
    step_number: int | None = None,
) -> dict:
    now = utc_now()
    event_id = str(uuid4())
    with connect() as conn:
        conn.execute(
            """
            insert into status_events (id, session_id, run_id, step_number, status, message, created_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, session_id, run_id, step_number, status, message, now),
        )
        row = conn.execute("select * from status_events where id = ?", (event_id,)).fetchone()
    return row_to_dict(row)


def replace_run_usage_breakdown(run_id: str, rows: list[dict[str, Any]]) -> None:
    now = utc_now()
    with connect() as conn:
        run = conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
        if not run:
            return
        run_dict = row_to_dict(run)
        session_id = str(run_dict["session_id"])
        run_ids = [
            str(row["id"])
            for row in conn.execute(
                "select id from runs where session_id = ? order by created_at asc, id asc",
                (session_id,),
            ).fetchall()
        ]
        run_number = run_ids.index(run_id) + 1 if run_id in run_ids else 1
        conn.execute("delete from run_usage_breakdown where run_id = ?", (run_id,))
        for index, row in enumerate(rows, start=1):
            conn.execute(
                """
                insert into run_usage_breakdown (
                    id, session_id, run_id, run_number, ordinal, phase, label, turn, candidate,
                    input_tokens, output_tokens, cost_usd, event_count, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row.get("id") or uuid4()),
                    session_id,
                    run_id,
                    run_number,
                    int(row.get("ordinal") or index),
                    str(row.get("phase") or "unattributed"),
                    str(row.get("label") or "Unattributed usage"),
                    _optional_int(row.get("turn")),
                    _optional_int(row.get("candidate")),
                    int(row.get("input_tokens") or 0),
                    int(row.get("output_tokens") or 0),
                    float(row.get("cost_usd") or 0),
                    int(row.get("event_count") or 0),
                    str(row.get("created_at") or now),
                ),
            )


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
    if persisted:
        return persisted
    return _usage_breakdown_from_raw_logs(session_id)


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
            " input_tokens, output_tokens, cost_usd, focus_formalization_id,"
            " focus_source_hash"
            " from runs where session_id = ? order by created_at asc, id asc",
            (session_id,),
        ).fetchall()
        project = None
        if session.get("project_id"):
            project = conn.execute(
                "select * from projects where id = ?",
                (session["project_id"],),
            ).fetchone()
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
    # working-copy verdict, derived from the latest *real* code_step (asc by seq).
    # Scratch/probe files (exact?/apply? scratchpads) are excluded so a session is
    # only 'ok' when an actual proof compiles, not when a throwaway probe does (M14).
    real_steps = [c for c in code_steps if "scratch" not in (c["path"] or "").lower()]
    latest_check_status = real_steps[-1]["check_status"] if real_steps else None
    latest_artifact_kind = real_steps[-1]["artifact_kind"] if real_steps else None
    latest_code_run_status = None
    if real_steps and real_steps[-1]["run_id"]:
        with connect() as conn:
            run_row = conn.execute(
                "select status from runs where id = ?",
                (real_steps[-1]["run_id"],),
            ).fetchone()
        latest_code_run_status = run_row["status"] if run_row else None
    return {
        **session,
        **usage,
        "status": _derive_session_status(
            latest_check_status, latest_artifact_kind, len(real_steps), active_run is not None, latest_code_run_status
        ),
        "messages": messages,
        "code_steps": [_normalize_code_step(step) for step in code_steps],
        "diagnostics": diagnostics_out,
        "status_events": [row_to_dict(row) for row in status_events],
        "approval_events": approval_events_for_session(session_id),
        "usage_breakdown": usage_breakdown_for_session(session_id),
        "active_run": _normalize_run(row_to_dict(active_run)) if active_run else None,
        "runs": [row_to_dict(r) for r in runs],
        "safe_verify": _safe_verify_summary(row_to_dict(latest_run)) if latest_run else None,
        "project": row_to_dict(project) if project else None,
    }


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


def _safe_verify_summary(run: dict) -> dict | None:
    """The latest run's SafeVerify verdict, for showing/auto-firing on reload."""
    status = run.get("safe_verify_status")
    if not status:
        return None
    return {"run_id": run.get("id"), "status": status, "detail": run.get("safe_verify_detail")}


# --- whole-database usage aggregates (AUDIT-2026-07-24 C1) -------------------
# These are deliberately NOT derived from `list_sessions()`. `usage_stats` used to
# sum the Python list that query returns — and that query ends in `limit 100`, so
# the "global" totals were the totals of the 100 most-recently-updated sessions.
# Past 100 sessions the reported spend *fell* as older sessions aged out of the
# window, and `max_spend_usd` is enforced against exactly that number, so the cap
# silently stopped biting once a workspace grew big enough to need it. The `daily`
# and `models` rollups next to it were already full-table SQL aggregates, so the
# Stats page could disagree with its own chart.
#
# The rule these encode: a total over "everything" is a SQL aggregate over
# everything. A paginated list is for rendering, never for arithmetic.


def total_spend_usd() -> float:
    """Persisted spend across every run — the number the cap is enforced against.

    One scalar aggregate, kept separate from `usage_stats()` on purpose: the cap is
    checked at every turn boundary and on every `UsageUpdated` event, and routing
    that through the full stats payload is both what made it wrong (above) and a
    heavy per-event query against a single-writer database."""
    with connect() as conn:
        row = conn.execute("select coalesce(sum(cost_usd), 0) as cost_usd from runs").fetchone()
    return float(row["cost_usd"] or 0)


def global_usage() -> dict:
    """The `global` block of `usage_stats` — every session and every run counted."""
    with connect() as conn:
        row = conn.execute(
            """
            select
                (select count(*) from sessions) as session_count,
                (select count(*) from timeline where kind != 'code') as message_count,
                coalesce(sum(input_tokens), 0) as input_tokens,
                coalesce(sum(output_tokens), 0) as output_tokens,
                coalesce(sum(cost_usd), 0) as cost_usd
            from runs
            """
        ).fetchone()
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
        "average_tokens_per_session": round(total_tokens / session_count) if session_count else 0,
        "average_cost_per_session": cost_usd / session_count if session_count else 0,
        "average_messages_per_session": round(message_count / session_count) if session_count else 0,
    }


def usage_stats() -> dict:
    # `sessions` stays the (100-row) list the Stats table renders — truncating a
    # rendered list is fine. `global` and `origins` are full-table aggregates, so
    # they no longer inherit that truncation.
    sessions = list_sessions()
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
            from runs
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
            from runs
            group by model
            order by cost_usd desc, total_tokens desc
            """
        ).fetchall()

    return {
        "sessions": sessions,
        "origins": _origin_rollup(),
        "global": global_usage(),
        "daily": [_normalize_usage_day(row_to_dict(row)) for row in daily_rows],
        "models": [_normalize_usage_model(row_to_dict(row)) for row in model_rows],
    }


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
            select
                coalesce(nullif(trim(s.origin), ''), 'ui') as origin,
                count(distinct s.id) as session_count,
                coalesce(sum(r.input_tokens), 0) as input_tokens,
                coalesce(sum(r.output_tokens), 0) as output_tokens,
                coalesce(sum(r.cost_usd), 0) as cost_usd
            from sessions s
            left join runs r on r.session_id = s.id
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
    # 'ui' and 'overleaf' first (stable UI order), then any unexpected origins.
    ordered = ["ui", "overleaf"] + [k for k in buckets if k not in ("ui", "overleaf")]
    return [buckets[k] for k in ordered]


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
    for key in ("run_number", "ordinal", "input_tokens", "output_tokens", "event_count"):
        row[key] = int(row.get(key) or 0)
    row["turn"] = _optional_int(row.get("turn"))
    row["candidate"] = _optional_int(row.get("candidate"))
    row["cost_usd"] = float(row.get("cost_usd") or 0)
    row["total_tokens"] = int(row["input_tokens"]) + int(row["output_tokens"])
    return row


def _normalize_run(row: dict) -> dict:
    raw_pending = row.get("pending_approval")
    if isinstance(raw_pending, str) and raw_pending:
        try:
            row["pending_approval"] = json.loads(raw_pending)
        except json.JSONDecodeError:
            row["pending_approval"] = None
    else:
        row["pending_approval"] = None
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


def _usage_breakdown_from_raw_logs(session_id: str) -> list[dict]:
    with connect() as conn:
        runs = [
            row_to_dict(row)
            for row in conn.execute(
                """
                select id, input_tokens, output_tokens, cost_usd
                from runs
                where session_id = ?
                order by created_at asc, id asc
                """,
                (session_id,),
            ).fetchall()
        ]
    rows: list[dict] = []
    for run_number, run in enumerate(runs, start=1):
        log_path = RAW_EVENT_LOG_DIR / f"{run['id']}.jsonl"
        if not log_path.exists():
            continue
        run_rows, totals = _usage_breakdown_from_log(log_path, run_number)
        input_total = max(int(run.get("input_tokens") or 0), int(totals.get("input_tokens") or 0))
        output_total = max(int(run.get("output_tokens") or 0), int(totals.get("output_tokens") or 0))
        cost_total = max(float(run.get("cost_usd") or 0), float(totals.get("cost_usd") or 0))
        _append_unattributed_usage(run_rows, input_total, output_total, cost_total, run_number)
        for ordinal, row in enumerate(run_rows, start=1):
            row["ordinal"] = ordinal
        rows.extend(_normalize_usage_breakdown_row(row) for row in run_rows)
    return rows


def approval_events_for_session(session_id: str) -> list[dict]:
    with connect() as conn:
        runs = [
            row_to_dict(row)
            for row in conn.execute(
                """
                select id
                from runs
                where session_id = ?
                order by created_at asc, id asc
                """,
                (session_id,),
            ).fetchall()
        ]
    approvals: list[dict] = []
    for run in runs:
        log_path = RAW_EVENT_LOG_DIR / f"{run['id']}.jsonl"
        if not log_path.exists():
            continue
        approvals.extend(_approval_events_from_log(log_path, run["id"], session_id))
    return approvals


def _approval_events_from_log(path, run_id: str, session_id: str) -> list[dict]:
    approvals: dict[str, dict] = {}
    order: list[str] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else frame
            frame_type = str(frame.get("type") or _event_type(payload)).lower()
            approval_id = str(payload.get("approval_id") or "")
            if not approval_id:
                continue
            if frame_type == "approval_requested":
                if approval_id not in approvals:
                    order.append(approval_id)
                approvals[approval_id] = {
                    "id": f"{run_id}:{approval_id}",
                    "session_id": session_id,
                    "run_id": run_id,
                    "approval_id": approval_id,
                    "tier": payload.get("tier"),
                    "candidate": _optional_int(payload.get("candidate")),
                    "lean_code": str(payload.get("lean_code") or ""),
                    "theorem_name": payload.get("theorem_name"),
                    "check_result": payload.get("check_result"),
                    "decision": None,
                    "feedback": None,
                    "resolved_at": None,
                }
            elif frame_type == "approval_resolved" and approval_id in approvals:
                approvals[approval_id]["decision"] = payload.get("decision") or "resolved"
                approvals[approval_id]["feedback"] = payload.get("feedback")
                approvals[approval_id]["resolved_at"] = payload.get("created_at")
    return [approvals[approval_id] for approval_id in order if approval_id in approvals]


def _usage_breakdown_from_log(path, run_number: int) -> tuple[list[dict], dict[str, float | int]]:
    rows: list[dict] = []
    current_turn: int | None = None
    totals: dict[str, float | int] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else frame
            frame_type = str(frame.get("type") or _event_type(payload)).lower()
            if frame_type == "turn_started":
                current_turn = _first_int(payload, "turn")
            if frame_type == "approval_requested":
                candidate = _first_int(payload, "candidate")
                preflight = _last_unlabeled_preflight(rows)
                if preflight is not None and candidate is not None:
                    preflight["candidate"] = candidate
                    preflight["label"] = f"Theorem translation preflight candidate {candidate}"

            input_tokens, output_tokens = _frame_usage(payload)
            cost_usd = _frame_cost(payload)
            if frame_type == "usage_updated":
                _add_usage_breakdown_event(rows, run_number, current_turn, input_tokens, output_tokens, cost_usd)
            elif frame_type in {"finished", "run_status"}:
                totals["input_tokens"] = max(int(totals["input_tokens"]), input_tokens or 0)
                totals["output_tokens"] = max(int(totals["output_tokens"]), output_tokens or 0)
                totals["cost_usd"] = max(float(totals["cost_usd"]), cost_usd or 0.0)
    return rows, totals


def _add_usage_breakdown_event(
    rows: list[dict],
    run_number: int,
    current_turn: int | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: float | None,
) -> None:
    if not input_tokens and not output_tokens and not cost_usd:
        return
    if current_turn is None:
        row = _last_unlabeled_preflight(rows)
        if row is None:
            row = _new_usage_breakdown_row(
                run_number=run_number,
                phase="theorem_translation",
                label="Theorem translation preflight",
                turn=None,
                candidate=None,
            )
            rows.append(row)
    else:
        row = next(
            (
                item for item in rows
                if item.get("run_number") == run_number
                and item.get("phase") == "proof_turn"
                and item.get("turn") == current_turn
            ),
            None,
        )
        if row is None:
            row = _new_usage_breakdown_row(
                run_number=run_number,
                phase="proof_turn",
                label=f"Turn {current_turn}",
                turn=current_turn,
                candidate=None,
            )
            rows.append(row)
    row["input_tokens"] += int(input_tokens or 0)
    row["output_tokens"] += int(output_tokens or 0)
    row["cost_usd"] += float(cost_usd or 0)
    row["event_count"] += 1


def _append_unattributed_usage(
    rows: list[dict],
    input_total: int,
    output_total: int,
    cost_total: float,
    run_number: int,
) -> None:
    input_seen = sum(int(row.get("input_tokens") or 0) for row in rows)
    output_seen = sum(int(row.get("output_tokens") or 0) for row in rows)
    cost_seen = sum(float(row.get("cost_usd") or 0) for row in rows)
    input_delta = max(0, input_total - input_seen)
    output_delta = max(0, output_total - output_seen)
    cost_delta = max(0.0, cost_total - cost_seen)
    if not input_delta and not output_delta and cost_delta < 0.000000001:
        return
    row = _new_usage_breakdown_row(
        run_number=run_number,
        phase="unattributed",
        label="Unattributed usage",
        turn=None,
        candidate=None,
    )
    row["input_tokens"] = input_delta
    row["output_tokens"] = output_delta
    row["cost_usd"] = cost_delta
    rows.append(row)


def _new_usage_breakdown_row(
    *,
    run_number: int,
    phase: str,
    label: str,
    turn: int | None,
    candidate: int | None,
) -> dict:
    return {
        "id": str(uuid4()),
        "run_number": run_number,
        "ordinal": 0,
        "phase": phase,
        "label": label,
        "turn": turn,
        "candidate": candidate,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "event_count": 0,
        "created_at": utc_now(),
    }


def _last_unlabeled_preflight(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    row = rows[-1]
    if row.get("phase") == "theorem_translation" and row.get("candidate") is None:
        return row
    return None


def _event_type(frame: dict[str, Any]) -> str:
    for candidate in _walk_dicts(frame):
        value = candidate.get("type") or candidate.get("event") or candidate.get("kind")
        if isinstance(value, str) and value:
            return value.strip().lower()
    return ""


def _frame_usage(frame: dict[str, Any]) -> tuple[int | None, int | None]:
    for candidate in _walk_dicts(frame):
        usage = candidate.get("usage") if isinstance(candidate.get("usage"), dict) else candidate
        input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
        output_tokens = usage.get("output_tokens") or usage.get("completion_tokens")
        if isinstance(input_tokens, int | float) or isinstance(output_tokens, int | float):
            return (
                int(input_tokens) if isinstance(input_tokens, int | float) else None,
                int(output_tokens) if isinstance(output_tokens, int | float) else None,
            )
    return None, None


def _frame_cost(frame: dict[str, Any]) -> float | None:
    for candidate in _walk_dicts(frame):
        value = candidate.get("cost")
        if isinstance(value, int | float):
            return float(value)
        value = candidate.get("cost_usd")
        if isinstance(value, int | float):
            return float(value)
    return None


def _first_int(frame: dict[str, Any], key: str) -> int | None:
    for candidate in _walk_dicts(frame):
        value = candidate.get(key)
        if isinstance(value, int):
            return value
    return None


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            found.append(item)
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return found
