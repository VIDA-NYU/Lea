"""Stage 8: real independent API/worker processes and denied UI execution."""
import ast,json,os,signal,socket,subprocess,sys,time
from pathlib import Path
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen
import pytest
from app import db,commands,process_state,store
ADAPTER=Path(__file__).resolve().parents[1]


def port():
    try:
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]
    except PermissionError:pytest.skip('Loopback sockets require the process-test sandbox grant')


def request(base,path,body=None,method=None,headers=None):
    req=Request(base+path,data=json.dumps(body).encode() if body is not None else None,
        method=method or ('POST' if body is not None else 'GET'),headers={'Content-Type':'application/json',**(headers or {})})
    with urlopen(req,timeout=3) as response:return json.load(response)


def wait(fn,timeout=25):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        try:
            value=fn()
            if value:return value
        except (OSError,URLError):pass
        time.sleep(.05)
    raise AssertionError('Process boundary condition timed out')


def environment(tmp_path,monkeypatch):
    data=tmp_path/'data';monkeypatch.setattr(db,'DB_PATH',data/'lea-interface.sqlite3');db.init_db()
    prover=tmp_path/'prover';(prover/'workspace').mkdir(parents=True)
    config=tmp_path/'config.toml';config.write_text(f'model="openai/gpt-test"\nlea_root={json.dumps(str(prover))}\n')
    return {**os.environ,'LEA_SHARED_DATA_DIR':str(data),'LEA_CONFIG_FILE':str(config),
        'LEA_COMPANION_IMPORT_DIR':str(tmp_path/'legacy'),'LEA_API_DENY_PATHS':os.pathsep.join([str(prover),str(config)]),
        'LEA_TEST_RELEASE':str(tmp_path/'release'),'LEA_MODEL_CATALOG_OFFLINE':'true','LITELLM_LOCAL_MODEL_COST_MAP':'True'}


def start(tmp_path,label,args,env):
    log=(tmp_path/(label+'.log')).open('w')
    child=subprocess.Popen([sys.executable,'-u',*map(str,args)],cwd=ADAPTER,env=env,stdout=log,stderr=log)
    log.close();return child


def stop(child):
    if child.poll() is None:
        child.terminate()
        try:child.wait(timeout=8)
        except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)


def test_api_imports_queries_admission_only_and_denies_execution(tmp_path,monkeypatch):
    env=environment(tmp_path,monkeypatch)
    static=tmp_path/'static';static.mkdir();(static/'index.html').write_text('<html>SQL UI</html>')
    env['LEA_WEB_DIST']=str(static)
    project=store.create_project('boundary')
    session=store.create_session('boundary',project_id=project['id'])
    form=store.create_formalization(project_id=project['id'],loose_session_id=None,display_title='Boundary')
    with db.write() as conn:
        conn.execute('''insert into reformalization_operations(id,project_id,formalization_id,session_id,
            request_hash,request_json,state,created_at,updated_at) values('retained',?,?,?,'hash','{}','paused',?,?)''',
            (project['id'],form['id'],session['id'],db.utc_now(),db.utc_now()))
    script=r'''
import os,sys,asyncio,importlib,json
from pathlib import Path
from app.main import app,startup
startup()
from app import workspace_commands,peripheral_views,db
assert not workspace_commands._actions and not peripheral_views._queries
for name in ('lea','lea.interface','app.routes.sessions','app.worker','app.command_host','app.bridge','app.runtime','app.settings','app.project_library','app.editor_worker','app.companion_worker','litellm'):
    try:importlib.import_module(name)
    except ImportError:pass
    else:raise AssertionError('Forbidden API import: '+name)
for path in (os.environ['LEA_CONFIG_FILE'],os.environ['LEA_API_DENY_PATHS'].split(os.pathsep)[0]+'/workspace/secret.lean'):
    try:open(path)
    except PermissionError:pass
    else:raise AssertionError('Workspace/config file opened')
import subprocess,socket
for fn in (lambda:subprocess.run(['true']),lambda:socket.create_connection(('127.0.0.1',9)),lambda:socket.getaddrinfo('example.org',443)):
    try:fn()
    except PermissionError:pass
    else:raise AssertionError('API executed an outgoing effect')
import httpx
async def probe():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as c:
        for route,operations in app.openapi()['paths'].items():
            if 'get' not in operations or not route.startswith('/api/'):continue
            if route.endswith('/events') or route=='/api/changes':continue
            import re
            path=re.sub(r'\{[^}]+\}','missing',route)
            response=await c.get(path)
            assert response.status_code<500 or response.status_code==503,(path,response.status_code,response.text)
        assert (await c.get('/api/health')).json()['capabilities']['database_boundary']['processes']=='separate'
        assert (await c.get('/')).status_code==200
        assert (await c.get('/api/reformalizations/retained')).json()['drafts']==[]
        assert (await c.post('/api/skills',json={'name':'sql-skill'})).status_code==201
        assert (await c.post('/api/custom-tools',json={'name':'sql-tool','url':'https://example.org/search'})).status_code==201
        assert (await c.post('/api/projects',json={'title':'queued project'})).status_code==503 # offline intake fails closed
asyncio.run(probe())
assert not any(name=='lea' or name.startswith('lea.') for name in sys.modules)
print('API import, filesystem, process and outgoing-network denials verified')
'''
    result=subprocess.run([sys.executable,'-c',script],cwd=ADAPTER,env={**env,'LEA_PROCESS_ROLE':'api'},capture_output=True,text=True,timeout=25)
    assert result.returncode==0,result.stdout+result.stderr


