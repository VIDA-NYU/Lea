"""Additive synthetic-only command admission and fenced ownership."""
from alembic import op
revision='0041_regulated_commands'
down_revision='0040_core_projections'
branch_labels=None
depends_on=None
DOMAIN_TABLES={'commands','command_attempts','command_producers','command_workers'}
INTERNAL_TABLES={'command_capacity','command_reservations'}


def upgrade():
    op.execute('''create table if not exists command_producers(id text primary key,kind text not null,
        credential_hash text not null unique,project_id text,enabled integer not null default 1,created_at text not null)''')
    op.execute('''create table if not exists command_workers(id text primary key,epoch text not null,
        lease_until text not null,heartbeat_at text not null)''')
    op.execute('''create table if not exists commands(admission_id integer primary key autoincrement,id text not null unique,
        producer_id text not null,idempotency_key text not null,request_hash text not null,kind text not null,
        version integer not null,lane text not null,project_id text,session_id text,formalization_id text,
        expected_revision integer,payload_json text not null,state text not null,created_at text not null,
        deadline text not null,owner text,worker_epoch text,generation integer not null default 0,
        attempt_id text,lease_until text,result_json text,error text,
        unique(producer_id,idempotency_key))''')
    op.execute('create index if not exists ix_commands_claim on commands(lane,state,admission_id)')
    op.execute('''create table if not exists command_attempts(id text primary key,command_id text not null,
        owner text not null,worker_epoch text not null,generation integer not null,started_at text not null,
        finished_at text,outcome text,unique(command_id,generation))''')
    op.execute('''create table if not exists command_capacity(key text primary key,count integer not null check(count>=0))''')
    op.execute('''create table if not exists command_reservations(key text primary key,command_id text not null,
        attempt_id text not null)''')
    for table in DOMAIN_TABLES:
        for action,ref in [('insert','new'),('update','new'),('delete','old')]:
            project=f'{ref}.project_id' if table in {'commands','command_producers'} else 'null'
            session=f'{ref}.session_id' if table=='commands' else 'null'
            form=f'{ref}.formalization_id' if table=='commands' else 'null'
            if table=='command_attempts':
                project=f'(select project_id from commands where id={ref}.command_id)'
                session=f'(select session_id from commands where id={ref}.command_id)'
                form=f'(select formalization_id from commands where id={ref}.command_id)'
            op.execute(f'''create trigger if not exists changes_{table}_{action} after {action} on {table} begin
                insert into domain_changes(batch_id,entity_type,entity_id,action,project_id,session_id,formalization_id)
                values(lea_change_batch(),'{table}',json_array({ref}.id),'{action}',{project},{session},{form}); end''')


def downgrade():
    for table in DOMAIN_TABLES:
        for action in ('insert','update','delete'):op.execute(f'drop trigger if exists changes_{table}_{action}')
    for table in ('command_reservations','command_capacity','command_attempts','commands','command_workers','command_producers'):
        op.drop_table(table)
