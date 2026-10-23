"""Durable serialized retries and isolated draft applicability."""
from alembic import op

revision = "0036_reformalization_operations"
down_revision = "0035_overleaf_project_names"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""create table if not exists reformalization_operations (
        id text primary key, project_id text not null, formalization_id text not null,
        session_id text not null, request_key text unique, request_hash text not null,
        request_json text not null, state text not null default 'queued', root_run_id text,
        manifest_json text, result_json text, queued_at text not null default '',
        created_at text not null, updated_at text not null
    )""")
    op.execute("create index if not exists reformalization_queue on reformalization_operations(project_id,state,created_at)")
    op.execute("""create unique index if not exists reformalization_owner on reformalization_operations(project_id)
        where state in ('active','applying')""")
    op.execute("""create table if not exists reformalization_drafts (
        id integer primary key, operation_id text not null, run_id text, path text not null,
        blob_id text not null, evidence_json text, created_at text not null
    )""")
    op.execute("create index if not exists reformalization_draft_path on reformalization_drafts(operation_id,path,id)")
    op.execute("""create table if not exists reformalization_applications (
        id text primary key, operation_id text not null, state text not null,
        manifest_json text not null, commit_sha text, created_at text not null, updated_at text not null
    )""")
    # As with earlier revisions, adopt an existing unstamped schema safely.
    columns = {row[1] for row in op.get_bind().exec_driver_sql('pragma table_info(runs)')}
    for name in ('reformalization_id', 'initiating_run_id'):
        if name not in columns:
            op.execute(f'alter table runs add column {name} text')


def downgrade():
    op.drop_column('runs', 'initiating_run_id')
    op.drop_column("runs", "reformalization_id")
    op.drop_table("reformalization_applications")
    op.drop_table("reformalization_drafts")
    op.drop_table("reformalization_operations")
