"""Immutable editor snapshots and bounded language results, never proof evidence."""
from alembic import op
revision='0047_queued_editor'
down_revision='0046_project_namespace_generation'
branch_labels=None
depends_on=None
DOMAIN_TABLES={'editor_buffers','editor_requests','editor_heads'}
INTERNAL_TABLES={'editor_clients','editor_request_slots','editor_contents'}


def upgrade():
    statements=[
        'create table if not exists editor_contents(id text primary key,content text not null)',
        '''create table if not exists editor_clients(id text primary key,session_id text not null,
            project_id text,scope text not null,created_at text not null,expires_at text not null)''',
        '''create table if not exists editor_buffers(id text primary key,owner_id text not null,session_id text not null,
            project_id text,path text not null,version integer not null,base_revision text not null,
            environment_revision text not null,blob_id text not null,sha256 text not null,
            created_at text not null,expires_at text not null,unique(owner_id,path,version))''',
        '''create table if not exists editor_heads(owner_id text not null,path text not null,buffer_id text not null,
            session_id text not null,project_id text,primary key(owner_id,path))''',
        '''create table if not exists editor_requests(command_id text primary key,owner_id text not null,
            session_id text not null,project_id text,buffer_id text not null,kind text not null,
            sequence integer not null,supersession_key text not null,request_hash text not null,
            position_json text,result_json text,result_state text not null,created_at text not null,expires_at text not null)''',
        '''create table if not exists editor_request_slots(owner_id text not null,path text not null,kind text not null,
            supersession_key text not null,sequence integer not null,command_id text not null,
            primary key(owner_id,path,kind,supersession_key))''',
        'create index if not exists ix_editor_buffers_owner on editor_buffers(owner_id,path,version)',
        'create index if not exists ix_editor_requests_buffer on editor_requests(buffer_id)',
        'create index if not exists ix_editor_requests_expiry on editor_requests(expires_at)']
    for statement in statements:op.execute(statement)
    for table in DOMAIN_TABLES:
        identity=f'json_array({{ref}}.owner_id,{{ref}}.path)' if table=='editor_heads' else f'json_array({{ref}}.'+('id)' if table=='editor_buffers' else 'command_id)')
        for action,ref in [('insert','new'),('update','new'),('delete','old')]:
            op.execute(f'''create trigger if not exists changes_{table}_{action} after {action} on {table} begin
                insert into domain_changes(batch_id,entity_type,entity_id,action,project_id,session_id,formalization_id)
                values(lea_change_batch(),'{table}',{identity.format(ref=ref)},'{action}',{ref}.project_id,{ref}.session_id,null); end''')


def downgrade():
    for table in DOMAIN_TABLES:
        for action in ('insert','update','delete'):op.execute(f'drop trigger if exists changes_{table}_{action}')
    for table in (*DOMAIN_TABLES,*INTERNAL_TABLES):op.drop_table(table)
