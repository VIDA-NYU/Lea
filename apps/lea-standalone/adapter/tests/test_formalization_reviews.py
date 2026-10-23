"""Lea Status replaces the retired human correspondence approval flow."""
import pytest
from fastapi import HTTPException
from app import db, store, formalizations, formalization_reviews

pytestmark = pytest.mark.usefixtures('certified_unit_inputs')


@pytest.fixture
def target(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'retired-review.sqlite3')
    db.init_db()
    session = store.create_session('Candidate')
    form = store.create_formalization(project_id=None, loose_session_id=session['id'],
        display_title='claim', declaration_name='claim', statement='Original statement.')
    store.link_session_formalization(session['id'], form['id'])
    store.link_formalization_file(form['id'], 'Claim.lean', 'primary')
    run = store.create_run(session['id'], 'test', None, 3, focus_formalization_id=form['id'])
    store.add_code_step(session['id'], run['id'], 'Claim.lean',
        content='theorem claim : True := by trivial\n', check_status='ok',
        artifact_kind='proof', formalization_id=form['id'])
    store.update_run(run['id'], 'needs_review', result_kind='needs_review', result_detail='NEEDS_REVIEW')
    return session, form, run


def test_saved_review_is_unconfirmed_and_never_prompts_for_approval(target):
    session, form, run = target
    current = formalizations.get(form['id'])
    assert current['validity_status'] == 'unchecked'
    assert current['check_current']
    assert current['review'] == dict(eligible=False, requested=False, reason='',
        revision_token=None, allowed_outcomes=[], approval=None)
    assert current['certification'] is None
    assert store.session_detail(session['id'])['status'] == 'unchecked'
    assert store.list_sessions()[0]['status'] == 'unchecked'
    assert store.get_run(run['id'])['result_kind'] == 'needs_review'  # history is retained


@pytest.mark.parametrize('action', ['approve', 'withdraw'])
def test_review_decisions_are_retired_without_changing_evidence(target, action):
    _, form, run = target
    before = store.get_run(run['id'])
    with pytest.raises(HTTPException) as error:
        formalization_reviews.decide(form['id'], dict(action=action, idempotency_key='old-client'))
    assert error.value.status_code == 410
    assert error.value.detail['code'] == 'formalization_review_retired'
    assert store.get_run(run['id']) == before
    with db.connect() as conn:
        assert conn.execute('select count(*) from formalization_review_events').fetchone()[0] == 0


def test_review_context_and_public_routes_are_retired(target):
    from app.api_routes import formalizations as public
    from app import core_query_dispatch
    _, form, _ = target
    for read in [formalization_reviews.context, public.review_context]:
        with pytest.raises(HTTPException) as error:
            read(form['id'])
        assert error.value.status_code == 410
    with pytest.raises(HTTPException) as error:
        public.review_decision(form['id'], public.ReviewDecision(action='approve', idempotency_key='old-client'))
    assert error.value.status_code == 410
    with pytest.raises(HTTPException) as error:
        core_query_dispatch._query('review', {'formalization_id': form['id']})
    assert error.value.status_code == 410


@pytest.mark.parametrize('result_kind', ['needs_review', 'proved', 'disproved'])
def test_compiler_failure_is_visible_without_a_review_flow(target, result_kind):
    session, form, run = target
    store.update_run(run['id'], result_kind, result_kind=result_kind, result_detail='The source omits a case.')
    store.add_code_step(session['id'], None, 'Claim.lean',
        content='theorem claim : False := by trivial\n', check_status='error',
        check_detail='type mismatch', artifact_kind='proof', formalization_id=form['id'])
    current = formalizations.get(form['id'])
    assert current['validity_status'] == 'failing'
    assert not current['review']['eligible']
    assert current['certification'] is None