def test_public_contracts_match_private_route_schemas():
    import importlib,inspect
    from pydantic import BaseModel
    root=ADAPTER/'app'
    for source in (root/'api_routes').glob('*.py'):
        if source.stem=='__init__':continue
        public=importlib.import_module('app.api_routes.'+source.stem)
        private=importlib.import_module('app.routes.'+source.stem)
        pub={(r.path,tuple(sorted(r.methods or ()))) for r in public.router.routes if hasattr(r,'methods')}
        old={(r.path,tuple(sorted(r.methods or ()))) for r in private.router.routes if hasattr(r,'methods')}
        assert pub==old,source.name
        def actions(path,decorator):
            return {node.name:call.args[0].value for node in ast.parse(path.read_text()).body
                if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) for call in node.decorator_list
                if isinstance(call,ast.Call) and isinstance(call.func,ast.Attribute)
                and isinstance(call.func.value,ast.Name) and call.func.value.id=='workspace_commands'
                and call.func.attr==decorator}
        assert actions(source,'intake')==actions(root/'routes'/source.name,'register'),source.name
        for name,cls in vars(public).items():
            if isinstance(cls,type) and issubclass(cls,BaseModel) and cls.__module__==public.__name__:
                assert cls.model_json_schema()==getattr(private,name).model_json_schema(),(source.name,name)


