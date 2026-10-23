"""A completed proof stays current while other Formalize all targets run."""
import json
from pathlib import Path

import pytest

from app import db, store, target_observations
from app.queries import core, proofs
from test_core_projections import fixture


def running_target(x, name='Other'):
    form = store.create_formalization(project_id=x['project']['id'], loose_session_id=None,
        display_title=name, declaration_name=name.lower())
    session = store.create_session(name, project_id=x['project']['id'])
    store.link_session_formalization(session['id'], form['id'])
    store.link_formalization_file(form['id'], name + '.lean', role='primary')
    run = store.create_run(session['id'], 'mock', None, 3, focus_formalization_id=form['id'])
    store.update_run(run['id'], 'running')
    return form, session, run


def test_completed_target_publishes_without_finishing_project_backfill_or_other_run(fixture, monkeypatch):
    x = fixture
    other, session, run = running_target(x)
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    # There has never been a completed project observation.
    assert core.metadata(x['scope'])['state'] == 'synchronizing'
    for i in range(3):
        (x['path'].parent / 'Other.lean').write_text(f'-- unrelated on-disk edit {i}\n')
        store.add_code_step(session['id'], run['id'], 'Other.lean',
            content=f'theorem other : True := by\n  -- edit {i}\n  trivial', formalization_id=other['id'])
        store.update_run(run['id'], 'running', result_detail=f'Working {i}')
        target_observations.maintain()
        assert proofs.get(x['fid'])['validity_status'] == 'proved'
        assert proofs.get(x['fid'])['projection']['state'] == 'observed'
        assert proofs.current_snapshot(x['fid'])['files'][0]['check_current']
        target = proofs.target_status(x['project'], 'result')['targets'][0]
        assert target['check_current'] and target['declaration_present']
        assert target['projection']['state'] == 'observed'
    assert store.get_run(run['id'])['status'] == 'running'
    def denied(*args, **kwargs):
        pytest.fail('A target GET attempted to observe the filesystem')
    monkeypatch.setattr(Path, 'read_bytes', denied)
    monkeypatch.setattr(Path, 'read_text', denied)
    monkeypatch.setattr('app.read_projections.ReadSnapshot', denied)
    assert proofs.get(x['fid'])['check_current']
    assert proofs.target_status(x['project'], 'result')['targets'][0]['check_current']


@pytest.mark.parametrize('change', ['statement', 'code', 'evidence', 'blob'])
def test_own_inputs_invalidate_immediately_without_promoting_retained_history(fixture, change):
    x = fixture
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    if change == 'statement':
        store.update_formalization(x['fid'], statement='Changed contract')
    elif change == 'code':
        store.add_code_step(x['session']['id'], x['run']['id'], 'Proof.lean',
            content='theorem result : True := by sorry', formalization_id=x['fid'])
    else:
        with db.write() as conn:
            if change == 'evidence':
                conn.execute("update proof_check_evidence set evidence_json='{}' where id=?", (x['step']['check_evidence_id'],))
            else:
                conn.execute("update artifact_blobs set content='corrupt' where id=?", (x['step']['blob_id'],))
    item = proofs.get(x['fid'])
    assert item['projection']['state'] == 'inputs_changed'
    assert not item['check_current'] and item['certification'] is None
    assert not item['review']['eligible']


