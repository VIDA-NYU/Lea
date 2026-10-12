"""Compiler input integrity, including real import preparation and warm caches."""
import json
from pathlib import Path
from queue import Queue
import shutil
import threading
import time
from types import SimpleNamespace

import pytest
from lea import interface, lsp_daemon
from lea.check_evidence import Coordinator, digest, freshness
from lea.check_runtime import CheckBudget, CheckFailure, CheckText, restore_result, serialize_result
from app import db, store
from app.proof_evidence import record_check

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / 'workspace'
    (root / 'proofs/Fixture').mkdir(parents=True)
    (root / 'lakefile.toml').write_text('name = "audit"\nversion = "0.1.0"\n[[lean_lib]]\nname = "Fixture"\nsrcDir = "proofs"\n')
    shutil.copyfile(ROOT / 'apps/lea-standalone/prover/workspace/lean-toolchain', root / 'lean-toolchain')
    yield root
    lsp_daemon._shutdown_all()


def test_new_imports_are_built_and_transitive_changes_are_detected(workspace):
    base = workspace / 'proofs/Fixture/Base.lean'
    middle = workspace / 'proofs/Fixture/Middle.lean'
    target = workspace / 'proofs/Fixture/Target.lean'
    base.write_text('namespace Fixture\ntheorem base : True := by trivial\nend Fixture\n')
    middle.write_text('import Fixture.Base\nnamespace Fixture\ntheorem middle : True := base\nend Fixture\n')
    target.write_text('import Fixture.Middle\nnamespace Fixture\ntheorem target : True := middle\nend Fixture\n')
    checked = interface.check(str(target))
    assert checked.status == 'ok', checked.detail
    assert freshness(checked.execution, target) == (True, None)
    (workspace / 'proofs/Fixture/Unrelated.lean').write_text('theorem unrelated : True := by trivial\n')
    assert freshness(checked.execution, target) == (True, None)
    base.write_text(base.read_text().replace('theorem base', 'theorem renamed'))
    assert freshness(checked.execution, target) == (False, 'dependency_changed')
    failed = interface.check(str(target))
    assert failed.status == 'error'
    assert failed.execution['failure']['kind'] == 'dependency_build'


def test_unchanged_warm_check_reuses_environment(workspace, monkeypatch):
    from lea import check_evidence
    base = workspace / 'proofs/Fixture/Base.lean'
    target = workspace / 'proofs/Fixture/Target.lean'
    base.write_text('namespace Fixture\ntheorem base : True := by trivial\nend Fixture\n')
    target.write_text('import Fixture.Base\nnamespace Fixture\ntheorem target : True := base\nend Fixture\n')
    first = interface.check(str(target))
    assert first.status == 'ok', first.detail
    original = check_evidence._command
    calls = []
    def command(args, *a, **kw):
        calls.append(args)
        return original(args, *a, **kw)
    monkeypatch.setattr(check_evidence, '_command', command)
    second = interface.check(str(target))
    assert second.status == 'ok'
    assert second.execution['daemon_generation'] == first.execution['daemon_generation']
    assert not calls


def test_checkpoint_preserves_structured_evidence():
    result = CheckText('OK', {'check_id': 'check', 'content_sha256': 'hash'}, 'theorem t : True := by trivial')
    restored = restore_result(json.loads(json.dumps(serialize_result(result))))
    assert restored.execution == result.execution
    assert restored.captured_content == result.captured_content
    assert getattr(restore_result('OK'), 'execution', None) is None