def test_independent_restart_crash_fencing_and_durable_receipts(tmp_path,monkeypatch):
    api_port=port();base=f'http://127.0.0.1:{api_port}'
    env=environment(tmp_path,monkeypatch);env['LEA_API_BASE_URL']=base
    children=[]
    api_args=[ADAPTER/'run_api.py','--port',api_port,'--log-level','error']
    worker_args=[ADAPTER/'tests/process_boundary_worker.py']
    try:
        api=start(tmp_path,'api',api_args,env);children.append(api)
        before=wait(lambda:request(base,'/api/health'))
        assert not before['ready'] and api.poll() is None
        # Migration also takes workspace ownership, so prepare the second DB
        # before the first worker starts.
        second_env={**env,'LEA_SHARED_DATA_DIR':str(tmp_path/'second-data')}
        subprocess.run([sys.executable,'bootstrap.py'],cwd=ADAPTER,env=second_env,check=True,capture_output=True,timeout=15)
        worker=start(tmp_path,'worker',worker_args,{**env,'LEA_TEST_OWNED_CHILD':str(tmp_path/'owned-child.json')});children.append(worker)
        wait(lambda:request(base,'/api/health')['ready'])
        wait(lambda:(tmp_path/'owned-child.json').exists())
        orphan=json.loads((tmp_path/'owned-child.json').read_text())
        first=request(base,'/api/health')
        duplicate=start(tmp_path,'duplicate',worker_args,env);children.append(duplicate)
        assert duplicate.wait(timeout=15)!=0
        assert 'Another workspace worker owns data directory' in (tmp_path/'duplicate.log').read_text()
        # Migration is a separate supervisor phase, refused under an active owner.
        bootstrap=subprocess.run([sys.executable,'bootstrap.py'],cwd=ADAPTER,env=env,capture_output=True,text=True,timeout=10)
        assert bootstrap.returncode!=0 and 'owns data directory' in bootstrap.stderr
        other_bootstrap=subprocess.run([sys.executable,'bootstrap.py'],cwd=ADAPTER,env=second_env,capture_output=True,text=True,timeout=10)
        assert other_bootstrap.returncode!=0 and 'owns this physical workspace' in other_bootstrap.stderr
        # A different DB cannot create a second writer to the same physical tree.
        other=start(tmp_path,'workspace-duplicate',worker_args,second_env);children.append(other)
        assert other.wait(timeout=15)!=0
        assert 'owns this physical workspace' in (tmp_path/'workspace-duplicate.log').read_text()
        run=request(base,'/api/runs',{'message':'Hold the scripted model until the stop command.','idempotency_key':'lost-ack'})
        rid=run['run_id']
        # The claimed row becomes running just before start_attempt publishes
        # its identity. Compare durable attempts after that publication.
        attempt=wait(lambda:request(base,'/api/runs/'+rid).get('attempt_id'))
        stop(api)
        assert worker.poll() is None
        api=start(tmp_path,'api-restarted',api_args,env);children.append(api)
        wait(lambda:request(base,'/api/health').get('ready'))
        replay=request(base,'/api/runs',{'message':'Hold the scripted model until the stop command.','idempotency_key':'lost-ack'})
        assert replay['run_id']==rid
        assert request(base,'/api/runs/'+rid)['attempt_id']==attempt
        request(base,'/api/runs/'+rid+'/interrupt',{})
        wait(lambda:request(base,'/api/runs/'+rid)['status']=='cancelled')
        assert request(base,'/api/health')['runtime_id']==first['runtime_id']
        # Crash the worker while a real owned process remains. The next lifetime
        # must reap its ownership manifest before it publishes ready/new epoch.
        credential=commands.register_producer('boundary-test')
        from datetime import datetime,timedelta,timezone
        admitted=commands.admit(credential,{'kind':'synthetic.uncertain','version':1,'idempotency_key':'uncertain',
            'payload':{'text':'unknown effect'},'deadline':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()})
        wait(lambda:commands.get(credential,admitted['id'])['state']=='claimed')
        with db.connect() as conn:old=dict(conn.execute('select * from commands where id=?',(admitted['id'],)).fetchone())
        worker.kill();worker.wait(timeout=5)
        assert request(base,'/api/sessions')['sessions']
        wait(lambda:not request(base,'/api/health')['ready'],timeout=20)
        worker=start(tmp_path,'worker-restarted',worker_args,env);children.append(worker)
        wait(lambda:request(base,'/api/health')['ready'])
        after=request(base,'/api/health')
        assert not Path(orphan['lease']).exists(), 'Worker published ready before reaping its old owned child'
        assert after['runtime_id']!=first['runtime_id'] and after['generation']>first['generation']
        assert not commands.complete(old,{'stale':True})
        assert request(base,'/api/runs/'+rid)['status']=='cancelled'
        # Queued runtime control targets the worker; API survives worker re-exec.
        request(base,'/api/runtime/restart',{})
        wait(lambda:request(base,'/api/health').get('runtime_id')!=after['runtime_id'] and request(base,'/api/health')['ready'])
        assert api.poll() is None
        request(base,'/api/runtime/restart',{'shutdown':True})
        worker.wait(timeout=15)
        assert worker.returncode==0 and request(base,'/api/health')['ok'] and api.poll() is None
    finally:
        for child in reversed(children):stop(child)
        # Clean up only verifiable fixture-owned groups if an assertion aborts
        # before the restart test reaches the production reaper.
        from lea import process_ownership
        monkeypatch.setenv('LEA_OWNED_PROCESS_DIR',str(tmp_path/'prover/workspace/.lea-owned-processes'))
        process_ownership.reap_previous()


