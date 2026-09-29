"""Durable run attempts and replay boundaries. Historical rows remain untouched."""

from alembic import op

revision = "0026_run_recovery"
down_revision = "0025_run_stop_usage"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    columns = {row[1] for row in conn.exec_driver_sql("pragma table_info(runs)")}
    additions = {
        "attempt_id": "text",
        "recovery_state": "text",
        "recovery_reason": "text",
        "recovery_count": "integer not null default 0",
        "checkpoint_at": "text",
        "deadline_at": "text",
        "resumes_run_id": "text",
        "client_request_id": "text",
        "client_request_hash": "text",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.exec_driver_sql(f"alter table runs add column {name} {definition}")
    conn.exec_driver_sql("create unique index if not exists ux_runs_client_request_id on runs(client_request_id) where client_request_id is not null")
    conn.exec_driver_sql("""create table if not exists run_attempts (
        id text primary key, run_id text not null, runtime_id text not null,
        started_at text not null, ended_at text, outcome text,
        foreign key(run_id) references runs(id))""")
    conn.exec_driver_sql("create index if not exists ix_run_attempts_run on run_attempts(run_id, started_at)")
    conn.exec_driver_sql("""create table if not exists run_checkpoints (
        id integer primary key autoincrement, run_id text not null, attempt_id text not null,
        created_at text not null, schema_version integer not null,
        payload text not null, manifest text not null,
        foreign key(run_id) references runs(id))""")
    conn.exec_driver_sql("create index if not exists ix_run_checkpoints_run on run_checkpoints(run_id, id)")
    conn.exec_driver_sql("""create table if not exists run_invocations (
        id text primary key, run_id text not null, attempt_id text not null,
        kind text not null, name text not null, turn integer not null,
        call_index integer, input_json text, result_json text,
        started_at text not null, completed_at text,
        foreign key(run_id) references runs(id))""")
    conn.exec_driver_sql("create index if not exists ix_run_invocations_run on run_invocations(run_id, started_at)")


def downgrade():
    conn = op.get_bind()
    for name in ("run_invocations", "run_checkpoints", "run_attempts"):
        conn.exec_driver_sql(f"drop table if exists {name}")
    conn.exec_driver_sql("drop index if exists ux_runs_client_request_id")
    for name in ("client_request_hash", "client_request_id", "resumes_run_id", "deadline_at", "checkpoint_at",
                 "recovery_count", "recovery_reason", "recovery_state", "attempt_id"):
        if name in {row[1] for row in conn.exec_driver_sql("pragma table_info(runs)")}:
            conn.exec_driver_sql(f"alter table runs drop column {name}")
