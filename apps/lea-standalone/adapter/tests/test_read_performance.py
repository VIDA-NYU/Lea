"""Metadata reads must not pay for certification; status reads must stay strict."""
import json
from collections import Counter

import pytest
from fastapi import HTTPException

from app import db, projects, store
from app.read_projections import current_snapshot, decode_evidence, read_projection
from evidence_fixtures import mock_execution
from lea import check_evidence


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.sqlite3')
    db.init_db()


def test_identity_uses_history_existence_without_certification(database, monkeypatch):
    project = store.create_project('metadata')
    session = store.create_session('proof', project_id=project['id'])
    store.add_code_step(session['id'], None, 'Target.lean', content='theorem t : True := by trivial')
    def forbidden(*args, **kwargs):
        pytest.fail('identity metadata must not project proof freshness')
    monkeypatch.setattr(store, 'list_project_sessions', forbidden)
    assert projects.project_identity(project)['hasRecordedProofs'] is True


def test_accounting_projection_does_not_read_proofs(database, monkeypatch):
    session = store.create_session('usage')
    run = store.create_run(session['id'], 'prove', 'test', 3)
    store.update_run(run['id'], 'completed', input_tokens=7, output_tokens=5, cost_usd=.25)
    def forbidden(*args, **kwargs):
        pytest.fail('accounting must not project proof freshness')
    monkeypatch.setattr(store, 'list_sessions', forbidden)
    stats = store.usage_stats(include_proof_status=False)
    assert stats['global']['input_tokens'] == 7
    assert stats['sessions'][0]['cost_usd'] == .25
    assert 'status' not in stats['sessions'][0]


@pytest.mark.parametrize('exclusive', [False, True], ids=['lean-check', 'dependency-build'])
def test_inventory_target_sync_does_not_wait_for_compiler_lease(database, tmp_path, monkeypatch, exclusive):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from app.config import LeaConfig
    from app.routes import projects as routes

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'lakefile.toml').write_text('name = "inventory"\n')
    config = LeaConfig(model='test', max_turns=3, lea_root=tmp_path)
    monkeypatch.setattr(routes, 'load_config', lambda: config)
    monkeypatch.setattr('app.config.load_config', lambda: config)
    project = store.create_project('inventory')
    request = routes.FormalizationTargetSyncRequest(targets=[{
        'origin_key': 'inventory:theorem:target', 'kind': 'theorem', 'label': 'target', 'declaration_name': 'target',
        'display_title': 'Target', 'statement': 'True', 'source_hash': 'source-v1',
    }])
    entered, release = Event(), Event()

    def compiler():
        with check_evidence.coordinator(workspace).lease(exclusive=exclusive):
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        checking = pool.submit(compiler)
        pending = None
        try:
            assert entered.wait(2)
            pending = pool.submit(routes.sync_project_formalizations_by_slug.worker, project['slug'], request)
            try:
                result = pending.result(timeout=1)
            except TimeoutError:
                pytest.fail('Database-only inventory synchronization waited for the compiler lease')
            target = store.get_formalization(result['targets'][0]['formalization_id'])
            assert target['statement'] == 'True'
            assert target['source_hash'] == 'source-v1'
        finally:
            release.set()
            checking.result(timeout=2)
            if pending is not None:
                pending.result(timeout=2)


def test_proof_save_still_waits_for_compiler_lease(database, tmp_path):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from app import proof_files

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'lakefile.toml').write_text('name = "inventory"\n')
    path = workspace / 'Target.lean'
    original = 'theorem target : True := by trivial\n'
    replacement = 'theorem target : True := by exact True.intro\n'
    path.write_text(original)
    project = store.create_project('save')
    session = store.create_session('proof', project_id=project['id'])
    store.add_code_step(session['id'], None, path.name, content=original)
    entered, saving, release = Event(), Event(), Event()

    def compiler():
        with check_evidence.coordinator(workspace).lease():
            entered.set()
            assert release.wait(5)

    def save():
        saving.set()
        return proof_files.save(session['id'], path.name, path, replacement)

    with ThreadPoolExecutor(max_workers=2) as pool:
        checking = pool.submit(compiler)
        pending = None
        try:
            assert entered.wait(2)
            pending = pool.submit(save)
            assert saving.wait(2)
            with pytest.raises(TimeoutError):
                pending.result(timeout=.2)
            assert path.read_text() == original
            assert store.current_proof_step(session['id'], path.name)['code'] == original
        finally:
            release.set()
            checking.result(timeout=2)
            if pending is not None:
                result = pending.result(timeout=2)
    assert path.read_text() == replacement
    assert result['code_step']['code'] == replacement
    assert result['code_step']['check_status'] is None


