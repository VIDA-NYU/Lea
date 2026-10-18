"""Revision-fenced, worker-published core read projections."""
from alembic import op
import importlib.util
from pathlib import Path

revision='0040_core_projections'
down_revision='0039_workspace_observations'
branch_labels=None
depends_on=None
DOMAIN_TABLES={'core_observations','core_projection_values','core_projection_enrollment','core_legacy_records'}
INTERNAL_TABLES={'core_scope_revisions','core_backfills'}
INPUT_TABLES='sessions projects timeline runs run_attempts run_checkpoints run_invocations formalizations formalization_files session_formalizations artifacts proof_check_evidence verification_events formalization_review_events reformalization_operations reformalization_applications reformalization_drafts project_dependencies project_dependency_attempts project_dependency_edges project_library_modules'.split()


def upgrade():
    columns={r[1] for r in op.get_bind().exec_driver_sql('pragma table_info(workspace_backfills)')}
    if 'phase' not in columns:op.execute("alter table workspace_backfills add column phase text not null default 'capture'")
    op.execute('''create table if not exists core_scope_revisions(scope text primary key,revision integer not null)''')
    op.execute('''create table if not exists core_projection_enrollment(scope text primary key,enabled integer not null default 1,
        observation_id text,last_scan_at text not null default '')''')
    op.execute('''create table if not exists core_observations(id text primary key,scope text not null,
        input_revision integer not null,cursor integer not null,observed_at text not null,expires_at text not null,
        state text not null,environment_revision text not null,inputs_json text not null)''')
    op.execute('''create table if not exists core_projection_values(observation_id text not null,scope text not null,
        kind text not null,entity_id text not null,payload text not null,primary key(observation_id,kind,entity_id))''')
    op.execute('''create table if not exists core_backfills(scope text primary key,observation_id text not null,
        checkpoint text not null default '')''')
    op.execute('''create table if not exists core_legacy_records(session_id text primary key,scope text not null,
        approvals_json text not null,usage_json text not null)''')
    op.execute('create index if not exists ix_core_observations_scope on core_observations(scope,observed_at)')
    # Frozen earlier scope rules preserve detached scopes and shared-file ownership.
    spec=importlib.util.spec_from_file_location('observation_scopes',Path(__file__).with_name('0037_durable_observation.py'))
    earlier=importlib.util.module_from_spec(spec);spec.loader.exec_module(earlier)
    conn=op.get_bind()
    for table in INPUT_TABLES:
        cols={r[1] for r in conn.exec_driver_sql(f'pragma table_info({table})')}
        for action,ref in [('insert','new'),('update','new'),('delete','old')]:
            refs=[ref,'old'] if action=='update' else [ref]
            scopes=[];aliases=[]
            for value in refs:
                project,session,form,_=earlier._scopes(table,cols,value)
                scopes.append(f"coalesce('p:'||({project}),'s:'||({session}))")
                aliases.append(f'''select 's:'||f.loose_session_id as scope from formalizations f
                    left join session_formalizations sf on sf.formalization_id=f.id
                    where f.project_id is null and f.loose_session_id is not null
                        and (sf.session_id=({session}) or f.id=({form}))''')
            guard=''
            if table=='timeline':
                guard=f" when {ref}.kind='code'" if action!='update' else " when new.kind='code' or old.kind='code'"
            if action=='update' and table in {'sessions','runs'}:
                fields={'sessions':['project_id','parent_id','title'],
                    'runs':['status','result_kind','result_detail','focus_formalization_id','focus_source_hash',
                            'pending_approval','purpose','reformalization_id','recovery_state','recoverable','stop_reason']}[table]
                guard=' when '+' or '.join(f'new.{c} is not old.{c}' for c in fields if c in cols)
            selection=' union '.join(['select '+s+' as scope' for s in scopes]+aliases)
            op.execute(f'''create trigger if not exists core_revision_{table}_{action} after {action} on {table}{guard} begin
                insert into core_scope_revisions select scope,1 from ({selection}) where scope is not null
                on conflict(scope) do update set revision=revision+1; end''')
    for table in DOMAIN_TABLES:
        key='json_array({ref}.id)' if table=='core_observations' else ('json_array({ref}.scope)' if table=='core_projection_enrollment'
            else 'json_array({ref}.observation_id,{ref}.kind,{ref}.entity_id)')
        if table=='core_legacy_records': key='json_array({ref}.session_id)'
        for action,ref in [('insert','new'),('update','new'),('delete','old')]:
            op.execute(f'''create trigger if not exists changes_{table}_{action} after {action} on {table} begin
                insert into domain_changes(batch_id,entity_type,entity_id,action,project_id,session_id)
                values(lea_change_batch(),'{table}',{key.format(ref=ref)},'{action}',
                    case when substr({ref}.scope,1,2)='p:' then substr({ref}.scope,3) end,
                    case when substr({ref}.scope,1,2)='s:' then substr({ref}.scope,3) end); end''')
    for action,ref in [('update','old'),('delete','old')]:
        op.execute(f'''create trigger if not exists core_blob_{action} after {action} on artifact_blobs begin
            insert into core_scope_revisions select scope,1 from (
                select coalesce('p:'||s.project_id,'s:'||s.id) as scope from timeline t
                    join sessions s on s.id=t.session_id where t.after_blob_id={ref}.id
                union select 's:'||f.loose_session_id from formalizations f join session_formalizations sf on sf.formalization_id=f.id
                    join timeline t on t.session_id=sf.session_id where t.after_blob_id={ref}.id
                    and f.project_id is null and f.loose_session_id is not null
                union select 'p:'||project_id from current_file_bindings where blob_id={ref}.id)
            where scope is not null on conflict(scope) do update set revision=revision+1; end''')
    for table,prefix in [('projects','p:'),('sessions','s:')]:
        extra="delete from core_legacy_records where session_id=old.id;" if table=='sessions' else ''
        op.execute(f'''create trigger if not exists delete_core_{table} before delete on {table} begin
            {extra}
            delete from core_legacy_records where scope='{prefix}'||old.id;
            delete from core_projection_values where scope='{prefix}'||old.id;
            delete from core_observations where scope='{prefix}'||old.id;
            delete from core_projection_enrollment where scope='{prefix}'||old.id;
            delete from core_backfills where scope='{prefix}'||old.id;
            delete from core_scope_revisions where scope='{prefix}'||old.id; end''')


def downgrade():
    op.drop_column('workspace_backfills','phase')
    for action in ('update','delete'):op.execute(f'drop trigger if exists core_blob_{action}')
    for table in INPUT_TABLES:
        for action in ('insert','update','delete'): op.execute(f'drop trigger if exists core_revision_{table}_{action}')
    for table in DOMAIN_TABLES:
        for action in ('insert','update','delete'): op.execute(f'drop trigger if exists changes_{table}_{action}')
    for table in ('projects','sessions'): op.execute(f'drop trigger if exists delete_core_{table}')
    for table in ('core_legacy_records','core_backfills','core_projection_values','core_observations','core_projection_enrollment','core_scope_revisions'):
        op.drop_table(table)
