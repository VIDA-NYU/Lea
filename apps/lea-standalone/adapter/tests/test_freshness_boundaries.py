"""Content validity, execution stability, and daemon compatibility are distinct."""
import os
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from lea import check_evidence as evidence, interface, lean_checks, lsp_daemon, tools
from lea.check_runtime import CheckBudget, CheckFailure


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'lakefile.toml').write_text('name = "fixture"\n')
    target = root / 'Target.lean'
    target.write_text('import A\nimport B\nexample : True := trivial\n')
    files = {}
    for name, kind in [('A.lean', 'source'), ('A.olean', 'compiled'),
                       ('A.olean.server', 'compiled'), ('compiler', 'compiler')]:
        path = root / name
        path.write_text(name + '\n')
        files[name] = kind

    def manifest(*args):
        configuration = evidence._configuration(root)
        return {'configuration': configuration,
                'configuration_versions': {name: evidence.file_version(root / name)
                                           for name in configuration},
                'files': {'workspace:' + name: {'path': str(root / name), 'kind': kind,
                           'sha256': evidence.file_hash(root / name),
                           'version': evidence.file_version(root / name)}
                          for name, kind in files.items()}}

    def execution():
        env = manifest()
        return {'evidence_version': 1, 'source_identity': target.name,
                'content_sha256': evidence.file_hash(target), 'environment': env,
                'environment_sha256': evidence.environment_hash(env)}

    monkeypatch.setattr(evidence, 'manifest', manifest)
    monkeypatch.setattr(evidence, '_command', lambda *args: (0, '', ''))
    monkeypatch.setattr(evidence, '_observed', {})
    monkeypatch.setattr(evidence, '_prepared', {})
    return SimpleNamespace(root=root, target=target, manifest=manifest, execution=execution)


@pytest.mark.parametrize('name', ['lakefile.toml', 'A.lean', 'A.olean', 'compiler'])
@pytest.mark.parametrize('change', ['chmod', 'rewrite', 'replace'])
def test_identical_inputs_keep_historical_evidence_current(inputs, name, change):
    checked = inputs.execution()
    path = inputs.root / name
    if change == 'chmod':
        path.chmod(0o600)
    elif change == 'rewrite':
        path.write_bytes(path.read_bytes())
    else:
        replacement = path.with_suffix('.replacement')
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
    assert evidence.freshness(checked, inputs.target) == (True, None)


def test_same_size_edit_with_restored_mtime_cannot_reuse_hash(inputs):
    checked = inputs.execution()
    path = inputs.root / 'A.lean'
    before = path.stat()
    path.write_text('B.lean\n')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert evidence.freshness(checked, inputs.target) == (False, 'dependency_changed')


def test_unreadable_configuration_cannot_match_recorded_absence(inputs, monkeypatch):
    checked = inputs.execution()
    path = inputs.root / 'lake-manifest.json'
    path.write_text('{}')
    read = Path.read_bytes

    def denied(p):
        if p == path:
            raise PermissionError('unreadable manifest')
        return read(p)

    monkeypatch.setattr(Path, 'read_bytes', denied)
    assert evidence.freshness(checked, inputs.target) == (False, 'unavailable')


@pytest.mark.parametrize('name', ['Target.lean', 'A.lean', 'A.olean', 'lakefile.toml'])
def test_changed_then_restored_inputs_reject_active_check(inputs, monkeypatch, name):
    retirements = []
    def retire(root, **kwargs):
        assert evidence.coordinator(root).readers.get(get_ident())
        retirements.append(kwargs['generation'])

    def check(*args, budget, **kwargs):
        budget.execution['daemon_generation'] = 'one'
        path = inputs.root / name
        original = path.read_bytes()
        path.write_bytes(original + b'-- transient edit\n')
        path.write_bytes(original)
        return 'OK'

    monkeypatch.setattr(lsp_daemon, 'check_via_lsp', check)
    monkeypatch.setattr(lsp_daemon, 'mark_stale', retire)
    result = lean_checks.check_file(inputs.target, str(inputs.root))
    assert result.status == 'error'
    assert result.execution['failure']['kind'] == 'source_changed'
    assert retirements == ['one']


def test_cancelled_check_with_changed_inputs_retires_before_releasing_lease(inputs, monkeypatch):
    def check(*args, budget, **kwargs):
        budget.execution['daemon_generation'] = 'one'
        dependency = inputs.root / 'A.olean'
        dependency.write_bytes(dependency.read_bytes())
        raise CheckFailure('cancelled', 'interrupted', retryable=False)

    retirements = []
    def retire(root, **kwargs):
        assert evidence.coordinator(root).readers.get(get_ident())
        retirements.append(kwargs['generation'])

    monkeypatch.setattr(lsp_daemon, 'check_via_lsp', check)
    monkeypatch.setattr(lsp_daemon, 'mark_stale', retire)
    result = lean_checks.check_file(inputs.target, str(inputs.root))
    assert result.execution['failure']['kind'] == 'cancelled'
    assert retirements == ['one']


