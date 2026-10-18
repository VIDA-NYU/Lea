"""Independent worker lifetime, SQL health and private bridge authentication."""
from alembic import op
revision='0048_process_boundary'
down_revision='0047_queued_editor'
branch_labels=None
depends_on=None
DOMAIN_TABLES={'worker_process'}

def upgrade():
    op.execute('''create table if not exists worker_process(id integer primary key check(id=1),
        runtime_id text not null,generation integer not null,pid integer not null,
        lifecycle_state text not null,started_at text not null,heartbeat_at text not null,
        expires_at text not null,capabilities_json text not null,snapshot_json text not null,
        bridge_hash text)''')
    for action,ref in [('insert','new'),('update','new'),('delete','old')]:
        op.execute(f'''create trigger if not exists changes_worker_process_{action} after {action} on worker_process begin
            insert into domain_changes(batch_id,entity_type,entity_id,action,project_id,session_id,formalization_id)
            values(lea_change_batch(),'worker_process',json_array({ref}.id),'{action}',null,null,null); end''')

def downgrade():
    for action in ('insert','update','delete'):op.execute(f'drop trigger changes_worker_process_{action}')
    op.drop_table('worker_process')
