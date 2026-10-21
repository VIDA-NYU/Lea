"""Published source -> real agent/bridge -> SQL -> companion stub workflow.

Only the model stream and compiler are scripted. Admission, tool dispatch,
checks, source materialization, durable jobs and registry publication are real.
"""
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from threading import Thread

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import bridge, commands, companion_service, db, run_commands, store, target_observations, workspace_commands
from app.api_routes import runs, sessions, projects, workspace
from app.config import LeaConfig
from app.source_context import source_hash
from evidence_fixtures import mock_execution
from published_source_fixtures import publish_for
from lea import agent
from lea.check_runtime import CheckText
from lea.providers import TextDelta, ToolCall, Done, Usage


def source(label, statement='True is true.', kind='theorem'):
    bundle = dict(version=2, targetKey=label, targetKind=kind, statement=statement, proof='',
        proofAssociation=dict(status='missing',method='none',sourceFile='',proofHash=''),
        statementLocation=dict(sourceFile='main.tex'), uses=[],context='',relevantSource=[],mirror=None)
    bundle['bundleHash'] = source_hash(bundle)
    bundle['sourceIdentityHash'] = source_hash(bundle,identity=True)
    return bundle


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'state.sqlite3')
    db.init_db()
    root = tmp_path/'prover'
    (root/'workspace').mkdir(parents=True)
    (root/'pyproject.toml').write_text('[project]\nname="stub-workflow"\nversion="0.0.0"\n')
    (root/'workspace/lakefile.toml').write_text('name="stub-workflow"\n')
    config = LeaConfig(model='o4-mini',max_turns=4,lea_root=root)
    monkeypatch.setattr('app.config.load_config',lambda:config)
    monkeypatch.setattr(runs,'load_config',lambda:config)
    monkeypatch.setattr('app.routes.runs.load_config',lambda:config)
    monkeypatch.setattr('app.routes.projects.load_config',lambda:config)
    monkeypatch.setattr('app.routes.projects._proofs_root',lambda:root/'workspace/proofs')
    monkeypatch.setattr(runs.runtime,'accepting_work',lambda:True)
    monkeypatch.setattr('app.admission_policy.admission_enabled',lambda:True)
    project = store.create_project('doc',namespace='Lea.StubWorkflow')
    state = {'calls':{}, 'code':None}
    def provider(model,system,messages,tools,model_kwargs=None,**kwargs):
        current = agent._tools.current_config()
        label = current.status_context['source_bundle']['targetKey']
        assert current.stub_generation and current.prompt_variant == 'overleaf_stub'
        assert 'classify the mathematical outcome' not in system
        count = state['calls'].get(label,0)+1
        state['calls'][label] = count
        if count == 1:
            code = state['code'] or f'namespace Lea.StubWorkflow\ntheorem {label} : True := by\n  sorry\nend Lea.StubWorkflow\n'
            yield ToolCall('write_file',{'path':label+'.lean','content':code})
            yield ToolCall('lean_check',{'path':label+'.lean'})
        else:
            yield TextDelta('The checked statement stub is ready; its proof remains sorry.')
        yield Done(Usage(3,2),0)
    def checked(raw):
        path = agent._tools._run_relative_path(raw)
        return CheckText("warning: declaration uses 'sorry'",mock_execution(path),path.read_text(),str(path))
    monkeypatch.setattr(agent,'_provider_stream',provider)
    monkeypatch.setattr(agent._tools,'lean_check',checked)
    def publish(label='a',statement='True is true.',kind='theorem'):
        form = store.create_formalization(project_id=project['id'],loose_session_id=None,display_title=label,
            declaration_name=label,kind=kind,statement=statement,origin='overleaf',origin_key=f'doc:{kind}:{label}')
        bundle = source(label,statement,kind)
        reference = publish_for(form['id'],bundle,files={'main.tex':'Published theorem source.'},coverage='complete')
        return form,dict(overleafProjectId='doc',targetKind=kind,targetLabel=label,targetText=statement,
            source_context_ref=reference,sourceBundle=bundle,mirrorAvailable=False)
    epoch = commands.start_worker('lea-command-host')
    def execute(run_id):
        token = commands.claim('lea-command-host',epoch,'compute',kinds={'run.execute'})
        assert token and json.loads(token['payload_json'])['run_id'] == run_id
        row = store.get_run(run_id)
        bridge.run_lea(bridge.RunnerContext(session_id=row['session_id'],run_id=run_id,
            task='Create a statement stub',config=config,events=Queue(),autonomous=True,
            purpose=row['purpose'],command_token=token))
        return store.get_run(run_id)
    return dict(root=root,config=config,project=project,state=state,publish=publish,execute=execute,epoch=epoch)


