"""Real Lean cache reuse, independent writable copies, and input invalidation."""
from pathlib import Path
import os
import time

import pytest

from app import reformalizations as ops, reformalization_cache as cache, store
from lea import execution, interface, result_selection
from lea.check_runtime import CheckBudget
from test_reformalization_operations import project, operation, selection


def retire(op):
    store.update_run(op['root_run_id'], 'failed')
    ops.update(op['id'], 'retained')


def test_warm_retry_uses_validated_baseline_and_lake_outputs_without_shared_inodes(project, monkeypatch):
    started = time.monotonic()
    first = operation(project, monkeypatch)
    cold = time.monotonic() - started
    retire(first)
    def forbidden(*args, **kwargs):
        raise AssertionError('An unchanged warm baseline must not rerun the compiler/inspector')
    with monkeypatch.context() as m:
        m.setattr(interface, 'check', forbidden)
        m.setattr(result_selection, 'inspect_file', forbidden)
        started = time.monotonic()
        second = operation(project, monkeypatch)
        warm = time.monotonic() - started
    assert second['manifest']['preparation']['cache_hits'] == ['proofs/Lea/Study/Shared.lean']
    assert second['manifest']['preparation']['cache_misses'] == []
    assert second['manifest']['checks']['Shared.lean']['status'] == 'ok'
    assert second['manifest']['workspace'] != first['manifest']['workspace']
    root = Path(second['manifest']['workspace'])
    source = root / 'proofs/Lea/Study/Shared.lean'
    canonical = project[1] / 'Shared.lean'
    artifact = root / '.lake/build/lib/lean/Lea/Study/Shared.olean'
    original_artifact = Path(second['manifest']['canonical_root']) / artifact.relative_to(root)
    assert artifact.read_bytes() == original_artifact.read_bytes()
    assert artifact.stat().st_ino != original_artifact.stat().st_ino
    assert source.stat().st_ino != canonical.stat().st_ino
    with execution.scope(ops.workspace_policy(root)):
        built = execution.run(['lake', 'build', 'Lea.Study.Shared'], cwd=root,
                              capture_output=True, text=True, timeout=60)
    assert built.returncode == 0, built.stdout + built.stderr
    assert 'Built Lea.Study.Shared' not in built.stdout + built.stderr
    artifact.write_bytes(b'private attempted output')
    source.write_text('-- private draft\n')
    assert original_artifact.read_bytes() != artifact.read_bytes()
    assert canonical.read_text() == project[3]
    first_artifact = Path(first['manifest']['workspace']) / artifact.relative_to(root)
    assert first_artifact.read_bytes() == original_artifact.read_bytes()
    print(f'PREPARATION_CACHE cold={cold:.3f}s warm={warm:.3f}s')


@pytest.mark.parametrize('change', ['source', 'import', 'config', 'toolchain', 'artifact', 'sidecar',
                                  'missing', 'inspector', 'new_module', 'alias', 'native', 'corrupt'])
def test_changed_inputs_require_actual_revalidation(project, monkeypatch, change):
    first = operation(project, monkeypatch)
    retire(first)
    root, repo = Path(first['manifest']['canonical_root']), project[1]
    if change == 'source':
        (repo / 'Shared.lean').write_text(project[3] + '-- revised source\n')
    elif change == 'import':
        (repo / 'Dependency.lean').write_text('namespace Lea.Study\ndef dependency := 0\nend Lea.Study\n')
        (repo / 'Shared.lean').write_text('import Lea.Study.Dependency\n' + project[3])
    elif change == 'config':
        with (root / 'lakefile.toml').open('a') as f:
            f.write('\n# changed configuration\n')
    elif change == 'toolchain':
        with (root / 'lean-toolchain').open('a') as f:
            f.write('\n')
    elif change == 'artifact':
        (root / '.lake/build/lib/lean/Lea/Study/Shared.olean').write_bytes(b'corrupted')
    elif change == 'sidecar':
        (root / '.lake/build/lib/lean/Lea/Study/Shared.olean.future-format').write_bytes(b'new')
    elif change == 'missing':
        (root / '.lake/build/lib/lean/Lea/Study/Shared.ilean').unlink()
    elif change == 'new_module':
        (repo / 'Shadow.lean').write_text('namespace Lea.Study\ndef shadow := 0\nend Lea.Study\n')
    elif change == 'alias':
        (root / 'proofs/Shadow').symlink_to(repo, target_is_directory=True)
    elif change == 'native':
        directory = root / '.lake/packages/library'
        directory.mkdir(parents=True)
        (directory / 'plugin.ir').write_bytes(b'new native build input')
    elif change == 'inspector':
        original = cache._implementation
        monkeypatch.setattr(cache, '_implementation', lambda: {**original(), 'changed': 'inspector'})
    else:
        next((cache.db.DB_PATH.parent / 'reformalization-cache').glob('*.json')).write_text('{broken')
    baseline = cache.Baseline(project[0], root, repo, CheckBudget())
    called = []
    def checked(path, **kwargs):
        called.append(path)
        raise RuntimeError('Actual compiler required')
    monkeypatch.setattr(interface, 'check', checked)
    with pytest.raises(RuntimeError, match='Actual compiler required'):
        baseline.module(repo / 'Shared.lean')
    assert called == [str(repo / 'Shared.lean')]


