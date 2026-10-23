"""Durable replay floors and compact public run state."""
from alembic import op

revision = "0038_observation_retention"
down_revision = "0037_durable_observation"
branch_labels = None
depends_on = None
DOMAIN_TABLES = {"run_observation_records"}
INTERNAL_TABLES = {"run_event_retention"}


def upgrade():
    op.execute("""create table if not exists run_event_retention (
        run_id text not null, attempt_id text not null, floor integer not null default 0,
        high_water integer not null default 0, checked_at text not null default '',
        primary key(run_id,attempt_id))""")
    op.execute("""insert or ignore into run_event_retention(run_id,attempt_id,high_water)
        select run_id,attempt_id,max(id) from run_events group by run_id,attempt_id""")
    op.execute("""create table if not exists run_observation_records (
        run_id text not null, attempt_id text not null, type text not null,
        record_key text not null, event_id integer not null, payload text not null,
        primary key(run_id,attempt_id,type,record_key))""")
    op.execute("create index if not exists ix_run_events_age on run_events(created_at,id)")
    op.execute("""insert or ignore into run_observation_records
        select run_id,attempt_id,type,record_key,id,payload from (
            select *,coalesce(json_extract(payload,'$.approval_id'),json_extract(payload,'$.id'),
                json_extract(payload,'$.child_id'),'latest') as record_key,
                row_number() over(partition by run_id,attempt_id,type,
                    coalesce(json_extract(payload,'$.approval_id'),json_extract(payload,'$.id'),
                        json_extract(payload,'$.child_id'),'latest') order by id desc) as rank
            from run_events where type not in ('assistant_delta','message','code_step','resync')
                and not(type='subagent_progress' and coalesce(json_extract(payload,'$.kind'),'')='text')
        ) where rank=1""")
    # Replay rows are transport history. Pruning them must not generate another
    # unbounded stream of tombstones; the atomic floor update represents expiry.
    op.execute('drop trigger if exists changes_run_events_delete')
    for action, ref in (("insert", "new"), ("update", "new"), ("delete", "old")):
        detached = ''
        if action == 'update':
            detached = '''insert into domain_changes(batch_id,entity_type,entity_id,action,project_id,session_id,run_id)
                select lea_change_batch(),'run_observation_records',
                    json_array(old.run_id,old.attempt_id,old.type,old.record_key),'detach',
                    (select project_id from runs where id=old.run_id),
                    (select session_id from runs where id=old.run_id),old.run_id
                where json_array(old.run_id,old.attempt_id,old.type,old.record_key) is not
                    json_array(new.run_id,new.attempt_id,new.type,new.record_key);'''
        op.execute(f'''create trigger if not exists changes_run_observation_records_{action}
            after {action} on run_observation_records begin
            insert into domain_changes(batch_id,entity_type,entity_id,action,project_id,session_id,run_id)
            values(lea_change_batch(),'run_observation_records',
                json_array({ref}.run_id,{ref}.attempt_id,{ref}.type,{ref}.record_key),'{action}',
                (select project_id from runs where id={ref}.run_id),
                (select session_id from runs where id={ref}.run_id),{ref}.run_id); {detached} end''')
    op.execute('drop trigger if exists delete_run_observation')
    op.execute("""create trigger delete_run_observation before delete on runs begin
        delete from run_events where run_id=old.id;
        delete from run_observation_records where run_id=old.id;
        delete from run_event_retention where run_id=old.id; end""")


def downgrade():
    op.execute('drop trigger if exists delete_run_observation')
    for action in ('insert', 'update', 'delete'):
        op.execute(f'drop trigger if exists changes_run_observation_records_{action}')
    op.drop_table('run_observation_records')
    op.drop_table('run_event_retention')
    op.drop_index('ix_run_events_age', table_name='run_events')
    op.execute("""create trigger changes_run_events_delete after delete on run_events begin
        insert into domain_changes(batch_id,entity_type,entity_id,action,project_id,session_id,run_id)
        values(lea_change_batch(),'run_events',json_array(old.id),'delete',
            (select project_id from runs where id=old.run_id),
            (select session_id from runs where id=old.run_id),old.run_id); end""")
    op.execute("""create trigger delete_run_observation before delete on runs
        begin delete from run_events where run_id=old.id; end""")