def test_changed_then_restored_dependency_rejects_kernel_verification(inputs, monkeypatch):
    monkeypatch.setattr(interface.safeverify, 'is_available', lambda: True)
    monkeypatch.setattr(interface.safeverify, 'safe_verify_binary', lambda: None)

    def verify(*args):
        path = inputs.root / 'A.lean'
        original = path.read_bytes()
        path.write_bytes(original + b'-- transient edit\n')
        path.write_bytes(original)
        return interface.VerifyResult('ok')

    monkeypatch.setattr(interface, '_verify_impl', verify)
    result = interface.verify(str(inputs.target))
    assert result.status == 'error'
    assert 'changed' in result.detail.lower()


@pytest.mark.parametrize('transient_edit', [False, True])
def test_selected_replay_pins_all_artifacts_and_rejects_transient_edits(inputs, monkeypatch, transient_edit):
    from lea import result_selection
    selected = inputs.root / 'proofs' / 'Fixture' / 'Selected.lean'
    selected.parent.mkdir(parents=True)
    selected.write_text('namespace Fixture\ntheorem target : True := trivial\nend Fixture\n')
    artifact = inputs.root / '.lake/build/lib/lean/Fixture/Selected.olean'
    artifact.parent.mkdir(parents=True)
    artifact.write_text('compiled module\n')
    companion = artifact.with_name(artifact.name + '.server')
    companion.write_text('compiled companion\n')
    monkeypatch.setattr(interface.safeverify, 'is_available', lambda: True)
    monkeypatch.setattr(interface.safeverify, 'safe_verify_binary', lambda: None)
    monkeypatch.setattr(result_selection, 'inspect_file', lambda p: {'inspection': 'compiled_module'})
    monkeypatch.setattr(result_selection, 'select', lambda *args: {'full_name': 'Fixture.target'})

    def replay(*args):
        if transient_edit:
            before = companion.read_bytes()
            companion.write_bytes(before + b'transient edit\n')
            companion.write_bytes(before)
        return True, None

    monkeypatch.setattr(interface.safeverify, 'verify_selected_module', replay)
    result = interface.verify(str(selected), declaration='Fixture.target')
    if transient_edit:
        assert result.status == 'error'
        assert 'changed' in result.detail.lower()
    else:
        assert result.status == 'ok', result.detail
        assert any(record['path'] == str(companion)
                   for record in result.execution['environment']['files'].values())


@pytest.fixture
def serving(inputs, monkeypatch):
    generation = ['one']
    restarts = []
    monkeypatch.setattr(lsp_daemon, 'runtime_snapshot', lambda root: {'generation': generation[0]})
    monkeypatch.setattr(lsp_daemon, 'mark_stale', lambda root: restarts.append(root))
    evidence.observe(inputs.root, inputs.manifest(), generation=generation[0])
    return generation, restarts


def test_reordered_imports_and_noop_build_preserve_daemon(inputs, serving, monkeypatch):
    builds = []
    monkeypatch.setattr(evidence, '_command', lambda args, *a: (builds.append(args) or 0, '', ''))
    for code in [inputs.target.read_text(), 'import B\nimport A\nexample : True := trivial\n']:
        with evidence.prepared_environment(inputs.target, code, inputs.root, CheckBudget()):
            pass
    assert len(builds) == 2  # Different admissions still receive Lake preparation.
    assert serving[1] == []


@pytest.mark.parametrize('name', ['A.olean', 'compiler', 'lakefile.toml'])
def test_metadata_only_changes_preserve_daemon(inputs, serving, name):
    (inputs.root / name).chmod(0o600)
    evidence.reconcile_daemon(inputs.root, inputs.manifest())
    assert serving[1] == []


@pytest.mark.parametrize('name', ['A.olean', 'A.olean.server', 'compiler', 'lakefile.toml'])
def test_changed_loaded_inputs_retire_daemon(inputs, serving, name):
    (inputs.root / name).write_text('changed\n')
    evidence.reconcile_daemon(inputs.root)
    assert serving[1] == [str(inputs.root)]


def test_new_artifact_companion_retires_daemon(inputs, serving):
    (inputs.root / 'A.olean.private').write_text('new artifact\n')
    evidence.reconcile_daemon(inputs.root)
    assert serving[1] == [str(inputs.root)]


