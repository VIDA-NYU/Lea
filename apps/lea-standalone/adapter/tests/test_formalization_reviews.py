from published_source_fixtures import publish_for
"""User certification is durable evidence, never another prover attempt."""
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app import db, formalizations, formalization_reviews as reviews, store

pytestmark = pytest.mark.usefixtures('certified_unit_inputs')


def setup_target(tmp_path, monkeypatch, *, kind='theorem', counterexample=False, project_target=False):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'review.sqlite3')
    db.init_db()
    project = store.create_project('review-project', title='Review', description=None,
        namespace='Lea.Review', repo_path='Lea/Review') if project_target else None
    session = store.create_session('Review candidate', project_id=project['id'] if project else None)
    target = store.create_formalization(project_id=project['id'] if project else None, loose_session_id=None if project else session['id'],
        display_title='claim', declaration_name='claim', kind=kind, statement='The original statement.')
    store.link_session_formalization(session['id'], target['id'])
    store.link_formalization_file(target['id'], 'Claim.lean', 'primary')
    run = store.create_run(session['id'], 'test', None, 3, focus_formalization_id=target['id'])
    code = ('def claim : Nat := 1\n' if kind == 'definition' else
            'theorem counterexample : ¬False := by simp\n' if counterexample else 'theorem claim : True := by trivial\n')
    step = store.add_code_step(session['id'], run['id'], 'Claim.lean', content=code,
        check_status='ok', artifact_kind='definition' if kind == 'definition' else 'proof', formalization_id=target['id'])
    store.update_run(run['id'], 'needs_review', result_kind='needs_review', result_detail='NEEDS_REVIEW')
    return session, target, run, step


def approve(target, outcome='proved', **kwargs):
    context = reviews.context(target['id'])
    return reviews.decide(target['id'], dict(action='approve', outcome=outcome,
        revision_token=context['review']['revision_token'], idempotency_key=uuid4().hex, **kwargs))


@pytest.mark.parametrize('kind,counterexample,outcome', [('theorem', False, 'proved'),
    ('definition', False, 'defined'), ('theorem', True, 'disproved')])
def test_approve_persists_and_withdraws_without_changing_run(tmp_path, monkeypatch, kind, counterexample, outcome):
    session, target, run, step = setup_target(tmp_path, monkeypatch, kind=kind, counterexample=counterexample)
    before = store.get_run(run['id'])
    before_digest = store.sessions_digest()
    ctx = reviews.context(target['id'])
    assert ctx['review']['eligible']
    assert ctx['artifacts'][0]['content'] == step['code']
    result = approve(target, outcome)
    assert result['formalization']['validity_status'] == outcome
    assert result['formalization']['certification']['source'] == 'user_review'
    assert store.get_run(run['id']) == before
    assert store.sessions_digest() != before_digest
    assert store.session_detail(session['id'])['status'] == outcome
    assert store.list_sessions()[0]['status'] == outcome
    db.init_db()  # restarting/migrating doesn't erase the evidence
    assert formalizations.get(target['id'])['validity_status'] == outcome
    withdrawn = reviews.decide(target['id'], dict(action='withdraw', approval_id=result['decision']['id'], idempotency_key=uuid4().hex))
    assert withdrawn['formalization']['validity_status'] == 'needs_review'
    assert store.get_run(run['id']) == before
    assert len(store.review_events_for_session(session['id'])) == 2


def test_duplicate_conflicting_and_invalid_decisions(tmp_path, monkeypatch):
    _, target, _, _ = setup_target(tmp_path, monkeypatch)
    request = dict(action='approve', outcome='proved', revision_token=reviews.context(target['id'])['review']['revision_token'], idempotency_key='one')
    result = reviews.decide(target['id'], request)
    assert reviews.decide(target['id'], request)['decision']['id'] == result['decision']['id']
    with pytest.raises(HTTPException) as exc:
        reviews.decide(target['id'], {**request, 'outcome': 'disproved'})
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        reviews.decide(target['id'], {**request, 'outcome': 'disproved', 'idempotency_key': 'two'})
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        approve(target, 'defined')
    assert exc.value.status_code == 422


@pytest.mark.parametrize('chat_result_kind', [None, 'answered'])
@pytest.mark.parametrize('chat_purpose', ['general', 'overleaf_continuation'])
def test_chat_and_identical_check_preserve_approval_but_new_attempt_does_not(tmp_path, monkeypatch, chat_result_kind, chat_purpose):
    session, target, run, step = setup_target(tmp_path, monkeypatch)
    approve(target)
    chat = store.create_run(session['id'], 'test', None, 3, focus_formalization_id=target['id'], purpose=chat_purpose)
    store.update_run(chat['id'], 'answered', result_kind=chat_result_kind)
    store.add_code_step(session['id'], None, 'Claim.lean', content=step['code'], check_status='ok',
        artifact_kind='proof', author='user', formalization_id=target['id'])
    assert formalizations.get(target['id'])['validity_status'] == 'proved'
    retry = store.create_run(session['id'], 'test', None, 3, focus_formalization_id=target['id'])
    assert not formalizations.get(target['id'])['review']['eligible']
    store.update_run(retry['id'], 'failed', result_kind='failed')
    assert formalizations.get(target['id'])['validity_status'] == 'needs_review'


