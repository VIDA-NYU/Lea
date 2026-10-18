"""Link existing solver lineages and exact tool gates to durable commands."""
from alembic import op

revision='0042_solver_commands'
down_revision='0041_regulated_commands'
branch_labels=None
depends_on=None
DOMAIN_TABLES={'run_commands','operation_commands','tool_approval_requests'}
INTERNAL_TABLES={'command_adoption_checkpoint'}


def upgrade():
    op.execute("create table if not exists command_adoption_checkpoint(id integer primary key check(id=1),cursor text not null)")
    op.execute("insert or ignore into command_adoption_checkpoint values (1,'')")
    op.execute('create index if not exists ix_runs_command_adoption on runs(status,id)')
    op.execute('''create table if not exists run_commands(run_id text primary key,command_id text not null unique)''')
    op.execute('''create table if not exists operation_commands(operation_id text not null,command_id text not null unique,
        request_key text not null,primary key(operation_id,request_key))''')
    op.execute('''create table if not exists tool_approval_requests(id text primary key,run_id text not null,
        attempt_id text,invocation_id text not null,tool_name text not null,args_json text not null,
        state text not null,decision text,command_id text,created_at text not null,resolved_at text,
        unique(run_id,attempt_id,invocation_id))''')
    for table in DOMAIN_TABLES:
        for action,ref in [('insert','new'),('update','new'),('delete','old')]:
            if table=='operation_commands':
                project=f'(select project_id from reformalization_operations where id={ref}.operation_id)'
                session=f'(select session_id from reformalization_operations where id={ref}.operation_id)'
                run='null';identity=f'json_array({ref}.operation_id,{ref}.request_key)'
            else:
                project=f'(select project_id from runs where id={ref}.run_id)'
                session=f'(select session_id from runs where id={ref}.run_id)'
                run=f'{ref}.run_id';identity=f'json_array({ref}.run_id)' if table=='run_commands' else f'json_array({ref}.id)'
            op.execute(f'''create trigger if not exists changes_{table}_{action} after {action} on {table} begin
                insert into domain_changes(batch_id,entity_type,entity_id,action,project_id,session_id,run_id)
                values(lea_change_batch(),'{table}',{identity},'{action}',{project},{session},{run}); end''')


def downgrade():
    op.execute('drop index if exists ix_runs_command_adoption')
    op.drop_table('command_adoption_checkpoint')
    for table in DOMAIN_TABLES:
        for action in ('insert','update','delete'):op.execute(f'drop trigger if exists changes_{table}_{action}')
        op.drop_table(table)
