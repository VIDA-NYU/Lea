"""Transactional invalidations and durable run observation, without source copies."""
from alembic import op

revision = "0037_durable_observation"
down_revision = "0036_reformalization_operations"
branch_labels = None
depends_on = None

# Frozen schema coverage. New domain tables must explicitly join the feed in their
# own revision; tests compare this classification with the actual migrated schema.
DOMAIN_TABLES = """agent_roles alignment_checks artifact_blobs artifacts custom_tool_projects
custom_tools formalization_files formalization_review_events formalization_review_requests
formalizations formalize_batch_reports github_import_declarations github_import_files github_imports
lea_status_run_contexts lea_status_updates mcp_server_projects mcp_servers overleaf_bindings
overleaf_source_requests project_dependencies project_dependency_attempts project_dependency_edges
project_files project_library_invocations project_library_modules projects proof_check_evidence
reformalization_applications reformalization_drafts reformalization_operations run_attempts run_events
run_checkpoints run_invocations run_usage_breakdown runs session_formalizations
session_skill_mcp_overrides sessions skill_files skill_projects skills status_events timeline
verification_events""".split()
EXCLUDED_TABLES = {"alembic_version", "messages", "code_steps", "domain_changes", "change_retention"}


def _scopes(table, columns, ref):
    def col(name):
        return f'{ref}."{name}"' if name in columns else "null"

    run = col("run_id") if table != "runs" else f"{ref}.id"
    session = col("session_id") if table != "sessions" else f"{ref}.id"
    form = col("formalization_id") if table != "formalizations" else f"{ref}.id"
    if table == "formalizations":
        session = col("loose_session_id")
    if table in {"reformalization_applications", "reformalization_drafts"}:
        session = f"(select session_id from reformalization_operations where id={ref}.operation_id)"
        form = f"(select formalization_id from reformalization_operations where id={ref}.operation_id)"
    if table == "project_dependency_attempts":
        form = f"(select formalization_id from project_dependencies where id={ref}.dependency_id)"
    if table == "github_import_files":
        session = f"(select session_id from github_imports where id={ref}.import_id)"
    session = f"coalesce({session},(select session_id from runs where id={run}))"
    project = col("project_id") if table != "projects" else f"{ref}.id"
    project = f"coalesce({project},(select project_id from sessions where id={session}),(select project_id from formalizations where id={form}))"
    return project, session, form, run


def upgrade():
    op.execute("""create table if not exists domain_changes (
        id integer primary key autoincrement, batch_id text not null,
        entity_type text not null, entity_id text not null, action text not null,
        project_id text, session_id text, formalization_id text, run_id text,
        created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )""")
    for scope in ("project_id", "session_id", "formalization_id", "run_id"):
        op.execute(f"create index if not exists ix_changes_{scope} on domain_changes({scope},id)")
    op.execute("create table if not exists change_retention (id integer primary key check(id=1), floor integer not null)")
    op.execute("insert or ignore into change_retention values (1,0)")
    op.execute("""create table if not exists run_events (
        id integer primary key autoincrement, run_id text not null,
        attempt_id text not null, type text not null, payload text not null,
        created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )""")
    op.execute("create index if not exists ix_run_events_attempt on run_events(run_id,attempt_id,id)")
    conn = op.get_bind()
    columns = {r[1] for r in conn.exec_driver_sql("pragma table_info(timeline)")}
    for name, definition in {"stream_state": "text", "stream_attempt_id": "text",
                             "stream_sequence": "integer not null default 0"}.items():
        if name not in columns:
            op.execute(f"alter table timeline add column {name} {definition}")
    op.execute("""create unique index if not exists ix_timeline_stream on timeline(run_id,stream_attempt_id)
        where stream_state='streaming'""")
    for table in DOMAIN_TABLES:
        info = list(conn.exec_driver_sql(f'pragma table_info("{table}")'))
        columns = {r[1] for r in info}
        keys = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
        for action, ref in (("insert", "new"), ("update", "new"), ("delete", "old")):
            identity = "json_array(" + ",".join(f'{ref}."{key}"' for key in keys) + ")"
            scopes = ",".join(_scopes(table, columns, ref))
            detached = ""
            if action == "update":
                old_identity = "json_array(" + ",".join(f'old."{key}"' for key in keys) + ")"
                old_scopes = _scopes(table, columns, "old")
                differences = " or ".join(f"({old}) is not ({new})" for old, new in
                                           zip(old_scopes, _scopes(table, columns, "new")))
                differences += f" or {old_identity} is not {identity}"
                detached = f'''insert into domain_changes
                    (batch_id,entity_type,entity_id,action,project_id,session_id,formalization_id,run_id)
                    select lea_change_batch(),'{table}',{old_identity},'detach',{','.join(old_scopes)}
                    where {differences};'''
            op.execute(f'''create trigger if not exists "changes_{table}_{action}" after {action} on "{table}"
                begin insert into domain_changes
                (batch_id,entity_type,entity_id,action,project_id,session_id,formalization_id,run_id)
                values (lea_change_batch(),'{table}',{identity},'{action}',{scopes}); {detached} end''')
    # Explicit domain deletion must remove replay payloads too. BEFORE preserves
    # the parent scope for the event tombstones (foreign keys are intentionally off).
    op.execute("""create trigger if not exists delete_run_observation before delete on runs
        begin delete from run_events where run_id=old.id; end""")


def downgrade():
    op.execute("drop trigger if exists delete_run_observation")
    for table in DOMAIN_TABLES:
        for action in ("insert", "update", "delete"):
            op.execute(f'drop trigger if exists "changes_{table}_{action}"')
    op.drop_index("ix_timeline_stream", table_name="timeline")
    for name in ("stream_sequence", "stream_attempt_id", "stream_state"):
        op.drop_column("timeline", name)
    for table in ("run_events", "change_retention", "domain_changes"):
        op.drop_table(table)