def test_warm_retry_still_performs_candidate_and_canonical_checks(project, monkeypatch):
    from app import reformalization_apply as publication
    first = operation(project, monkeypatch)
    retire(first)
    second = operation(project, monkeypatch)
    staged = Path(second['manifest']['repo'])
    candidate = project[3].replace('theorem b : True := by sorry', 'theorem b : True := by trivial')
    (staged / 'Shared.lean').write_text(candidate)
    actual, calls = interface.check, []
    def checked(path, **kwargs):
        calls.append(str(path))
        return actual(path, **kwargs)
    monkeypatch.setattr(interface, 'check', checked)
    receipt = publication.apply(second['id'], selection(), 'proved')
    assert str(staged / 'Shared.lean') in calls
    assert str(project[1] / 'Shared.lean') in calls
    assert receipt['outcome'] == 'proved'
    assert {'Lea.Study.a', 'Lea.Study.c'} <= set(receipt['preservation']['protected'])
    assert (project[1] / 'Shared.lean').read_text() == candidate


def test_transitive_source_change_invalidates_consumer_but_not_independent_module(project, monkeypatch):
    repo = project[1]
    dependency = repo / 'Dependency.lean'
    dependency.write_text('namespace Lea.Study\ndef dependency := 0\nend Lea.Study\n')
    (repo / 'Shared.lean').write_text('import Lea.Study.Dependency\n' + project[3])
    independent = repo / 'Independent.lean'
    independent.write_text('namespace Lea.Study\ntheorem independent : True := by trivial\nend Lea.Study\n')
    first = operation(project, monkeypatch)
    retire(first)
    dependency.write_text(dependency.read_text().replace(':= 0', ':= 1'))
    baseline = cache.Baseline(project[0], Path(first['manifest']['canonical_root']), repo, CheckBudget())
    def forbidden(*args, **kwargs):
        raise RuntimeError('Actual compiler required')
    monkeypatch.setattr(interface, 'check', forbidden)
    assert baseline.module(independent)['check']['status'] == 'ok'
    assert baseline.hits == ['proofs/Lea/Study/Independent.lean']
    with pytest.raises(RuntimeError, match='Actual compiler required'):
        baseline.module(repo / 'Shared.lean')


def test_changed_and_restored_input_during_artifact_copy_is_rejected(project, monkeypatch):
    first = operation(project, monkeypatch)
    retire(first)
    copy = cache.shutil.copy2
    changed = []
    source = project[1] / 'Shared.lean'
    def racing(src, dest, **kwargs):
        result = copy(src, dest, **kwargs)
        if Path(src).suffix == '.olean' and not changed:
            changed.append(True)
            code = source.read_bytes()
            source.write_bytes(code + b'-- raced\n')
            source.write_bytes(code)
        return result
    monkeypatch.setattr(cache.shutil, 'copy2', racing)
    with pytest.raises(ValueError, match='Baseline changed'):
        operation(project, monkeypatch)
    assert changed
    assert source.read_text() == project[3]


