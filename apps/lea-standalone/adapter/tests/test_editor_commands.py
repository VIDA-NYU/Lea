"""Stage 7: SQL admission, supersession, ownership and language-only results."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import db,store,commands,editor_service as service,editor_worker,workspace_commands
from app.queries import core
from app.routes import editor,sessions


@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'db.sqlite');db.init_db()
    session=store.create_session('Editor')
    store.add_code_step(session['id'],None,'Main.lean',content='example : True := by\n  trivial\n',author='user')
    scope='s:'+session['id']
    with db.write() as conn:
        conn.execute('insert into core_observations values (?,?,?,?,?,?,?,?,?)',('observed',scope,core.revision(conn,scope),0,db.utc_now(),(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),'observed','test-environment','{}'))
        conn.execute("insert into core_projection_enrollment values (?,1,'observed','')",(scope,))
    owner=service.create_client(session['id']);info=service.file_info(session['id'],'Main.lean')
    editor_worker.start()
    epoch=commands.start_worker('test-editor')
    return {'session':session,'scope':scope,'owner':owner,'key':owner['credential'],'info':info,'epoch':epoch}


def publish(env,version=1,content=None,**kw):
    info=env['info']
    return service.publish(env['key'],service.Buffer(version=version,path=info['path'],base_revision=info['file_revision'],
        environment_revision=info['environment_revision'],content=content if content is not None else info['content'],**kw))


def submit(env,buffer,seq=1,kind='editor.analyze',key=None,**kw):
    value={'version':1,'kind':kind,'buffer_id':buffer['id'],'environment_revision':buffer['environment_revision'],
        'idempotency_key':key or 'request-'+str(seq),'sequence':seq,
        'deadline':(datetime.now(timezone.utc)+timedelta(seconds=4.5)).isoformat(),**kw}
    if kind!='editor.analyze':value.setdefault('position',{'line':1,'character':2})
    req=service.LanguageRequest(**value)
    return service.submit(env['key'],req),req


def execute(env,monkeypatch,value=None):
    token=commands.claim('test-editor',env['epoch'],'editor',kinds=service.KINDS)
    assert token
    monkeypatch.setattr(editor_worker,'analyze',lambda *a: {'value':value if value is not None else {'diagnostics':[]},'analysis_only':True})
    editor_worker.execute(token)
    return service.get(env['key'],token['id'])


def test_admission_queries_and_legacy_info_are_sql_only(env,monkeypatch):
    def denied(*a,**kw):pytest.fail('Editor HTTP entered workspace/executor')
    monkeypatch.setattr(Path,'read_text',denied);monkeypatch.setattr(Path,'read_bytes',denied)
    monkeypatch.setattr(editor_worker,'analyze',denied)
    b=publish(env);r,_=submit(env,b)
    app=FastAPI();app.include_router(editor.router);app.include_router(sessions.router)
    client=TestClient(app)
    for url in (f"/api/sessions/{env['session']['id']}/editor",f"/api/sessions/{env['session']['id']}/lsp-info",f"/api/editor/requests/{r['id']}"):
        assert client.get(url,headers={'Authorization':'Bearer '+env['key']}).status_code==200
    assert r['state']=='queued'


def test_exact_replay_and_unchanged_content_reuse(env):
    b=publish(env);assert publish(env)==b
    newer=publish(env,2);assert newer['sha256']==b['sha256']
    with db.connect() as conn:assert conn.execute('select count(*) from editor_contents').fetchone()[0]==1
    r,req=submit(env,newer)
    assert service.submit(env['key'],req)['id']==r['id']
    with pytest.raises(commands.QueueError,match='idempotency_conflict'):
        service.submit(env['key'],req.model_copy(update={'kind':'editor.hover','position':service.Position(line=0,character=1)}))


def test_concurrent_duplicate_has_one_capacity_reservation(env):
    b=publish(env);r,req=submit(env,b)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts=list(pool.map(lambda _:service.submit(env['key'],req),range(4)))
    assert {v['id'] for v in receipts}=={r['id']}
    with db.connect() as conn:assert conn.execute("select count from command_capacity where key='editor:global'").fetchone()[0]==1


def test_new_buffer_cancels_old_running_request_and_cannot_publish(env,monkeypatch):
    b=publish(env);r,_=submit(env,b)
    token=commands.claim('test-editor',env['epoch'],'editor',kinds=service.KINDS)
    newer=publish(env,2,env['info']['content']+'-- new')
    assert service.get(env['key'],r['id'])['result_state']=='superseded'
    assert not commands.complete(token,{'diagnostics':[{'message':'late'}]})
    assert submit(env,newer,2)[0]['state']=='queued'
    with db.connect() as conn:assert conn.execute("select count from command_capacity where key='editor:global'").fetchone()[0]==1


def test_pending_position_requests_coalesce_and_older_sequences_are_rejected(env):
    b=publish(env);first,_=submit(env,b,kind='editor.goals')
    latest,_=submit(env,b,2,kind='editor.goals',position={'line':1,'character':3})
    assert service.get(env['key'],first['id'])['state']=='cancelled'
    with pytest.raises(commands.QueueError,match='request_superseded'):submit(env,b,1,kind='editor.goals',key='old')
    assert latest['state']=='queued'


def test_owner_is_scope_and_credential_bound(env):
    b=publish(env);r,_=submit(env,b)
    foreign=service.create_client(env['session']['id'])
    with pytest.raises(commands.QueueError,match='editor_buffer_not_found'):
        service.submit(foreign['credential'],submit(env,b,2)[1])
    with pytest.raises(commands.QueueError,match='editor_request_not_found'):service.get(foreign['credential'],r['id'])
    with pytest.raises(commands.QueueError,match='unauthorized'):service.get('invalid',r['id'])


def test_request_result_is_versioned_analysis_never_proof_evidence(env,monkeypatch):
    b=publish(env);r,_=submit(env,b,kind='editor.goals')
    result=execute(env,monkeypatch,{'rendered':'⊢ True','goals':['⊢ True']})
    assert result['current'] and result['result']['value']['goals']==['⊢ True']
    with db.connect() as conn:
        assert conn.execute('select count(*) from proof_check_evidence').fetchone()[0]==0
        assert conn.execute('select count(*) from editor_requests').fetchone()[0]==1
        assert conn.execute("select count(*) from domain_changes where entity_type='editor_requests'").fetchone()[0]>=2
    with db.write() as conn:conn.execute("update core_observations set environment_revision='changed'")
    result=service.get(env['key'],r['id']);assert not result['current'] and result['result'] is None
    assert result['result_state']=='environment_changed'


def test_worker_crash_retries_under_new_epoch_and_old_worker_is_fenced(env,monkeypatch):
    b=publish(env);r,_=submit(env,b)
    token=commands.claim('test-editor',env['epoch'],'editor',kinds=service.KINDS)
    with db.write() as conn:
        conn.execute("update commands set lease_until='2000-01-01' where id=?",(token['id'],))
        conn.execute("update command_workers set lease_until='2000-01-01'")
    assert commands.recover()['retried']==1
    env['epoch']=commands.start_worker('test-editor')
    assert not commands.complete(token,{'wrong':'epoch'})
    assert execute(env,monkeypatch)['current']


def test_quotas_reject_new_work_but_supersession_releases_capacity(env,monkeypatch):
    b=publish(env)
    monkeypatch.setattr(service.limits,'EDITOR_QUEUED_PRODUCER',2)
    a,_=submit(env,b,1,supersession_key='a');submit(env,b,2,supersession_key='b')
    with pytest.raises(commands.QueueError,match='queue_full'):submit(env,b,3,supersession_key='c')
    assert submit(env,b,4,supersession_key='a')[0]['state']=='queued'
    assert service.get(env['key'],a['id'])['state']=='cancelled'


def test_deadline_and_position_validation(env):
    b=publish(env)
    with pytest.raises(commands.QueueError,match='invalid_deadline'):
        submit(env,b,deadline=(datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat())
    with pytest.raises(commands.QueueError,match='position_outside_buffer'):
        submit(env,b,kind='editor.hover',position={'line':90,'character':0})
    with pytest.raises(commands.QueueError,match='unsupported_editor_request'):submit(env,b,kind='$/lean/rpc/call')


def test_saved_bytes_and_scope_changes_invalidate_without_fs(env,monkeypatch):
    b=publish(env);r,_=submit(env,b)
    store.add_code_step(env['session']['id'],None,'Main.lean',content='example : False := by sorry',author='user')
    assert service.get(env['key'],r['id'])['result_state']=='saved_file_changed'
    with pytest.raises(commands.QueueError,match='buffer_base_changed'):publish(env,2)
    project=store.create_project('Other')
    with db.write() as conn:conn.execute('update sessions set project_id=? where id=?',(project['id'],env['session']['id']))
    with pytest.raises(commands.QueueError,match='editor_scope_changed'):service.get(env['key'],r['id'])


def test_expiry_and_bounded_retention_remove_private_buffer_bytes(env,monkeypatch):
    b=publish(env,content='-- private ephemeral draft');r,_=submit(env,b)
    execute(env,monkeypatch)
    with db.write() as conn:
        conn.execute("update editor_requests set expires_at='2000-01-01'")
        conn.execute("update editor_buffers set expires_at='2000-01-01'")
    assert service.get(env['key'],r['id'])['result_state']=='result_expired'
    assert service.retain()>=2
    with db.connect() as conn:
        assert not conn.execute('select 1 from editor_contents').fetchone()
        assert conn.execute("select count(*) from timeline where kind='code'").fetchone()[0]==1


def test_large_output_and_lost_worker_never_get_promoted(env,monkeypatch):
    b=publish(env);r,_=submit(env,b)
    result=execute(env,monkeypatch,{'rendered':'X'*(service.RESULT_BYTES+1)})
    assert not result['current'] and result['result'] is None and result['error']=='editor_result_too_large'


def test_direct_websocket_rejects_local_and_foreign_clients_without_process(env,monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    import subprocess
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('retired endpoint spawned a process'))
    app=FastAPI();app.include_router(sessions.router);client=TestClient(app)
    for origin in ('http://localhost','https://evil.example.com'):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/api/sessions/{env['session']['id']}/lsp",headers={'Origin':origin}):pass
        assert exc.value.code==1008


@pytest.mark.skipif(__import__('os').environ.get('LEA_TEST_NATIVE_EDITOR')!='1',reason='Explicit native confined Lean qualification')
def test_native_worker_language_protocol_and_warm_queue_latency(env,monkeypatch,tmp_path):
    import os,subprocess,time,hashlib,platform,sqlite3
    from app.config import LeaConfig
    from app import command_host
    workspace=tmp_path/'workspace';repo=workspace/'proofs'/env['session']['id'];repo.mkdir(parents=True)
    (workspace/'lean-toolchain').write_text('leanprover/lean4:v4.29.0\n')
    (workspace/'lakefile.toml').write_text('name = "editor_fixture"\nversion = "0.1.0"\n')
    path=repo/'Main.lean';path.write_text(env['info']['content'])
    warmed=subprocess.run(['lake','env','lean','--version'],cwd=workspace,capture_output=True,text=True,timeout=30)
    assert warmed.returncode==0,warmed.stderr
    monkeypatch.setattr('app.config.load_config',lambda:LeaConfig(model='test',lea_root=tmp_path,max_turns=1))
    editor_worker.start();command_host.start()
    buffer=publish(env);sequence=0
    app=FastAPI();app.include_router(editor.router);client=TestClient(app)
    timings=[]
    def language(kind,position=None):
        nonlocal sequence
        sequence+=1;start=time.monotonic()
        request={'version':1,'kind':kind,'buffer_id':buffer['id'],'environment_revision':buffer['environment_revision'],
            'sequence':sequence,'idempotency_key':'native-'+str(sequence),
            'deadline':(datetime.now(timezone.utc)+timedelta(seconds=4.9)).isoformat(),**({'position':position} if position else {})}
        headers={'Authorization':'Bearer '+env['key']}
        response=client.post('/api/editor/requests',json=request,headers=headers)
        assert response.status_code==202,response.text
        value=response.json()
        while value['state'] in {'queued','claimed'} and time.monotonic()-start<6:
            time.sleep(.025);value=client.get('/api/editor/requests/'+value['id'],headers=headers).json()
        assert value.get('current'),value
        return value['result']['value'],(time.monotonic()-start)*1000
    first,cold=language('editor.analyze');assert first['diagnostics']==[]
    goals,_=language('editor.goals',{'line':1,'character':2});assert 'True' in goals['rendered']
    hover,_=language('editor.hover',{'line':0,'character':11});assert hover and hover['contents']
    completion,_=language('editor.complete',{'line':1,'character':4});assert isinstance(completion['items'],list)
    term,_=language('editor.term_goal',{'line':1,'character':2});assert term is None or isinstance(term,dict)
    for sample in range(20):
        started=time.monotonic()
        info=client.get(f"/api/sessions/{env['session']['id']}/editor",params={'path':'Main.lean'}).json()
        published=client.post('/api/editor/buffers',headers={'Authorization':'Bearer '+env['key']},json={
            'version':sample+2,'path':'Main.lean','content':env['info']['content']+f'-- warm edit {sample}\n',
            'base_revision':info['file_revision'],'environment_revision':info['environment_revision']})
        assert published.status_code==201,published.text
        buffer=published.json()
        value,_=language('editor.analyze');assert value['diagnostics']==[];timings.append((time.monotonic()-started)*1000)
    buffer=publish(env,22,'example : True := by\n  exact false\n')
    errors,_=language('editor.analyze');assert any(d['severity']==1 for d in errors['diagnostics'])
    assert path.read_text()==env['info']['content']
    with db.connect() as conn:assert conn.execute('select count(*) from proof_check_evidence').fetchone()[0]==0
    command_host.close()
    # SQL results remain observable with no language worker. Restart creates a
    # fresh private server, never reconnects a browser execution channel.
    previous=service.get(env['key'],idempotency_key='native-'+str(sequence))
    assert previous['current']
    command_host.start()
    goals,_=language('editor.goals',{'line':1,'character':2})
    assert isinstance(goals,dict)
    command_host.close()
    report={'version':1,'captured_at':datetime.now(timezone.utc).isoformat(),'environment':{'system':platform.system(),'machine':platform.machine(),'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,'lean':warmed.stdout.strip()},
        'transport':'ASGI HTTP SQL metadata read, changed immutable buffer publication, real editor queue, SQL result polling','cold_analysis_ms':round(cold,2),
        'warm_diagnostics':{'samples':len(timings),'p50_ms':round(sorted(timings)[len(timings)//2],2),'p95_ms':round(sorted(timings)[18],2),'max_ms':round(max(timings),2)},
        'scope':'20 distinct small-file edits after debounce; no Mathlib, paid model, network provider, workspace mutation or proof certification'}
    root=Path(__file__).resolve().parents[4]
    paths=['apps/lea-standalone/adapter/'+p for p in ['app/editor_service.py','app/editor_worker.py','app/command_host.py','app/commands.py','app/routes/editor.py','app/routes/sessions.py','app/main.py','app/observation_maintenance.py','migrations/versions/0047_queued_editor.py']]
    paths += ['apps/lea-standalone/prover/lea/'+p for p in ['lsp_daemon.py','check_evidence.py','check_runtime.py','execution.py']]
    paths += ['apps/lea-standalone/src/app/'+p for p in ['lib/editorClient.mjs','lib/queuedMonaco.ts','lib/leanQuery.ts','components/LiveEditor.tsx','components/Canvas.tsx']]
    paths += ['apps/lea-standalone/adapter/tests/test_editor_commands.py','tests/browser/editor.spec.mjs']
    for relative in paths:report.setdefault('source_sha256',{})[relative]=hashlib.sha256((root/relative).read_bytes()).hexdigest()
    if os.environ.get('LEA_EDITOR_BASELINE_OUTPUT'):Path(os.environ['LEA_EDITOR_BASELINE_OUTPUT']).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


def test_shared_loose_conversation_uses_the_canonical_file_scope(env,monkeypatch,tmp_path):
    from app.config import LeaConfig
    form=store.create_formalization(project_id=None,loose_session_id=env['session']['id'],display_title='Shared')
    store.link_session_formalization(env['session']['id'],form['id'])
    other=store.create_session('Another conversation');store.link_session_formalization(other['id'],form['id'])
    with db.write() as conn:conn.execute('update core_observations set input_revision=?',(core.revision(conn,env['scope']),))
    owner=service.create_client(other['id'])
    info=service.file_info(other['id'],'Main.lean')
    assert info['content']==env['info']['content']
    buffer=service.publish(owner['credential'],service.Buffer(version=1,path='Main.lean',content=info['content'],base_revision=info['file_revision'],environment_revision=info['environment_revision']))
    workspace=tmp_path/'workspace';repo=workspace/'proofs'/env['session']['id'];repo.mkdir(parents=True)
    (workspace/'lakefile.toml').write_text('name="fixture"\n');(repo/'Main.lean').write_text(info['content'])
    monkeypatch.setattr('app.config.load_config',lambda:LeaConfig(model='test',lea_root=tmp_path,max_turns=1))
    with db.connect() as conn:row=dict(conn.execute('select * from editor_buffers where id=?',(buffer['id'],)).fetchone())
    assert editor_worker._target(row)[0]==repo/'Main.lean'


def test_private_language_notifications_are_bounded_and_widgets_are_not_relayed(monkeypatch,tmp_path):
    from lea.lsp_daemon import LeanDaemon
    from lea.check_runtime import CheckBudget
    from queue import Queue
    monkeypatch.setattr(LeanDaemon,'start',lambda *a:True)
    session=editor_worker.Session(tmp_path,CheckBudget(1))
    q=Queue();session.daemon._uri_queues['file:///Main.lean']=q
    for number in range(100):
        session.daemon._dispatch({'method':'textDocument/publishDiagnostics','params':{'uri':'file:///Main.lean','version':number,'diagnostics':[]}})
        session.daemon._dispatch({'method':'$/lean/fileProgress','params':{'textDocument':{'uri':'file:///Main.lean'},'processing':[]}})
    assert q.qsize()==2
    assert [q.get()['params']['version'],q.get()['params']['version']]==[98,99]
    session.close()


def test_worker_availability_and_restart_identity_are_sql_observations(env):
    assert service.file_info(env['session']['id'])['worker']['state']=='unavailable'
    epoch=commands.start_worker('lea-command-host')
    first=service.file_info(env['session']['id'])['worker']
    assert first=={'state':'available','epoch':epoch}
    with db.write() as conn:conn.execute("update command_workers set lease_until='2000-01-01' where id='lea-command-host'")
    assert service.file_info(env['session']['id'])['worker']['state']=='unavailable'
    assert commands.start_worker('lea-command-host')!=epoch
    assert service.file_info(env['session']['id'])['worker']['epoch']!=epoch


def test_sql_drain_fences_analysis_and_cancel_allows_fresh_admission(env):
    from app import run_commands
    buffer=publish(env);first,_=submit(env,buffer)
    with db.write() as conn:
        control=run_commands._enqueue(conn,'runtime.control',{'force':False,'cancel':False,'shutdown':False},key='test-drain')
    assert service.file_info(env['session']['id'])['worker']['state']=='draining'
    assert service.get(env['key'],first['id'])['result_state']=='worker_draining'
    with pytest.raises(commands.QueueError,match='adapter_draining'):submit(env,buffer,2)
    with db.write() as conn:
        commands._settle(conn,conn.execute('select * from commands where id=?',(control['id'],)).fetchone(),'cancelled')
    assert submit(env,buffer,2)[0]['state']=='queued'


def test_result_storage_has_an_aggregate_owner_limit(env,monkeypatch):
    buffer=publish(env);monkeypatch.setattr(service,'RESULT_OWNER_BYTES',240)
    submit(env,buffer,kind='editor.goals')
    first=execute(env,monkeypatch,{'rendered':'X'*100});assert first['current']
    submit(env,buffer,2,kind='editor.hover')
    second=execute(env,monkeypatch,{'rendered':'Y'*100})
    assert not second['current'] and second['error']=='editor_result_quota'
    assert service.get(env['key'],first['id'])['current']


def test_generic_command_endpoint_cannot_bypass_typed_editor_admission(env):
    with pytest.raises(commands.QueueError,match='typed_editor_endpoint_required'):
        commands.admit(env['key'],{'kind':'editor.goals','idempotency_key':'bypass','session_id':env['session']['id'],
            'payload':{'text':'$/lean/rpc/call'}})
    with db.connect() as conn:assert conn.execute('select count(*) from commands').fetchone()[0]==0


def test_closing_a_view_retires_its_producer_and_releases_active_capacity(env,monkeypatch):
    buffer=publish(env);request,_=submit(env,buffer)
    monkeypatch.setattr(service,'MAX_CLIENTS',1)
    with pytest.raises(commands.QueueError,match='editor_clients_full'):service.create_client(env['session']['id'])
    assert service.close_client(env['key'])['closed']
    with db.connect() as conn:
        row=conn.execute('select state,error from commands where id=?',(request['id'],)).fetchone()
        assert row['state']=='suspended' and row['error']=='producer_revoked'
        assert conn.execute("select count from command_capacity where key='editor:global'").fetchone()[0]==0
    assert service.create_client(env['session']['id'])['owner_id']!=env['owner']['owner_id']
