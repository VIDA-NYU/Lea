"""Real SQLite qualification of synthetic admission, quotas and ownership."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import json
import threading
import time
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import db,store,commands,command_host,migration_limits
from app.routes import commands as routes
from app.queries import core


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'commands.sqlite')
    db.init_db();project=store.create_project('queue')
    session=store.create_session('Queue',project_id=project['id'])
    credential=commands.register_producer('operator',project_id=project['id'])
    return {'project':project,'session':session,'credential':credential}


def request(x,key='one',kind='synthetic.echo',**values):
    return {'kind':kind,'idempotency_key':key,'session_id':x['session']['id'],'payload':{'text':'hello'},**values}


def test_concurrent_idempotency_is_producer_scoped_and_canonical(fixture):
    x=fixture;value=request(x)
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts=list(pool.map(lambda _:commands.admit(x['credential'],value),range(16)))
    assert len({r['id'] for r in receipts})==1
    with db.connect() as conn:
        assert conn.execute('select count(*) from commands').fetchone()[0]==1
        assert {r[0] for r in conn.execute('select count from command_capacity')}=={1}
    other=commands.register_producer('other',project_id=x['project']['id'])
    assert commands.admit(other,value)['id']!=receipts[0]['id']
    with pytest.raises(commands.QueueError,match='idempotency_conflict'):
        commands.admit(x['credential'],{**value,'payload':{'text':'changed'}})


def test_quota_saturation_and_controls_have_separate_capacity(fixture,monkeypatch):
    x=fixture
    monkeypatch.setattr(migration_limits,'COMPUTE_QUEUED_GLOBAL',8)
    monkeypatch.setattr(migration_limits,'COMPUTE_QUEUED_PRODUCER',8)
    monkeypatch.setattr(migration_limits,'COMPUTE_QUEUED_PROJECT',4)
    def submit(i):
        try:return commands.admit(x['credential'],request(x,str(i)))
        except commands.QueueError as exc:return exc.code
    with ThreadPoolExecutor(max_workers=12) as pool:results=list(pool.map(submit,range(32)))
    assert sum(isinstance(r,dict) for r in results)==4
    assert set(r for r in results if isinstance(r,str))=={'queue_full'}
    control=commands.admit(x['credential'],request(x,'control','synthetic.control'))
    assert control['lane']=='control'
    with db.connect() as conn:
        counters={r['key']:r['count'] for r in conn.execute('select * from command_capacity')}
    assert counters['compute:global']==4 and counters['compute:producer:operator']==4


def test_scope_revision_blob_and_unknown_fields_are_rejected_atomically(fixture):
    x=fixture;other=store.create_project('other')
    for values,error in [({'project_id':other['id']},'scope_mismatch'),({'expected_revision':9999},'revision_conflict')]:
        with pytest.raises(commands.QueueError,match=error):commands.admit(x['credential'],request(x,**values))
    with pytest.raises(Exception):commands.admit(x['credential'],request(x,payload={'text':'x','shell':'rm'}))
    with pytest.raises(commands.QueueError,match='blob_scope_denied'):
        commands.admit(x['credential'],request(x,payload={'blob_id':'foreign'}))
    with pytest.raises(commands.QueueError,match='unsupported_command'):
        commands.admit(x['credential'],request(x,kind='formalize'))
    with db.connect() as conn:
        assert conn.execute('select count(*) from commands').fetchone()[0]==0
        assert conn.execute('select count(*) from command_capacity').fetchone()[0]==0


def test_concurrent_claims_exclude_same_command_and_project(fixture):
    x=fixture
    for i in range(4):commands.admit(x['credential'],request(x,str(i)))
    epochs={str(i):commands.start_worker(str(i)) for i in range(8)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed=list(pool.map(lambda owner:commands.claim(owner,epochs[owner],'compute'),epochs))
    live=[c for c in claimed if c]
    assert len(live)==1
    assert commands.complete(live[0],{'ok':True})
    assert not commands.complete(live[0],{'ok':True})
    assert commands.claim('0',epochs['0'],'compute')


def test_recovery_fences_old_attempts_and_suspends_unknown_effect(fixture):
    x=fixture
    commands.admit(x['credential'],request(x))
    epoch=commands.start_worker('worker');old=commands.claim('worker',epoch,'compute')
    future=datetime.now(timezone.utc)+timedelta(seconds=31)
    assert commands.recover(now=future)['retried']==1
    assert not commands.complete(old,{'stale':True})
    new=commands.claim('worker',epoch,'compute')
    assert new['generation']>old['generation'] and new['attempt_id']!=old['attempt_id']
    assert commands.heartbeat(new) and commands.complete(new,{'ok':True})
    unknown=commands.admit(x['credential'],request(x,'uncertain','synthetic.uncertain'))
    commands.claim('worker',epoch,'compute')
    assert commands.recover(now=future)['suspended']==1
    assert commands.get(x['credential'],unknown['id'])['state']=='suspended'
    with db.connect() as conn:assert {r[0] for r in conn.execute('select count from command_capacity')}=={0}


def test_revocation_invalidates_claim_and_receipt_is_owned(fixture):
    x=fixture;item=commands.admit(x['credential'],request(x))
    other=commands.register_producer('other')
    with pytest.raises(commands.QueueError,match='command_not_found'):commands.get(other,item['id'])
    epoch=commands.start_worker('worker');token=commands.claim('worker',epoch,'compute')
    commands.revoke('operator')
    assert not commands.heartbeat(token) and not commands.complete(token,{})
    with pytest.raises(commands.QueueError,match='unauthorized'):commands.admit(x['credential'],request(x))


def test_idempotency_receipt_survives_revision_change_but_claim_rechecks(fixture):
    x=fixture
    with db.connect() as conn:rev=core.revision(conn,'p:'+x['project']['id'])
    value=request(x,expected_revision=rev);item=commands.admit(x['credential'],value)
    store.update_project(x['project']['id'],title='new identity')
    assert commands.admit(x['credential'],value)['id']==item['id']
    epoch=commands.start_worker('worker')
    assert commands.claim('worker',epoch,'compute') is None
    assert commands.get(x['credential'],item['id'])['error']=='revision_conflict'


def test_expired_command_and_stale_epoch_cannot_execute(fixture):
    x=fixture;item=commands.admit(x['credential'],request(x,'editor','synthetic.editor'))
    epoch=commands.start_worker('worker')
    assert commands.claim('worker','wrong-epoch','editor') is None
    commands.recover(now=datetime.now(timezone.utc)+timedelta(seconds=6))
    assert commands.get(x['credential'],item['id'])['state']=='expired'


def test_http_admission_is_bounded_authenticated_and_database_observed(fixture):
    x=fixture;app=FastAPI();app.include_router(routes.router)
    headers={'Authorization':'Bearer '+x['credential']}
    with TestClient(app) as client:
        assert client.post('/api/commands',json=request(x)).status_code==401
        response=client.post('/api/commands',json=request(x),headers=headers)
        assert response.status_code==202
        command_id=response.json()['id']
        assert client.get('/api/commands/'+command_id,headers=headers).json()['state']=='queued'
        assert client.post('/api/commands',content=b'x'*65537,headers=headers).status_code==413
        assert client.post('/api/commands',json=request(x,producer_id='forged'),headers=headers).status_code==422


def test_control_consumer_proceeds_while_both_compute_consumers_wait(fixture,monkeypatch):
    x=fixture;gate=threading.Event();started=threading.Event();count=0;lock=threading.Lock()
    def execute(token):
        nonlocal count
        if token['lane']=='compute':
            with lock:
                count+=1
                if count==2:started.set()
            gate.wait(5)
        return {'ok':True}
    monkeypatch.setattr(command_host,'execute',execute)
    # Different projects permit two concurrent synthetic compute reservations.
    second=store.create_project('second');other=commands.register_producer('second',project_id=second['id'])
    commands.admit(x['credential'],request(x))
    commands.admit(other,{'kind':'synthetic.echo','idempotency_key':'second','project_id':second['id']})
    command_host.start()
    try:
        assert started.wait(3)
        control=commands.admit(x['credential'],request(x,'control','synthetic.control'))
        until=time.monotonic()+3
        while time.monotonic()<until and commands.get(x['credential'],control['id'])['state']!='succeeded':time.sleep(.02)
        assert commands.get(x['credential'],control['id'])['state']=='succeeded'
        assert not gate.is_set()
    finally:gate.set();command_host.close()
