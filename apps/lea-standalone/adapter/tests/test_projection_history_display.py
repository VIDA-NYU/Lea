"""Retained SQL history labels never promote uncertain proof evidence."""
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from app import db, store
from app.queries import core, proofs


@pytest.fixture
def historical(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.sqlite3')
    db.init_db()
    project = store.create_project('history-display')
    form = store.create_formalization(project_id=project['id'], loose_session_id=None,
                                      display_title='target', declaration_name='target', source_hash='source-a')
    scope = 'p:' + project['id']
    now = datetime.now(timezone.utc)
    recorded = {**form, 'validity_status': 'proved', 'check_current': True,
                'certification': {'must_not_be_current': True}, 'completion_run_id': 'old-run'}
    with db.write() as conn:
        revision = core.revision(conn, scope)
        conn.execute('insert into core_observations values(?,?,?,?,?,?,?,?,?)',
                     ('recorded', scope, revision - 1, 0, now.isoformat(),
                      (now + timedelta(minutes=2)).isoformat(), 'observed', 'old-environment', '{}'))
        conn.execute('insert into core_projection_enrollment(scope,enabled,observation_id) values(?,1,?)', (scope, 'recorded'))
        conn.execute('insert into core_projection_values values(?,?,?,?,?)',
                     ('recorded', scope, 'formalization', form['id'], json.dumps(recorded)))
    return project, form, scope, recorded


@pytest.mark.parametrize('result', ['proved', 'defined', 'disproved'])
def test_retained_result_is_labelled_history_without_current_certification(historical, monkeypatch, result):
    project, form, scope, recorded = historical
    with db.write() as conn:
        conn.execute('update core_projection_values set payload=?', (json.dumps({**recorded, 'validity_status': result}),))
    def denied(*args, **kwargs):
        pytest.fail('Displaying historical status accessed the workspace')
    monkeypatch.setattr(Path, 'read_bytes', denied)
    monkeypatch.setattr(Path, 'read_text', denied)
    item = proofs.get(form['id'])
    assert item['projection']['state'] == 'inputs_changed'
    assert item['last_observed_result']['validity_status'] == result
    assert item['last_observed_result']['source_hash'] == 'source-a'
    assert item['last_observed_result']['observed_at']
    assert item['check_current'] is False
    assert item['certification'] is None
    assert item['completion_run_id'] is None
    assert item['review']['eligible'] is False
    assert core.fact(scope, 'formalization', form['id']) is None
    target = proofs.target_status(project, 'target')['targets'][0]
    assert target['last_observed_result'] == item['last_observed_result']
    assert target['check_current'] is False


@pytest.mark.parametrize('change', ['source', 'unchecked', 'disabled', 'missing'])
def test_changed_or_unverified_source_does_not_keep_a_success_label(historical, change):
    _, form, scope, recorded = historical
    with db.write() as conn:
        if change == 'source':
            conn.execute('update formalizations set source_hash=? where id=?', ('source-b', form['id']))
        elif change == 'unchecked':
            conn.execute('update core_projection_values set payload=?', (json.dumps({**recorded, 'check_current': False}),))
        elif change == 'disabled':
            conn.execute('update core_projection_enrollment set enabled=0')
        else:
            conn.execute('delete from core_projection_values')
    assert proofs.get(form['id'])['last_observed_result'] is None


def test_expired_observation_keeps_only_the_historical_label(historical):
    _, form, scope, _ = historical
    with db.write() as conn:
        conn.execute("update core_observations set input_revision=?,expires_at='2000-01-01T00:00:00+00:00'", (core.revision(conn, scope),))
    item = proofs.get(form['id'])
    assert item['projection']['state'] == 'observation_expired'
    assert item['last_observed_result']['validity_status'] == 'proved'
    assert item['check_current'] is False
    assert item['review']['eligible'] is False


@pytest.mark.parametrize('state', ['planned', 'draft'])
def test_known_unformalized_state_survives_scope_refresh_expiry_and_source_edit(historical,state):
    project,form,scope,recorded=historical
    empty={**recorded,'validity_status':state,'check_current':False,'files':[],
           'artifact':None,'latest_run':None,'certification':None,'completion_run_id':None}
    with db.write() as conn:
        conn.execute('update core_projection_values set payload=?',(json.dumps(empty),))
    for phase in ['inputs_changed','observation_expired','inputs_changed','source_changed']:
        with db.write() as conn:
            if phase=='observation_expired':
                conn.execute("update core_observations set input_revision=?,expires_at='2000-01-01T00:00:00+00:00'",(core.revision(conn,scope),))
            else:
                conn.execute('update core_scope_revisions set revision=revision+1 where scope=?',(scope,))
            if phase=='source_changed':
                conn.execute('update formalizations set source_hash=? where id=?',('edited-unformalized-statement',form['id']))
        item=proofs.get(form['id'])
        assert item['last_observed_result']['validity_status']==state
        assert not item['check_current'] and not item['review']['eligible']
        assert proofs.target_status(project,'target')['targets'][0]['last_observed_result']['validity_status']==state


@pytest.mark.parametrize('event',['run_started','file_linked','artifact_registered'])
def test_known_empty_state_is_not_retained_over_actual_target_work(historical,event):
    project,form,_,recorded=historical
    with db.write() as conn:
        conn.execute('update core_projection_values set payload=?',(json.dumps({**recorded,
            'validity_status':'planned','check_current':False,'files':[],'artifact':None,'latest_run':None}),))
    if event=='run_started':
        session=store.create_session('Actual work',project_id=project['id'])
        store.create_run(session['id'],'test',None,3,focus_formalization_id=form['id'])
    elif event=='file_linked':
        store.link_formalization_file(form['id'],'Target.lean','primary')
    else:
        with db.write() as conn:
            conn.execute('insert into artifacts(scope,declaration_name,kind,path,created_at,updated_at) values(?,?,?,?,?,?)',
                (project['id'],'target','proof','Target.lean',db.utc_now(),db.utc_now()))
    assert proofs.get(form['id'])['last_observed_result'] is None