def test_noop_decisions_retain_idempotency_after_withdrawal(tmp_path, monkeypatch):
    _, target, _, _ = setup_target(tmp_path, monkeypatch)
    request = dict(action='approve', outcome='proved', revision_token=reviews.context(target['id'])['review']['revision_token'], idempotency_key='first')
    first = reviews.decide(target['id'], request)['decision']
    noop = {**request, 'idempotency_key': 'second'}
    assert reviews.decide(target['id'], noop)['decision']['id'] == first['id']
    reviews.decide(target['id'], dict(action='withdraw', approval_id=first['id'], idempotency_key='withdraw'))
    assert reviews.decide(target['id'], noop)['decision']['id'] == first['id']
    with pytest.raises(HTTPException) as exc:
        reviews.decide(target['id'], {**noop, 'outcome': 'disproved'})
    assert exc.value.status_code == 409
    with db.connect() as conn:
        assert conn.execute('select count(*) from formalization_review_events').fetchone()[0] == 2


@pytest.mark.parametrize('code', ['theorem claim : True := by sorry\n',
    'axiom cheat : False\ntheorem claim : True := by trivial\n',
    'theorem claim : True := by native_decide\n'])
def test_shortcuts_are_not_review_ready(tmp_path, monkeypatch, code):
    session, target, _, _ = setup_target(tmp_path, monkeypatch)
    store.add_code_step(session['id'], None, 'Claim.lean', content=code, check_status='ok',
        artifact_kind='proof', formalization_id=target['id'])
    assert formalizations.get(target['id'])['validity_status'] == 'failing'
    assert not reviews.context(target['id'])['review']['eligible']


def test_helper_and_missing_evidence_are_unchecked(tmp_path, monkeypatch):
    _, target, _, step = setup_target(tmp_path, monkeypatch)
    with db.connect() as conn:
        conn.execute("update formalization_files set role='support' where formalization_id=?", (target['id'],))
        conn.execute("update formalizations set declaration_name='missing_target' where id=?", (target['id'],))
    assert formalizations.get(target['id'])['validity_status'] == 'unchecked'
    with db.connect() as conn:
        conn.execute("update formalization_files set role='primary' where formalization_id=?", (target['id'],))
        conn.execute("update formalizations set declaration_name='claim' where id=?", (target['id'],))
        conn.execute('update timeline set check_evidence_id=null where id=?', (int(step['id']),))
    assert formalizations.get(target['id'])['validity_status'] == 'unchecked'


def test_legacy_review_recovers_only_unambiguous_checked_named_target(tmp_path, monkeypatch):
    _, target, _, _ = setup_target(tmp_path, monkeypatch)
    with db.connect() as conn:
        conn.execute("update formalization_files set role='support' where formalization_id=?", (target['id'],))
    assert reviews.context(target['id'])['review']['eligible']
    assert approve(target)['formalization']['validity_status'] == 'proved'


def test_changed_revision_rejects_dialog_and_invalidate_approval(tmp_path, monkeypatch):
    session, target, _, _ = setup_target(tmp_path, monkeypatch)
    token = reviews.context(target['id'])['review']['revision_token']
    approve(target)
    store.add_code_step(session['id'], None, 'Claim.lean', content='theorem claim : True := by simp\n',
        check_status='ok', artifact_kind='proof', formalization_id=target['id'])
    assert formalizations.get(target['id'])['validity_status'] == 'needs_review'
    with pytest.raises(HTTPException) as exc:
        reviews.decide(target['id'], dict(action='approve', outcome='proved', revision_token=token, idempotency_key='stale'))
    assert exc.value.status_code == 409


def test_evidence_binds_environment_and_retains_blobs(tmp_path, monkeypatch):
    _, target, _, step = setup_target(tmp_path, monkeypatch)
    result = approve(target)
    bindings = json.loads(result['decision']['evidence_json'])
    assert bindings[0]['blob_id'] == step['blob_id']
    assert bindings[0]['check_evidence_id'] and bindings[0]['environment_sha256']