@pytest.mark.parametrize('name', ['A.lean', 'A.olean'])
def test_changed_resolved_path_retires_daemon(inputs, serving, name):
    current = inputs.manifest()
    replacement = inputs.root / ('Other' + Path(name).suffix)
    replacement.write_bytes((inputs.root / name).read_bytes())
    current['files']['workspace:' + name]['path'] = str(replacement)
    evidence.reconcile_daemon(inputs.root, current)
    assert serving[1] == [str(inputs.root)]


def test_new_generation_drops_predecessor_inputs(inputs, serving):
    serving[0][0] = 'two'
    current = inputs.manifest()
    del current['files']['workspace:A.olean']
    del current['files']['workspace:A.olean.server']
    evidence.observe(inputs.root, current, generation='two')
    (inputs.root / 'A.olean').write_text('not loaded by generation two\n')
    evidence.reconcile_daemon(inputs.root)
    assert serving[1] == []


def test_unknown_daemon_inputs_require_retirement(inputs, serving):
    evidence.observe(inputs.root, None, generation='one')
    evidence.reconcile_daemon(inputs.root)
    assert serving[1] == [str(inputs.root)]


def test_unreadable_daemon_inputs_require_retirement(inputs, serving, monkeypatch):
    monkeypatch.setattr(evidence, 'file_observation', lambda path: (_ for _ in ()).throw(PermissionError()))
    evidence.reconcile_daemon(inputs.root)
    assert serving[1] == [str(inputs.root)]


def test_failed_request_still_records_potentially_loaded_inputs(inputs, monkeypatch):
    class RejectingDaemon:
        generation = 'failed-admission'
        stale = broken = False
        _check_count = 0
        closed = Event()

        def __init__(self, root):
            pass

        def start(self, budget):
            return True

        def _acquire_lease(self):
            pass

        def _release_lease(self):
            pass

        def check(self, *args, **kwargs):
            raise CheckFailure('cancelled', 'interrupted request', retryable=False)

    monkeypatch.setattr(lsp_daemon, '_roots', {})
    monkeypatch.setattr(lsp_daemon, '_daemons', {})
    monkeypatch.setattr(lsp_daemon, 'LeanDaemon', RejectingDaemon)
    budget = CheckBudget()
    budget.execution['environment'] = inputs.manifest()
    with pytest.raises(CheckFailure, match='interrupted request'):
        lsp_daemon.check_via_lsp(str(inputs.target), inputs.target.read_text(), str(inputs.root), budget=budget)
    (inputs.root / 'A.olean').write_text('changed after interrupted request\n')
    evidence.reconcile_daemon(inputs.root)
    assert lsp_daemon._daemons[str(inputs.root)].stale


@pytest.mark.parametrize('failed', [False, True])
def test_build_that_changes_loaded_artifacts_retires_even_on_failure(inputs, serving, monkeypatch, failed):
    # The new request does not import A; the serving generation previously did.
    manifest = inputs.manifest
    def other_manifest(*args):
        current = manifest()
        current['files'] = {name: record for name, record in current['files'].items()
                            if not name.startswith('workspace:A.')}
        return current
    monkeypatch.setattr(evidence, 'manifest', other_manifest)

    def build(*args):
        (inputs.root / 'A.olean').write_text('rebuilt\n')
        return (1 if failed else 0), '', 'build failed' if failed else ''

    monkeypatch.setattr(evidence, '_command', build)
    if failed:
        with pytest.raises(evidence.CheckFailure, match='build failed'):
            with evidence.prepared_environment(inputs.target, inputs.target.read_text(), inputs.root, CheckBudget()):
                pass
    else:
        with evidence.prepared_environment(inputs.target, inputs.target.read_text(), inputs.root, CheckBudget()):
            pass
    assert serving[1] == [str(inputs.root)]


def test_explicit_noop_rebuild_preserves_daemon(inputs, serving, monkeypatch):
    proof = inputs.root / 'proofs' / 'Target.lean'
    proof.parent.mkdir()
    proof.write_text('example : True := trivial\n')
    monkeypatch.setattr(tools.command_executor, 'run',
        lambda *a, **k: SimpleNamespace(returncode=0, stdout='', stderr=''))
    tools.rebuild_module(str(proof))
    assert serving[1] == []


@pytest.mark.parametrize('failed', [False, True])
def test_explicit_rebuild_reconciles_success_and_failure(inputs, serving, monkeypatch, failed):
    proof = inputs.root / 'proofs' / 'Target.lean'
    proof.parent.mkdir()
    proof.write_text('example : True := trivial\n')

    def build(*args, **kwargs):
        (inputs.root / 'A.olean').write_text('rebuilt\n')
        return SimpleNamespace(returncode=1 if failed else 0, stdout='', stderr='')

    monkeypatch.setattr(tools.command_executor, 'run', build)
    tools.rebuild_module(str(proof))
    assert serving[1] == [str(inputs.root)]