@pytest.mark.parametrize('code,accepted',[(None,True),
    ('def a : Prop := True\ntheorem helper : True := by sorry\n',False),
    ('theorem a : True := by trivial\ntheorem helper : True := by sorry\n',False),
    ('theorem a : True := by trivial\nexample : True := by sorry\n',False),
    ('theorem a : True := by trivial\n#check (sorry : True)\n',False),
    ('/- theorem a : True := by sorry -/\ntheorem helper : True := by sorry\n',False)])
def test_adapter_stub_admission_and_real_agent_completion(workflow,code,accepted):
    x=workflow;form,payload=x['publish']();x['state']['code']=code
    request=runs.RunRequest(message='Create a Lean sorry stub and stop after lean_check.',purpose='overleaf_stub',autonomous=True,
        project_slug='doc',focus_formalization_id=form['id'],source_context_ref=payload['source_context_ref'],idempotency_key='one-stub')
    admitted=runs.create_run(request)
    assert runs.create_run(request)['run_id']==admitted['run_id']
    result=x['execute'](admitted['run_id'])
    assert result['purpose']=='overleaf_stub'
    assert result['status']==('answered' if accepted else 'needs_review'),result['result_detail']
    assert result['result_kind']==(None if accepted else 'needs_review')
    assert x['state']['calls']['a']==2
    steps=store.current_code_steps_for_formalization(form['id'])
    assert steps and steps[0]['check_status']=='ok' and steps[0]['check_current']
    assert target_observations.capture(form['id'])['state']=='observed'
    from app.queries import proofs
    projected=proofs.get(form['id'])
    assert projected['validity_status'] not in {'proved','defined'}
    assert not projected.get('certification')


@pytest.mark.parametrize('module_name',['app.routes.runs','app.api_routes.runs'])
@pytest.mark.parametrize('problem',['definition','interactive','unpublished'])
def test_stub_admission_requires_published_autonomous_theorem(workflow,monkeypatch,module_name,problem):
    import importlib
    module=importlib.import_module(module_name)
    form,payload=workflow['publish'](kind='definition' if problem=='definition' else 'theorem')
    with pytest.raises(HTTPException) as failure:
        module.create_run(module.RunRequest(message='Create a stub',purpose='overleaf_stub',autonomous=problem!='interactive',
            project_slug='doc',focus_formalization_id=form['id'],
            source_context_ref=payload['source_context_ref'] if problem!='unpublished' else None))
    assert failure.value.status_code==422


@pytest.mark.parametrize('changed', [False, True])
def test_structured_stub_preserves_the_confirmed_statement(workflow, changed):
    x = workflow
    form, payload = x['publish'](statement='theorem a (n : Nat := 0) : n = n := by sorry')
    x['state']['code'] = ('theorem a (n : Nat := 0) : False := by sorry' if changed
                        else 'theorem a (n : Nat := 0) : n = n := by sorry')
    admitted = runs.create_run(runs.RunRequest(message='Translate only the statement', purpose='overleaf_stub',
        autonomous=True, project_slug='doc', focus_formalization_id=form['id'],
        source_context_ref=payload['source_context_ref']))
    result = x['execute'](admitted['run_id'])
    assert result['status'] == ('needs_review' if changed else 'answered'), result['result_detail']
    if changed:
        assert 'differs' in result['result_detail']