def test_legacy_rows_remain_unchecked(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.sqlite3')
    db.init_db()
    session = store.create_session('legacy')
    step = store.add_code_step(session['id'], None, 'Target.lean', content='theorem t : True := by trivial', check_status='ok', artifact_kind='proof')
    assert step['check_status'] == 'ok'
    assert step['check_current'] is False
    assert store.session_detail(session['id'])['status'] == 'unchecked'
    assert store.list_sessions()[0]['status'] == 'unchecked'


def test_workspace_leases_allow_readers_and_cancel_waiting_writer():
    coordinator = Coordinator()
    outcomes = Queue()
    def write():
        try:
            with coordinator.lease(exclusive=True, budget=CheckBudget(timeout=.05)):
                outcomes.put('unexpected writer')
        except CheckFailure as exc:
            outcomes.put(exc.kind)
    with coordinator.lease():
        with coordinator.lease():
            worker = threading.Thread(target=write)
            worker.start()
            worker.join(timeout=1)
            assert not worker.is_alive()
            assert outcomes.get_nowait() == 'queue_timeout'


@pytest.mark.parametrize('use_lsp', [False, True])
def test_completed_check_preserves_its_blob_after_overwrite(workspace, monkeypatch, use_lsp):
    from app import config
    from app.config import LeaConfig
    from lea.lean_checks import check_file
    monkeypatch.setattr(db, 'DB_PATH', workspace.parent / 'test.sqlite3')
    monkeypatch.setattr(config, 'load_config', lambda: LeaConfig(model='test', max_turns=3, lea_root=workspace.parent))
    db.init_db()
    session = store.create_session('immutable check')
    path = workspace / 'proofs' / session['id'] / 'Target.lean'
    path.parent.mkdir()
    good = 'theorem target : True := by trivial\n'
    bad = 'theorem target : False := by trivial\n'
    path.write_text(good)
    original = store.add_code_step(session['id'], None, path.name, content=good)
    checked = check_file(path, str(workspace), use_lsp=use_lsp)
    assert checked.status == 'ok', checked.output
    result = interface.CheckResult(str(path), checked.status, execution=checked.execution,
                                   content=checked.captured_content)
    path.write_text(bad)
    replacement = store.add_code_step(session['id'], None, path.name, content=bad)
    accepted, historical = record_check(session['id'], path.name, path, result)
    assert accepted is False
    assert historical['id'] == original['id']
    assert historical['check_status'] == 'ok' and historical['check_current'] is False
    head = store.latest_code_step_for_path(session['id'], path.name)
    assert head['id'] == replacement['id'] and head['check_status'] is None
    with db.connect() as conn:
        row = conn.execute('select b.content from proof_check_evidence e join artifact_blobs b on b.id=e.blob_id').fetchone()
        assert row['content'] == good


def test_check_without_write_event_and_wrong_path_are_distinct(workspace, monkeypatch):
    from app import config
    from app.config import LeaConfig
    monkeypatch.setattr(db, 'DB_PATH', workspace.parent / 'test.sqlite3')
    monkeypatch.setattr(config, 'load_config', lambda: LeaConfig(model='test', max_turns=3, lea_root=workspace.parent))
    db.init_db()
    session = store.create_session('shell-created proof')
    path = workspace / 'proofs' / session['id'] / 'Target.lean'
    path.parent.mkdir()
    path.write_text('theorem target : True := by trivial\n')
    checked = interface.check(str(path))
    accepted, step = record_check(session['id'], path.name, path, checked)
    assert accepted and step['check_current']
    other = path.with_name('Other.lean')
    other.write_bytes(path.read_bytes())
    assert record_check(session['id'], other.name, other, checked) == (False, None)
    path.unlink()
    assert store.latest_code_step_for_path(session['id'], path.name)['check_current'] is False


def test_compiler_and_package_configuration_are_in_manifest(workspace):
    # Even implicit Init imports pin the actual Lean compiler installation.
    target = workspace / 'proofs/Fixture/Target.lean'
    target.write_text('theorem target : True := by trivial\n')
    checked = interface.check(str(target))
    assert checked.status == 'ok'
    assert any(record['kind'] == 'compiler' for record in checked.execution['environment']['files'].values())
    (workspace / 'lakefile.toml').write_text((workspace / 'lakefile.toml').read_text() + '\n# changed config\n')
    assert freshness(checked.execution, target) == (False, 'environment_changed')
    rechecked = interface.check(str(target))
    assert rechecked.status == 'ok'
    assert rechecked.execution['daemon_generation'] != checked.execution['daemon_generation']


def test_standalone_input_pins_implicit_dependencies(tmp_path):
    from lea.lean_checks import check_file
    target = tmp_path / 'Target.lean'
    target.write_text('theorem target : True := by trivial\n')
    checked = check_file(target, None, use_lsp=False)
    assert checked.status == 'ok', checked.output
    files = checked.execution['environment']['files']
    assert 'toolchain:src/lean/Init.lean' in files
    assert any(record['kind'] == 'compiler' for record in files.values())
    assert freshness(checked.execution, target) == (True, None)


def test_unicode_imports_are_traversed(workspace):
    target = workspace / 'proofs/Fixture/Target.lean'
    base = workspace / 'proofs/Fixture/α.lean'
    base.write_text('namespace Fixture\ntheorem alpha : True := by trivial\nend Fixture\n')
    target.write_text('import Fixture.α\nexample : True := Fixture.alpha\n')
    checked = interface.check(str(target))
    assert checked.status == 'ok', checked.detail
    base.write_text(base.read_text().replace('theorem alpha', 'theorem beta'))
    assert freshness(checked.execution, target) == (False, 'dependency_changed')


def test_changes_during_classification_prevent_terminal_success(workspace, monkeypatch):
    from lea import agent
    from lea.config import LeaConfig
    from lea.events import Finished
    from lea.providers import TextDelta, ToolCall, Done, Usage
    proof = workspace / 'proofs/Fixture/Target.lean'
    turns = 0
    def stream(model, system, messages, schemas, model_kwargs=None, streaming=True):
        nonlocal turns
        if 'classify the mathematical outcome' in system:
            proof.write_text('theorem target : False := by trivial\n')
            yield TextDelta('PROVED')
        else:
            turns += 1
            if turns == 1:
                yield ToolCall('write_file', {'path': str(proof), 'content': 'theorem target : True := by trivial\n'})
                yield ToolCall('lean_check', {'path': str(proof)})
            else:
                yield TextDelta('The proof is complete.')
        yield Done(Usage(), 0)
    monkeypatch.setattr(agent, 'stream', stream)
    events = list(agent.run_events(LeaConfig(model='test', max_turns=3,
        tools=['write_file', 'lean_check']), [{'role': 'user', 'content': 'Prove target.'}],
        working_dir=str(proof.parent)))
    final = next(event for event in reversed(events) if isinstance(event, Finished))
    assert final.result_kind == 'needs_review'
    assert 'changed' in final.result_detail
