"""first-class formalizations inside durable conversation sessions

Revision ID: 0008_multi_formalization_sessions
Revises: 0007_merge_artifact_and_compaction

Adds stable formalization targets, their session/file associations, focused-run
and timeline attribution, and snapshot-scoped SafeVerify evidence. Existing
artifact rows are backfilled conservatively: ambiguous shared-path history is
left unattributed rather than guessed.
"""

from alembic import op


revision = "0008_multi_formalization_sessions"
down_revision = "0007_merge_artifact_and_compaction"
branch_labels = None
depends_on = None


def _formalization_id(artifact_id: str) -> str:
    return f"formalization:{artifact_id}"


def _add_column_if_missing(table: str, column: str, definition: str) -> None:
    """Keep adoption of an unstamped pre-existing database idempotent."""
    conn = op.get_bind()
    columns = {
        row[1]
        for row in conn.exec_driver_sql(f"pragma table_info({table})").fetchall()
    }
    if column not in columns:
        op.execute(f"alter table {table} add column {column} {definition}")


def upgrade() -> None:
    op.execute(
        """
        create table if not exists formalizations (
            id text primary key,
            project_id text references projects(id),
            loose_session_id text references sessions(id),
            display_title text not null,
            declaration_name text,
            kind text not null,
            statement text,
            origin text not null default 'ui',
            origin_key text,
            source_hash text,
            created_at text not null,
            updated_at text not null,
            check (
                (project_id is not null and loose_session_id is null)
                or (project_id is null and loose_session_id is not null)
            )
        )
        """
    )
    op.execute(
        """
        create unique index if not exists ux_formalizations_project_declaration
        on formalizations(project_id, declaration_name)
        where project_id is not null and declaration_name is not null
        """
    )
    op.execute(
        """
        create unique index if not exists ux_formalizations_loose_declaration
        on formalizations(loose_session_id, declaration_name)
        where loose_session_id is not null and declaration_name is not null
        """
    )
    op.execute(
        """
        create unique index if not exists ux_formalizations_project_origin
        on formalizations(project_id, origin, origin_key)
        where project_id is not null and origin_key is not null
        """
    )
    op.execute(
        "create index if not exists ix_formalizations_project_updated "
        "on formalizations(project_id, updated_at desc)"
    )

    op.execute(
        """
        create table if not exists session_formalizations (
            session_id text not null references sessions(id),
            formalization_id text not null references formalizations(id),
            created_at text not null,
            primary key (session_id, formalization_id)
        )
        """
    )
    op.execute(
        "create index if not exists ix_session_formalizations_formalization "
        "on session_formalizations(formalization_id, session_id)"
    )

    op.execute(
        """
        create table if not exists formalization_files (
            formalization_id text not null references formalizations(id),
            path text not null,
            role text not null,
            created_at text not null,
            updated_at text not null,
            primary key (formalization_id, path),
            check (role in ('primary', 'support', 'generated'))
        )
        """
    )
    op.execute(
        """
        create unique index if not exists ux_formalization_files_primary
        on formalization_files(formalization_id)
        where role = 'primary'
        """
    )
    op.execute(
        "create index if not exists ix_formalization_files_path "
        "on formalization_files(path, formalization_id)"
    )

    _add_column_if_missing(
        "runs", "focus_formalization_id", "text references formalizations(id)"
    )
    _add_column_if_missing("runs", "focus_source_hash", "text")
    _add_column_if_missing(
        "timeline", "formalization_id", "text references formalizations(id)"
    )
    _add_column_if_missing(
        "artifacts", "formalization_id", "text references formalizations(id)"
    )
    _add_column_if_missing("artifacts", "source_hash", "text")

    op.execute(
        "create index if not exists ix_runs_focus_status "
        "on runs(focus_formalization_id, status, created_at)"
    )
    op.execute(
        "create index if not exists ix_timeline_formalization "
        "on timeline(formalization_id, id)"
    )
    op.execute(
        "create index if not exists ix_artifacts_formalization "
        "on artifacts(formalization_id)"
    )

    op.execute(
        """
        create table if not exists verification_events (
            id text primary key,
            formalization_id text references formalizations(id),
            session_id text not null references sessions(id),
            run_id text references runs(id),
            code_step_id integer references timeline(id),
            path text not null,
            status text not null,
            detail text,
            created_at text not null,
            check (status in ('ok', 'rejected', 'error', 'unavailable'))
        )
        """
    )
    op.execute(
        "create index if not exists ix_verification_formalization_path "
        "on verification_events(formalization_id, path, created_at desc)"
    )

    bind = op.get_bind()
    artifacts = bind.exec_driver_sql(
        """
        select id, project_id, session_id, run_id, declaration_name, kind, path,
               created_at, updated_at
        from artifacts
        order by created_at asc, id asc
        """
    ).mappings().all()

    for artifact in artifacts:
        project_id = artifact["project_id"]
        session_id = artifact["session_id"]
        if not project_id and not session_id:
            continue
        fid = _formalization_id(str(artifact["id"]))
        artifact_kind = str(artifact["kind"] or "proof")
        kind = "definition" if artifact_kind == "definition" else "theorem"
        created_at = str(artifact["created_at"])
        updated_at = str(artifact["updated_at"])
        bind.exec_driver_sql(
            """
            insert or ignore into formalizations (
                id, project_id, loose_session_id, display_title, declaration_name,
                kind, origin, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, 'backfill', ?, ?)
            """,
            (
                fid,
                project_id,
                None if project_id else session_id,
                artifact["declaration_name"],
                artifact["declaration_name"],
                kind,
                created_at,
                updated_at,
            ),
        )
        bind.exec_driver_sql(
            """
            insert or ignore into formalization_files (
                formalization_id, path, role, created_at, updated_at
            ) values (?, ?, 'primary', ?, ?)
            """,
            (fid, artifact["path"], created_at, updated_at),
        )
        bind.exec_driver_sql(
            "update artifacts set formalization_id = ? where id = ?",
            (fid, artifact["id"]),
        )
        if session_id:
            bind.exec_driver_sql(
                """
                insert or ignore into session_formalizations (
                    session_id, formalization_id, created_at
                ) values (?, ?, ?)
                """,
                (session_id, fid, created_at),
            )

    # A historical run gets a focus only when exactly one artifact/formalization
    # candidate belongs to it. Multi-output runs remain honestly unfocused.
    run_candidates = bind.exec_driver_sql(
        """
        select run_id, min(formalization_id) as formalization_id
        from artifacts
        where run_id is not null and formalization_id is not null
        group by run_id
        having count(distinct formalization_id) = 1
        """
    ).mappings().all()
    for candidate in run_candidates:
        bind.exec_driver_sql(
            """
            update runs
            set focus_formalization_id = ?
            where id = ? and focus_formalization_id is null
            """,
            (candidate["formalization_id"], candidate["run_id"]),
        )

    # Attribute an old code row by path only when that path names exactly one
    # formalization in the session's durable scope.
    code_rows = bind.exec_driver_sql(
        """
        select t.id, t.session_id, t.path, s.project_id
        from timeline t
        join sessions s on s.id = t.session_id
        where t.kind = 'code' and t.formalization_id is null and t.path is not null
        order by t.id asc
        """
    ).mappings().all()
    for row in code_rows:
        if row["project_id"]:
            candidates = bind.exec_driver_sql(
                """
                select distinct f.id
                from formalizations f
                join formalization_files ff on ff.formalization_id = f.id
                where f.project_id = ? and ff.path = ?
                """,
                (row["project_id"], row["path"]),
            ).all()
        else:
            candidates = bind.exec_driver_sql(
                """
                select distinct f.id
                from formalizations f
                join formalization_files ff on ff.formalization_id = f.id
                where f.loose_session_id = ? and ff.path = ?
                """,
                (row["session_id"], row["path"]),
            ).all()
        if len(candidates) == 1:
            bind.exec_driver_sql(
                "update timeline set formalization_id = ? where id = ?",
                (candidates[0][0], row["id"]),
            )


def downgrade() -> None:
    raise NotImplementedError("multi-formalization attribution is not reversibly droppable")
