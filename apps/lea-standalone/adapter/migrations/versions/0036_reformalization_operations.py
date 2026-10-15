"""Durable serialized retries and isolated draft applicability."""
from alembic import op

revision = "0036_reformalization_operations"
down_revision = "0035_overleaf_project_names"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""create table reformalization_operations (
        id text primary key, project_id text not null, formalization_id text not null,
        session_id text not null, request_key text unique, request_hash text not null,
        request_json text not null, state text not null default 'queued', root_run_id text,
        manifest_json text, result_json text, queued_at text not null default '',
        created_at text not null, updated_at text not null
    )""")
    op.execute("create index reformalization_queue on reformalization_operations(project_id,state,created_at)")
    op.execute("""create unique index reformalization_owner on reformalization_operations(project_id)
        where state in ('active','applying')""")
    op.execute("""create table reformalization_drafts (
        id integer primary key, operation_id text not null, run_id text, path text not null,
        blob_id text not null, evidence_json text, created_at text not null
    )""")
    op.execute("create index reformalization_draft_path on reformalization_drafts(operation_id,path,id)")
    op.execute("""create table reformalization_applications (
        id text primary key, operation_id text not null, state text not null,
        manifest_json text not null, commit_sha text, created_at text not null, updated_at text not null
    )""")
    op.execute("alter table runs add column reformalization_id text")
    op.execute('alter table runs add column initiating_run_id text')


def downgrade():
    op.drop_column('runs', 'initiating_run_id')
    op.drop_column("runs", "reformalization_id")
    op.drop_table("reformalization_applications")
    op.drop_table("reformalization_drafts")
    op.drop_table("reformalization_operations")