def test_inventory_target_sync_retains_project_reservation(database, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app import project_coordination
    from app.routes import projects as routes

    project = store.create_project('reserved')
    request = routes.FormalizationTargetSyncRequest(targets=[])
    entered, release = Event(), Event()

    def sync(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'targets': []}

    monkeypatch.setattr(routes.github_import_service, 'sync_targets', sync)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(routes.sync_project_formalizations_by_slug.worker, project['slug'], request)
        try:
            assert entered.wait(2)
            with pytest.raises(HTTPException) as exc:
                with project_coordination.migration(project['id']):
                    pytest.fail('Namespace migration overlapped inventory synchronization')
            assert exc.value.status_code == 409
            assert exc.value.detail['error'] == 'project_busy'
        finally:
            release.set()
            pending.result(timeout=2)
    with project_coordination.migration(project['id']):
        assert project_coordination.snapshot(project['id'])['renameInProgress']


def execution_with_imports(tmp_path, count=100):
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'lakefile.toml').write_text('name = "projection"\n')
    target = root / 'Target.lean'
    target.write_text('theorem t : True := by trivial\n')
    execution = mock_execution(target)
    files = execution['environment']['files']
    for index in range(count):
        dep = root / f'Import{index}.lean'
        dep.write_text(f'-- dependency {index}\n')
        files[f'workspace:{dep.name}'] = {'path': str(dep), 'kind': 'source',
            'sha256': check_evidence.file_hash(dep), 'version': check_evidence.file_version(dep)}
    execution['environment_sha256'] = check_evidence.environment_hash(execution['environment'])
    return target, json.dumps(execution)


def test_large_shared_manifest_observed_once_per_response(tmp_path, monkeypatch):
    target, raw = execution_with_imports(tmp_path, 6500)
    counts = Counter()
    original = check_evidence.file_observation
    def observe(path):
        counts[str(path)] += 1
        return original(path)
    monkeypatch.setattr(check_evidence, 'file_observation', observe)
    @read_projection
    def project():
        return [check_evidence.freshness(decode_evidence(raw), target,
            snapshot=current_snapshot()) for _ in range(12)]
    assert project() == [(True, None)] * 12
    assert len(counts) == 6505  # target, four configuration candidates and all imports
    assert set(counts.values()) == {1}
    counts.clear()
    (target.parent / 'Import0.lean').write_text('-- changed\n')
    assert project() == [(False, 'dependency_changed')] * 12


@pytest.mark.parametrize('change', ['source', 'dependency'])
def test_assessment_snapshot_shares_observations_but_rechecks_next_read(database, tmp_path, monkeypatch, change):
    from pathlib import Path
    from app import artifact_snapshots, proof_evidence
    from lea.events import CheckResult

    target, raw = execution_with_imports(tmp_path)
    execution = json.loads(raw)
    monkeypatch.setattr(proof_evidence, 'canonical_path', lambda step, evidence, *_: Path(evidence['source_path']))
    project = store.create_project('assessment')
    session = store.create_session('proof', project_id=project['id'])
    form = store.create_formalization(project_id=project['id'], loose_session_id=None,
        display_title='t', declaration_name='t', kind='theorem')
    store.link_session_formalization(session['id'], form['id'])
    store.link_formalization_file(form['id'], target.name, 'primary')
    run = store.create_run(session['id'], 'test', None, 3, project_id=project['id'], focus_formalization_id=form['id'])
    store.add_code_step(session['id'], run['id'], target.name, content=target.read_text(), formalization_id=form['id'])
    result = CheckResult(str(target), 'ok', execution=execution, content=target.read_text())
    persisted, _ = proof_evidence.record_check(session['id'], target.name, target, result,
        run_id=run['id'], formalization_id=form['id'])
    assert persisted
    store.update_run(run['id'], 'proved', result_kind='proved')
    counts = Counter()
    observe = check_evidence.file_observation

    def observed(path):
        counts[str(path)] += 1
        return observe(path)

    monkeypatch.setattr(check_evidence, 'file_observation', observed)
    snapshot, steps = artifact_snapshots._snapshot(form['id'])
    assert snapshot['validity_status'] == 'proved'
    assert steps[0]['check_current']
    assert len(counts) == 105
    assert set(counts.values()) == {1}
    counts.clear()
    if change == 'source':
        target.write_text('theorem t : False := by trivial\n')
    else:
        (target.parent / 'Import0.lean').write_text('-- modified dependency\n')
    changed, steps = artifact_snapshots._snapshot(form['id'])
    assert changed['validity_status'] == 'unchecked'
    assert not steps[0]['check_current']
    assert steps[0]['check_status'] == 'ok'
    assert steps[0]['check_stale_reason'] == ('source_changed' if change == 'source' else 'dependency_changed')


