"""Immutable Overleaf inputs, named compiler checks and complete SQL batches."""
import hashlib
import json
from pathlib import Path
from queue import Queue
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from app import bridge, commands, companion_service, db, source_publications, store, target_observations
from app.config import LeaConfig
from app.queries import proofs
from app.source_context import source_hash
from evidence_fixtures import mock_execution
from lea.events import CheckResult, FileChanged, Finished
from lea.providers import Usage


def source(label, statement, kind='theorem', file='main.tex'):
    value = dict(version=2, targetKey=label, targetKind=kind, statement=statement, proof='',
        proofAssociation=dict(status='missing', method='none', sourceFile='', proofHash=''),
        statementLocation=dict(sourceFile=file), uses=[], context='', relevantSource=[], mirror=None)
    value['bundleHash'] = source_hash(value)
    value['sourceIdentityHash'] = source_hash(value, identity=True)
    return value


@pytest.mark.usefixtures('certified_unit_inputs')
@pytest.mark.parametrize('inventory_first', [False, True])
def test_generated_target_inventory_and_run_keep_one_lean_name(tmp_path, monkeypatch, inventory_first):
    from app import github_import_service, run_commands
    from app.github_project_import import TaggedTarget
    from published_source_fixtures import publish_for

    monkeypatch.setattr(db, 'DB_PATH', tmp_path/'names.sqlite3')
    db.init_db()
    project = store.create_project('doc', namespace='Lea.Names')
    label = 'lea_auto_def_Ua_12345678'
    bundle = source(label, r'\mathcal{U}_a(q) is the generating series.', 'definition')
    target = TaggedTarget(origin_key=f'doc:definition:{label}', label=label,
        declaration_name='Ua', display_title=label, kind='definition', statement=bundle['statement'],
        source_hash=bundle['sourceIdentityHash'])

    def sync(descriptor):
        result = github_import_service.sync_targets(project, [descriptor], tmp_path/'proofs')['targets'][0]
        assert result['conflict'] is None
        assert result['declaration_name'] == 'Ua'
        return store.get_formalization(result['formalization_id'])

    if inventory_first:
        form = sync(target)
    ref = publish_for(form['id'] if inventory_first else None, bundle, files={'main.tex': bundle['statement']})
    admitted = store.create_run_bundle(message='Formalize Ua', session_id=None, project_id=project['id'],
        session_origin='overleaf', session_origin_url=None, model='test', provider=None, max_turns=3,
        autonomous=True, purpose='overleaf_solver', source_bundle=bundle, source_context_ref=ref,
        new_formalization=dict(display_title=label, declaration_name='Ua', kind='definition',
            origin='overleaf', origin_key=target.origin_key, source_hash=bundle['sourceIdentityHash']))
    run, session = admitted['run'], admitted['session']
    fid = run['focus_formalization_id']
    # An inventory captured earlier can arrive after admission. It must not
    # replace the established Lean name with the generated source identifier.
    from dataclasses import replace
    for descriptor in [target, replace(target, declaration_name=label)]:
        refreshed = sync(descriptor)
        assert refreshed['id'] == fid
        assert refreshed['declaration_name'] == 'Ua'

    cfg = LeaConfig(model='test', max_turns=3, lea_root=tmp_path/'prover')
    (cfg.lea_root/'workspace').mkdir(parents=True)
    monkeypatch.setattr('app.config.load_config', lambda: cfg)

    def events(config, messages, *, working_dir, **kwargs):
        path = Path(working_dir)/f'{label}.lean'
        code = 'namespace Lea.Names\nprivate def helper : Nat := 0\ndef Ua : Nat := helper\nend Lea.Names\n'
        path.write_text(code)
        yield FileChanged(str(path), content=code)
        yield CheckResult(str(path), 'ok', execution=mock_execution(path), content=code)
        yield Finished('completed', 'Definition compiled.', 1, session['id'], 'test', Usage(), 0,
            {'messages': messages}, result_kind='defined', candidate_path=str(path))

    monkeypatch.setattr(bridge, 'run_events', events)
    run_commands.ensure_run(run['id'])
    epoch = commands.start_worker('names-worker')
    token = commands.claim('names-worker', epoch, 'compute', kinds={'run.execute'})
    bridge.run_lea(bridge.RunnerContext(session_id=session['id'], run_id=run['id'], task='Formalize Ua',
        config=cfg, events=Queue(), autonomous=True, purpose='overleaf_solver', command_token=token))
    outcome = store.get_run(run['id'])
    assert outcome['result_kind'] == 'defined', outcome['result_detail']
    assert target_observations.capture(fid)['state'] == 'observed'
    assert proofs.get(fid)['validity_status'] == 'defined'
    with db.connect() as conn:
        assert conn.execute('select declaration_name from artifacts where formalization_id=?', (fid,)).fetchone()[0] == 'Ua'
        assert conn.execute('select count(*) from formalizations where project_id=?', (project['id'],)).fetchone()[0] == 1


