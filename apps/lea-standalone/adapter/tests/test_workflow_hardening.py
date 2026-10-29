"""Real SQL admission/publication, Node orchestration, reports and both UI stores.

Only theorem generation is scripted. Publication and admission run production
commands, with an interrupted registry installation held across the next proof.
"""
import json
import select
import subprocess
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import commands, db, store, run_commands, workspace_commands, workspace_files, target_observations, proof_evidence, command_execution
from app.api_routes import workspace, projects, runs, sessions, formalize_batch_reports
from app.routes import projects as worker_projects, formalize_batch_reports as worker_reports
from app.gitstore import GitStore
from test_core_projections import fixture
from evidence_fixtures import mock_execution
from lea.events import CheckResult


@pytest.mark.parametrize('expired',[False,True])
def test_node_lease_probe_distinguishes_actual_contention_from_lost_ownership(fixture, expired):
    epoch=commands.start_worker('lease-worker')
    holder=sqlite3.connect(db.DB_PATH,isolation_level=None,check_same_thread=False)
    holder.create_function('lea_change_batch',0,lambda:'held-lease-probe')
    holder.execute('begin exclusive')
    if expired:holder.execute("update command_workers set lease_until='2000-01-01T00:00:00+00:00' where id='lease-worker'")
    companion=Path(__file__).resolve().parents[3]/'overleaf-extension/companion'
    script='''import fs from 'node:fs';import assert from 'node:assert/strict';
      const x=JSON.parse(fs.readFileSync(0,'utf8'));
      const {openWorkerDatabase}=await import(x.database);
      const {probeWorkerLease}=await import(x.policy);
      const db=openWorkerDatabase(x);let lost=0;
      const onLost=()=>lost++;
      assert.equal(probeWorkerLease(db.fence,onLost),false);assert.equal(lost,0);
      console.log('busy');
      // Wait for the actual SQL lock release, independently of SQLite's
      // synchronous busy timeout. A busy probe must never imply lost ownership.
      const until=Date.now()+5000;let owned=false;
      while(Date.now()<until) {
        owned=probeWorkerLease(db.fence,onLost);
        if(owned||lost)break;
        await new Promise(r=>setTimeout(r,25));
      }
      assert.equal(owned,!x.expired);
      assert.equal(lost,x.expired?1:0);db.close();'''
    child=subprocess.Popen(['node','--input-type=module','-e',script],
                           stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        child.stdin.write(json.dumps(dict(
            database=(companion/'databaseState.mjs').as_uri(),policy=(companion/'workflowPolicy.mjs').as_uri(),
            filename=str(db.DB_PATH),owner='lease-worker',epoch=epoch,expired=expired)))
        child.stdin.close();child.stdin=None
        ready,_,_=select.select([child.stdout],[],[],5)
        assert ready,'Node did not reach its contention probe'
        assert child.stdout.readline().strip()=='busy'
        holder.commit()
        _,errors=child.communicate(timeout=10)
        assert child.returncode==0,errors
    finally:
        if child.poll() is None:child.kill();child.communicate(timeout=5)
        holder.close()


def test_job_publication_does_not_rewrite_or_notify_an_unchanged_project(fixture):
    x=fixture
    other=store.create_project('unrelated',namespace='Lea.Unrelated')
    session=store.create_session('Other',project_id=other['id'])
    run=store.create_run(session['id'],'fixture',None,3)
    epoch=commands.start_worker('row-worker')
    module=Path(__file__).resolve().parents[3]/'overleaf-extension/companion/databaseState.mjs'
    script='''import fs from 'node:fs';
      const input=JSON.parse(fs.readFileSync(0,'utf8'));
      const {openWorkerDatabase}=await import(input.module);
      const db=openWorkerDatabase(input);
      const jobs={a:{jobId:'a',overleafProjectId:'observed',apiRunId:input.a,status:'in_progress'},
        b:{jobId:'b',overleafProjectId:'unrelated',apiRunId:input.b,status:'in_progress'}};
      db.put('jobs.json',jobs);jobs.a.status='formalized';db.put('jobs.json',jobs);db.close();'''
    result=subprocess.run(['node','--input-type=module','-e',script],input=json.dumps(dict(module=module.as_uri(),
        filename=str(db.DB_PATH),owner='row-worker',epoch=epoch,baseUrl='http://fixture',leaRoot=str(x['root']),
        a=x['run']['id'],b=run['id'])),text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    with db.connect() as conn:
        changes=[tuple(r) for r in conn.execute("select action,project_id from domain_changes where entity_type='companion_jobs' order by id")]
    assert changes==[('insert',x['project']['id']),('insert',other['id']),('update',x['project']['id'])]


@pytest.mark.parametrize('fenced',[False,True])
def test_completed_response_retries_actual_sql_contention_without_reexecution(fixture,monkeypatch,fenced):
    from app import companion_service,companion_worker
    receipt=companion_service.admit('/statuses',{'overleafProjectId':'observed'},'response-busy')
    epoch=commands.start_worker('lea-command-host')
    token=commands.claim('lea-command-host',epoch,'editor',kinds={'companion.query'})
    calls=[]
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def read(self,*_):return b'{"verified":true}'
    monkeypatch.setattr(companion_worker,'_start',lambda *_:None)
    monkeypatch.setattr(companion_worker,'_port',1)
    monkeypatch.setattr(companion_worker,'_bridge_key','fixture')
    monkeypatch.setattr(companion_worker,'urlopen',lambda *a,**k:(calls.append(True) or Response()))
    original=db._open
    def impatient(*args,**kwargs):
        conn=original(*args,**kwargs);conn.execute('pragma busy_timeout=1');return conn
    monkeypatch.setattr(db,'_open',impatient)
    holder=sqlite3.connect(db.DB_PATH,isolation_level=None,check_same_thread=False)
    holder.create_function('lea_change_batch',0,lambda:'held-response-publication')
    holder.execute('begin immediate')
    if fenced:holder.execute("update commands set state='suspended' where id=?",(token['id'],))
    def release():time.sleep(.25);holder.commit()
    thread=Thread(target=release);thread.start()
    try:
        companion_worker.execute(token)
        assert len(calls)==1
        result=companion_service.receipt(receipt['command']['id'])['command']
        assert result['state']==('suspended' if fenced else 'succeeded')
        if not fenced:assert result['result']['body']=={'verified':True}
    finally:thread.join(timeout=5);holder.close()


@pytest.mark.parametrize('disruption',['busy','lost_response_restart'])
def test_publication_to_next_proof_survives_contention_and_restart(fixture, monkeypatch, disruption):
    x=fixture
    (x['root']/'pyproject.toml').write_text('[project]\nname="workflow-fixture"\nversion="0.0.0"\n')
    monkeypatch.setattr(worker_projects,'_proofs_root',lambda:x['root']/'workspace/proofs')
    from app.config import load_config
    monkeypatch.setattr(worker_projects,'load_config',load_config)
    monkeypatch.setattr(runs,'load_config',lambda:SimpleNamespace(model='fixture',max_turns=3,max_spend_usd=None,max_cost_usd=None))
    monkeypatch.setattr(runs.runtime,'accepting_work',lambda:True)
    monkeypatch.setattr(worker_reports.service,'generate',lambda *_:None)
    GitStore(x['root']/'workspace/proofs').init_repo(x['path'].parent)
    epoch=commands.start_worker('lea-command-host')
    with db.write() as conn:
        parent=run_commands._enqueue(conn,'companion.dispatch',{},key='workflow-parent',project_id=x['project']['id'])['id']
    owner=commands.claim('lea-command-host',epoch,'compute',kinds={'companion.dispatch'})
    commands.complete(owner,{'accepted':True})
    def context(run,label,fid):
        with db.write() as conn:
            conn.execute('''insert or ignore into lea_status_run_contexts(run_id,formalization_id,project_id,session_id,
                run_generation,operation,source_bundle_json,source_identity_hash,source_bundle_hash,created_at)
                values(?,?,?,?,1,'overleaf_solver',?,'source','bundle',?)''',
                (run['id'],fid,x['project']['id'],run['session_id'],json.dumps({'targetKind':'theorem','targetKey':label}),db.utc_now()))
    context(x['run'],'result',x['fid'])
    assert target_observations.capture(x['fid'])['state']=='observed'
    # Crash after SQL intent, before bytes installation: production recovery must
    # finish it, and the next run may be admitted but cannot execute across it.
    receipt=workspace_commands.admit('project.save',{'project_id':x['project']['id'],
        'request':{'path':'.lea/project-registry.md','content':'# Registry\n'}},idempotency_key='held-registry',expected_revision='missing',strict=True)
    token=commands.claim('lea-command-host',epoch,'compute',kinds=workspace_commands.KINDS)
    class ProcessExit(BaseException):pass
    with monkeypatch.context() as gate:
        gate.setattr(workspace_files,'materialize',lambda *_:(_ for _ in ()).throw(ProcessExit()))
        with pytest.raises(ProcessExit):workspace_commands.execute(token)
    with db.connect() as conn:
        assert conn.execute('select state from workspace_effects').fetchone()[0]=='prepared'
    dispatched=[]
    monkeypatch.setattr('app.bridge._try_dispatch',lambda *a,**k:dispatched.append(a))
    api=FastAPI()
    for module in (workspace,projects,runs,sessions,formalize_batch_reports):api.include_router(module.router)
    client=TestClient(api);posts=[];held=[True];trace=[];report_posts=[]
    def release():
        if held[0]:
            workspace_files.recover()
            commands.complete(token,{'recovered':True})
            held[0]=False
    def finish(run_id):
        run=store.get_run(run_id)
        if run['status']!='pending':return
        label=store.get_formalization(run['focus_formalization_id'])['declaration_name']
        code=f'theorem {label} : True := by trivial\n'
        path=x['path'].parent/(label+'.lean');path.write_text(code)
        store.link_formalization_file(run['focus_formalization_id'],path.name,role='primary')
        store.add_code_step(run['session_id'],run_id,path.name,content=code,formalization_id=run['focus_formalization_id'])
        assert proof_evidence.record_check(run['session_id'],path.name,path,
            CheckResult(str(path),'ok',execution=mock_execution(path),content=code),run_id=run_id,formalization_id=run['focus_formalization_id'])[0]
        store.update_run(run_id,'disproved' if label=='false_claim' else 'proved',result_kind='disproved' if label=='false_claim' else 'proved',usage_status='final')
        context(run,label,run['focus_formalization_id'])
        assert target_observations.capture(run['focus_formalization_id'])['state']=='observed'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def handle_request(self):
            body=self.rfile.read(int(self.headers.get('Content-Length',0))) or None
            if self.path=='/test/release':
                release();self.send_response(200);self.end_headers();self.wfile.write(b'{}');return
            if self.command=='POST' and self.path.endswith('/formalize-batch-reports'):
                report_posts.append(json.loads(body))
                if disruption=='busy' and len(report_posts)<=4:
                    self.send_response(503);self.end_headers();self.wfile.write(b'{"detail":{"error":"database_busy"}}');return
            if self.command=='POST' and self.path=='/api/runs':
                posts.append(json.loads(body))
                if len(posts)==1 and disruption=='busy':
                    self.send_response(503);self.end_headers();self.wfile.write(b'{"detail":{"error":"project_busy"}}');return
            if self.path.endswith('/events') and '/runs/' in self.path:finish(self.path.split('/')[3])
            response=client.request(self.command,self.path,content=body,headers={'Content-Type':'application/json'})
            if len(trace)<12:trace.append((self.path,response.status_code,response.text[:600]))
            commands.heartbeat_execution('lea-command-host',epoch)
            if self.command=='POST' and self.path=='/api/runs' and response.status_code==200 and held[0]:
                admitted=response.json();assert store.get_run(admitted['run_id'])['status']=='pending'
                command_execution.solver_tick('lea-command-host',epoch,{})
                command_execution.solver_tick('lea-command-host',epoch,{})
                assert not any(args[0]==admitted['run_id'] for args in dispatched)
                # A held publication fences its own project, not other work.
                stranger=client.post('/api/runs',json={'message':'Other project','project_slug':'unrelated',
                    'idempotency_key':'workflow-stranger','autonomous':True}).json()
                assert stranger.get('run_id'),stranger
                command_execution.solver_tick('lea-command-host',epoch,{})
                command_execution.solver_tick('lea-command-host',epoch,{})
                assert any(args[0]==stranger['run_id'] for args in dispatched)
                assert not any(args[0]==admitted['run_id'] for args in dispatched)
            if self.command=='POST' and response.status_code<300 and response.json().get('workspace_command'):
                work=commands.claim('lea-command-host',epoch,'compute',kinds=workspace_commands.KINDS)
                assert work;workspace_commands.execute(work)
            if self.path=='/api/runs' and len(posts)==1 and disruption=='lost_response_restart':
                self.close_connection=True;return
            self.send_response(response.status_code);self.send_header('Content-Type',response.headers.get('Content-Type','application/json'))
            self.end_headers();self.wfile.write(response.content)
        do_GET=do_POST=handle_request
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    driver=Path(__file__).resolve().parents[3]/'overleaf-extension/tests/workflowDatabaseDriver.mjs'
    options=dict(filename=str(db.DB_PATH),owner='lea-command-host',epoch=epoch,leaRoot=str(x['root']),
        baseUrl=f'http://127.0.0.1:{server.server_port}',parent=parent,projectId=x['project']['id'],
        first=dict(jobId='first',jobKey='observed:theorem:result',targetLabel='result',targetKind='theorem',
            overleafProjectId='observed',projectSlug='observed',projectNamespace='Lea.Observed',status='formalized',
            finalStatus='formalized',startedAt=db.utc_now(),finishedAt=db.utc_now(),apiRunId=x['run']['id'],
            formalizationId=x['fid'],leaSessionId=x['session']['id']))
    def drive(phase):
        result=subprocess.run(['node',str(driver)],input=json.dumps({**options,'phase':phase}),text=True,capture_output=True,timeout=35)
        assert result.returncode==0,result.stdout+result.stderr+json.dumps(trace)
        return json.loads(result.stdout)
    try:
        if disruption=='lost_response_restart':
            retained=drive('crash')['retained'];assert retained['status']=='in_progress' and not retained.get('apiRunId')
            with db.connect() as conn:
                assert conn.execute("select count(*) from runs where client_request_id='workflow-next'").fetchone()[0]==1
                assert conn.execute('select count(*) from formalize_batch_reports').fetchone()[0]==0
            assert drive('seal')['sealed']
            with db.connect() as conn:assert conn.execute('select count(*) from formalize_batch_reports').fetchone()[0]==0
        result=drive('finish');assert result['uis'] and result['batch']
        assert len(report_posts)==(5 if disruption=='busy' else 1)
        assert all(post==report_posts[0] for post in report_posts)
        assert drive('stale-report')['sealed']
        recovered=drive('finish')
        assert recovered['reportId']=='formalize-batch-workflow' and recovered['reportError'] is None
        assert len(report_posts)==(5 if disruption=='busy' else 1)  # Read back the existing report, no resynthesis.
        assert posts[0]==posts[1]
        with db.connect() as conn:
            assert conn.execute("select count(*) from runs where client_request_id='workflow-next'").fetchone()[0]==1
            facts=json.loads(conn.execute("select facts_json from formalize_batch_reports where batch_id='formalize-batch-workflow'").fetchone()[0])
            assert facts['counts']['verified']==4 and facts['counts']['disproved']==1
            assert facts['counts']['failed']==facts['counts']['needsReview']==facts['counts']['dependencySkipped']==0
        for name in ('next','independent','dependent'):
            assert result['jobs'][name]['status']=='formalized'
            assert result['jobs'][name]['registryUpdate']['state']=='saved'
    finally:
        release();server.shutdown();server.server_close();thread.join(timeout=5);client.close()
