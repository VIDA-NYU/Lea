"""Refresh boundaries preserve the same durable facts as live observation."""
import os
import sqlite3
import time
import pytest
from app import db, store, observation, process_state
from app.queries import children


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path/'communication.sqlite')
    db.init_db()


def test_child_snapshot_survives_event_retention_and_separates_progress_kinds(isolated):
    parent = store.create_session('Coordinator')
    run = store.create_run(parent['id'], 'test', None, 5)
    child = store.create_session('Child', parent_id=parent['id'])
    child_run = store.create_run(child['id'], 'test', None, 5)
    emit = lambda kind, payload: observation.append_run_event(run['id'], kind, {'child_id':child['id'], **payload})
    emit('subagent_started', {'state':'queued'})
    assert children.observations_for_session(parent['id'])[0]['queued']
    emit('subagent_running', {})
    emit('subagent_progress', {'kind':'turn','turn':2})
    message = store.append_message_chunk(child['id'], child_run['id'], 'λ durable narration')
    emit('subagent_progress', {'kind':'text','text':message['content'],'message':message})
    emit('subagent_progress', {'kind':'check','status':'ok'})
    emit('subagent_progress', {'kind':'tool','tool':'read_file'})
    before = children.observations_for_session(parent['id'])[0]
    assert before['queued'] is False
    assert before['progress'] == {'text':message['content'],'turn':2,'tool':'read_file','check':'ok',
                                  'message_id':message['id'],'stream_sequence':1}
    with db.write() as conn:
        conn.execute('delete from run_events where run_id=?', (run['id'],))
    assert children.observations_for_session(parent['id'])[0] == before
    emit('subagent_progress', {'kind':'turn','turn':3})
    assert children.observations_for_session(parent['id'])[0]['progress']['text'] == ''
    emit('subagent_finished', {'error':'Provider unavailable','stop_notice':'Stopped early'})
    finished = children.observations_for_session(parent['id'])[0]
    assert finished['finished'] and not finished['queued']
    assert finished['error'] == 'Provider unavailable' and finished['stop_notice'] == 'Stopped early'


@pytest.mark.parametrize('lock', ['writer','reader'])
def test_heartbeat_bounds_begin_and_commit_without_publishing_expired_status(isolated, lock):
    process_state.record_start('runtime', os.getpid())
    holder = sqlite3.connect(db.DB_PATH, isolation_level=None)
    holder.execute('begin immediate' if lock=='writer' else 'begin')
    holder.execute('select * from worker_process').fetchall()
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError):
            process_state.publish('runtime', {'lifecycle_state':'ready'}, {'database_boundary':process_state.BOUNDARY})
        assert .8 < time.monotonic()-started < 2
    finally:
        holder.rollback(); holder.close()
    assert not process_state.snapshot()['ready']
    process_state.publish('runtime', {'lifecycle_state':'ready'}, {'database_boundary':process_state.BOUNDARY})
    assert process_state.snapshot()['ready']
    with db.connect() as conn:
        assert conn.execute('select generation from worker_process').fetchone()[0] == 1


def test_nested_write_cannot_extend_outer_deadline_and_rolls_back(isolated):
    session = store.create_session('Before')
    with pytest.raises(sqlite3.OperationalError, match='deadline'):
        with db.write(timeout_seconds=.05) as conn:
            with db.write(timeout_seconds=10):
                conn.execute("update sessions set title='After' where id=?", (session['id'],))
                time.sleep(.06)
    with db.connect() as conn:
        assert conn.execute('select title from sessions where id=?', (session['id'],)).fetchone()[0] == 'Before'


def test_historical_file_token_identifies_its_bytes_and_is_read_only(tmp_path, monkeypatch):
    from app import workspace_observer
    from app.api_routes import projects
    monkeypatch.setattr(db, 'DB_PATH', tmp_path/'history.sqlite'); db.init_db()
    project = store.create_project('history'); root=tmp_path/'workspace';root.mkdir()
    (root/'Proof.lean').write_text('old bytes')
    workspace_observer.enroll(project['id'])
    first=workspace_observer.scan(project['id'],root)['observation_id']
    (root/'Proof.lean').write_text('current bytes')
    workspace_observer.scan(project['id'],root)
    old=projects.read_project_file(project['id'],'Proof.lean',first)
    current=projects.read_project_file(project['id'],'Proof.lean')
    assert old['content']=='old bytes' and old['editable'] is False
    assert current['content']=='current bytes' and current['editable'] is True
    assert old['file_revision'] != current['file_revision']
    assert old['file_revision']=='observed:'+old['blob_id']


def test_accepted_decision_is_in_snapshot_before_worker_consumes_it(isolated):
    from app import run_commands
    from app.queries.core import approval_events_for_session
    session=store.create_session('Gate')
    run=store.create_run(session['id'],'test',None,5)
    store.claim_pending_run(run['id'])
    card=run_commands.bind_approval(run['id'],'gate','invocation','write_file',{'path':'Proof.lean'})
    observation.append_run_event(run['id'],'approval_requested',card)
    first=approval_events_for_session(session['id'])[0]
    assert first['actionable'] and first['decision'] is None
    run_commands.approve(run['id'],'gate','allow')
    current=approval_events_for_session(session['id'])[0]
    assert current['decision']=='allow' and current['actionable'] is False
    assert current['seq']==first['seq']
    assert store.get_run(run['id'])['pending_approval']  # Still awaiting worker consumption.


@pytest.mark.parametrize('module',['app.api_routes.runs','app.routes.runs'])
def test_pending_cursor_resets_before_first_attempt_events(isolated,module):
    import asyncio, importlib
    routes=importlib.import_module(module)
    session=store.create_session('Pending stream')
    run=store.create_run(session['id'],'test',None,5)
    with db.write() as conn:
        conn.execute("update runs set attempt_id='first-attempt',status='running' where id=?",(run['id'],))
    observation.append_run_event(run['id'],'status',{'status':'running','message':'Started'})
    async def check():
        stream=routes._subscribe_durable(run['id'],0,None)
        try:
            frame=await anext(stream)
            assert 'event: resync' in frame and 'id: first-attempt:0' in frame
            assert 'event: status' in await anext(stream)
        finally:await stream.aclose()
    asyncio.run(check())