@pytest.mark.parametrize('change', ['source', 'dependency', 'delete', 'replace', 'configuration'])
def test_change_during_projection_retries_before_publishing(tmp_path, change):
    target, raw = execution_with_imports(tmp_path, 2)
    attempts = []
    @read_projection
    def project():
        answer = check_evidence.freshness(decode_evidence(raw), target, snapshot=current_snapshot())
        attempts.append(answer)
        if len(attempts) == 1:
            dep = target.parent / 'Import1.lean'
            if change == 'source':
                target.write_text('theorem t : False := by trivial\n')
            elif change == 'dependency':
                dep.write_text('-- changed\n')
            elif change == 'delete':
                dep.unlink()
            elif change == 'replace':
                replacement = dep.with_suffix('.tmp')
                replacement.write_bytes(dep.read_bytes())
                replacement.replace(dep)
            else:
                (target.parent / 'lakefile.toml').write_text('name = "changed"\n')
        return answer
    assert project()[0] is (change == 'replace')
    assert attempts[0] == (True, None)
    assert len(attempts) == 2


@pytest.mark.parametrize('metadata_only', [False, True])
def test_continuous_changes_fail_closed_and_reset_context(tmp_path, metadata_only):
    target, raw = execution_with_imports(tmp_path, 1)
    @read_projection
    def project():
        check_evidence.freshness(decode_evidence(raw), target, snapshot=current_snapshot())
        target.write_text(target.read_text() + ('' if metadata_only else '\n'))
        return {'check_current': True}
    with pytest.raises(HTTPException) as exc:
        project()
    assert exc.value.status_code == 503
    assert current_snapshot() is None


def test_nearer_lake_root_created_during_read_is_detected(tmp_path):
    target, raw = execution_with_imports(tmp_path, 1)
    sub = target.parent / 'nested'
    sub.mkdir()
    target = target.rename(sub / target.name)
    execution = json.loads(raw)
    execution.update(source_identity='nested/Target.lean', source_path=str(target))
    attempts = []
    @read_projection
    def project():
        answer = check_evidence.freshness(execution, target, snapshot=current_snapshot())
        attempts.append(answer)
        if len(attempts) == 1:
            (sub / 'lakefile.toml').write_text('name = "nearer"\n')
        return answer
    assert project() == (False, 'source_changed')
    assert len(attempts) == 2


def test_accounting_matches_status_rows_and_is_not_truncated(database):
    project = store.create_project('accounting')
    for index in range(105):
        session = store.create_session(str(index), project_id=project['id'])
        run = store.create_run(session['id'], 'test', None, 3)
        store.update_run(run['id'], 'completed', input_tokens=2, output_tokens=3,
            cost_usd=.1, usage_status='partial' if index == 0 else 'final')
    lightweight = {row['id']: row for row in store.accounting_sessions()}
    assert len(lightweight) == 105
    for row in store.list_sessions():
        assert all(row[key] == lightweight[row['id']][key] for key in
            ('input_tokens','output_tokens','cost_usd','run_count','usage_details',
             'incomplete_usage_runs','unconfirmed_usage_runs'))