def imported_chain(x):
    """A imports B, which imports C; compiler evidence binds all three files."""
    from app import proof_evidence
    from evidence_fixtures import mock_execution
    from lea.events import CheckResult
    from lea.check_evidence import environment_hash, digest
    dependencies = []
    for name, code in [('C', 'theorem c : True := by trivial\n'),
                       ('B', 'import Lea.Observed.C\ntheorem b : True := by trivial\n')]:
        form, session, run = running_target(x, name)
        path = x['path'].parent / (name + '.lean')
        path.write_text(code)
        store.add_code_step(session['id'], run['id'], path.name, content=code, formalization_id=form['id'])
        with db.write() as conn:
            conn.execute('''insert into artifacts(id,scope,declaration_name,kind,path,module_name,formalization_id,created_at,updated_at)
                values(?,?,?,?,?,?,?,?,?)''', (name, x['project']['id'], name.lower(), 'proof', path.name,
                'Lea.Observed.' + name, form['id'], db.utc_now(), db.utc_now()))
        dependencies.append((form, session, run, path))
    x['path'].write_text('import Lea.Observed.B\ntheorem result : True := by trivial\n')
    store.add_code_step(x['session']['id'], x['run']['id'], 'Proof.lean',
                       content=x['path'].read_text(), formalization_id=x['fid'])
    execution = mock_execution(x['path'])
    execution['environment']['files'] = {p.name: {'path': str(p), 'sha256': digest(p.read_bytes()), 'kind': 'source'}
        for _, _, _, p in dependencies}
    execution['environment_sha256'] = environment_hash(execution['environment'])
    assert proof_evidence.record_check(x['session']['id'], 'Proof.lean', x['path'],
        CheckResult(str(x['path']), 'ok', execution=execution, content=x['path'].read_text()),
        run_id=x['run']['id'], formalization_id=x['fid'])[0]
    return dependencies


@pytest.mark.parametrize('dependency', [0, 1])
def test_transitive_dependency_sql_changes_invalidate_consumers_but_unrelated_code_does_not(fixture, dependency):
    x = fixture
    chain = imported_chain(x)
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    item = proofs.get(x['fid'])
    assert item['check_current'], (item['projection'], item['validity_status'], item['validity_reason'])
    other, session, run = running_target(x)
    store.add_code_step(session['id'], run['id'], 'Other.lean', content='-- unrelated', formalization_id=other['id'])
    assert proofs.get(x['fid'])['check_current']
    form, session, run, path = chain[dependency]
    store.add_code_step(session['id'], run['id'], path.name, content='-- changed import', formalization_id=form['id'])
    assert not proofs.get(x['fid'])['check_current']
    assert proofs.get(x['fid'])['projection']['state'] == 'inputs_changed'


@pytest.mark.parametrize('change', ['dependency', 'configuration'])
def test_worker_tracks_import_files_independently_but_shared_config_invalidates_both(fixture, change):
    x = fixture
    chain = imported_chain(x)
    from app import proof_evidence
    from evidence_fixtures import mock_execution
    from lea.events import CheckResult
    other, session, run = running_target(x, 'Independent')
    path = x['path'].parent / 'Independent.lean'
    path.write_text('theorem independent : True := by trivial\n')
    store.add_code_step(session['id'], run['id'], path.name, content=path.read_text(), formalization_id=other['id'])
    assert proof_evidence.record_check(session['id'], path.name, path,
        CheckResult(str(path), 'ok', execution=mock_execution(path), content=path.read_text()),
        run_id=run['id'], formalization_id=other['id'])[0]
    store.update_run(run['id'], 'proved', result_kind='proved', result_detail='PROVED')
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    assert target_observations.capture(other['id'])['state'] == 'observed'
    if change == 'dependency':
        chain[0][3].write_text('theorem c : True := by sorry\n')
    else:
        (x['root'] / 'workspace/lakefile.toml').write_text('name="changed"\n')
    for _ in range(10):
        target_observations.maintain()
    assert not proofs.get(x['fid'])['check_current']
    assert proofs.get(other['id'])['check_current'] is (change == 'dependency')


@pytest.mark.parametrize('related', [False, True])
def test_sql_changes_during_publication_retry_only_the_affected_target(fixture, monkeypatch, related):
    x = fixture
    other, session, run = running_target(x)
    from app.read_projections import ReadSnapshot
    unchanged = ReadSnapshot.unchanged
    raced = False
    def mutate(snapshot):
        nonlocal raced
        if not raced and db._snapshot_connection.get() is None and db._write_connection.get() is None:
            raced = True
            if related:
                store.update_formalization(x['fid'], source_hash='new-source')
            else:
                store.add_code_step(session['id'], run['id'], 'Other.lean', content='-- concurrent', formalization_id=other['id'])
        return unchanged(snapshot)
    monkeypatch.setattr(ReadSnapshot, 'unchanged', mutate)
    assert target_observations.capture(x['fid'])['state'] == ('retry' if related else 'observed')
    assert raced
    assert proofs.get(x['fid'])['check_current'] is (not related)


