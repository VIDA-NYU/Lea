"""Bind current verdicts to immutable compiler inputs without certifying legacy rows."""
from alembic import op
import sqlalchemy as sa

revision = '0032_proof_check_evidence'
down_revision = '0031_usage_details'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''create table if not exists proof_check_evidence (
        id text primary key,
        session_id text not null references sessions(id),
        project_id text references projects(id),
        run_id text references runs(id),
        code_step_id integer references timeline(id),
        blob_id text not null references artifact_blobs(id),
        path text not null,
        status text not null check(status in ('ok','error')),
        detail text,
        evidence_json text not null,
        created_at text not null
    )''')
    op.execute('create index if not exists ix_proof_evidence_scope_path on proof_check_evidence(project_id,session_id,path,created_at)')
    for table, name in [('timeline', 'check_evidence_id'), ('project_library_modules', 'check_evidence_json'),
                        ('verification_events', 'blob_id'), ('verification_events', 'evidence_json')]:
        columns = {row[1] for row in op.get_bind().exec_driver_sql(f'pragma table_info({table})')}
        if name not in columns:
            op.add_column(table, sa.Column(name, sa.Text(), nullable=True))


def downgrade():
    op.drop_column('project_library_modules', 'check_evidence_json')
    op.drop_column('verification_events', 'evidence_json')
    op.drop_column('verification_events', 'blob_id')
    op.drop_column('timeline', 'check_evidence_id')
    op.drop_table('proof_check_evidence')
