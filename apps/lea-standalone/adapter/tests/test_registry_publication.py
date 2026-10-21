"""Node finalization + real SQL workspace commands, file materialization and report facts."""
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import commands, db, store, run_commands, runregistry, target_observations, workspace_commands
from app.api_routes import workspace, projects, formalize_batch_reports
from app.routes import formalize_batch_reports as worker_reports
from app.gitstore import GitStore
from test_core_projections import fixture


@pytest.mark.parametrize('kind,blocker', [('definition', 'busy'), ('theorem', 'busy'), ('theorem', 'conflict'), ('theorem','credential')])
def test_registry_rejection_keeps_verified_job_and_report_then_recovers_safely(fixture, monkeypatch, kind, blocker):
    x = fixture
    monkeypatch.setattr('app.routes.projects._proofs_root', lambda: x['root']/'workspace/proofs')
    from app.config import load_config
    monkeypatch.setattr('app.routes.projects.load_config', load_config)
    if kind == 'definition':
        from app import proof_evidence
        from evidence_fixtures import mock_execution
        from lea.events import CheckResult
        code = 'def result : Nat := 1\n'
        x['path'].write_text(code)
        store.update_formalization(x['fid'], kind='definition')
        store.add_code_step(x['session']['id'], x['run']['id'], 'Proof.lean', content=code, formalization_id=x['fid'])
        assert proof_evidence.record_check(x['session']['id'], 'Proof.lean', x['path'],
            CheckResult(str(x['path']), 'ok', execution=mock_execution(x['path']), content=code),
            run_id=x['run']['id'], formalization_id=x['fid'])[0]
        with db.write() as conn:
            conn.execute("update runs set result_kind='defined',result_detail=null where id=?", (x['run']['id'],))
    GitStore(x['root']/'workspace/proofs').init_repo(x['path'].parent)
    with db.write() as conn:
        conn.execute('''insert into lea_status_run_contexts(run_id,formalization_id,project_id,session_id,
            run_generation,operation,source_bundle_json,source_identity_hash,source_bundle_hash,created_at)
            values(?,?,?,?,1,'overleaf_solver',?,'source','bundle',?)''',
            (x['run']['id'], x['fid'], x['project']['id'], x['session']['id'],
             json.dumps({'targetKind': kind, 'targetKey': 'result'}), db.utc_now()))
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    epoch = commands.start_worker('lea-command-host')
    with db.write() as conn:
        parent = run_commands._enqueue(conn, 'companion.dispatch', {'path': '/formalize/all'},
            key='registry-parent', project_id=x['project']['id'])['id']
    token = commands.claim('lea-command-host', epoch, 'compute', kinds={'companion.dispatch'})
    commands.complete(token, {'accepted': True})
    monkeypatch.setattr(worker_reports.service, 'generate', lambda *_: None)
    api = FastAPI()
    for module in (workspace, projects, formalize_batch_reports):
        api.include_router(module.router)
    client = TestClient(api)
    trace = [];deny_credential=[blocker=='credential']
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def handle_request(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0))) or None
            if deny_credential[0] and self.path.startswith('/api/workspace/file'):
                self.send_response(403);self.end_headers();self.wfile.write(b'{"detail":"A private worker credential is required"}');return
            response = client.request(self.command, self.path, content=body,
                headers={'Content-Type': 'application/json'})
            if len(trace) < 8:
                trace.append((self.path, response.status_code, response.text[:1600]))
            if self.command == 'POST' and response.status_code < 300 and response.json().get('workspace_command'):
                token = commands.claim('lea-command-host', epoch, 'compute', kinds=workspace_commands.KINDS)
                assert token
                workspace_commands.execute(token)
            self.send_response(response.status_code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(response.content)
        do_GET = do_POST = handle_request
    try:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    except PermissionError:
        pytest.skip('Loopback sockets require the process-test sandbox grant')
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    driver = Path(__file__).resolve().parents[3]/'overleaf-extension/tests/registryDatabaseDriver.mjs'
    options = dict(filename=str(db.DB_PATH), owner='lea-command-host', epoch=epoch,
        baseUrl=f'http://127.0.0.1:{server.server_port}', leaRoot=str(x['root']), parent=parent,
        kind=kind, runId=x['run']['id'], sessionId=x['session']['id'], formalizationId=x['fid'])
    def drive(phase):
        try:
            result = subprocess.run(['node', str(driver)], input=json.dumps({**options, 'phase': phase}),
                text=True, capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail('Node publication timed out: ' + json.dumps(trace))
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)
    holder = 'registry-busy-holder'
    runregistry.registry.try_admit(holder, 'holder-session', project_id=x['project']['id'])
    if blocker == 'conflict':
        from fastapi import HTTPException
        def conflict(*args, **kwargs):
            raise HTTPException(409, detail={'code': 'workspace_reconciliation_required',
                'message': 'Reconcile the interrupted workspace operation before editing.'})
        monkeypatch.setattr('app.workspace_files.save', conflict)
    try:
        job = drive('finalize')
        assert job['status'] == job['finalStatus'] == 'formalized'
        assert job['resultKind'] == ('defined' if kind == 'definition' else 'proved')
        assert job['registryUpdate']['state'] == ('pending' if blocker == 'busy' else 'blocked')
        assert job['registryUpdate']['attempts'] == 1
        with db.connect() as conn:
            failed = conn.execute("select r.error_status,c.state from workspace_requests r join commands c on c.id=r.command_id where r.action='project.save'").fetchone()
            assert failed is None if blocker=='credential' else tuple(failed)==(409,'failed')
            facts = json.loads(conn.execute("select facts_json from formalize_batch_reports where batch_id='formalize-batch-registry'").fetchone()[0])
            assert facts['counts']['verified'] == 1 and facts['counts']['failed'] == 0
            assert facts['items'][0]['state'] == 'formalized'
        if blocker == 'conflict':
            job = drive('retry')
            assert job['status'] == 'formalized' and job['registryUpdate']['state'] == 'blocked'
            assert job['registryUpdate']['attempts'] == 1
            return
        runregistry.registry.release(holder);deny_credential[0]=False
        # Another accepted writer publishes an entry before the retry. The retry
        # must merge fresh SQL bytes, preserving the other writer's content.
        draft = '# Project\n\n## Theorem: other\n\n<!-- lea:theorem name="other" proof="Other.lean" -->\n'
        receipt = workspace_commands.admit('project.save', {'project_id': x['project']['id'],
            'request': {'path': '.lea/project-registry.md', 'content': draft}}, idempotency_key='other-entry',
            expected_revision='missing', strict=True)
        token = commands.claim('lea-command-host', epoch, 'compute', kinds=workspace_commands.KINDS)
        workspace_commands.execute(token)
        saved = workspace_commands.get(receipt['command']['id'])['command']
        assert saved['state'] == 'succeeded', saved
        with db.write() as conn:
            conn.execute("delete from command_workers where id='lea-command-host'")
        epoch = commands.start_worker('lea-command-host')
        options['epoch'] = epoch
        job = drive('retry')  # A fresh worker epoch rehydrates the durable retry.
        assert job['status'] == 'formalized' and job['registryUpdate']['state'] == 'saved'
        assert job['registryUpdate']['attempts'] == 2
        content = workspace.file('.lea/project-registry.md', project_id=x['project']['id'])['content']
        assert 'name="other"' in content and 'name="result"' in content
        assert (x['path'].parent/'.lea/project-registry.md').read_text() == content
        assert store.get_run(x['run']['id'])['status'] == 'proved'
        # A lagging artifact listing must not permanently omit registry work.
        job = drive('missing')
        assert job['status'] == 'formalized' and job['registryUpdate']['state'] == 'saved'
        assert job['recordedProofPath'].endswith('/Proof.lean')
        job = drive('legacy-busy')
        assert job['status'] == 'formalized' and job['registryUpdate']['state'] == 'saved'
        assert 'error' not in job and job['recoveredRegistryError']
        job = drive('different-run')
        assert job['status'] == 'failed' and 'registryUpdate' not in job
    finally:
        runregistry.registry.release(holder)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        client.close()