def test_worker_contract_mismatch_closes_admission_but_keeps_sql_health(tmp_path,monkeypatch):
    environment(tmp_path,monkeypatch)
    from app import peripheral_views
    with db.connect() as conn:before=peripheral_views.input_revision(conn)
    process_state.record_start('first',os.getpid())
    process_state.publish('first',{'lifecycle_state':'ready'},{'database_boundary':process_state.BOUNDARY,'commands':{'admission_enabled':True}})
    assert process_state.accepting_work()
    with db.connect() as conn:assert peripheral_views.input_revision(conn)==before
    process_state.publish('first',{'lifecycle_state':'ready'},{'database_boundary':{**process_state.BOUNDARY,'editor':2},'commands':{'admission_enabled':True}})
    assert not process_state.accepting_work()
    value=process_state.health()
    assert value['ok'] and value['lifecycle_state']=='worker_incompatible'
    assert not value['capabilities']['commands']['admission_enabled']


def test_worker_rejects_a_future_command_version_before_execution(tmp_path,monkeypatch):
    environment(tmp_path,monkeypatch)
    credential=commands.register_producer('future-version')
    accepted=commands.admit(credential,{'kind':'synthetic.echo','version':1,'idempotency_key':'future','payload':{'text':'retained'}})
    with db.write() as conn:conn.execute('update commands set version=2 where id=?',(accepted['id'],))
    epoch=commands.start_worker('version-owner')
    assert commands.claim('version-owner',epoch,'compute') is None
    receipt=commands.get(credential,accepted['id'])
    assert receipt['state']=='failed' and receipt['error']=='unsupported_command_version'
    assert receipt['attempt_id'] is None


def test_unregistered_held_child_lease_refuses_takeover(tmp_path,monkeypatch):
    from lea import process_ownership
    monkeypatch.setenv('LEA_OWNED_PROCESS_DIR',str(tmp_path/'owned'))
    lease=process_ownership.prepare()
    try:
        with pytest.raises(RuntimeError,match='Could not verify cleanup'):
            process_ownership.reap_previous()
        assert lease.path.exists()
    finally:process_ownership.release_parent(lease,spawned=False)
    assert process_ownership.reap_previous()==[]


def test_existing_database_cannot_silently_retarget_its_workspace(tmp_path,monkeypatch):
    environment(tmp_path,monkeypatch)
    from app import runtime
    monkeypatch.setattr(runtime,'_workspace_lock_handle',None)
    monkeypatch.setattr(runtime,'_workspace_owned',None)
    runtime.acquire_workspace_lock(tmp_path/'original')
    try:
        with pytest.raises(RuntimeError,match='bound to a different physical workspace'):
            runtime.acquire_workspace_lock(tmp_path/'replacement')
        assert not (tmp_path/'replacement').exists()
    finally:runtime._workspace_lock_handle.close()


def test_old_runtime_control_cannot_restart_the_replacement_worker(tmp_path,monkeypatch):
    environment(tmp_path,monkeypatch)
    from app import runtime,command_execution
    monkeypatch.setattr(runtime,'begin_drain',lambda **_:pytest.fail('Old control restarted a replacement lifetime'))
    for identity in ('previous-worker',None):
        result=command_execution.control({'kind':'runtime.control','payload_json':json.dumps({
            'force':False,'cancel':False,'shutdown':False,'target_runtime_id':identity})})
        assert result['already_transitioned'] and result['runtime_id']==runtime.IDENTITY


