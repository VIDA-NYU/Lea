"""Durable reports for Overleaf Formalize all batches."""
from alembic import op

revision = "0024_formalize_batch_reports"
down_revision = "0023_source_pause_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""create table if not exists formalize_batch_reports (
        batch_id text primary key, project_id text not null references projects(id),
        request_hash text not null, started_at text not null, finished_at text not null,
        canceled integer not null default 0, state text not null,
        facts_json text not null, narrative text, error text,
        model text, input_tokens integer not null default 0,
        output_tokens integer not null default 0, cost_usd real not null default 0,
        attempts integer not null default 0, created_at text not null, updated_at text not null
    )""")
    op.execute("create index if not exists ix_formalize_batch_reports_project on formalize_batch_reports(project_id, finished_at desc)")


def downgrade():
    op.execute("drop table if exists formalize_batch_reports")