def test_unstable_target_does_not_starve_completed_one(fixture, monkeypatch):
    x = fixture
    other, _, _ = running_target(x)
    capture = target_observations.capture
    monkeypatch.setattr(target_observations, 'capture',
        lambda fid: {'state': 'retry'} if fid == other['id'] else capture(fid))
    for _ in range(4):
        target_observations.maintain()
    assert proofs.get(x['fid'])['check_current']


def test_completion_publication_does_not_wait_for_inventory_or_other_running_targets(fixture):
    x = fixture
    _, _, other_run = running_target(x)
    store.update_run(x['run']['id'], 'running')
    assert target_observations.publish_run(x['run']['id'])['state'] == 'waiting'
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    store.update_run(x['run']['id'], 'proved', result_kind='proved')
    assert not proofs.get(x['fid'])['check_current']
    assert target_observations.publish_run(x['run']['id'])['state'] == 'observed'
    item = proofs.get(x['fid'])
    assert item['check_current'] and item['completion_run_id'] == x['run']['id']
    assert core.metadata(x['scope'])['state'] == 'synchronizing'
    assert store.get_run(other_run['id'])['status'] == 'running'


def test_new_completion_has_priority_over_old_inventory_even_outside_scan_limit(fixture):
    x = fixture
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    for i in range(21):
        running_target(x, f'Running{i}')
    store.update_run(x['run']['id'], 'proved', result_kind='proved', final_text='Newly completed')
    assert target_observations.maintain(limit=1)['formalization_id'] == x['fid']
    assert proofs.get(x['fid'])['check_current']
    assert proofs.get(x['fid'])['validity_status'] == 'proved'


def test_independent_target_loop_publishes_while_broad_maintenance_is_blocked(fixture, monkeypatch):
    from threading import Event
    from time import monotonic
    from app import observation_maintenance
    x = fixture
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    entered, release = Event(), Event()
    def slow_inventory():
        entered.set()
        release.wait(10)
        return {}
    monkeypatch.setattr(observation_maintenance, 'sweep', slow_inventory)
    observation_maintenance.start()
    try:
        assert entered.wait(2)
        store.update_run(x['run']['id'], 'proved', result_kind='proved', final_text='Completion after broad scan began')
        deadline = monotonic() + 5
        while not proofs.get(x['fid'])['check_current'] and monotonic() < deadline:
            release.wait(0.02)
        assert proofs.get(x['fid'])['check_current']
        assert proofs.get(x['fid'])['validity_status'] == 'proved'
        assert not release.is_set()
    finally:
        release.set()
        observation_maintenance.close()


def test_completion_publication_respects_disabled_scope(fixture):
    x = fixture
    with db.write() as conn:
        conn.execute('update core_projection_enrollment set enabled=0 where scope=?', (x['scope'],))
    assert target_observations.publish_run(x['run']['id'])['state'] == 'disabled'


def test_shared_library_observation_reads_only_its_module_and_bindings(fixture):
    x = fixture
    from app import project_library
    with db.read_snapshot() as conn:
        with db.capture_read_dependencies() as statements:
            project_library.entries(x['project'], repo=x['path'].parent, path='Proof.lean')
        queries = [(sql, target_observations._digest(conn, sql)) for sql in statements]
    running_target(x)
    with db.read_snapshot() as conn:
        assert target_observations._matches(conn, queries)
    store.update_formalization(x['fid'], statement='Changed dependency contract')
    with db.read_snapshot() as conn:
        assert not target_observations._matches(conn, queries)


