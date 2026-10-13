"""Audit reproductions: isolated SQLite/repositories, fake compiler and transports."""
import io
import json
import tempfile
import threading
import urllib.request
from pathlib import Path
from unittest.mock import patch
from email.message import Message
from urllib.response import addinfourl

from app import bridge, db, formalizations, projects, store
from queue import Queue
from lea.interface import Finished
from lea.providers import Usage
from app.config import LeaConfig
from app.routes import projects as pr
from app.routes import sessions as sr
from lea.interface import CheckResult
from lea import http_tools, agent

GOOD = 'theorem audit_target : True := by trivial\n'
BAD = 'theorem audit_target : False := by definitely_not_a_tactic\n'

with tempfile.TemporaryDirectory(prefix='lea-audit-repro-') as tmp:
    root = Path(tmp)
    db.DB_PATH = root / 'audit.sqlite3'
    db.init_db()
    cfg = LeaConfig(model='test', max_turns=3, lea_root=root)
    sr.load_config = pr.load_config = lambda: cfg
    project = projects.ensure_project('audit', root / 'workspace' / 'proofs')
    session = store.create_session('Audit session', project_id=project['id'])
    target = store.create_formalization(project_id=project['id'], loose_session_id=None,
        display_title='Audit', declaration_name='audit_target')
    store.link_session_formalization(session['id'], target['id'])
    store.link_formalization_file(target['id'], 'Target.lean', 'primary')
    run = store.create_run(session['id'], 'test', None, 3, project_id=project['id'],
        focus_formalization_id=target['id'])
    repo = projects.project_repo_dir(project, root / 'workspace' / 'proofs')
    proof = repo / 'Target.lean'
    proof.write_text(GOOD)
    store.add_code_step(session['id'], run['id'], 'Target.lean', content=GOOD,
        check_status='ok', artifact_kind='proof', formalization_id=target['id'])
    store.update_run(run['id'], 'proved', result_kind='proved')
    print('filesystem_before:', formalizations.get(target['id'])['validity_status'])
    pr.interface_check = lambda path: CheckResult(path, 'error', 'bad tactic')
    result = pr.write_project_file(project['id'], pr.FilePut(path='Target.lean', content=BAD))
    print('filesystem_write:', result['check'])
    print('filesystem_after:', formalizations.get(target['id'])['validity_status'])
    print('filesystem_snapshot_matches_disk:', formalizations.current_snapshot(target['id'])['files'][0]['code'] == proof.read_text())
    same = sr.write_file_session(session['id'], sr.FileWriteRequest(path='Target.lean', content=GOOD,
        formalization_id=target['id']))
    print('save_original_snapshot:', same['unchanged'], 'disk_restored:', proof.read_text() == GOOD)

    proof.write_text(GOOD)
    started, release = threading.Event(), threading.Event()
    checked_bytes = []
    def fake_check(path, **kwargs):
        checked_bytes.append(Path(path).read_text())
        started.set()
        assert release.wait(5)
        return CheckResult(path, 'ok', None)
    sr.interface_check = fake_check
    sr.lean_check_runtime = lambda sid: {'state': 'healthy'}
    errors = []
    def do_check():
        try:
            sr._lean_check_session(session['id'], sr.PathRequest(path='Target.lean', formalization_id=target['id']))
        except Exception as exc:
            errors.append(repr(exc))
    worker = threading.Thread(target=do_check)
    worker.start()
    assert started.wait(5)
    sr.write_file_session(session['id'], sr.FileWriteRequest(path='Target.lean', content=BAD,
        formalization_id=target['id']))
    release.set()
    worker.join(10)
    latest = store.latest_code_step_for_path(session['id'], 'Target.lean')
    print('check_race:', {'checked_good_bytes': checked_bytes == [GOOD], 'latest_is_bad': latest['code'] == BAD,
        'latest_verdict': latest['check_status'], 'errors': errors})

    active = store.create_run(session['id'], 'test', None, 3, project_id=project['id'])
    store.update_run(active['id'], 'running')
    print('delete_project_active_before:', store.project_has_active_run(project['id']))
    print('delete_project_response:', pr.delete_project(project['id']))
    print('delete_project_active_after:', store.get_run(active['id']), 'repo_exists:', repo.exists())

seen = []
class FakeHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        seen.append({'url': request.full_url, 'authorization': request.get_header('Authorization')})
        headers = Message()
        if len(seen) == 1:
            headers['Location'] = 'http://127.0.0.1:8001/api/settings'
            response = addinfourl(io.BytesIO(b''), headers, request.full_url, 302)
            response.msg = 'Found'
        else:
            response = addinfourl(io.BytesIO(b'LOCAL_RESPONSE'), headers, request.full_url, 200)
            response.msg = 'OK'
        return response
class FakeHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return FakeHTTPSHandler().https_open(request)
opener = urllib.request.build_opener(FakeHTTPSHandler(), FakeHTTPHandler())
with patch.object(http_tools, '_is_public', lambda host: host == 'public.example'), \
     patch.object(http_tools.urllib.request, 'urlopen', opener.open), \
     patch.dict(http_tools.os.environ, {'AUDIT_DUMMY_KEY': 'dummy-secret'}):
    result = http_tools.make_handler({'name': 'audit', 'url': 'https://public.example/data',
        'auth_key_name': 'AUDIT_DUMMY_KEY'})({})
    print('http_redirect:', {'result': result, 'requests': seen})

def failed_classifier(*args, **kwargs):
    raise RuntimeError('Your api key: sk-dummy-audit-secret is invalid')
    yield
with patch.object(agent, 'stream', failed_classifier):
    result = agent._classify_final_result(model='test', messages=[{'role': 'user', 'content': 'prove True'}],
        final_text='Done', theorem_signature='theorem audit_target : True', config=LeaConfig(model='test', max_turns=3))
    print('classifier_error_detail:', result[:2])

# Verify the same classifier error reaches both durable history and SSE unchanged.
with tempfile.TemporaryDirectory(prefix='lea-audit-redaction-') as tmp:
    root = Path(tmp)
    db.DB_PATH = root / 'audit.sqlite3'
    db.init_db()
    session = store.create_session('Audit classifier redaction')
    run = store.create_run(session['id'], 'test', None, 3)
    events = Queue()
    ctx = bridge.RunnerContext(session_id=session['id'], run_id=run['id'],
        task='Prove True', config=LeaConfig(model='test', max_turns=3, lea_root=root),
        events=events)
    def classified_events(*args, **kwargs):
        yield Finished('completed', 'Please review.', 1, session['id'], 'test',
            Usage(), 0.0, {}, result_kind='needs_review', result_detail=result[1])
    with patch.object(bridge, 'run_events', classified_events):
        bridge.run_lea(ctx)
    persisted = store.get_run(run['id'])['result_detail']
    done = None
    while not events.empty():
        event = events.get_nowait()
        if event['type'] == 'done':
            done = event['payload']
    print('classifier_error_persistence:', {'stored_secret': 'sk-dummy-audit-secret' in persisted,
        'streamed_secret': 'sk-dummy-audit-secret' in (done or {}).get('result_detail', '')})