@pytest.mark.usefixtures('certified_unit_inputs')
@pytest.mark.parametrize('variant', ['exact', 'changed_statement', 'missing_selection', 'unsupported_axiom', 'definition'])
def test_published_lean_block_gets_named_compiler_evidence_and_durable_verdict(tmp_path, monkeypatch, variant):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path/'workflow.sqlite3')
    db.init_db()
    kind = 'definition' if variant == 'definition' else 'theorem'
    statement = ('def result (n : Nat := 0) : Nat := n + n' if kind == 'definition'
                 else 'theorem result (n : Nat := 0) : n + 0 = n := by\n  rfl')
    bundle = source('result', statement, kind)
    project = store.create_project('doc', namespace='Lea.Published')
    form = store.create_formalization(project_id=project['id'], loose_session_id=None,
        display_title='result', declaration_name='result', kind=kind, statement=statement,
        origin='overleaf', origin_key=f'doc:{kind}:result')
    from published_source_fixtures import publish_for
    ref = publish_for(form['id'], bundle, files={'main.tex': statement}, coverage='complete')
    admitted = store.create_run_bundle(message='Formalize result', session_id=None, project_id=project['id'],
        session_origin='overleaf', session_origin_url=None, model='test', provider=None, max_turns=3,
        autonomous=True, purpose='overleaf_solver', focus_formalization_id=form['id'],
        source_bundle=bundle, source_context_ref=ref)
    run, session = admitted['run'], admitted['session']
    cfg = LeaConfig(model='test', max_turns=3, lea_root=tmp_path/'prover')
    (cfg.lea_root/'workspace').mkdir(parents=True)
    (cfg.lea_root/'pyproject.toml').write_text('[project]\nname="completion-fixture"\nversion="0.0.0"\n')
    (cfg.lea_root/'workspace/lakefile.toml').write_text('name="completion-fixture"\n')
    monkeypatch.setattr('app.config.load_config', lambda: cfg)
    inspected = []

    def events(config, messages, *, working_dir, **kwargs):
        from lea import tools, result_selection
        from lea.check_runtime import CheckText
        from lea.runctx import run_context
        assert config.selected_result == {'declaration': 'result', 'kind': 'defined' if kind == 'definition' else 'proved'}
        code = f'namespace Lea.Published\n{statement}\nend Lea.Published\n'
        if variant == 'changed_statement':
            code = code.replace(': n + 0 = n', ': n = n')
        path = Path(working_dir)/'Result.lean'
        path.write_text(code)
        yield FileChanged(str(path), content=code)
        def inspect(path, name, expected_kind):
            inspected.append((name, expected_kind))
            return dict(full_name='Lea.Published.result', user_name='Lea.Published.result',
                kind='definition' if kind == 'definition' else 'theorem',
                axioms=['untrusted'] if variant == 'unsupported_axiom' else [],
                content_sha256=hashlib.sha256(code.encode()).hexdigest())
        monkeypatch.setattr(result_selection, 'inspect_selected', inspect)
        monkeypatch.setattr(tools, 'lean_check', lambda _: CheckText('OK', mock_execution(path), code, str(path)))
        with run_context(config=config, working_dir=str(working_dir), run_key=run['id']):
            checked = tools._generated_lean_check(str(path))
        execution = dict(checked.execution)
        if variant == 'missing_selection':
            execution.pop('selected_result')
        yield CheckResult(str(path), 'ok', execution=execution, content=code)
        yield Finished('completed', 'Checked.', 1, session['id'], 'test', Usage(), 0,
            {'messages': messages}, result_kind='defined' if kind == 'definition' else 'proved', candidate_path=str(path))

    monkeypatch.setattr(bridge, 'run_events', events)
    from app import run_commands
    run_commands.ensure_run(run['id'])
    epoch = commands.start_worker('completion-worker')
    token = commands.claim('completion-worker', epoch, 'compute', kinds={'run.execute'})
    assert token
    bridge.run_lea(bridge.RunnerContext(session_id=session['id'], run_id=run['id'], task='Formalize result',
        config=cfg, events=Queue(), autonomous=True, purpose='overleaf_solver', command_token=token))
    assert inspected == [('result', 'defined' if kind == 'definition' else 'proved')], store.get_run(run['id'])['result_detail']
    accepted = variant in {'exact', 'definition'}
    outcome = store.get_run(run['id'])
    assert outcome['result_kind'] == (('defined' if kind == 'definition' else 'proved') if accepted else 'incomplete'), outcome['result_detail']
    assert target_observations.capture(form['id'])['state'] == 'observed'
    projected = proofs.get(form['id'])
    assert projected['validity_status'] == (outcome['result_kind'] if accepted else 'unchecked')
    # File checking and acceptance of the selected theorem are independent.
    assert projected['check_current'] is True
    assert bool(projected['certification']) is accepted
    with db.connect() as conn:
        evidence = json.loads(conn.execute('select evidence_json from proof_check_evidence order by created_at desc limit 1').fetchone()[0])
    assert ('selected_result' in evidence) is (variant != 'missing_selection')
    if variant == 'changed_statement':
        assert 'differs' in outcome['result_detail']
    if variant in {'exact', 'missing_selection'}:
        assert_job_report_and_ui_delivery(tmp_path, monkeypatch, project, form, run, session, epoch, accepted)


