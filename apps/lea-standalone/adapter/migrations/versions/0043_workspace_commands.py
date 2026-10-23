"""Durable workspace requests, immutable file revisions and effect journals."""
from alembic import op

revision = '0043_workspace_commands'
down_revision = '0042_solver_commands'
branch_labels = None
depends_on = None
DOMAIN_TABLES = {'workspace_requests', 'workspace_file_revisions', 'workspace_files', 'workspace_effects'}
INTERNAL_TABLES = {'workspace_blob_refs'}


def upgrade():
    statements = [
        'create table if not exists workspace_blob_refs(command_id text not null,blob_id text not null,primary key(command_id,blob_id))',
        '''create table if not exists workspace_requests(
            command_id text primary key, action text not null, project_id text, session_id text,
            formalization_id text, arguments_json text not null, result_blob_id text,
            error_status integer, created_at text not null)''',
        '''create table if not exists workspace_file_revisions(
            id text primary key, command_id text not null, scope text not null, path text not null,
            blob_id text not null, base_revision text not null, draft_id text, created_at text not null,
            unique(command_id,path))''',
        '''create table if not exists workspace_files(
            scope text not null, path text not null, project_id text, session_id text,
            revision_id text not null, blob_id text not null, materialization text not null,
            commit_sha text, detail text, encoding text not null default 'utf8', primary key(scope,path))''',
        '''create table if not exists workspace_effects(
            id text primary key, command_id text not null, project_id text, session_id text,
            kind text not null, state text not null, manifest_json text not null,
            created_at text not null, updated_at text not null)''',
        'create index if not exists ix_workspace_effects_recovery on workspace_effects(state)',
        'create index if not exists ix_workspace_solver_gate on commands(kind,state,project_id,session_id)',
    ]
    for statement in statements:
        op.execute(statement)
    for table in DOMAIN_TABLES:
        for action, ref in [('insert', 'new'), ('update', 'new'), ('delete', 'old')]:
            project = f'{ref}.project_id' if table != 'workspace_file_revisions' else f"case when {ref}.scope like 'p:%' then substr({ref}.scope,3) end"
            session = f'{ref}.session_id' if table != 'workspace_file_revisions' else f"case when {ref}.scope like 's:%' then substr({ref}.scope,3) end"
            identity = f'json_array({ref}.scope,{ref}.path)' if table == 'workspace_files' else f'json_array({ref}.command_id)' if table == 'workspace_requests' else f'json_array({ref}.id)'
            op.execute(f'''create trigger if not exists changes_{table}_{action} after {action} on {table} begin
                insert into domain_changes(batch_id,entity_type,entity_id,action,project_id,session_id)
                values(lea_change_batch(),'{table}',{identity},'{action}',{project},{session}); end''')
            if table in {'workspace_files','workspace_effects'}:
                op.execute(f'''create trigger if not exists core_revision_{table}_{action} after {action} on {table} begin
                    insert into core_scope_revisions select coalesce('p:'||{ref}.project_id,'s:'||{ref}.session_id),1
                    where coalesce({ref}.project_id,{ref}.session_id) is not null
                    on conflict(scope) do update set revision=revision+1; end''')


def downgrade():
    op.execute('drop index if exists ix_workspace_solver_gate')
    op.drop_table('workspace_blob_refs')
    for table in DOMAIN_TABLES:
        for action in ('insert', 'update', 'delete'):
            op.execute(f'drop trigger if exists changes_{table}_{action}')
            op.execute(f'drop trigger if exists core_revision_{table}_{action}')
        op.drop_table(table)
