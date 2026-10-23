"""Published source, companion migration and worker-owned peripheral views."""
from alembic import op

revision = '0044_source_companion_projections'
down_revision = '0043_workspace_commands'
branch_labels = None
depends_on = None
DOMAIN_TABLES = {'source_publishers','source_publications','source_heads','source_archives','companion_documents',
                 'companion_jobs','companion_associations','companion_imports','companion_issues',
                 'peripheral_views','application_settings','download_artifacts'}
INTERNAL_TABLES = {'companion_requests','source_publication_receipts'}


def upgrade():
    statements = [
        '''create table if not exists source_publishers(document_id text primary key,client_id text not null,
            generation integer not null,sequence integer not null,lease_until text not null)''',
        '''create table if not exists source_publication_receipts(document_id text not null,client_id text not null,
            generation integer not null,sequence integer not null,request_hash text not null,publication_id text not null,
            primary key(document_id,client_id,generation,sequence))''',
        '''create table if not exists source_publications(id text primary key,document_id text not null,
            client_id text not null,generation integer not null,sequence integer not null,project_id text,
            formalization_id text,source_identity_hash text not null,bundle_hash text not null,coverage text not null,
            bundle_json text not null,files_json text not null,request_hash text not null,created_at text not null,
            unique(document_id,client_id,generation,sequence))''',
        '''create table if not exists source_heads(document_id text not null,target_kind text not null,target_key text not null,
            publication_id text not null,project_id text,formalization_id text,updated_at text not null,
            primary key(document_id,target_kind,target_key))''',
        '''create table if not exists source_archives(id text primary key,project_id text not null,files_json text not null,
            mode text not null,created_at text not null)''',
        '''create table if not exists companion_documents(name text primary key,payload text not null,
            revision integer not null,updated_at text not null)''',
        '''create table if not exists companion_jobs(id text primary key,document_id text,job_key text,
            project_id text,session_id text,run_id text,formalization_id text,payload text not null,updated_at text not null)''',
        '''create table if not exists companion_associations(target_key text primary key,document_id text,
            project_id text,session_id text,formalization_id text,payload text not null,updated_at text not null)''',
        '''create table if not exists companion_imports(id text primary key,path text not null,digest text not null,
            version integer not null,record_count integer not null,payload text,created_at text not null,archive_path text not null,
            unique(path,digest,version))''',
        '''create table if not exists companion_issues(id text primary key,import_id text,record_id text,
            reason text not null,payload text not null,created_at text not null)''',
        '''create table if not exists companion_requests(command_id text primary key,method text not null,
            path text not null,payload text not null,result_json text)''',
        '''create table if not exists peripheral_views(key text primary key,kind text not null,arguments_json text not null,
            project_id text,session_id text,payload text,status integer not null,headers_json text not null,
            observed_at text not null,expires_at text not null,input_revision integer not null)''',
        '''create table if not exists application_settings(id text primary key,payload text not null,
            provenance text not null,updated_at text not null)''',
        '''create table if not exists download_artifacts(id text primary key,project_id text,session_id text,
            content blob not null,media_type text not null,filename text not null,created_at text not null,expires_at text not null)''',
        'create index if not exists ix_source_heads_form on source_heads(formalization_id)',
        'create index if not exists ix_companion_jobs_run on companion_jobs(run_id)',
    ]
    for statement in statements:op.execute(statement)
    for action,ref in [('insert','new'),('update','new'),('delete','old')]:
        op.execute(f'''create trigger if not exists core_revision_source_heads_{action} after {action} on source_heads begin
            insert into core_scope_revisions select 'p:'||{ref}.project_id,1 where {ref}.project_id is not null
            on conflict(scope) do update set revision=revision+1; end''')
    for table in DOMAIN_TABLES:
        identity='document_id' if table=='source_publishers' else 'name' if table=='companion_documents' else 'target_key' if table=='companion_associations' else 'key' if table=='peripheral_views' else 'id'
        for action,ref in [('insert','new'),('update','new'),('delete','old')]:
            key=f'json_array({ref}.document_id,{ref}.target_kind,{ref}.target_key)' if table=='source_heads' else f'json_array({ref}.{identity})'
            project=f'{ref}.project_id' if table in {'source_publications','source_heads','source_archives','companion_jobs','companion_associations','peripheral_views','download_artifacts'} else 'null'
            session=f'{ref}.session_id' if table in {'companion_jobs','companion_associations','peripheral_views','download_artifacts'} else 'null'
            form=f'{ref}.formalization_id' if table in {'source_publications','source_heads','companion_jobs','companion_associations'} else 'null'
            op.execute(f'''create trigger if not exists changes_{table}_{action} after {action} on {table} begin
                insert into domain_changes(batch_id,entity_type,entity_id,action,project_id,session_id,formalization_id)
                values(lea_change_batch(),'{table}',{key},'{action}',{project},{session},{form}); end''')


def downgrade():
    for action in ('insert','update','delete'):op.execute(f'drop trigger if exists core_revision_source_heads_{action}')
    for table in DOMAIN_TABLES:
        for action in ('insert','update','delete'):op.execute(f'drop trigger if exists changes_{table}_{action}')
    for table in (*INTERNAL_TABLES,*DOMAIN_TABLES):op.drop_table(table)