@pytest.mark.parametrize('batch',[False,True])
def test_sql_companion_stub_workflow_survives_registry_outage_and_process_restart(workflow,batch):
    x=workflow
    targets=[x['publish'](label)[1] for label in (['a','b'] if batch else ['a'])]
    parent=companion_service.admit('/stub/all' if batch else '/stub',
        {'overleafProjectId':'doc','items':targets} if batch else targets[0], 'stub-workflow-parent')['command']
    token=commands.claim('lea-command-host',x['epoch'],'compute',kinds={'companion.dispatch'})
    assert token['id']==parent['id'];commands.complete(token,{'accepted':True})
    api=FastAPI()
    for module in (runs,sessions,projects,workspace):api.include_router(module.router)
    client=TestClient(api);blocked=[True];starts=[];trace=[];observations={}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def handle_request(self):
            body=self.rfile.read(int(self.headers.get('Content-Length',0))) or None
            if self.path=='/api/health':
                response_body=json.dumps({'capabilities':{'lea_status':{'version':1,'admission_enabled':True,'source_pause_policy':1}}}).encode();status=200
            elif self.path=='/api/settings':response_body=b'{}';status=404
            elif blocked[0] and self.path.startswith('/api/workspace/file'):
                response_body=b'{"detail":{"code":"worker_unavailable","message":"Publication temporarily unavailable"}}';status=503
            else:
                response=client.request(self.command,self.path,content=body,headers={'Content-Type':'application/json'})
                status=response.status_code;response_body=response.content
                if self.path=='/api/runs' and status<300:
                    created=response.json();starts.append(created['run_id']);x['execute'](created['run_id'])
                if self.command=='POST' and status<300 and response.json().get('workspace_command'):
                    work=commands.claim('lea-command-host',x['epoch'],'compute',kinds=workspace_commands.KINDS)
                    assert work;workspace_commands.execute(work)
                if self.command=='GET' and self.path.startswith('/api/sessions/') and status==200:
                    snapshot=response.json()
                    if snapshot.get('code_steps'):
                        observations[self.path]=observations.get(self.path,0)+1
                        if observations[self.path]==1:
                            # Terminal SSE can arrive before the observation worker
                            # publishes this run's fresh compiler evidence.
                            for step in snapshot['code_steps']:
                                step.update(check_current=False,projection={'state':'inputs_changed'})
                            response_body=json.dumps(snapshot).encode()
            commands.heartbeat_execution('lea-command-host',x['epoch'])
            trace.append((self.command,self.path,status,response_body[:300].decode(errors='replace')))
            self.send_response(status);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(response_body)
        do_GET=do_POST=handle_request
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    driver=Path(__file__).resolve().parents[3]/'overleaf-extension/tests/stubWorkflowDatabaseDriver.mjs'
    options=dict(filename=str(db.DB_PATH),owner='lea-command-host',epoch=x['epoch'],leaRoot=str(x['root']),
        baseUrl=f'http://127.0.0.1:{server.server_port}',parent=parent['id'],targets=targets,batch=batch)
    def drive(phase):
        result=subprocess.run(['node',str(driver)],input=json.dumps({**options,'phase':phase}),text=True,capture_output=True,timeout=30)
        assert result.returncode==0,result.stdout+result.stderr+json.dumps(trace[-15:])
        return json.loads(result.stdout)
    try:
        first=drive('generate')
        assert all(job['status']=='sorry_stub' and job['registryUpdate']['state']=='pending' for job in first['jobs'])
        assert len(starts)==len(targets)
        assert len(observations)==len(targets) and all(count>=2 for count in observations.values())
        blocked[0]=False
        # A new Node process and worker epoch must replay publication, never generation.
        with db.write() as conn:conn.execute("delete from command_workers where id='lea-command-host'")
        x['epoch']=commands.start_worker('lea-command-host');options['epoch']=x['epoch']
        recovered=drive('retry')
        assert all(job['status']=='sorry_stub' and job['registryUpdate']['state']=='saved' for job in recovered['jobs'])
        assert len(starts)==len(targets)
        for label in (['a','b'] if batch else ['a']):
            assert x['state']['calls'][label]==2
        content=(x['root']/'workspace/proofs/Lea/StubWorkflow/.lea/project-registry.md').read_text()
        assert all(f'name="{item["targetLabel"]}"' in content for item in targets)
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5);client.close()