def test_inspection_rejects_changed_and_restored_compiled_inputs(project, monkeypatch):
    first = operation(project, monkeypatch)
    artifact = Path(first['manifest']['canonical_root']) / '.lake/build/lib/lean/Lea/Study/Shared.olean'
    execute = execution.run
    changed = []
    def racing(args, **kwargs):
        result = execute(args, **kwargs)
        if any(str(a).endswith('/Inspect.lean') for a in args) and not changed:
            changed.append(True)
            code = artifact.read_bytes()
            artifact.write_bytes(code + b'raced')
            artifact.write_bytes(code)
        return result
    monkeypatch.setattr(execution, 'run', racing)
    root = Path(first['manifest']['canonical_root'])
    with execution.scope(ops.canonical_policy(root, project[1])), pytest.raises(ValueError, match='Compiled module changed'):
        result_selection.inspect_file(project[1] / 'Shared.lean')
    assert changed


def test_new_shadowing_module_during_copy_is_rejected(project, monkeypatch):
    first = operation(project, monkeypatch)
    retire(first)
    copy = cache.shutil.copy2
    changed = []
    def racing(src, dest, **kwargs):
        result = copy(src, dest, **kwargs)
        if Path(src).suffix == '.olean' and not changed:
            changed.append(True)
            outside = Path(first['manifest']['canonical_root']) / 'proofs/Shadow/New.lean'
            outside.parent.mkdir(parents=True)
            outside.write_text('def newly_shadowing := 0\n')
        return result
    monkeypatch.setattr(cache.shutil, 'copy2', racing)
    with pytest.raises(ValueError, match='resolution context changed'):
        operation(project, monkeypatch)
    assert changed
    assert (project[1] / 'Shared.lean').read_text() == project[3]


def test_warm_baseline_does_not_reuse_target_source_metadata(project, monkeypatch):
    first = operation(project, monkeypatch)
    retire(first)
    with cache.db.write() as conn:
        conn.execute('update formalizations set statement=? where id=?', ('False', project[2]['id']))
    second = operation(project, monkeypatch, source_update_evidence={
        'previous_statement': 'True', 'current_statement': 'False'})
    assert second['manifest']['preparation']['cache_misses'] == []
    assert second['manifest']['target_identity']['statement'] == 'False'
    assert second['manifest']['source_update']['current_statement'] == 'False'


@pytest.mark.parametrize('shadow_executable', [False, True])
def test_command_identity_tracks_resolved_tools_instead_of_unrelated_path_entries(project, monkeypatch, tmp_path, shadow_executable):
    first = operation(project, monkeypatch)
    commands = tmp_path / 'commands'
    commands.mkdir()
    if shadow_executable:
        executable = commands / 'lake'
        executable.write_text('#!/bin/sh\nexit 99\n')
        executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(commands) + os.pathsep + os.environ['PATH'])
    baseline = cache.Baseline(project[0], Path(first['manifest']['canonical_root']), project[1], CheckBudget())
    def forbidden(*args, **kwargs):
        raise RuntimeError('Actual compiler required')
    monkeypatch.setattr(interface, 'check', forbidden)
    if shadow_executable:
        with pytest.raises(RuntimeError, match='Actual compiler required'):
            baseline.module(project[1] / 'Shared.lean')
    else:
        assert baseline.module(project[1] / 'Shared.lean')['check']['status'] == 'ok'
        assert baseline.hits == ['proofs/Lea/Study/Shared.lean']


def test_warm_upstream_proof_rewrite_preserves_dependent_source(project, monkeypatch):
    from app import reformalization_apply as publication
    repo = project[1]
    completed = project[3].replace('theorem b : True := by sorry', 'theorem b : True := by trivial')
    (repo / 'Shared.lean').write_text(completed)
    consumer = ('import Lea.Study.Shared\nnamespace Lea.Study\n'
                '-- Keep this dependent proof.\ntheorem consumer : True := b\nend Lea.Study\n')
    (repo / 'Consumer.lean').write_text(consumer)
    first = operation(project, monkeypatch)
    retire(first)
    second = operation(project, monkeypatch)
    assert len(second['manifest']['preparation']['cache_hits']) == 2
    candidate = completed.replace('theorem b : True := by trivial', 'theorem b : True := by exact True.intro')
    (Path(second['manifest']['repo']) / 'Shared.lean').write_text(candidate)
    receipt = publication.apply(second['id'], selection(), 'proved')
    assert receipt['repair_needed'] == []
    assert 'Lea.Study.consumer' in receipt['preservation']['protected']
    assert (repo / 'Consumer.lean').read_text() == consumer
    assert (repo / 'Shared.lean').read_text() == candidate
