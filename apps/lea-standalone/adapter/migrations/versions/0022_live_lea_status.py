"""Durable source-bound solver status, independent of historical evaluator records."""
from alembic import op

revision = "0022_live_lea_status"
down_revision = "0021_github_import_alignment"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""create table if not exists lea_status_run_contexts (
        run_id text primary key references runs(id),
        formalization_id text not null references formalizations(id),
        project_id text, session_id text not null, run_generation integer not null,
        contract_version integer not null default 1, operation text not null,
        source_bundle_json text not null, source_identity_hash text not null,
        source_bundle_hash text not null, inherited_update_id text, created_at text not null,
        unique(formalization_id, run_generation)
    )""")
    op.execute("""create table if not exists lea_status_updates (
        id text primary key, run_id text not null references runs(id),
        formalization_id text not null references formalizations(id),
        sequence integer not null, invocation_key text not null,
        schema_version integer not null default 1, kind text not null,
        payload_json text not null, assessment_json text not null,
        artifact_snapshot_json text not null, dependency_hash text not null,
        provenance text not null default 'solver_tool', created_at text not null,
        unique(formalization_id, sequence), unique(run_id, invocation_key)
    )""")
    op.execute("create index if not exists ix_lea_status_run on lea_status_updates(run_id, sequence)")


def downgrade():
    op.execute("drop table if exists lea_status_updates")
    op.execute("drop table if exists lea_status_run_contexts")
