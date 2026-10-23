"""Opt-in database file projections, immutable observed versions and checkpoints."""
from alembic import op

revision = '0039_workspace_observations'
down_revision = '0038_observation_retention'
branch_labels = None
depends_on = None
DOMAIN_TABLES = {'workspace_observations', 'current_file_bindings', 'projection_enrollment'}
INTERNAL_TABLES = {'workspace_backfills'}


def upgrade():
    op.execute('''create table if not exists workspace_observations (
        id text primary key, project_id text not null, environment_revision text not null,
        input_cursor integer not null, observed_at text not null, expires_at text not null,
        state text not null check(state in ('capturing','observed','superseded')),
        provenance text not null, manifest_revision text not null)''')
    op.execute('''create table if not exists current_file_bindings (
        project_id text not null, observation_id text not null, path text not null,
        blob_id text, size integer not null, state text not null
        check(state in ('present','deleted','unavailable')), reason text,
        primary key(project_id,observation_id,path))''')
    op.execute('''create table if not exists projection_enrollment (
        project_id text primary key, version integer not null,
        observation_id text, enabled integer not null default 1,
        last_scan_at text not null default '')''')
    op.execute('''create table if not exists workspace_backfills (
        project_id text primary key, observation_id text not null,
        checkpoint text not null default '', input_revision integer not null,
        manifest_revision text not null)''')
    op.execute('create index if not exists ix_workspace_observations_project on workspace_observations(project_id,observed_at)')
    for table in DOMAIN_TABLES:
        identity = {'workspace_observations': 'json_array({ref}.id)',
                    'current_file_bindings': 'json_array({ref}.project_id,{ref}.observation_id,{ref}.path)',
                    'projection_enrollment': 'json_array({ref}.project_id)'}[table]
        for action, ref in (('insert','new'),('update','new'),('delete','old')):
            detached = ''
            if action == 'update':
                detached=f'''insert into domain_changes(batch_id,entity_type,entity_id,action,project_id)
                    select lea_change_batch(),'{table}',{identity.format(ref='old')},'detach',old.project_id
                    where old.project_id is not new.project_id or
                        {identity.format(ref='old')} is not {identity.format(ref='new')};'''
            op.execute(f'''create trigger if not exists changes_{table}_{action} after {action} on {table} begin
                insert into domain_changes(batch_id,entity_type,entity_id,action,project_id)
                values(lea_change_batch(),'{table}',{identity.format(ref=ref)},'{action}',{ref}.project_id);
                {detached} end''')
    op.execute('''create trigger if not exists delete_workspace_observations before delete on projects begin
        delete from current_file_bindings where project_id=old.id;
        delete from workspace_observations where project_id=old.id;
        delete from projection_enrollment where project_id=old.id;
        delete from workspace_backfills where project_id=old.id; end''')


def downgrade():
    op.execute('drop trigger delete_workspace_observations')
    for table in DOMAIN_TABLES:
        for action in ('insert','update','delete'):
            op.execute(f'drop trigger changes_{table}_{action}')
    for table in ('workspace_backfills','projection_enrollment','current_file_bindings','workspace_observations'):
        op.drop_table(table)
