"""Retention qualification uses canonical SQL history, never a memory broker."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import select
import subprocess
import sys

import pytest
from starlette.requests import Request

from app import db, observation, runbroker, store
from app.routes import sessions
from app.routes.runs import _subscribe_durable


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'retention.sqlite')
    db.init_db()
    session = store.create_session('Retention')
    run = store.create_run(session['id'], 'fixture', None, 3)
    return session, run


def expire(run, *, terminal=True):
    old = (datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
    with db.write() as conn:
        conn.execute('update run_events set created_at=? where run_id=?', (old, run['id']))
        conn.execute('update runs set updated_at=?,status=? where id=?',
                     (old, 'answered' if terminal else 'running', run['id']))


def test_final_text_audit_and_diagnostics_survive_expiry_and_reopen(fixture):
    session, run = fixture
    broker = runbroker.create(run['id'])
    message = store.append_message_chunk(session['id'], run['id'], 'Hello ∀🙂')
    broker.put({'type': 'assistant_delta', 'payload': {'text': message['content'], 'message': message}})
    final = store.finish_message_stream(message['id'])
    broker.put({'type': 'message', 'payload': final})
    for kind, payload in [('approval_requested', {'approval_id':'a', 'tool_name':'write_file'}),
                          ('approval_resolved', {'approval_id':'a', 'decision':'deny'}),
                          ('diagnostic', {'id':'failure', 'message':'preserved error'}),
                          ('done', {'status':'answered'})]:
        broker.put({'type': kind, 'payload': payload})
    assert broker._events == []
    expire(run)
    assert observation.compact_run_events() == {'removed':6, 'protected':0}
    runbroker.drop(run['id'])
    with pytest.raises(observation.RunCursorReset):
        observation.run_events(run['id'], None, 0)
    db.init_db()  # Reopen through the same migration/startup path.
    snapshot = observation.session_snapshot(session['id'])
    assert snapshot['timeline'][0]['content'] == 'Hello ∀🙂'
    assert str(snapshot['timeline'][0]['id']) == message['id']
    assert snapshot['timeline'][0]['stream_state'] == 'final'
    records = snapshot['run_records']
    assert {r['type'] for r in records} == {'approval_requested','approval_resolved','diagnostic','done'}
    assert next(r for r in records if r['type']=='approval_resolved')['payload']['decision']=='deny'
    async def frames():
        return [frame async for frame in _subscribe_durable(run['id'], 0, None)]
    replay = asyncio.run(frames())
    assert len(replay)==2 and 'events_expired' in replay[0] and 'event: done' in replay[1]


@pytest.mark.parametrize('kind', ['active','partial','legacy','child_legacy'])
def test_unpreserved_text_and_active_runs_are_protected(fixture, kind):
    session, run = fixture
    payload = {'text':'do not lose me'}
    if kind=='partial':
        payload['message'] = store.append_message_chunk(session['id'],run['id'],payload['text'])
    if kind=='child_legacy':
        payload.update(kind='text', child_id='unknown')
    observation.append_run_event(run['id'], 'subagent_progress' if kind=='child_legacy' else 'assistant_delta',payload)
    expire(run,terminal=kind!='active')
    result = observation.compact_run_events()
    assert result['removed']==0
    assert observation.run_events(run['id'],None,0)[0]['payload']==payload
    if kind=='partial':
        store.interrupt_message_streams(run['id'])
        assert observation.compact_run_events()['removed']==1
        assert observation.session_snapshot(session['id'])['timeline'][0]['stream_state']=='interrupted'


def test_bounded_batches_preserve_contiguous_floor_and_attempt_isolation(fixture):
    _, run = fixture
    for i in range(7):
        observation.append_run_event(run['id'], 'status', {'text':str(i)})
    expire(run)
    assert observation.compact_run_events(limit=3)['removed']==3
    with db.connect() as conn:
        floor = conn.execute('select floor from run_event_retention').fetchone()[0]
        assert conn.execute('select count(*) from run_events').fetchone()[0]==4
    assert len(observation.run_events(run['id'],None,floor))==4
    with db.write() as conn:
        conn.execute("update runs set attempt_id='new' where id=?",(run['id'],))
    event = observation.append_run_event(run['id'],'status',{'text':'new attempt'})
    assert observation.run_events(run['id'],'new',0)[0]['seq']==event['seq']
    with pytest.raises(observation.RunCursorReset):
        observation.run_events(run['id'],'new',event['seq']+100)


def test_compaction_failure_rolls_back_floor_and_payloads(fixture, monkeypatch):
    _, run = fixture
    observation.append_run_event(run['id'],'status',{'text':'preserved'})
    expire(run)
    def fail(*args):
        raise RuntimeError('injected')
    monkeypatch.setattr(observation,'_record_event',fail)
    with pytest.raises(RuntimeError,match='injected'):
        observation.compact_run_events()
    assert observation.run_events(run['id'],None,0)[0]['payload']['text']=='preserved'


def test_session_snapshot_subscription_race_and_expired_cursor(fixture, monkeypatch):
    session, _ = fixture
    snapshot = sessions.list_sessions()
    # Simulate another process: SQL invalidations, without the store wakeup token.
    with db.write() as conn:
        conn.execute("update sessions set title='committed later' where id=?",(session['id'],))
    async def receive():
        scope={'type':'http','method':'GET','path':'/api/sessions/events',
               'query_string':f"after={snapshot['cursor']}".encode(),'headers':[],
               'app':type('App',(),{'state':type('State',(),{})()})()}
        request=Request(scope,receive=lambda: None)
        async def connected(): return False
        monkeypatch.setattr(request,'is_disconnected',connected)
        response=await sessions.session_list_events(request)
        iterator=response.body_iterator
        frame=await anext(iterator)
        await iterator.aclose()
        return frame
    assert 'sessions_changed' in asyncio.run(receive())
    observation.compact_changes(now=datetime.now(timezone.utc)+timedelta(days=8))
    assert '"reset": true' in asyncio.run(receive())
    assert sessions.list_sessions()['sessions'][0]['title']=='committed later'


def test_process_death_preserves_only_committed_partial_text(fixture):
    session,run=fixture
    script='''import sys
from pathlib import Path
from app import db,store,runbroker
db.DB_PATH=Path(sys.argv[1])
message=store.append_message_chunk(sys.argv[2],sys.argv[3],'committed ∀')
runbroker.create(sys.argv[3]).put({'type':'assistant_delta','payload':{'text':message['content'],'message':message}})
with db.write() as conn:
 conn.execute("update timeline set content=content||' uncommitted' where id=?",(message['id'],))
 print('ready',flush=True)
 import time
 time.sleep(60)
'''
    child=subprocess.Popen([sys.executable,'-c',script,str(db.DB_PATH),session['id'],run['id']],
        cwd=Path(__file__).parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert select.select([child.stdout],[],[],15)[0], 'child did not commit fixture'
        assert child.stdout.readline().strip()=='ready'
        child.kill();child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill();child.wait(timeout=5)
        child.stdout.close();child.stderr.close()
    db.init_db()
    store.interrupt_message_streams()
    snapshot=observation.session_snapshot(session['id'])
    assert snapshot['timeline'][0]['content']=='committed ∀'
    assert snapshot['timeline'][0]['stream_state']=='interrupted'
    assert observation.run_events(run['id'],None,0)[0]['payload']['text']=='committed ∀'