@pytest.mark.parametrize('project_target', [False, True])
def test_api_validation_and_no_execution_or_history_mutation(tmp_path, monkeypatch, project_target):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import bridge
    from app.routes.formalizations import router
    from lea import interface
    session, target, _, proof = setup_target(tmp_path, monkeypatch, project_target=project_target)
    def forbidden(*args, **kwargs):
        pytest.fail('Review must never execute a prover or compiler.')
    monkeypatch.setattr(interface, 'check', forbidden)
    monkeypatch.setattr(interface, 'run_events', forbidden)
    monkeypatch.setattr(bridge, 'enqueue_run', forbidden)
    from app import core_observer,commands,workspace_commands
    from app.queries import core
    scope=core.scope_for(formalization_id=target['id']);core_observer.enroll(scope)
    for _ in range(20):
        if core_observer.capture(scope)['state']=='observed':break
    epoch=commands.start_worker('review-fixture')
    def settled(response):
        assert response.status_code==202,response.text
        accepted=response.json()['command']['id']
        token=commands.claim('review-fixture',epoch,'compute',kinds={'workspace.execute'})
        if token:workspace_commands.execute(token)
        return workspace_commands.get(accepted)['command']
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    def immutable_history():
        with db.connect() as conn:
            return {table: [tuple(row) for row in conn.execute(f'select * from {table}')]
                for table in ['runs', 'timeline', 'proof_check_evidence', 'run_usage_breakdown']}
    before = immutable_history()
    path = f"/api/formalizations/{target['id']}"
    ctx = client.get(path + '/review').json()
    request = dict(action='approve', outcome='proved', revision_token=ctx['review']['revision_token'], idempotency_key='api')
    result=settled(client.post(path+'/review-decisions',json=request))
    assert result['state']=='succeeded',result
    assert result['result']['formalization']['validity_status']=='proved'
    duplicate=settled(client.post(path+'/review-decisions',json=request))
    assert duplicate['result']['decision']['id']==result['result']['decision']['id']
    invalid=settled(client.post(path+'/review-decisions',json={'action':'approve','idempotency_key':'missing'}))
    assert invalid['state']=='failed' and invalid['error_status']==422
    assert client.get('/api/formalizations/missing/review').status_code==404
    withdrawn=settled(client.post(path+'/review-decisions',json=dict(action='withdraw',approval_id=result['result']['decision']['id'],idempotency_key='withdraw')))
    assert withdrawn['state']=='succeeded',withdrawn
    assert store.blob_content(proof['blob_id'])==proof['code']
    assert immutable_history() == before


def test_failed_check_and_ambiguous_artifacts_block_approval(tmp_path, monkeypatch):
    session, target, _, _ = setup_target(tmp_path, monkeypatch)
    token = reviews.context(target['id'])['review']['revision_token']
    store.add_code_step(session['id'], None, 'Claim.lean', content='theorem claim : True := by trivial\n',
        check_status='error', artifact_kind='proof', formalization_id=target['id'])
    assert formalizations.get(target['id'])['validity_status'] == 'failing'
    with pytest.raises(HTTPException) as exc:
        reviews.decide(target['id'], dict(action='approve', outcome='proved', revision_token=token, idempotency_key='failed'))
    assert exc.value.status_code == 409
    store.add_code_step(session['id'], None, 'Claim.lean', content='theorem claim : True := by trivial\n',
        check_status='ok', artifact_kind='proof', formalization_id=target['id'])
    store.link_formalization_file(target['id'], 'Duplicate.lean', 'support')
    store.add_code_step(session['id'], None, 'Duplicate.lean', content='theorem claim : True := by trivial\n',
        check_status='ok', artifact_kind='proof', formalization_id=target['id'])
    assert formalizations.get(target['id'])['validity_status'] == 'unchecked'
    assert 'ambiguous' in reviews.context(target['id'])['review']['reason']


def test_source_and_dependency_changes_invalidate_certification(tmp_path, monkeypatch):
    from lea.check_evidence import digest, environment_hash
    _, target, run, step = setup_target(tmp_path, monkeypatch)
    dependency = tmp_path / 'Dependency.lean'
    dependency.write_text('def dependency := 1')
    with db.connect() as conn:
        row = conn.execute('select evidence_json from proof_check_evidence where id=?', (step['check_evidence_id'],)).fetchone()
        evidence = json.loads(row[0])
        evidence['environment']['files'] = {'dependency': {'path': str(dependency), 'sha256': digest(dependency.read_bytes())}}
        evidence['environment_sha256'] = environment_hash(evidence['environment'])
        conn.execute('update proof_check_evidence set evidence_json=? where id=?', (json.dumps(evidence), step['check_evidence_id']))
    approve(target)
    dependency.write_text('def dependency := 2')
    assert formalizations.get(target['id'])['validity_status'] == 'unchecked'
    assert reviews.context(target['id'])['review']['approval'] is None
    dependency.write_text('def dependency := 1')
    store.update_formalization(target['id'], source_hash='new-source')
    with db.connect() as conn:
        conn.execute('update runs set focus_source_hash=? where id=?', ('old-source', run['id']))
    assert formalizations.get(target['id'])['validity_status'] == 'stale'