def test_target_expiry_and_scope_changes_fail_closed(fixture):
    x = fixture
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    with db.write() as conn:
        conn.execute("update core_target_observations set expires_at='2000-01-01T00:00:00+00:00'")
    assert not proofs.get(x['fid'])['check_current']
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    other = store.create_project('Moved')
    with db.write() as conn:
        conn.execute('update sessions set project_id=? where id=?', (other['id'], x['session']['id']))
    assert not proofs.get(x['fid'])['check_current']


def test_unrelated_target_in_the_same_conversation_does_not_invalidate_completed_proof(fixture):
    x = fixture
    other = store.create_formalization(project_id=x['project']['id'], loose_session_id=None,
                                      display_title='Other', declaration_name='other')
    store.link_session_formalization(x['session']['id'], other['id'])
    store.link_formalization_file(other['id'], 'Other.lean', 'primary')
    run = store.create_run(x['session']['id'], 'mock', None, 3, focus_formalization_id=other['id'])
    store.update_run(run['id'], 'running')
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    store.add_code_step(x['session']['id'], run['id'], 'Other.lean', content='-- other work', formalization_id=other['id'])
    store.add_message(x['session']['id'], 'assistant', 'Working on another theorem', run_id=run['id'], formalization_id=other['id'])
    assert proofs.get(x['fid'])['check_current']


def test_target_publication_notifies_the_project_but_scanning_does_not_create_feedback(fixture):
    x = fixture
    from app import observation
    with db.connect() as conn:
        before = observation.watermark(conn)
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    changed = observation.changes(before, project_id=x['project']['id'], formalization_id=x['fid'])
    assert any(row['entity_type'] == 'core_target_observations' for row in changed['changes'])
    target_observations.maintain()
    assert not observation.changes(changed['cursor'], formalization_id=x['fid'])['changes']


def test_disabled_enrollment_does_not_reuse_a_target_certificate(fixture):
    x = fixture
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    with db.write() as conn:
        conn.execute('update core_projection_enrollment set enabled=0 where scope=?', (x['scope'],))
    assert not proofs.get(x['fid'])['check_current']


@pytest.mark.parametrize('related', [False, True])
def test_publication_conflicts_block_only_the_affected_files(fixture, related):
    x = fixture
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    path = x['path'] if related else x['path'].with_name('Unrelated.lean')
    with db.write() as conn:
        conn.execute('insert into workspace_effects values(?,?,?,?,?,?,?,?,?)',
                     ('effect', 'command', x['project']['id'], None, 'file', 'conflict',
                      json.dumps({'absolute': str(path)}), db.utc_now(), db.utc_now()))
    assert proofs.get(x['fid'])['check_current'] is (not related)
    assert target_observations.capture(x['fid'])['state'] == ('retry' if related else 'observed')


def test_real_sql_api_process_reads_current_target_while_workspace_access_is_denied(fixture, tmp_path):
    import os
    from test_process_boundary import port, start, stop, wait, request, ADAPTER
    x = fixture
    running_target(x)
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    api_port = port(); base = f'http://127.0.0.1:{api_port}'
    env = {**os.environ, 'LEA_DATABASE_FILE': str(db.DB_PATH),
           'LEA_API_DENY_PATHS': str(x['root']), 'LEA_MODEL_CATALOG_OFFLINE': 'true',
           'LITELLM_LOCAL_MODEL_COST_MAP': 'True'}
    child = start(tmp_path, 'target-api', [ADAPTER / 'run_api.py', '--port', api_port, '--log-level', 'error'], env)
    try:
        target = wait(lambda: request(base, '/api/projects/by-slug/observed/target-status?declarations=result'))['targets'][0]
        assert target['check_current'] and target['projection']['state'] == 'observed'
        snapshot = request(base, '/api/formalizations/' + x['fid'] + '/current')
        assert snapshot['files'][0]['check_current'] and snapshot['projection']['state'] == 'observed'
    finally:
        stop(child)
