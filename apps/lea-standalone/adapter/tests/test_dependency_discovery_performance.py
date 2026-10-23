"""Dependency discovery stays linear in modules, not import edges or directory size."""
from collections import Counter
from pathlib import Path

import pytest

from lea import check_evidence as evidence
from lea.check_runtime import CheckBudget, CheckFailure
from lea.runctx import run_context

REAL_RESOLVE = evidence._resolve


@pytest.fixture
def graph(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'lakefile.toml').write_text('name = "fixture"\n')
    source = root / '.lake/packages/library/Lib'
    compiled = root / '.lake/packages/library/.lake/build/lib/lean/Lib'
    source.mkdir(parents=True)
    compiled.mkdir(parents=True)
    for name, imports in {'Top': ['Left', 'Right'], 'Left': ['Common'],
                          'Right': ['Common'], 'Common': [], 'Added': []}.items():
        (source / (name + '.lean')).write_text(''.join('import Lib.' + dep + '\n' for dep in imports))
        (compiled / (name + '.olean')).write_text('compiled ' + name)
        (compiled / (name + '.olean.server')).write_text('server ' + name)
    target = root / 'Target.lean'
    target.write_text('import Lib.Top\n')
    def resolve(_path, code, *_args):
        return [(source / (name.split('.')[-1] + '.lean'), compiled / (name.split('.')[-1] + '.olean'))
                for name in evidence.direct_imports(code)]
    monkeypatch.setattr(evidence, '_resolve', resolve)
    monkeypatch.setattr(evidence, '_source_imports', {})
    return root, source, compiled, target


def test_import_diamonds_observe_each_input_once_and_scan_each_directory_once(graph, monkeypatch):
    root, source, compiled, target = graph
    observations, scans = Counter(), Counter()
    observe, listdir = evidence.file_observation, evidence.os.listdir
    def observed(path):
        observations[str(path)] += 1
        return observe(path)
    def listed(path):
        scans[str(path)] += 1
        return listdir(path)
    monkeypatch.setattr(evidence, 'file_observation', observed)
    monkeypatch.setattr(evidence.os, 'listdir', listed)
    manifest = evidence.manifest(target, target.read_text(), root)
    assert observations[str(source / 'Common.lean')] == 1
    assert observations[str(compiled / 'Common.olean.server')] == 1
    assert scans[str(compiled)] == 1
    assert len([r for r in manifest['files'].values() if r['kind'] == 'source']) == 4


def test_import_cache_reuses_checked_bytes_and_discovers_new_transitive_imports(graph, monkeypatch):
    root, source, compiled, target = graph
    reads = Counter()
    original = Path.read_bytes
    def read(path):
        reads[str(path)] += 1
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', read)
    evidence.manifest(target, target.read_text(), root)
    reads.clear()
    evidence.manifest(target, target.read_text(), root)
    assert not reads[str(source / 'Common.lean')]
    (source / 'Common.lean').write_text('import Lib.Added\n')
    current = evidence.manifest(target, target.read_text(), root)
    assert any(r['path'] == str(source / 'Added.lean') for r in current['files'].values())
    assert reads[str(source / 'Common.lean')]


def test_artifact_family_snapshot_detects_new_unknown_sidecars_without_rescanning_per_module(graph, monkeypatch):
    _, _, compiled, _ = graph
    paths = [str(compiled / (name + '.olean')) for name in ['Top', 'Left', 'Right', 'Common']]
    snapshot = evidence.ReadSnapshot()
    snapshot.artifact_sets = evidence._artifact_families(paths)
    assert snapshot.unchanged()
    added = compiled / 'Common.olean.future-format'
    added.write_text('new input')
    assert not snapshot.unchanged()
    added.unlink()
    assert snapshot.unchanged()
    (compiled / 'Common.olean.server').unlink()
    assert not snapshot.unchanged()


def test_manifest_polls_cancellation_inside_python_dependency_traversal(graph, monkeypatch):
    root, _, _, target = graph
    calls = []
    def stopped():
        calls.append(True)
        return len(calls) > 3
    ticks = iter(i / 10 for i in range(1000))
    monkeypatch.setattr(evidence.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(CheckFailure, match='cancelled'):
        evidence.manifest(target, target.read_text(), root, CheckBudget(should_stop=stopped))
    assert len(calls) == 4
    with run_context(should_stop=lambda: True), pytest.raises(CheckFailure, match='cancelled'):
        evidence.manifest(target, target.read_text(), root)


def test_concurrent_source_rewrite_cannot_poison_import_cache(graph, monkeypatch):
    root, source, _, target = graph
    original = Path.read_bytes
    raced = source / 'Common.lean'
    def read(path):
        if path == raced and str(path) not in evidence._source_imports:
            # The file was already fingerprinted; return different parse bytes.
            return b'import Lib.Added\n'
        return original(path)
    observation = {'sha256': evidence.file_hash(raced), 'version': evidence.file_version(raced)}
    monkeypatch.setattr(Path, 'read_bytes', read)
    with pytest.raises(CheckFailure, match='changed'):
        evidence._imports_for_source(raced, observation)
    assert not evidence._source_imports


def test_siblings_share_import_probes_but_configuration_changes_invalidate_them(graph, monkeypatch):
    root, source, compiled, target = graph
    # The fixture replaced _resolve; restore the actual cache/resolver for this test.
    from lea.imports import direct_imports
    monkeypatch.setattr(evidence, '_resolve', REAL_RESOLVE)
    monkeypatch.setattr(evidence, '_resolutions', {})
    calls = []
    aux_source = root / '.lake/packages/aux/Aux/Utility.lean'
    aux_compiled = root / '.lake/packages/aux/.lake/build/lib/lean/Aux/Utility.olean'
    aux_source.parent.mkdir(parents=True)
    aux_compiled.parent.mkdir(parents=True)
    aux_source.write_text('')
    aux_compiled.write_text('compiled')
    (source / 'Top.lean').write_text('import Aux.Utility\n')
    def command(args, cwd, budget, content):
        names = direct_imports(content)
        pairs = [(aux_source, aux_compiled) if name == 'Aux.Utility' else
                 (source / (name.split('.')[-1] + '.lean'), compiled / (name.split('.')[-1] + '.olean'))
                 for name in names]
        if content == 'import Aux.Utility\n':
            calls.append(args[-1])
        index = 0 if '--src-deps' in args else 1
        return 0, ''.join(str(pair[index]) + '\n' for pair in pairs), ''
    monkeypatch.setattr(evidence, '_command', command)
    for name in ['First.lean', 'Second.lean']:
        path = root / name
        evidence.manifest(path, 'import Lib.Top\n', root)
    assert len(calls) == 2  # One --src-deps / --deps pair across both targets.
    assert set(calls) == {str(root / 'LeaDependencyImports.lean')}
    (root / 'lakefile.toml').write_text('name = "changed"\n')
    evidence.manifest(target, 'import Lib.Top\n', root)
    assert len(calls) == 4