def test_shared_ownership_volume_fences_different_container_roots_and_databases(tmp_path,monkeypatch):
    from app import runtime
    environment(tmp_path,monkeypatch)
    ownership=tmp_path/'shared-ownership';monkeypatch.setenv('LEA_WORKSPACE_OWNERSHIP_DIR',str(ownership))
    monkeypatch.setattr(runtime,'_workspace_lock_handle',None);monkeypatch.setattr(runtime,'_workspace_owned',None)
    runtime.acquire_workspace_lock(tmp_path/'container-one/workspace')
    handle=runtime._workspace_lock_handle
    try:
        monkeypatch.setattr(db,'DB_PATH',tmp_path/'second-db/db.sqlite')
        with pytest.raises(RuntimeError,match='owns this physical workspace'):
            runtime.acquire_workspace_lock(tmp_path/'container-two/workspace')
        assert runtime.owned_process_dir()==ownership/'.lea-owned-processes'
    finally:handle.close()


def test_same_database_cannot_retarget_its_ownership_mount(tmp_path,monkeypatch):
    from app import runtime
    environment(tmp_path,monkeypatch)
    monkeypatch.setattr(runtime,'_workspace_lock_handle',None);monkeypatch.setattr(runtime,'_workspace_owned',None)
    monkeypatch.setenv('LEA_WORKSPACE_OWNERSHIP_DIR',str(tmp_path/'first-volume'))
    workspace=tmp_path/'container/workspace';runtime.acquire_workspace_lock(workspace)
    runtime._workspace_lock_handle.close()
    monkeypatch.setenv('LEA_WORKSPACE_OWNERSHIP_DIR',str(tmp_path/'replacement-volume'))
    with pytest.raises(RuntimeError,match='different workspace ownership volume'):
        runtime.acquire_workspace_lock(workspace)
    assert not (tmp_path/'replacement-volume/.lea-workspace-id').exists()


def test_existing_path_binding_adopts_a_volume_identity_under_its_lock(tmp_path,monkeypatch):
    from app import runtime
    environment(tmp_path,monkeypatch)
    monkeypatch.setattr(runtime,'_workspace_lock_handle',None);monkeypatch.setattr(runtime,'_workspace_owned',None)
    workspace=tmp_path/'workspace'
    binding=db.DB_PATH.parent/'.lea-workspace.json'
    binding.write_text(json.dumps({'version':1,'workspace':str(workspace)}))
    runtime.acquire_workspace_lock(workspace)
    try:
        saved=json.loads(binding.read_text());assert saved['version']==2
        assert saved['ownership_id']==(workspace/'.lea-workspace-id').read_text()
    finally:runtime._workspace_lock_handle.close()


def test_non_enforcing_lock_filesystem_fails_before_binding(tmp_path,monkeypatch):
    from app import runtime
    environment(tmp_path,monkeypatch)
    monkeypatch.setenv('LEA_REQUIRE_SHARED_OWNERSHIP','0')
    monkeypatch.setattr(runtime.fcntl,'flock',lambda *args:None)
    with pytest.raises(RuntimeError,match='does not enforce exclusive locks'):
        runtime.acquire_workspace_lock(tmp_path/'unsupported')
    assert not (db.DB_PATH.parent/'.lea-workspace.json').exists()
    assert not (tmp_path/'unsupported/.lea-workspace-id').exists()


def test_production_profile_requires_a_shared_ownership_mount(tmp_path,monkeypatch):
    from app import runtime
    environment(tmp_path,monkeypatch)
    monkeypatch.setenv('LEA_REQUIRE_SHARED_OWNERSHIP','1')
    monkeypatch.setenv('LEA_WORKSPACE_OWNERSHIP_DIR',str(tmp_path/'unmounted'))
    with pytest.raises(RuntimeError,match='require a shared Docker local ownership volume'):
        runtime.acquire_workspace_lock(tmp_path/'workspace')