def assert_job_report_and_ui_delivery(root, monkeypatch, project, form, run, session, epoch, accepted):
    """Production Node finalization and report admission over the same real DB."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import workspace_commands
    from app.api_routes import projects, sessions, formalizations, formalize_batch_reports, workspace
    from app.routes import formalize_batch_reports as worker_reports, projects as worker_projects
    monkeypatch.setattr(worker_reports.service, 'generate', lambda *_: None)
    api = FastAPI()
    for module in (projects, sessions, formalizations, formalize_batch_reports, workspace):
        api.include_router(module.router)
    client = TestClient(api)
    with db.connect() as conn:
        reference = conn.execute('select publication_id from source_heads where formalization_id=?', (form['id'],)).fetchone()[0]
    parent = companion_service.admit('/formalize/all', {'overleafProjectId': 'doc', 'items': [
        {'targetKind': 'theorem', 'targetLabel': 'result', 'source_context_ref': reference}]}, 'completion-batch')['command']
    token = commands.claim('completion-worker', epoch, 'compute', kinds={'companion.dispatch'})
    assert token['id'] == parent['id']
    commands.complete(token, {'accepted': True})
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def handle_request(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0))) or None
            response = client.request(self.command, self.path, content=body, headers={'Content-Type': 'application/json'})
            commands.heartbeat_execution('completion-worker', epoch)
            if self.command == 'POST' and response.status_code < 300 and response.json().get('workspace_command'):
                token = commands.claim('completion-worker', epoch, 'compute', kinds=workspace_commands.KINDS)
                assert token
                workspace_commands.execute(token)
            self.send_response(response.status_code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(response.content)
        do_GET = do_POST = handle_request
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    companion = Path(__file__).resolve().parents[3]/'overleaf-extension/companion'
    script = '''import fs from 'node:fs';import assert from 'node:assert/strict';const x=JSON.parse(fs.readFileSync(0,'utf8'));
      const {openWorkerDatabase}=await import(x.database);const worker=await import(x.worker);
      const db=openWorkerDatabase(x);worker.setWorkerDatabase(db);
      const state={settings:{leaRepoPath:x.leaRoot,leaApiBaseUrl:x.baseUrl},env:{},fetchImpl:fetch,
        jobsPath:'/__sql__/jobs.json',repairBatchesPath:'/__sql__/jobs.json.batches.json',jobs:{}};
      const job={jobId:'completion',jobKey:'doc:theorem:result',targetKind:'theorem',targetLabel:'result',declarationName:'result',
        overleafProjectId:'doc',projectSlug:'doc',projectNamespace:'Lea.Published',apiRunId:x.runId,
        formalizationId:x.formId,leaSessionId:x.sessionId,acceptedCommandId:x.parent,status:'in_progress',startedAt:new Date().toISOString(),logPath:'/__sql__/completion.log'};
      state.jobs.completion=job;
      try{await db.runAccepted(x.parent,async()=>{await worker.applyProofOutcomeToJob({state,job,target:{targetKind:'theorem',targetLabel:'result',
        overleafProjectId:'doc',projectSlug:'doc',projectNamespace:'Lea.Published'},
        exit:{ok:x.accepted,resultKind:x.accepted?'proved':'incomplete',resultDetail:'Named compiler evidence is missing.'}});
        assert.equal(job.status,x.accepted?'formalized':'failed');
        const batch={batchId:'formalize-batch-completion',operation:'formalize',overleafProjectId:'doc',
          acceptedCommandId:x.parent,createdAt:job.startedAt,done:false,running:false,usesByLabel:new Map(),items:[{targetKind:'theorem',targetLabel:'result',state:'running',runJobId:job.jobId}]};
        state.repairBatches={batch};await worker.reconcileRecoveredBatches(state);await batch.reportSubmission;
        assert.ok(batch.reportId,batch.reportError);
        const pane=await worker.handleGetStatuses({overleafProjectId:'doc',targets:[{targetKind:'theorem',targetLabel:'result',targetText:x.statement}]},state);
        assert.equal(pane.statusCode,200,JSON.stringify(pane.body));
        assert.equal(pane.body.statuses['theorem:result'].status,job.status);
        process.stdout.write(JSON.stringify({status:job.status,reportId:batch.reportId}));});
      }finally{worker.setWorkerDatabase(null);db.close();}'''
    try:
        result = subprocess.run(['node', '--input-type=module', '-e', script], input=json.dumps(dict(
            database=(companion/'databaseState.mjs').as_uri(), worker=(companion/'workerService.mjs').as_uri(),
            filename=str(db.DB_PATH), owner='completion-worker', epoch=epoch,
            baseUrl=f'http://127.0.0.1:{server.server_port}', leaRoot=str(root/'prover'),
            runId=run['id'], formId=form['id'], sessionId=session['id'], statement=form['statement'], accepted=accepted, parent=parent['id'])),
            text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        target_observations.capture(form['id'])
        # These are the SQL API projections consumed by standalone's project and
        # session UIs; the extension above consumed the same target observation.
        standalone = client.get(f"/api/projects/{project['id']}/formalizations").json()
        assert standalone['formalizations'][0]['validity_status'] == ('proved' if accepted else 'unchecked')
        with db.connect() as conn:
            job = json.loads(conn.execute("select payload from companion_jobs where id='completion'").fetchone()[0])
            facts = json.loads(conn.execute("select facts_json from formalize_batch_reports where batch_id='formalize-batch-completion'").fetchone()[0])
        assert job['status'] == ('formalized' if accepted else 'failed')
        assert facts['items'][0]['state'] == ('formalized' if accepted else 'incomplete')
        assert facts['counts']['verified'] == int(accepted)
        assert facts['counts']['needsReview'] == 0
        assert facts['counts']['failed'] == 0
        assert facts['counts']['incomplete'] == int(not accepted)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize('omitted', [False, True])
def test_project_batch_checks_all_ten_targets_against_its_frozen_sql_archive(tmp_path, monkeypatch, omitted):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path/'coverage.sqlite3')
    db.init_db()
    files = {'main.tex': '\n'.join(f'\\begin{{theorem}}\n% lea: formalize label=target_{i}\nTrue.\n\\end{{theorem}}' for i in range(6)),
             'sections/more.tex': '\n'.join(f'\\begin{{theorem}}\n% lea: formalize label=target_{i}\nTrue.\n\\end{{theorem}}' for i in range(6,10))}
    owner = source_publications.publisher(source_publications.PublisherRequest(document_id='doc', client_id='tab'))
    items = []
    for i in range(10):
        bundle = source(f'target_{i}', 'True.', file='main.tex' if i < 6 else 'sections/more.tex')
        publication = source_publications.publish(source_publications.PublicationRequest(document_id='doc', client_id='tab',
            generation=owner['generation'], sequence=i+1, source_bundle=bundle, files=files, coverage='complete'))
        items.append(dict(targetKind='theorem', targetLabel=f'target_{i}', source_context_ref=publication['source_context_ref']))
    payload = dict(overleafProjectId='doc', scope='project', items=items[:6] if omitted else items)
    receipt = companion_service.admit('/formalize/all', payload, 'coverage')['command']
    epoch = commands.start_worker('coverage-worker')
    claimed = commands.claim('coverage-worker', epoch, 'compute', kinds={'companion.dispatch'})
    assert claimed['id'] == receipt['id']
    companion = Path(__file__).resolve().parents[3]/'overleaf-extension/companion'
    script = '''import fs from 'node:fs';const x=JSON.parse(fs.readFileSync(0,'utf8'));
      const {openWorkerDatabase}=await import(x.database);const {setWorkerDatabase,validateProjectBatchCoverage}=await import(x.worker);
      const db=openWorkerDatabase(x);setWorkerDatabase(db);
      try{const result=await db.runAccepted(x.command,()=>validateProjectBatchCoverage(x.payload,{settings:{},jobs:{},generatedTargets:{},generatedTargetsPath:'/__sql__/generated-targets.json'}));
        process.stdout.write(JSON.stringify(result));}finally{setWorkerDatabase(null);db.close();}'''
    result = subprocess.run(['node', '--input-type=module', '-e', script], input=json.dumps(dict(
        database=(companion/'databaseState.mjs').as_uri(), worker=(companion/'workerService.mjs').as_uri(),
        filename=str(db.DB_PATH), owner='coverage-worker', epoch=epoch, command=receipt['id'], payload=payload,
        baseUrl='http://offline.invalid', leaRoot=str(tmp_path))), text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    error = json.loads(result.stdout)
    if omitted:
        assert error['statusCode'] == 409
        assert error['body']['error'] == 'incomplete_project_batch'
        assert all(f'target_{i}' in error['body']['message'] for i in range(6,10))
    else:
        assert error is None
    with db.connect() as conn:
        assert conn.execute('select count(*) from companion_source_bindings where command_id=?', (receipt['id'],)).fetchone()[0] == (6 if omitted else 10)
