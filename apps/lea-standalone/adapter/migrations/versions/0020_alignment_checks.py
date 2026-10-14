"""Overleaf run purposes, structured stop metadata, and Lea alignment checks.

Revision ID: 0020_alignment_checks
Revises: 0019_custom_tools
"""

from alembic import op

revision = "0020_alignment_checks"
down_revision = "0019_custom_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    run_columns = {
        row[1] for row in conn.exec_driver_sql("pragma table_info(runs)")
    }
    # A legacy database may be unstamped even though its tables were previously
    # created at a newer revision. Every additive migration in this repository is
    # therefore reconciliatory: add only what the physical schema lacks.
    if "purpose" not in run_columns:
        conn.exec_driver_sql(
            "alter table runs add column purpose text not null default 'general'"
        )
    if "stop_reason" not in run_columns:
        conn.exec_driver_sql("alter table runs add column stop_reason text")
    if "recoverable" not in run_columns:
        conn.exec_driver_sql(
            "alter table runs add column recoverable integer not null default 0"
        )
    op.execute(
        """
        create table if not exists alignment_checks (
            id text primary key,
            formalization_id text not null references formalizations(id),
            project_id text references projects(id),
            session_id text references sessions(id),
            solver_run_id text references runs(id),
            evaluator_run_id text,
            target_kind text not null,
            target_label text not null,
            status text not null,
            verdict text,
            trigger text not null,
            source_bundle_hash text not null,
            artifact_hash text not null,
            artifact_commit text,
            dependency_hash text not null default '',
            source_bundle_json text not null,
            artifact_snapshot_json text not null,
            report_json text,
            report_schema_version integer not null default 1,
            prompt_version text not null,
            attempt integer not null default 1,
            retry_of text references alignment_checks(id),
            model text,
            provider text,
            input_tokens integer not null default 0,
            output_tokens integer not null default 0,
            cost_usd real not null default 0,
            stop_reason text,
            error text,
            created_at text not null,
            started_at text,
            paused_at text,
            completed_at text,
            updated_at text not null,
            check (status in ('pending', 'running', 'paused', 'completed', 'failed')),
            check (verdict is null or verdict in ('approved', 'warning', 'error'))
        )
        """
    )
    op.execute(
        """
        create unique index if not exists uq_alignment_check_snapshot
        on alignment_checks (
            formalization_id, source_bundle_hash, artifact_hash,
            dependency_hash, prompt_version, attempt
        )
        """
    )
    op.execute(
        """
        create index if not exists ix_alignment_checks_formalization
        on alignment_checks (formalization_id, created_at desc)
        """
    )


def downgrade() -> None:
    op.execute("drop index if exists ix_alignment_checks_formalization")
    op.execute("drop index if exists uq_alignment_check_snapshot")
    op.execute("drop table if exists alignment_checks")
    conn = op.get_bind()
    for column in ("recoverable", "stop_reason", "purpose"):
        columns = {row[1] for row in conn.exec_driver_sql("pragma table_info(runs)")}
        if column in columns:
            try:
                conn.exec_driver_sql(f"alter table runs drop column {column}")
            except Exception:  # noqa: BLE001 -- older SQLite cannot drop columns
                pass