@pytest.mark.skipif(os.environ.get('LEA_TEST_NATIVE_EDITOR')!='1',reason='Explicit native confined Lean qualification')
def test_native_editor_through_separate_api_and_worker(tmp_path,monkeypatch):
    from datetime import datetime,timedelta,timezone
    env=environment(tmp_path,monkeypatch)
    api_port=port();base=f'http://127.0.0.1:{api_port}'
    env['LEA_API_BASE_URL']=base
    session=store.create_session('Native process boundary')
    content='example : True := by\n  trivial\n'
    store.add_code_step(session['id'],None,'Main.lean',content=content,author='user')
    workspace=tmp_path/'prover/workspace';repo=workspace/'proofs'/session['id'];repo.mkdir(parents=True)
    path=repo/'Main.lean';path.write_text(content)
    (workspace/'lean-toolchain').write_text('leanprover/lean4:v4.29.0\n')
    (workspace/'lakefile.toml').write_text('name="boundary_editor"\nversion="0.1.0"\n')
    warm=subprocess.run(['lake','env','lean','--version'],cwd=workspace,capture_output=True,text=True,timeout=30)
    assert warm.returncode==0,warm.stderr
    children=[];samples={}
    try:
        api=start(tmp_path,'native-api',[ADAPTER/'run_api.py','--port',api_port,'--log-level','error'],env);children.append(api)
        worker=start(tmp_path,'native-worker',[ADAPTER/'run_worker.py'],env);children.append(worker)
        wait(lambda:request(base,'/api/health')['ready'])
        url=f"/api/sessions/{session['id']}/editor"
        info=wait(lambda:(v if (v:=request(base,url)).get('environment_revision') else None))
        owner=request(base,url+'/clients',{})
        headers={'Authorization':'Bearer '+owner['credential']}
        buffer=request(base,'/api/editor/buffers',{'version':1,'path':'Main.lean','content':content,
            'base_revision':info['file_revision'],'environment_revision':info['environment_revision']},headers=headers)
        last=None
        for sequence,(kind,position) in enumerate([
            ('editor.analyze',None),('editor.goals',{'line':1,'character':2}),
            ('editor.hover',{'line':0,'character':11}),('editor.complete',{'line':1,'character':4}),
            ('editor.term_goal',{'line':1,'character':2})],1):
            started=time.monotonic()
            accepted=request(base,'/api/editor/requests',{'version':1,'kind':kind,'buffer_id':buffer['id'],
                'environment_revision':buffer['environment_revision'],'sequence':sequence,'idempotency_key':kind,
                'deadline':(datetime.now(timezone.utc)+timedelta(seconds=4.9)).isoformat(),
                **({'position':position} if position else {})},headers=headers)
            result_url='/api/editor/requests/'+accepted['id']
            last=wait(lambda:(v if (v:=request(base,result_url,headers=headers))['state'] not in {'queued','claimed'} else None),timeout=7)
            assert last.get('current'),last
            value=last['result']['value']
            if kind=='editor.analyze':assert value['diagnostics']==[]
            elif kind=='editor.goals':assert 'True' in value['rendered']
            elif kind=='editor.hover':assert value['contents']
            elif kind=='editor.complete':assert isinstance(value['items'],list)
            else:assert value is None or isinstance(value,dict)
            samples[kind]=round((time.monotonic()-started)*1000,2)
        assert path.read_text()==content
        with db.connect() as conn:assert conn.execute('select count(*) from proof_check_evidence').fetchone()[0]==0
        stop(worker)
        retained=request(base,result_url,headers=headers)
        assert retained['result']==last['result'] and api.poll() is None
        report={'schema':1,'transport':'canonical separate API and worker; real HTTP; queued native confined Lean; SQL results',
            'lean':warm.stdout.strip(),'request_latency_ms':samples,'canonical_file_unchanged':True,
            'no_proof_evidence_created':True,'result_readable_after_worker_stop':True}
        if os.environ.get('LEA_STAGE8_EDITOR_REPORT'):
            Path(os.environ['LEA_STAGE8_EDITOR_REPORT']).write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
    finally:
        for child in reversed(children):stop(child)
