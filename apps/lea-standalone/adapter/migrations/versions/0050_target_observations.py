"""Independently published target facts and their exact dependency read sets."""
from alembic import op

revision = '0050_target_observations'
down_revision = '0049_status_history'
branch_labels = None
depends_on = None
DOMAIN_TABLES = {'core_target_observations'}


def upgrade():
    op.execute('''create table if not exists core_target_observations(
        formalization_id text primary key, scope text not null, observation_id text not null,
        state text not null, payload_json text not null, queries_json text not null,
        inputs_json text not null, observed_at text not null, expires_at text not null,
        last_scan_at text not null)''')
    op.execute('create index if not exists ix_core_target_scope on core_target_observations(scope)')
    for action, ref in [('insert', 'new'), ('update', 'new'), ('delete', 'old')]:
        guard = ' when new.observation_id is not old.observation_id or new.state is not old.state' if action == 'update' else ''
        op.execute(f'''create trigger if not exists changes_core_target_observations_{action} after {action}
            on core_target_observations{guard} begin
            insert into domain_changes(batch_id,entity_type,entity_id,action,project_id,session_id,formalization_id)
            values(lea_change_batch(),'core_target_observations',json_array({ref}.formalization_id),'{action}',
                case when substr({ref}.scope,1,2)='p:' then substr({ref}.scope,3) end,
                case when substr({ref}.scope,1,2)='s:' then substr({ref}.scope,3) end,{ref}.formalization_id); end''')
    op.execute('''create trigger if not exists delete_core_target_observation before delete on formalizations begin
        delete from core_target_observations where formalization_id=old.id; end''')


def downgrade():
    op.execute('drop trigger delete_core_target_observation')
    for action in ('insert', 'update', 'delete'):
        op.execute(f'drop trigger changes_core_target_observations_{action}')
    op.drop_table('core_target_observations')
