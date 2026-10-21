"""Keep public tool/check history independently of transport retention."""
from alembic import op

revision = '0049_status_history'
down_revision = '0048_process_boundary'
branch_labels = None
depends_on = None


def upgrade():
    columns = {row[1] for row in op.get_bind().exec_driver_sql('pragma table_info(status_events)')}
    if 'payload' not in columns:
        op.execute("alter table status_events add column payload text not null default '{}'")
    # Recover retained events and the latest compacted observation. Shared IDs
    # make this safe when a record is represented in both stores.
    for source, event_id in [('run_events', 'e.id'), ('run_observation_records', 'e.event_id')]:
        op.execute(f'''insert or ignore into status_events
            (id,session_id,run_id,step_number,status,message,created_at,payload)
            select coalesce(json_extract(e.payload,'$.id'),
                'run-status:'||e.run_id||':'||coalesce(nullif(e.attempt_id,''),'pending')||':'||{event_id}),
                r.session_id,e.run_id,json_extract(e.payload,'$.turn'),
                json_extract(e.payload,'$.status'),
                coalesce(json_extract(e.payload,'$.message'),json_extract(e.payload,'$.status'),'status update'),
                coalesce(json_extract(e.payload,'$.created_at'),r.updated_at),e.payload
            from {source} e join runs r on r.id=e.run_id where e.type='status' ''')


def downgrade():
    op.drop_column('status_events', 'payload')