def test_unfinished_save_or_settling_worker_blocks_decision(tmp_path, monkeypatch):
    from app import project_coordination
    _, target, run, _ = setup_target(tmp_path, monkeypatch, project_target=True)
    with project_coordination.mutation(target['project_id']):
        assert not reviews.context(target['id'])['review']['eligible']
        with pytest.raises(HTTPException) as exc:
            approve(target)
        assert exc.value.status_code == 409
    with project_coordination.file_edit(target['project_id']):
        assert not reviews.context(target['id'])['review']['eligible']
        with pytest.raises(HTTPException) as exc:
            approve(target)
        assert exc.value.status_code == 409
    project_coordination.worker_started(target['project_id'], run['id'])
    try:
        assert not reviews.context(target['id'])['review']['eligible']
        with pytest.raises(HTTPException) as exc:
            approve(target)
        assert exc.value.status_code == 409
    finally:
        project_coordination.worker_finished(target['project_id'], run['id'])
    assert reviews.context(target['id'])['review']['eligible']


def test_withdrawing_old_approval_does_not_revoke_reapproval(tmp_path, monkeypatch):
    _, target, _, _ = setup_target(tmp_path, monkeypatch)
    first = approve(target)['decision']
    withdrawal = dict(action='withdraw', approval_id=first['id'], idempotency_key='withdraw-old')
    reviews.decide(target['id'], withdrawal)
    second = approve(target, 'disproved')['decision']
    reviews.decide(target['id'], {**withdrawal, 'idempotency_key': 'withdraw-old-again'})
    assert formalizations.get(target['id'])['review']['approval']['id'] == second['id']


def test_active_retry_blocks_withdrawal(tmp_path, monkeypatch):
    session, target, _, _ = setup_target(tmp_path, monkeypatch)
    first = approve(target)['decision']
    store.create_run(session['id'], 'test', None, 3, focus_formalization_id=target['id'])
    with pytest.raises(HTTPException) as exc:
        reviews.decide(target['id'], dict(action='withdraw', approval_id=first['id'], idempotency_key='busy'))
    assert exc.value.status_code == 409


def test_project_deletion_retains_other_review_blobs_and_removes_records(tmp_path, monkeypatch):
    _, target, _, step = setup_target(tmp_path, monkeypatch, project_target=True)
    approved = approve(target)
    other = store.create_project('other', title='Other', description=None, namespace='Lea.Other', repo_path='Lea/Other')
    store.create_session('Other', project_id=other['id'])
    # Simulate an otherwise unreferenced review snapshot to exercise GC retention.
    with db.connect() as conn:
        conn.execute('delete from proof_check_evidence')
        conn.execute('delete from timeline')
    assert store.delete_project_cascade(other['id'])
    assert store.blob_content(step['blob_id']) == step['code']
    assert store.delete_project_cascade(target['project_id'])
    with db.connect() as conn:
        assert conn.execute('select count(*) from formalization_review_events').fetchone()[0] == 0


def test_overleaf_requires_current_bound_source_at_submission(tmp_path, monkeypatch):
    from app import overleaf_source
    from app.source_context import source_hash
    from test_lea_status import bundle
    _, target, run, _ = setup_target(tmp_path, monkeypatch)
    source = bundle()
    source['targetKey'] = 'claim'
    source['bundleHash'] = source_hash(source)
    source['sourceIdentityHash'] = source_hash(source, identity=True)
    with db.connect() as conn:
        conn.execute("update formalizations set origin='overleaf', origin_key='doc:theorem:claim', source_hash=? where id=?",
            (source['sourceIdentityHash'], target['id']))
        conn.execute('update runs set focus_source_hash=? where id=?', (source['sourceIdentityHash'], run['id']))
        overleaf_source.bind(conn, dict(conn.execute('select * from formalizations where id=?', (target['id'],)).fetchone()), source)
    ref = publish_for(target['id'],source)
    ctx = reviews.context(target['id'], ref)
    assert ctx['source_snapshot']['statement'] == source['statement']
    request = dict(action='approve', outcome='proved', revision_token=ctx['review']['revision_token'], idempotency_key='overleaf')
    with pytest.raises(HTTPException) as exc:
        reviews.decide(target['id'], request)
    assert exc.value.status_code == 409
    with db.connect() as conn:
        conn.execute("update source_publishers set lease_until='2000' where document_id='doc'")
    with pytest.raises(HTTPException) as exc:
        reviews.decide(target['id'], {**request, 'source_context_ref': ref})
    assert exc.value.status_code == 409
    ref = publish_for(target['id'],source)
    assert reviews.decide(target['id'], {**request, 'source_context_ref': ref})['formalization']['validity_status'] == 'proved'
