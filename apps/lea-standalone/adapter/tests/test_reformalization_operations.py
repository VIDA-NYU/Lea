"""Deterministic operation tests, with real local Lean and OS confinement."""
import json
from pathlib import Path
import subprocess
import time

import pytest

from app import db, store, reformalizations as ops, project_library,run_commands
from app import reformalization_apply as publication
from app.gitstore import GitStore
from lea import execution
from app import config
from lea.config import LeaConfig


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('LEA_REFORMALIZATION_ENABLED', '1')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'data/test.sqlite3')
    db.init_db()
    root = tmp_path / 'workspace'
    repo = root / 'proofs/Lea/Study'
    repo.mkdir(parents=True)
    (root / 'lean-toolchain').write_text('leanprover/lean4:v4.29.0\n')
    (root / 'lakefile.toml').write_text('name = "fixture"\n[[lean_lib]]\nname = "Lea"\nsrcDir = "proofs"\n')
    subprocess.run(['lake', 'env', 'lean', '--version'], cwd=root, capture_output=True, check=True)
    p = store.get_or_create_project('study', 'Study')
    monkeypatch.setattr(config, 'load_config', lambda: LeaConfig(model='fixture', max_turns=10, lea_root=tmp_path))
    monkeypatch.setattr(project_library, 'project_repo', lambda _: repo)
    gs = GitStore(repo.parent)
    gs.init_repo(repo)
    code = 'namespace Lea.Study\n-- Preserve the hand-written proof.\ntheorem a : True := by trivial\ntheorem b : True := by sorry\ntheorem c : True := by sorry\nend Lea.Study\n'
    (repo / 'Shared.lean').write_text(code)
    gs.commit_all(repo, 'Initial current work', paths=['Shared.lean'])
    form = store.create_formalization(project_id=p['id'], loose_session_id=None,
        display_title='B', declaration_name='Lea.Study.b', statement='True')
    store.link_formalization_file(form['id'], 'Shared.lean', 'primary')
    return p, repo, form, code


def operation(project, monkeypatch, **request):
    p, repo, form, _ = project
    if not execution.capability()['available']:
        pytest.skip('Real filesystem executor unavailable; run on supported host outside a nested sandbox')
    op = ops.enqueue({'message': 'Retry B', 'focus_formalization_id': form['id'], **request}, p['id'])
    session = store.get_session(op['session_id'])
    run = store.create_run(session['id'], 'fixture', None, 10, project_id=p['id'], focus_formalization_id=form['id'])
    store.claim_pending_run(run['id'])
    ops.update(op['id'], 'active')
    ops.attach_run(op['id'], run['id'])
    ops.prepare(op['id'])
    return ops.get(op['id'])


def selection():
    return {'path': 'Shared.lean', 'declaration': 'Lea.Study.b', 'kind': 'proved'}


def test_claimed_operation_transfers_capacity_and_scope_to_its_run(project,monkeypatch):
    from app import commands,bridge,runregistry,runtime
    from app.routes import runs
    p,repo,form,_=project
    monkeypatch.setattr(runregistry,'registry',runregistry.RunRegistry(max_concurrent=4))
    monkeypatch.setattr(runtime,'accepting_work',lambda:True)
    monkeypatch.setattr(runs,'load_config',config.load_config)
    def forbidden(*a,**kw):raise AssertionError('Admission must not start a provider')
    monkeypatch.setattr(bridge,'run_events',forbidden)
    op=ops.enqueue({'message':'Retry B','focus_formalization_id':form['id']},p['id'])
    epoch=commands.start_worker('operation-fixture')
    token=commands.claim('operation-fixture',epoch,'compute',kinds={'operation.dispatch'})
    ops.wake(op['id'],command_token=token)
    current=ops.get(op['id']);run=store.get_run(current['root_run_id'])
    try:
        assert run['reformalization_id']==op['id'] and run['status']=='pending'
        assert run_commands.for_run(run['id'])['state']=='queued'
        with db.connect() as conn:
            assert conn.execute('select state from commands where id=?',(token['id'],)).fetchone()[0]=='succeeded'
            assert conn.execute("select count from command_capacity where key='compute:global'").fetchone()[0]==1
            assert conn.execute("select count(*) from timeline where author='user'").fetchone()[0]==1
    finally:runregistry.registry.release(run['id'])


def test_command_cancellation_before_acceptance_restores_canonical_files(project,monkeypatch):
    from app import commands,run_commands
    op=operation(project,monkeypatch)
    run_commands.ensure_run(op['root_run_id'])
    epoch=commands.start_worker('publication-fixture')
    token=commands.claim('publication-fixture',epoch,'compute',kinds={'run.execute'})
    candidate=project[3].replace('theorem b : True := by sorry','theorem b : True := by trivial')
    staged=Path(op['manifest']['repo'])/'Shared.lean';staged.write_text(candidate)
    accept=publication._accept
    def cancel_before_accept(*args):
        run_commands.stop(op['root_run_id'])
        return accept(*args)
    monkeypatch.setattr(publication,'_accept',cancel_before_accept)
    with pytest.raises(publication.PreservationConflict,match='cancelled or fenced'):
        publication.apply(op['id'],selection(),'proved',command_token=token)
    assert (project[1]/'Shared.lean').read_text()==project[3]
    assert staged.read_text()==candidate
    assert ops.get(op['id'])['state']=='retained'


def test_real_shared_retry_preserves_a_and_unfinished_c(project, monkeypatch):
    started = time.monotonic()
    op = operation(project, monkeypatch)
    prepare_seconds = time.monotonic() - started
    _, repo, form, original = project
    staged = Path(op['manifest']['repo'])
    candidate = original.replace('theorem b : True := by sorry', 'theorem b : True := by trivial')
    (staged / 'Shared.lean').write_text(candidate)
    started = time.monotonic()
    receipt = publication.apply(op['id'], selection(), 'proved')
    apply_seconds = time.monotonic() - started
    assert receipt['outcome'] == 'proved'
    assert (repo / 'Shared.lean').read_text() == candidate
    assert 'Lea.Study.a' in receipt['preservation']['protected']
    assert 'Lea.Study.c' in receipt['preservation']['protected']
    assert ops.get(op['id'])['state'] == 'settled'
    steps = store.current_code_steps_for_formalization(form['id'])
    with db.connect() as conn:
        record = conn.execute('select * from proof_check_evidence where id=?', (steps[0]['check_evidence_id'],)).fetchone()
    from app.proof_evidence import canonical_path
    from lea.check_evidence import freshness
    evidence = json.loads(record['evidence_json'])
    assert freshness(evidence, canonical_path(steps[0], evidence, project[0]['namespace']))[0]
    assert steps[0]['check_current'], steps[0].get('check_stale_reason')
    assert steps[0]['check_status'] == 'ok'
    publication.recover()
    assert (repo / 'Shared.lean').read_text() == candidate
    versions = {v['blob_id'] for v in ops.versions(op['id'])}
    print('REFORMALIZATION_MEASUREMENT ' + json.dumps({
        'prepare_seconds': round(prepare_seconds, 3), 'checked_apply_seconds': round(apply_seconds, 3),
        'canonical_source_bytes': len(candidate.encode()),
        'retained_unique_blob_bytes': sum(len(store.blob_content(blob).encode()) for blob in versions),
        'workspace_bytes': sum(p.stat().st_size for p in Path(op['manifest']['workspace']).rglob('*') if p.is_file()),
        'provider_calls': 0}))


def test_cancelled_preparation_stops_before_checks_or_model_work(project, monkeypatch):
    from lea.check_runtime import CheckFailure
    from lea.runctx import run_context
    p, repo, form, original = project
    queued = ops.enqueue({'message': 'Retry B', 'focus_formalization_id': form['id']}, p['id'])
    with run_context(should_stop=lambda: True), pytest.raises(CheckFailure, match='cancelled'):
        ops.prepare(queued['id'])
    assert ops.get(queued['id'])['manifest'] is None
    assert (repo / 'Shared.lean').read_text() == original
    with db.connect() as conn:
        assert conn.execute('select count(*) from runs').fetchone()[0] == 0


def test_real_retry_retains_compiler_generated_constants_without_ranges(project, monkeypatch):
    _, repo, _, original = project
    energy = 'namespace Lea.Study\ndef energy : Fin 1 := ⟨0, by decide⟩\nend Lea.Study\n'
    (repo / 'Energy.lean').write_text(energy)
    original = 'import Lea.Study.Energy\n' + original
    (repo / 'Shared.lean').write_text(original)
    op = operation(project, monkeypatch)
    generated = {d['user_name'] for d in op['manifest']['reports']['Energy.lean']['declarations']
                 if not d.get('range')}
    assert generated, 'The real compiler fixture must produce a constant without a range'
    candidate = original.replace('theorem b : True := by sorry', 'theorem b : True := by trivial')
    (Path(op['manifest']['repo']) / 'Shared.lean').write_text(candidate)
    from lea.runctx import run_context, current_working_dir, current_run_key, current_should_stop
    from lea import interface
    staged = str(Path(op['manifest']['repo']))
    stop = lambda: False
    with run_context(working_dir=staged, run_key=op['root_run_id'], should_stop=stop):
        # A yielded Finished event still carries the agent's isolated context.
        # Host publication must check canonical files and restore that boundary.
        assert 'outside this run' in interface.check(str(repo / 'Shared.lean'), cold=True).detail
        receipt = publication.apply(op['id'], selection(), 'proved')
        assert current_working_dir() == staged
        assert current_run_key() == op['root_run_id']
        assert current_should_stop() is stop
        assert 'outside this run' in interface.check(str(repo / 'Shared.lean'), cold=True).detail
    assert generated <= set(receipt['preservation']['protected'])
    assert (repo / 'Energy.lean').read_text() == energy
    assert (repo / 'Shared.lean').read_text() == candidate
    assert ops.get(op['id'])['state'] == 'settled'


def test_destructive_compiling_candidate_and_freshness_conflict(project, monkeypatch):
    op = operation(project, monkeypatch)
    _, repo, _, original = project
    staged = Path(op['manifest']['repo'])
    (staged / 'Shared.lean').write_text(original.replace('theorem b : True := by sorry', 'theorem b : True := by exact c'))
    # c must precede b for this to be a compiling transitive-sorry candidate.
    (staged / 'Shared.lean').write_text((staged / 'Shared.lean').read_text().replace(
        'theorem b : True := by exact c\ntheorem c : True := by sorry',
        'theorem c : True := by sorry\ntheorem b : True := by exact c'))
    with pytest.raises(ValueError, match='trust|axiom'):
        publication.assess(op['id'], selection())
    (staged / 'Shared.lean').write_text(original.replace('by sorry', 'by trivial').replace('by trivial', 'by exact True.intro', 1))
    with pytest.raises(ValueError, match='Protected source'):
        publication.assess(op['id'], selection())
    assert (repo / 'Shared.lean').read_text() == original
    (repo / 'Manual.lean').write_text('-- an untracked human file\n')
    with pytest.raises(ValueError, match='sources changed'):
        publication.assess(op['id'], selection())


def test_definition_retry_preserves_unicode_neighbors_and_crlf(project, monkeypatch):
    _, repo, form, _ = project
    before = ('namespace Lea.Study\n-- Hand-written context α.\ndef preserved : Nat := 7\n'
        'theorem α : True := by trivial\ndef b : Nat := by sorry\n'
        'theorem c : True := by sorry\nend Lea.Study\n').replace('\n', '\r\n')
    (repo / 'Shared.lean').write_bytes(before.encode())
    with db.write() as conn:
        conn.execute("update formalizations set kind='definition',statement='A natural number' where id=?", (form['id'],))
    op = operation(project, monkeypatch)
    candidate = before.replace('def b : Nat := by sorry', 'def b : Nat := 0')
    (Path(op['manifest']['repo']) / 'Shared.lean').write_bytes(candidate.encode())
    receipt = publication.apply(op['id'], {**selection(), 'kind': 'defined'}, 'defined')
    assert receipt['outcome'] == 'defined'
    assert (repo / 'Shared.lean').read_bytes() == candidate.encode()
    assert store.blob_content(op['manifest']['files']['Shared.lean']['blob_id']) == before
    assert {'Lea.Study.preserved', 'Lea.Study.α', 'Lea.Study.c'} <= set(receipt['preservation']['protected'])


def test_prior_retained_draft_is_advisory_and_does_not_replace_current_work(project, monkeypatch):
    op = operation(project, monkeypatch)
    original = project[3]
    ops.save_draft(op['id'], op['root_run_id'], 'Shared.lean', 'retained partial attempt')
    store.update_run(op['root_run_id'], 'cancelled', result_kind='interrupted')
    ops.update(op['id'], 'retained')
    next_op = operation(project, monkeypatch)
    paths = ops.retained_context(next_op)
    assert len(paths) == 1
    assert Path(paths[0]).read_text() == 'retained partial attempt'
    assert (Path(next_op['manifest']['repo']) / 'Shared.lean').read_text() == original
    assert (project[1] / 'Shared.lean').read_text() == original


def test_no_replace_install_preserves_external_destination(tmp_path):
    path = tmp_path / 'Proof.lean'
    path.write_text('human edit')
    with pytest.raises(FileExistsError):
        publication._install(path, 'candidate')
    assert path.read_text() == 'human edit'


def test_observed_old_descriptor_edit_is_retained_and_restored(project, monkeypatch):
    import os
    op = operation(project, monkeypatch)
    _, repo, _, original = project
    candidate = original.replace('theorem b : True := by sorry', 'theorem b : True := by trivial')
    external = original + '-- saved through an already-open editor descriptor\n'
    (Path(op['manifest']['repo']) / 'Shared.lean').write_text(candidate)
    original_check = publication._canonical_checks
    with (repo / 'Shared.lean').open('r+b') as editor:
        def check_then_editor_save(*args):
            result = original_check(*args)
            editor.seek(0)
            editor.write(external.encode())
            editor.truncate()
            editor.flush()
            os.fsync(editor.fileno())
            return result
        monkeypatch.setattr(publication, '_canonical_checks', check_then_editor_save)
        with pytest.raises(ValueError, match='displaced source changed'):
            publication.apply(op['id'], selection(), 'proved')
    assert (repo / 'Shared.lean').read_text() == external
    retained = {store.blob_content(v['blob_id']) for v in ops.versions(op['id'])}
    assert {original, candidate, external} <= retained


def test_exact_commit_preserves_unrelated_index_and_unstaged_bytes(tmp_path):
    gs = GitStore(tmp_path)
    repo = gs.init_session('git')
    (repo / 'A.lean').write_text('first')
    (repo / 'Notes.txt').write_text('staged notes')
    gs.commit_all(repo, 'baseline', paths=['A.lean'])
    gs._git(repo, 'add', 'Notes.txt')
    (repo / 'Notes.txt').write_text('new unstaged notes')
    (repo / 'A.lean').write_text('candidate')
    sha = gs.commit_exact(repo, 'selected retry', {'A.lean': 'candidate'}, application_id='exact')
    assert gs._git(repo, 'show', sha + ':A.lean') == 'candidate'
    assert gs._git(repo, 'diff', '--cached', '--name-only').strip() == 'Notes.txt'
    assert gs._git(repo, 'show', ':Notes.txt') == 'staged notes'
    assert (repo / 'Notes.txt').read_text() == 'new unstaged notes'


def test_shell_and_lean_children_cannot_write_canonical_sentinel(project, monkeypatch):
    op = operation(project, monkeypatch)
    _, repo, _, _ = project
    staged = Path(op['manifest']['repo'])
    sentinel = repo / 'sentinel.txt'
    sentinel.write_text('original')
    probe = staged / 'Escape.lean'
    probe.write_text('import Lean\n#eval IO.FS.writeFile ' + json.dumps(str(sentinel)) + ' "changed"\n')
    for policy in (ops.workspace_policy(op['manifest']['workspace']),
                   ops.canonical_policy(op['manifest']['canonical_root'], repo)):
        with execution.scope(policy):
            result = execution.run(['/bin/sh', '-c', 'echo changed > "$1"', 'probe', str(sentinel)], capture_output=True, text=True)
            assert result.returncode != 0
            result = execution.run(['lake', 'env', 'lean', str(probe)], cwd=op['manifest']['workspace'], capture_output=True, text=True)
            assert result.returncode != 0
        assert sentinel.read_text() == 'original'


@pytest.mark.parametrize('point', ['displaced', 'installed', 'committed'])
def test_recovery_at_filesystem_and_git_boundaries(project, monkeypatch, point):
    op = operation(project, monkeypatch)
    _, repo, _, original = project
    staged = Path(op['manifest']['repo'])
    candidate = original.replace('theorem b : True := by sorry', 'theorem b : True := by trivial')
    (staged / 'Shared.lean').write_text(candidate)
    class ProcessDeath(BaseException):
        pass
    real = publication._write_journal
    def crash(journal_id, state, journal, commit=None):
        entry = journal['files'].get('Shared.lean', {})
        if ((point == 'displaced' and entry.get('displaced_blob') and not entry.get('installed'))
                or (point == 'installed' and entry.get('installed') and not journal.get('checks'))
                or (point == 'committed' and commit)):
            raise ProcessDeath()
        return real(journal_id, state, journal, commit)
    monkeypatch.setattr(publication, '_write_journal', crash)
    with pytest.raises(ProcessDeath):
        publication.apply(op['id'], selection(), 'proved')
    monkeypatch.setattr(publication, '_write_journal', real)
    publication.recover()
    publication.recover()
    assert (repo / 'Shared.lean').read_text() == (candidate if point == 'committed' else original)
    with db.connect() as conn:
        states = [r[0] for r in conn.execute('select state from reformalization_applications')]
    assert states == ['published' if point == 'committed' else 'restored']


def test_external_occupied_path_is_retained_during_restore(project, monkeypatch):
    op = operation(project, monkeypatch)
    _, repo, _, original = project
    staged = Path(op['manifest']['repo'])
    (staged / 'Shared.lean').write_text(original.replace('theorem b : True := by sorry', 'theorem b : True := by trivial'))
    real = publication._install
    def occupied(path, content, mode=0o644):
        if path == repo / 'Shared.lean' and not path.exists():
            path.write_text('external editor version\n')
        return real(path, content, mode)
    monkeypatch.setattr(publication, '_install', occupied)
    with pytest.raises(FileExistsError):
        publication.apply(op['id'], selection(), 'proved')
    assert (repo / 'Shared.lean').read_text() == 'external editor version\n'
    with db.connect() as conn:
        journal = conn.execute('select state,manifest_json from reformalization_applications').fetchone()
        blobs = [r[0] for r in conn.execute('select content from artifact_blobs')]
    assert journal['state'] == 'conflict'
    assert original in blobs
    assert 'external editor version\n' in blobs


def test_source_update_extracts_target_and_retains_broken_consumer(project, monkeypatch):
    p, repo, form, _ = project
    original = ('namespace Lea.Study\n'
        'theorem a : True := by trivial\n'
        'theorem b : True := by trivial\n'
        'theorem consumer : True := b\n'
        'end Lea.Study\n')
    (repo / 'Shared.lean').write_text(original)
    store.upsert_artifact(project_id=p['id'], session_id=None, run_id=None,
        declaration_name='Lea.Study.b', kind='theorem', path='Shared.lean',
        module_name='Lea.Study.Shared', formalization_id=form['id'], source_hash='previous')
    store.update_formalization(form['id'], source_hash='current', statement='1 = 1')
    op = operation(project, monkeypatch, source_update_evidence={
        'previous_statement': 'True', 'current_statement': '1 = 1'})
    staged = Path(op['manifest']['repo'])
    (staged / 'Shared.lean').write_text(original.replace('theorem b : True := by trivial', 'theorem b : 1 = 1 := by rfl'))
    assessment = publication.assess(op['id'], selection())
    assert assessment['selection']['path'].startswith('Reformalized/')
    receipt = publication.apply(op['id'], assessment['selection'], 'proved', assessment=assessment)
    assert receipt['repair_needed'] == ['Shared.lean']
    assert 'theorem consumer : True := b' in (repo / 'Shared.lean').read_text()
    assert (repo / receipt['selected_result']['path']).is_file()


def test_explicit_disproof_keeps_source_target_identity(project, monkeypatch):
    op = operation(project, monkeypatch)
    _, repo, form, original = project
    candidate = original.replace('end Lea.Study', 'theorem b_counterexample : ¬ False := by simp\nend Lea.Study')
    (Path(op['manifest']['repo']) / 'Shared.lean').write_text(candidate)
    selected = {'path': 'Shared.lean', 'declaration': 'Lea.Study.b_counterexample', 'kind': 'disproved', 'outcome': 'disproved'}
    receipt = publication.apply(op['id'], selected, 'disproved')
    assert receipt['outcome'] == 'disproved'
    assert store.get_formalization(form['id'])['declaration_name'] == 'Lea.Study.b'
    assert store.list_artifacts_for_scope(project[0]['id'])[0]['declaration_name'] == 'Lea.Study.b_counterexample'


@pytest.mark.parametrize('changed_after_approval', [False, True])
def test_review_applies_exact_draft_without_another_run(project, monkeypatch, changed_after_approval):
    from app import reformalization_review as review
    from lea.interface import check
    op = operation(project, monkeypatch)
    _, repo, form, original = project
    candidate = original.replace('theorem b : True := by sorry', 'theorem b : True := by trivial')
    staged = Path(op['manifest']['repo'])
    (staged / 'Shared.lean').write_text(candidate)
    with execution.scope(ops.workspace_policy(op['manifest']['workspace'])):
        checked = check(str(staged / 'Shared.lean'), cold=True)
    ops.save_draft(op['id'], op['root_run_id'], 'Shared.lean', candidate,
        {'status': checked.status, 'execution': checked.execution})
    store.update_run(op['root_run_id'], 'needs_review', result_kind='needs_review')
    ops.update(op['id'], 'review', result={'selection': selection(), 'outcome': 'needs_review'})
    info = review.context(ops.get(op['id']), store.get_formalization(form['id']))
    assert info['review']['eligible']
    assert info['artifacts'][0]['artifact_scope'] == 'attempt'
    monkeypatch.setattr(ops, 'dispatch', lambda _: None)
    decision = review.decide(form['id'], {'action': 'approve', 'revision_token': info['review']['revision_token'],
        'outcome': 'proved', 'idempotency_key': 'approve-exact'})
    assert ops.get(op['id'])['state'] == 'queued'
    if changed_after_approval:
        (staged / 'Shared.lean').write_text(candidate.replace('theorem b : True := by trivial', 'theorem b : True := by exact True.intro'))
    ops.update(op['id'], 'active')
    review.apply_approved(ops.get(op['id']))
    if changed_after_approval:
        assert ops.get(op['id'])['state'] == 'retained'
        assert (repo / 'Shared.lean').read_text() == original
        return
    assert ops.get(op['id'])['state'] == 'settled'
    assert (repo / 'Shared.lean').read_text() == candidate
    with db.connect() as conn:
        assert conn.execute('select count(*) from runs').fetchone()[0] == 1
    from app import formalizations
    assert store.get_run(op['root_run_id'])['result_kind'] == 'needs_review'
    assert formalizations.get(form['id'])['validity_status'] == 'proved'
    review.decide(form['id'], {'action': 'withdraw', 'approval_id': decision['decision']['id'],
        'idempotency_key': 'withdraw-exact'})
    assert formalizations.get(form['id'])['validity_status'] == 'needs_review'
    assert (repo / 'Shared.lean').read_text() == candidate


def test_exact_protected_relocation_keeps_mapping_and_completion(project, monkeypatch):
    from app import formalizations
    p, repo, _, original = project
    a = store.create_formalization(project_id=p['id'], loose_session_id=None,
        display_title='A', declaration_name='Lea.Study.a', statement='True')
    store.link_formalization_file(a['id'], 'Shared.lean', 'primary')
    session = store.create_session('Original A', project_id=p['id'])
    prior = store.create_run(session['id'], 'fixture', None, 10, project_id=p['id'], focus_formalization_id=a['id'])
    store.link_session_formalization(session['id'], a['id'])
    store.add_code_step(session['id'], prior['id'], 'Shared.lean', content=original,
        check_status='ok', artifact_kind='proof', formalization_id=a['id'])
    from lea import interface, result_selection
    from app.proof_evidence import record_check
    checked = interface.check(str(repo / 'Shared.lean'), cold=True)
    checked.execution['selected_results'] = [result_selection.inspect_selected(repo / 'Shared.lean', 'Lea.Study.a')]
    record_check(session['id'], 'Shared.lean', repo / 'Shared.lean', checked,
        run_id=prior['id'], formalization_id=a['id'])
    store.update_run(prior['id'], 'proved', result_kind='proved')
    assert formalizations.get(a['id'])['validity_status'] == 'proved', formalizations.get(a['id'])['validity_reason']
    op = operation(project, monkeypatch)
    staged = Path(op['manifest']['repo'])
    command = 'theorem a : True := by trivial'
    (staged / 'Preserved.lean').write_text('namespace Lea.Study\n' + command + '\nend Lea.Study\n')
    candidate = 'import Lea.Study.Preserved\n' + original.replace(command, '').replace('theorem b : True := by sorry', 'theorem b : True := by trivial')
    (staged / 'Shared.lean').write_text(candidate)
    receipt = publication.apply(op['id'], selection(), 'proved')
    assert receipt['preservation']['relocations'] == [{'name': 'Lea.Study.a',
        'before_path': 'Shared.lean', 'after_path': 'Preserved.lean'}]
    current = formalizations.get(a['id'])
    assert current['primary_path'] == 'Preserved.lean'
    assert current['validity_status'] == 'proved', current['validity_reason']
    assert current['completion_run_id'] == prior['id']
    assert (repo / 'Preserved.lean').read_text().count(command) == 1


def test_scripted_solver_keeps_drafts_out_of_canonical_history(project, monkeypatch):
    from app import bridge
    from app.reformalization_runner import run as run_attempt
    from lea.events import FileChanged, Finished, CheckResult
    from lea.providers import Usage
    from lea.interface import check
    from lea.result_selection import inspect_selected
    from queue import Queue
    op = operation(project, monkeypatch)
    p, repo, form, original = project
    store.update_run(op['root_run_id'], 'pending')
    candidate = original.replace('theorem b : True := by sorry', 'theorem b : True := by trivial')
    def script_steps(cfg, messages, **options):
        path = Path(options['working_dir']) / 'Shared.lean'
        path.write_text(candidate)
        yield FileChanged(str(path), candidate)
        assert (repo / 'Shared.lean').read_text() == original
        assert store.current_code_steps_for_formalization(form['id']) == []
        assert ops.drafts(op['id'])[0]['code'] == candidate
        checked = check(str(path), cold=True)
        yield checked
        selected = inspect_selected(path, 'Lea.Study.b')
        yield Finished('completed', 'Done', 1, op['session_id'], 'fixture', Usage(), 0,
            {'messages': messages}, result_kind='proved', result_detail='PROVED',
            candidate_path=str(path), selected_result=selected)
    def scripted(cfg, messages, **options):
        from lea.runctx import run_context
        with run_context(working_dir=options['working_dir'], run_key=op['root_run_id'],
                         config=cfg, should_stop=options.get('should_stop')):
            yield from script_steps(cfg, messages, **options)
    monkeypatch.setattr(bridge, 'run_events', scripted)
    context = bridge.RunnerContext(session_id=op['session_id'], run_id=op['root_run_id'],
        task='Retry B', config=config.load_config(), events=Queue(), autonomous=True)
    run_attempt(context, op)
    assert store.get_run(op['root_run_id'])['status'] == 'proved'
    assert ops.get(op['id'])['state'] == 'settled'
    assert (repo / 'Shared.lean').read_text() == candidate


def test_fifo_second_capture_sees_first_publication(project, monkeypatch):
    from app import bridge, runtime, runregistry
    from app.routes import runs
    op = operation(project, monkeypatch)
    p, repo, _, original = project
    c = store.create_formalization(project_id=p['id'], loose_session_id=None,
        display_title='C', declaration_name='Lea.Study.c', statement='True')
    store.link_formalization_file(c['id'], 'Shared.lean', 'primary')
    queued = ops.enqueue({'message': 'Retry C', 'focus_formalization_id': c['id']}, p['id'])
    monkeypatch.setattr(runtime, 'accepting_work', lambda: True)
    monkeypatch.setattr(runs, 'load_config', config.load_config)
    monkeypatch.setattr(runregistry, 'registry', runregistry.RunRegistry(max_concurrent=4))
    ops.dispatch(p['id'])
    assert ops.get(queued['id'])['root_run_id'] is None
    with db.connect() as conn:
        assert conn.execute('select count(*) from runs').fetchone()[0] == 1
    candidate = original.replace('theorem b : True := by sorry', 'theorem b : True := by trivial')
    (Path(op['manifest']['repo']) / 'Shared.lean').write_text(candidate)
    publication.apply(op['id'], selection(), 'proved')
    ops.dispatch(p['id'])
    second = ops.get(queued['id'])
    assert run_commands.for_run(second['root_run_id'])
    captured = ops.prepare(second['id'])
    assert store.blob_content(captured['files']['Shared.lean']['blob_id']) == candidate
    runregistry.registry.release(second['root_run_id'])


def test_capacity_wait_has_no_run_row_and_can_be_cancelled(project, monkeypatch):
    from app import runtime, runregistry
    p, _, form, _ = project
    monkeypatch.setattr(execution, 'capability', lambda: {'available': True})
    registry = runregistry.RunRegistry(max_concurrent=1)
    monkeypatch.setattr(runregistry, 'registry', registry)
    monkeypatch.setattr(runtime, 'accepting_work', lambda: True)
    assert registry.try_admit('other-project-run', 'other-session').ok
    queued = ops.enqueue({'message': 'Retry B', 'focus_formalization_id': form['id'], 'idempotency_key': 'same-request'}, p['id'])
    same = ops.enqueue({'message': 'Retry B', 'focus_formalization_id': form['id'], 'idempotency_key': 'same-request'}, p['id'])
    assert same['id'] == queued['id']
    ops.dispatch(p['id'])
    assert ops.get(queued['id'])['state'] == 'queued'
    assert ops.get(queued['id'])['root_run_id'] is None
    with db.connect() as conn:
        assert conn.execute('select count(*) from runs').fetchone()[0] == 0
        assert conn.execute('select count(*) from sessions').fetchone()[0] == 1
    ops.cancel(queued['id'])
    assert ops.get(queued['id'])['state'] == 'retained'
    registry.release('other-project-run')


def test_overleaf_refresh_does_not_block_admission_and_cancel_starts_no_run(project, monkeypatch):
    from app import bridge, overleaf_source, runtime, runregistry
    from app.routes import runs
    p, _, form, _ = project
    with db.write() as conn:
        conn.execute("update formalizations set origin='overleaf' where id=?", (form['id'],))
    monkeypatch.setattr(execution, 'capability', lambda: {'available': True})
    monkeypatch.setattr(runtime, 'accepting_work', lambda: True)
    registry = runregistry.RunRegistry(max_concurrent=1)
    monkeypatch.setattr(runregistry, 'registry', registry)
    starts = []
    class DeferredThread:
        def __init__(self, *, target, args, **kwargs):
            self.target, self.args = target, args
        def start(self):
            starts.append(self)
    monkeypatch.setattr(bridge, 'Thread', DeferredThread)
    monkeypatch.setattr(overleaf_source, 'prepare', lambda *_: pytest.fail('Reverse source callbacks are retired'))
    monkeypatch.setattr(runs, 'create_run', lambda *_: pytest.fail('A cancelled operation must not start a solver'))
    queued = ops.enqueue({'message': 'Retry B', 'focus_formalization_id': form['id']}, p['id'])
    ops.dispatch(p['id'])
    assert len(starts) == 1
    assert ops.response(ops.get(queued['id']))['run_id'] is None
    assert registry.is_active(queued['id'])
    ops.cancel(queued["id"])
    starts[0].target(*starts[0].args)
    assert ops.get(queued['id'])['result']['outcome'] == 'cancelled'
    assert not registry.is_active(queued['id'])
    with db.connect() as conn:
        assert conn.execute('select count(*) from runs').fetchone()[0] == 0


def test_staged_continuation_waits_for_capacity_and_replays_admission(project, monkeypatch):
    from app import bridge, recovery, runregistry, runtime
    from fastapi import HTTPException
    op = operation(project, monkeypatch)
    old = store.get_run(op['root_run_id'])
    staged = Path(op['manifest']['repo'])
    saved = dict(stage='turn_boundary', turn=2, messages=[{'role': 'user', 'content': 'Retained progress'}],
        proof_state={'selection': {**selection(), 'path': str(staged / 'Shared.lean')}},
        status_context={}, input_tokens=100, output_tokens=20, cost_usd=0.2)
    attempt = recovery.start_attempt(old['id'])
    recovery.Recorder(old['id'], attempt, recovery.manifest(run=old, config=config.load_config(), repo=staged))('checkpoint', saved)
    store.update_run(old['id'], 'max_turns', recoverable=True, stop_reason='turn_cap')
    old = store.get_run(old['id'])
    ops.update(op['id'], 'paused')
    registry = runregistry.RunRegistry(max_concurrent=1)
    monkeypatch.setattr(runregistry, 'registry', registry)
    monkeypatch.setattr(runtime, 'accepting_work', lambda: True)
    assert registry.try_admit('another-run', 'another-session').ok
    request = {'idempotency_key': 'resume-staged', 'timeout_seconds': 90}
    queued = ops.queue_continuation(ops.get(op['id']), old, request)
    assert queued['run_id'] is None
    assert queued['state'] == 'queued'
    assert ops.queue_continuation(ops.get(op['id']), old, request)['run_id'] is None
    registry.release('another-run')
    ops.wake(op['id'])
    current = ops.get(op['id'])
    run = store.get_run(current['root_run_id'])
    assert run_commands.for_run(run['id']) and run['resumes_run_id'] == old['id']
    assert run['resume_checkpoint_id'] and run['max_turns'] == 12
    assert ops.queue_continuation(current, old, request)['run_id'] == run['id']
    with pytest.raises(HTTPException, match='409'):
        ops.queue_continuation(current, old, {**request, 'timeout_seconds': 120})
    with db.connect() as conn:
        assert conn.execute('select count(*) from runs').fetchone()[0] == 2
    assert recovery.continuation_checkpoint(old['id'])[0]['messages'] == saved['messages']
    registry.release(run['id'])


def test_unrelated_project_changes_do_not_invalidate_own_attempt(project, monkeypatch):
    op = operation(project, monkeypatch)
    other = Path(op['manifest']['canonical_root']) / 'proofs/Lea/Other/Independent.lean'
    other.parent.mkdir(parents=True)
    other.write_text('theorem independent : True := by trivial\n')
    ops.fresh(op)


def test_inline_helper_publication_does_not_leak_target_draft(project, monkeypatch):
    from app import project_dependencies as deps
    from app.reformalization_helpers import publish
    op = operation(project, monkeypatch)
    p, repo, _, original = project
    with execution.scope(ops.canonical_policy(op['manifest']['canonical_root'], repo)):
        dep = deps.ensure(p, {'name': 'SupportingFact', 'kind': 'theorem', 'signature': ': True', 'imports': []},
            run_id=op['root_run_id'], mode='inline')['dependency']
    staged = Path(op['manifest']['repo'])
    (staged / dep['path']).parent.mkdir(parents=True)
    short = dep['declaration_name'].rsplit('.', 1)[-1]
    (staged / dep['path']).write_text('namespace Lea.Study\ntheorem ' + short + ' : True := by trivial\nend Lea.Study\n')
    (staged / 'Shared.lean').write_text(original.replace('theorem b : True := by sorry', 'theorem b : True := by trivial'))
    result = publish(op['id'], op['root_run_id'], dep['path'])
    assert result['outcome'] == 'published', result
    assert (repo / 'Shared.lean').read_text() == original
    assert (repo / dep['path']).is_file()
    assert deps.get(dep['id'])['ready']
    ops.update(op['id'], 'retained', result={'outcome': 'cancelled'})
    assert deps.get(dep['id'])['ready']


def test_private_unfinished_helper_and_transitive_trust(project, monkeypatch):
    _, repo, _, original = project
    before = original.replace('theorem b : True := by sorry',
        'private theorem hiddenHelper : True := by sorry\ntheorem b : True := by exact hiddenHelper')
    (repo / 'Shared.lean').write_text(before)
    op = operation(project, monkeypatch)
    candidate = before.replace('private theorem hiddenHelper : True := by sorry',
                               'private theorem hiddenHelper : True := by trivial')
    (Path(op['manifest']['repo']) / 'Shared.lean').write_text(candidate)
    result = publication.apply(op['id'], selection(), 'proved')
    assert 'Lea.Study.hiddenHelper' in result['preservation']['editable']
    assert (repo / 'Shared.lean').read_text() == candidate


def test_same_file_helper_only_publication_and_existing_identity(project, monkeypatch):
    from app import project_dependencies as deps
    from app.reformalization_helpers import publish
    p, repo, _, original = project
    before = original.replace('theorem b : True := by sorry',
        'theorem sharedHelper : True := by sorry\ntheorem b : True := by sorry')
    (repo / 'Shared.lean').write_text(before)
    op = operation(project, monkeypatch)
    with execution.scope(ops.canonical_policy(op['manifest']['canonical_root'], repo)):
        requested = deps.ensure(p, {'name': 'Lea.Study.sharedHelper', 'kind': 'theorem', 'signature': ': True', 'imports': []},
            run_id=op['root_run_id'], mode='inline')
    dep = requested['dependency']
    assert dep['declaration_name'] == 'Lea.Study.sharedHelper'
    assert dep['path'] == 'Shared.lean'
    staged = Path(op['manifest']['repo'])
    candidate = before.replace('theorem sharedHelper : True := by sorry', 'theorem sharedHelper : True := by trivial').replace('theorem b : True := by sorry', 'theorem b : True := by exact sharedHelper')
    (staged / 'Shared.lean').write_text(candidate)
    result = publish(op['id'], op['root_run_id'], 'Shared.lean')
    assert result['outcome'] == 'published', result
    current = (repo / 'Shared.lean').read_text()
    assert 'theorem sharedHelper : True := by trivial' in current
    assert 'theorem b : True := by sorry' in current
    assert (staged / 'Shared.lean').read_text() == candidate
    assert deps.get(dep['id'])['ready']


def test_managed_worker_gets_own_stage_under_waiting_parent(project, monkeypatch):
    from app import bridge, runtime, runregistry, project_dependencies as deps
    from app.routes import runs
    op = operation(project, monkeypatch)
    p, repo, _, _ = project
    with execution.scope(ops.canonical_policy(op['manifest']['canonical_root'], repo)):
        dep = deps.ensure(p, {'name': 'DeferredFact', 'kind': 'theorem', 'signature': ': True', 'imports': []},
            run_id=op['root_run_id'], mode='deferred')['dependency']
    store.update_run(op['root_run_id'], 'cancelled', result_kind='interrupted', stop_reason='dependency_wait', recoverable=True)
    monkeypatch.setattr(runtime, 'accepting_work', lambda: True)
    monkeypatch.setattr(runs, 'load_config', config.load_config)
    monkeypatch.setattr(runregistry, 'registry', runregistry.RunRegistry(max_concurrent=4))
    monkeypatch.setattr(bridge, 'enqueue_run', lambda _: None)
    admitted = runs.create_run(runs.RunRequest(message='Prove the pinned shared obligation',
        project_slug=p['slug'], purpose='project_dependency', autonomous=True,
        focus_formalization_id=dep['formalization_id'], dependency_id=dep['id'], initiating_run_id=op['root_run_id']))
    worker = ops.for_run(admitted['run_id'])
    assert worker and worker['id'] != op['id']
    assert worker['request']['_foreground_owner'] == op['id']
    assert ops.owner(p['id'])['id'] == op['id']
    assert ops.get(op['id'])['root_run_id'] == op['root_run_id']
    with ops.acting(worker['id']):
        ops.assert_writer(p['id'])
    workspace = ops.prepare(worker['id'])
    assert workspace['repo'] != op['manifest']['repo']
    # A paused dependency worker resumes under the same waiting foreground
    # owner rather than joining the queue behind that owner.
    from app import recovery
    child = store.get_run(admitted['run_id'])
    store.claim_pending_run(child['id'])
    attempt = recovery.start_attempt(child['id'])
    recovery.Recorder(child['id'], attempt,
        recovery.manifest(run=child, config=config.load_config(), repo=Path(workspace['repo'])))(
        'checkpoint', {'stage': 'turn_boundary', 'turn': 2, 'messages': [],
            'proof_state': {}, 'status_context': {}})
    store.update_run(child['id'], 'max_turns', recoverable=True, stop_reason='turn_cap')
    deps.settle_run(child['id'])
    ops.update(worker['id'], 'paused')
    resumed = ops.queue_continuation(ops.get(worker['id']), store.get_run(child['id']),
        {'idempotency_key': 'resume-worker', 'timeout_seconds': 90})
    ops.wake(worker['id'])
    resumed=ops.response(ops.get(worker['id']))
    assert resumed['run_id'] and resumed['run_id'] != child['id'], json.dumps(resumed)
    assert ops.get(worker['id'])['state'] == 'worker'
    assert ops.owner(p['id'])['id'] == op['id']
    assert ops.get(op['id'])['root_run_id'] == op['root_run_id']
    runregistry.registry.release(resumed['run_id'])


def paused_operation(project, monkeypatch, *, review=False, reason='turn_cap'):
    from app import bridge, recovery, runregistry, runtime
    from app.routes import runs
    op = operation(project, monkeypatch)
    old = store.get_run(op['root_run_id'])
    attempt = recovery.start_attempt(old['id'])
    recovery.Recorder(old['id'], attempt,
        recovery.manifest(run=old, config=config.load_config(), repo=Path(op['manifest']['repo'])))(
        'checkpoint', {'stage': 'turn_boundary', 'turn': 2,
            'messages': [{'role': 'assistant', 'content': 'Saved useful progress'}],
            'proof_state': {}, 'status_context': {}})
    store.update_run(old['id'], 'cancelled' if review or reason == 'dependency_wait' else 'max_turns',
        recoverable=True, stop_reason=reason)
    if review:
        with db.write() as conn:
            conn.execute("update runs set recovery_state='review' where id=?", (old['id'],))
    ops.update(op['id'], 'active' if reason == 'dependency_wait' else 'retained' if review else 'paused')
    monkeypatch.setattr(runregistry, 'registry', runregistry.RunRegistry(max_concurrent=4))
    monkeypatch.setattr(runtime, 'accepting_work', lambda: True)
    monkeypatch.setattr(runs, 'load_config', config.load_config)
    monkeypatch.setattr(bridge, 'enqueue_run', lambda _: None)
    return ops.get(op['id']), store.get_run(old['id'])


def test_cancel_settled_is_noop_and_preserves_completion_receipt(project, monkeypatch):
    from app import formalizations
    op = operation(project, monkeypatch)
    candidate = project[3].replace('theorem b : True := by sorry', 'theorem b : True := by trivial')
    (Path(op['manifest']['repo']) / 'Shared.lean').write_text(candidate)
    receipt = publication.apply(op['id'], selection(), 'proved')
    assert formalizations.get(project[2]['id'])['validity_status'] == 'proved'
    ops.cancel(op['id'])
    ops.cancel(op['id'])
    assert ops.get(op['id'])['state'] == 'settled'
    assert ops.get(op['id'])['result'] == receipt
    assert formalizations.get(project[2]['id'])['validity_status'] == 'proved'
    assert (project[1] / 'Shared.lean').read_text() == candidate


def test_cancelled_dependency_wait_cannot_be_revived_by_late_automatic_resume(project, monkeypatch):
    from fastapi import HTTPException
    op, old = paused_operation(project, monkeypatch, reason='dependency_wait')
    ops.cancel(op['id'])
    with pytest.raises(HTTPException, match='automatic continuation'):
        ops.queue_continuation(ops.get(op['id']), old,
            {'idempotency_key': 'late-automatic', 'dependency_resume': True})
    assert ops.get(op['id'])['state'] == 'retained'
    with db.connect() as conn:
        assert conn.execute('select count(*) from runs').fetchone()[0] == 1


def test_public_continue_replays_same_admission_and_rejects_changed_inputs(project, monkeypatch):
    from fastapi import HTTPException
    from app.routes import runs
    from app import runregistry
    op, old = paused_operation(project, monkeypatch)
    request = runs.ContinueRequest(idempotency_key='public-continue', timeout_seconds=90)
    first = runs.continue_run(old['id'], request)
    second = runs.continue_run(old['id'], request)
    assert first['run_id'] == second['run_id']
    ops.wake(op['id'])
    first=ops.response(ops.get(op['id']))
    with pytest.raises(HTTPException):
        runs.continue_run(old['id'], request.model_copy(update={'timeout_seconds': 91}))
    with db.connect() as conn:
        assert conn.execute('select count(*) from runs').fetchone()[0] == 2
    runregistry.registry.release(first['run_id'])


def test_acknowledged_manual_recovery_starts_new_turn_with_same_draft(project, monkeypatch):
    from app.routes import runs
    from app import runregistry, recovery, bridge
    from app.reformalization_runner import run as run_attempt
    from lea.events import Finished
    from lea.providers import Usage
    from queue import Queue
    op, old = paused_operation(project, monkeypatch, review=True)
    stage = Path(op['manifest']['repo'])
    candidate = project[3] + '-- unfinished idea survives recovery\n'
    (stage / 'Shared.lean').write_text(candidate)
    # An invocation outside the checkpoint must never be blindly replayed.
    uncertain_attempt = old['attempt_id']
    with db.write() as conn:
        conn.execute("insert into run_invocations(id,run_id,attempt_id,kind,name,turn,started_at) values (?,?,?,?,?,?,?)",
            ('uncertain-action', old['id'], uncertain_attempt, 'tool', 'bash', 3, db.utc_now()))
    resumed = runs.resume_run(old['id'], runs.ResumeRequest(acknowledge_uncertainty=True, idempotency_key='manual-recovery'))
    ops.wake(op['id'])
    resumed=ops.response(ops.get(op['id']))
    successor = store.get_run(resumed['run_id'])
    assert successor['resume_checkpoint_id'] is None
    assert successor['max_turns'] == 8
    assert successor['resumes_run_id'] == old['id']
    assert ops.for_run(successor['id'])['id'] == op['id']
    def scripted(cfg, messages, **options):
        assert options['resume_state'] is None
        assert any(m.get('content') == 'Saved useful progress' for m in messages)
        assert 'do not repeat a mutating action blindly' in messages[-1]['content']
        assert (stage / 'Shared.lean').read_text() == candidate
        yield Finished('max_turns', 'Paused', 1, op['session_id'], 'fixture', Usage(), 0, {'messages': messages})
    monkeypatch.setattr(bridge, 'run_events', scripted)
    run_attempt(bridge.RunnerContext(session_id=op['session_id'], run_id=successor['id'],
        task=store.get_run_submission('manual-recovery')['message']['content'], config=config.load_config(), events=Queue(), autonomous=True), ops.get(op['id']))
    assert (stage / 'Shared.lean').read_text() == candidate
    assert (project[1] / 'Shared.lean').read_text() == project[3]
    runregistry.registry.release(successor['id'])


def test_blocked_publication_does_not_prepare_another_journal(project, monkeypatch):
    from fastapi import HTTPException
    op = operation(project, monkeypatch)
    contender = ops.enqueue({'message': 'another attempt', 'focus_formalization_id': project[2]['id']}, project[0]['id'])
    ops.update(contender['id'], 'worker', manifest=op['manifest'])
    with db.write() as conn:
        conn.execute("insert into reformalization_applications(id,operation_id,state,manifest_json,created_at,updated_at) values (?,?,'prepared','{}',?,?)",
            ('interrupted', op['id'], db.utc_now(), db.utc_now()))
    with pytest.raises(HTTPException):
        publication.apply(contender['id'], selection(), 'proved')
    with db.connect() as conn:
        assert conn.execute('select count(*) from reformalization_applications').fetchone()[0] == 1
    assert (project[1] / 'Shared.lean').read_text() == project[3]


@pytest.mark.parametrize('multiple', [False, True])
def test_recovery_is_idempotent_after_original_reinstalled(project, monkeypatch, multiple):
    op = operation(project, monkeypatch)
    original = project[3]
    repo = project[1]
    other = ops.enqueue({'message': 'legacy contender', 'focus_formalization_id': project[2]['id']}, project[0]['id'])
    ops.update(other['id'], 'worker', manifest=op['manifest'])
    with db.write() as conn:
        after = store._put_blob(conn, original.replace('by sorry', 'by trivial'))
        for num, oid in enumerate([op['id'], other['id']] if multiple else [op['id']]):
            displaced = repo / '.lea/displaced' / str(num) / 'Shared.lean'
            displaced.parent.mkdir(parents=True)
            displaced.write_text(original)
            journal = {'files': {'Shared.lean': {'before_blob': op['manifest']['files']['Shared.lean']['blob_id'],
                'after_blob': after, 'displaced': str(displaced), 'displaced_blob': op['manifest']['files']['Shared.lean']['blob_id'], 'mode': 0o644}}}
            conn.execute("insert into reformalization_applications(id,operation_id,state,manifest_json,created_at,updated_at) values (?,?,'restoring',?,?,?)",
                ('restore-' + str(num), oid, ops.dumps(journal), db.utc_now(), db.utc_now()))
    publication.recover()
    publication.recover()
    with db.connect() as conn:
        assert {r[0] for r in conn.execute('select state from reformalization_applications')} == {'restored'}
    assert (repo / 'Shared.lean').read_text() == original
    ops.assert_readable(project[0]['id'])


def test_unaffected_proved_neighbor_keeps_completion_across_source_update(project, monkeypatch):
    from app import formalizations
    from lea import interface, result_selection
    from app.proof_evidence import record_check
    p, repo, form, original = project
    a = store.create_formalization(project_id=p['id'], loose_session_id=None,
        display_title='A', declaration_name='Lea.Study.a', statement='True')
    store.link_formalization_file(a['id'], 'Shared.lean', 'primary')
    session = store.create_session('Original A', project_id=p['id'])
    prior = store.create_run(session['id'], 'fixture', None, 10, project_id=p['id'], focus_formalization_id=a['id'])
    store.link_session_formalization(session['id'], a['id'])
    store.add_code_step(session['id'], prior['id'], 'Shared.lean', content=original,
        check_status='ok', artifact_kind='proof', formalization_id=a['id'])
    checked = interface.check(str(repo / 'Shared.lean'), cold=True)
    checked.execution['selected_results'] = [result_selection.inspect_selected(repo / 'Shared.lean', 'Lea.Study.a')]
    record_check(session['id'], 'Shared.lean', repo / 'Shared.lean', checked, run_id=prior['id'], formalization_id=a['id'])
    store.update_run(prior['id'], 'proved', result_kind='proved')
    store.update_formalization(form['id'], source_hash='new-statement', statement='1 = 1')
    op = operation(project, monkeypatch, source_update_evidence={'previous_statement': 'True', 'current_statement': '1 = 1'})
    candidate = original.replace('theorem b : True := by sorry', 'theorem b : 1 = 1 := by rfl')
    (Path(op['manifest']['repo']) / 'Shared.lean').write_text(candidate)
    receipt = publication.apply(op['id'], selection(), 'proved')
    assert 'Lea.Study.a' in receipt['preservation']['stable_protected']
    assert formalizations.get(a['id'])['validity_status'] == 'proved'
    assert formalizations.get(a['id'])['completion_run_id'] == prior['id']
    with db.connect() as conn:
        assert conn.execute('select count(*) from formalization_review_events').fetchone()[0] == 0


def test_review_publication_joins_fifo_at_approval_time(project, monkeypatch):
    from app import reformalization_review as review
    op = operation(project, monkeypatch)
    store.update_run(op['root_run_id'], 'needs_review', result_kind='needs_review')
    ops.update(op['id'], 'review', result={'selection': selection(), 'outcome': 'needs_review'})
    with db.write() as conn:
        conn.execute("update reformalization_operations set queued_at='2000-01-01' where id=?", (op['id'],))
    c = store.create_formalization(project_id=project[0]['id'], loose_session_id=None,
        display_title='C', declaration_name='Lea.Study.c', statement='True')
    store.link_formalization_file(c['id'], 'Shared.lean', 'primary')
    first = ops.enqueue({'message': 'already waiting', 'focus_formalization_id': c['id']}, project[0]['id'])
    monkeypatch.setattr(review, 'context', lambda *_: {'review': {'eligible': True, 'revision_token': 'reviewed', 'allowed_outcomes': ['proved']}})
    monkeypatch.setattr(review, 'binding', lambda *_: ('reviewed', []))
    monkeypatch.setattr(ops, 'dispatch', lambda _: None)
    review.decide(project[2]['id'], {'action': 'approve', 'revision_token': 'reviewed', 'outcome': 'proved', 'idempotency_key': 'fifo-approval'})
    with db.connect() as conn:
        ids = [r[0] for r in conn.execute("select id from reformalization_operations where state='queued' order by queued_at,rowid")]
    assert ids == [first['id'], op['id']]


def test_logical_owner_excludes_rename_and_delete_between_segments(project, monkeypatch):
    from fastapi import HTTPException
    from app import project_coordination
    op, _ = paused_operation(project, monkeypatch, reason='dependency_wait')
    for guard in (project_coordination.migration, project_coordination.deletion):
        with pytest.raises(HTTPException):
            with guard(project[0]['id']):
                pytest.fail('Logical ownership must outlive a worker lease')
    assert ops.owner(project[0]['id'])['id'] == op['id']


def test_standalone_retry_reuses_conversation_and_reload_observes_preparing(project, monkeypatch):
    from app.routes import sessions
    p, _, form, _ = project
    prior = store.create_session('Existing work', project_id=p['id'])
    store.link_session_formalization(prior['id'], form['id'])
    op = ops.enqueue({'message': 'Retry in the same conversation', 'focus_formalization_id': form['id']}, p['id'])
    assert op['session_id'] == prior['id']
    ops.update(op['id'], 'active')
    assert sessions.session_detail.worker(prior['id'])['queued_reformalizations'] == [{'id': op['id']}]
    ops.update(op['id'], result={'continuation': {'old_run_id': 'earlier'}})
    assert sessions.session_detail.worker(prior['id'])['queued_reformalizations'] == [{'id': op['id']}]
    with db.connect() as conn:
        assert conn.execute('select count(*) from sessions').fetchone()[0] == 1


def test_snapshot_omits_unrelated_projects_and_recovery_archives(project, monkeypatch):
    p, repo, form, _ = project
    root = repo.parents[2]
    unrelated = root / 'proofs/Lea/Unrelated/Huge.lean'
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text('invalid unrelated work that is never imported\n')
    for n in range(4):
        archive = repo / '.lea/displaced' / str(n) / 'Shared.lean'
        archive.parent.mkdir(parents=True)
        archive.write_text(project[3] * 100)
    (repo / '.lea/memory.md').write_text('Preserve useful context')
    op = operation(project, monkeypatch)
    stage = Path(op['manifest']['repo'])
    assert (stage / '.lea/memory.md').read_text() == 'Preserve useful context'
    assert not (stage / '.lea/displaced').exists()
    assert not (Path(op['manifest']['workspace']) / unrelated.relative_to(root)).exists()
    assert set(op['manifest']['source_hashes']) == {'proofs/Lea/Study/Shared.lean'}
    store.update_run(op['root_run_id'], 'cancelled', result_kind='interrupted')
    ops.update(op['id'], 'retained')
    (repo / '.lea/displaced/another-large-archive').write_text(project[3] * 1000)
    second = operation(project, monkeypatch)
    next_stage = Path(second['manifest']['repo'])
    assert not (next_stage / '.lea/displaced').exists()
    assert {rel: path.read_bytes() for rel, path in ops.lean_files(stage).items()} == {
        rel: path.read_bytes() for rel, path in ops.lean_files(next_stage).items()}


@pytest.mark.parametrize('crash_after_acceptance', [False, True])
def test_managed_siblings_execute_serially_and_capture_published_helpers(project, monkeypatch, crash_after_acceptance):
    from app import bridge, runregistry, runtime, project_dependencies as deps
    from app.routes import runs
    from app.reformalization_helpers import refresh_parent
    op = operation(project, monkeypatch)
    p, repo, _, original = project
    imported = repo.parents[2] / 'proofs/Lea/Foreign/Fact.lean'
    imported.parent.mkdir(parents=True)
    imported.write_text('namespace Lea.Foreign\ntheorem fact : True := by trivial\nend Lea.Foreign\n')
    (Path(op['manifest']['repo']) / 'Shared.lean').write_text(original + '-- parent draft must survive helper refresh\n')
    with execution.scope(ops.canonical_policy(op['manifest']['canonical_root'], repo)):
        dependencies = [deps.ensure(p, {'name': name, 'kind': 'theorem', 'signature': signature, 'imports': []},
            run_id=op['root_run_id'], mode='deferred')['dependency']
            for name, signature in [('FirstHelper', ': True'), ('SecondHelper', ': False → False')]]
    store.update_run(op['root_run_id'], 'cancelled', recoverable=True, stop_reason='dependency_wait')
    monkeypatch.setattr(runtime, 'accepting_work', lambda: True)
    monkeypatch.setattr(runs, 'load_config', config.load_config)
    monkeypatch.setattr(bridge, 'enqueue_run', lambda _: None)
    registry = runregistry.RunRegistry(max_concurrent=4)
    monkeypatch.setattr(runregistry, 'registry', registry)
    children = [runs.create_run(runs.RunRequest(message='Prove ' + dep['declaration_name'], project_slug=p['slug'],
        purpose='project_dependency', autonomous=True, focus_formalization_id=dep['formalization_id'],
        dependency_id=dep['id'], initiating_run_id=op['root_run_id'])) for dep in dependencies]
    first, second = [ops.for_run(child['run_id']) for child in children]
    assert registry.try_admit(first['root_run_id'], first['session_id'], project_id=p['id'], serialize_project=True).ok
    assert registry.try_admit(second['root_run_id'], second['session_id'], project_id=p['id'], serialize_project=True).outcome == runregistry.AT_CAPACITY
    assert ops.get(second['id'])['manifest'] is None
    for child, dep, proof in zip([first, second], dependencies, ['trivial', 'intro h; exact h']):
        if child['id'] == second['id']:
            assert registry.try_admit(child['root_run_id'], child['session_id'], project_id=p['id'], serialize_project=True).ok
        store.claim_pending_run(child['root_run_id'])
        manifest = ops.prepare(child['id'])
        if child['id'] == second['id']:
            assert dependencies[0]['path'] in manifest['files']
        target = Path(manifest['repo']) / dep['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        signature = 'True' if child['id'] == first['id'] else 'False → False'
        imports = ''
        if child['id'] == first['id']:
            copied = Path(manifest['workspace']) / 'proofs/Lea/Foreign/Fact.lean'
            copied.parent.mkdir(parents=True)
            copied.write_bytes(imported.read_bytes())
            imports, proof = 'import Lea.Foreign.Fact\n', 'exact Lea.Foreign.fact'
        target.write_text(imports + 'namespace Lea.Study\ntheorem ' + dep['declaration_name'].rsplit('.', 1)[-1] + ' : ' + signature + ' := by ' + proof + '\nend Lea.Study\n')
        chosen = {'path': dep['path'], 'declaration': dep['declaration_name'], 'kind': 'proved'}
        if crash_after_acceptance and child['id'] == first['id']:
            from app import reformalization_helpers
            class ProcessDeath(BaseException):
                pass
            with monkeypatch.context() as patch:
                def interrupted_refresh(_):
                    raise ProcessDeath()
                patch.setattr(reformalization_helpers, 'refresh_parent', interrupted_refresh)
                with pytest.raises(ProcessDeath):
                    publication.apply(child['id'], chosen, 'proved', helper=dep)
            assert ops.get(child['id'])['state'] == 'settled'
            publication.recover()
        else:
            publication.apply(child['id'], chosen, 'proved', helper=dep)
        refresh_parent(child['id'])
        assert deps.get(dep['id'])['ready']
        registry.release(child['root_run_id'])
    ops.fresh(ops.get(op['id']))
    assert len(ops.get(op['id'])['manifest']['helper_applications']) == 2
    refreshed = ops.get(op['id'])['manifest']
    assert (Path(refreshed['workspace']) / 'proofs/Lea/Foreign/Fact.lean').read_bytes() == imported.read_bytes()
    assert refreshed['source_hashes']['proofs/Lea/Foreign/Fact.lean'] == ops.digest(imported.read_bytes())
    publication.recover()
    assert len(ops.get(op['id'])['manifest']['helper_applications']) == 2
    assert (repo / 'Shared.lean').read_text() == original
    assert (Path(op['manifest']['repo']) / 'Shared.lean').read_text().endswith('-- parent draft must survive helper refresh\n')


def test_cancel_during_publication_restores_files_instead_of_accepting(project, monkeypatch):
    op = operation(project, monkeypatch)
    candidate = project[3].replace('theorem b : True := by sorry', 'theorem b : True := by trivial')
    (Path(op['manifest']['repo']) / 'Shared.lean').write_text(candidate)
    original_checks = publication._canonical_checks
    def stop_before_accept(*args):
        checks = original_checks(*args)
        ops.cancel(op['id'])
        return checks
    monkeypatch.setattr(publication, '_canonical_checks', stop_before_accept)
    with pytest.raises(ValueError, match='cancelled before acceptance'):
        publication.apply(op['id'], selection(), 'proved')
    assert ops.get(op['id'])['state'] == 'retained'
    assert (project[1] / 'Shared.lean').read_text() == project[3]


def test_snapshot_materializes_only_actual_transitive_foreign_imports(project, monkeypatch):
    _, repo, _, original = project
    root = repo.parents[2]
    foreign = root / 'proofs/Lea/Foreign'
    foreign.mkdir(parents=True)
    (foreign / 'Base.lean').write_text('namespace Lea.Foreign\ntheorem base : True := by trivial\nend Lea.Foreign\n')
    (foreign / 'Needed.lean').write_text('import Lea.Foreign.Base\nnamespace Lea.Foreign\ntheorem needed : True := base\nend Lea.Foreign\n')
    (foreign / 'Unrelated.lean').write_text('-- unrelated source must not be copied\n')
    (repo / 'Shared.lean').write_text('import Lea.Foreign.Needed\n' + original)
    op = operation(project, monkeypatch)
    stage_root = Path(op['manifest']['workspace'])
    assert (stage_root / 'proofs/Lea/Foreign/Needed.lean').is_file()
    assert (stage_root / 'proofs/Lea/Foreign/Base.lean').is_file()
    assert not (stage_root / 'proofs/Lea/Foreign/Unrelated.lean').exists()
    assert {'proofs/Lea/Foreign/Base.lean', 'proofs/Lea/Foreign/Needed.lean'} <= set(op['manifest']['source_hashes'])
    ops.fresh(op)


def test_pending_owned_continuation_cannot_be_replaced_by_another_request(project, monkeypatch):
    from app import runregistry
    from fastapi import HTTPException
    op, old = paused_operation(project, monkeypatch, reason='dependency_wait')
    registry = runregistry.RunRegistry(max_concurrent=1)
    monkeypatch.setattr(runregistry, 'registry', registry)
    assert registry.try_admit('another-project', 'another-chat').ok
    first = {'idempotency_key': 'first-resume', 'dependency_resume': True}
    waiting = ops.queue_continuation(op, old, first)
    assert waiting['run_id'] is None
    assert ops.queue_continuation(ops.get(op['id']), old, first)['run_id'] is None
    with pytest.raises(HTTPException, match='already waiting'):
        ops.queue_continuation(ops.get(op['id']), old, {**first, 'idempotency_key': 'second-resume'})
    assert ops.get(op['id'])['result']['continuation']['request'] == first
    ops.cancel(op['id'])
    registry.release('another-project')
    ops.wake(op['id'])
    assert ops.get(op['id'])['state'] == 'retained'
    assert ops.get(op['id'])['root_run_id'] == old['id']


@pytest.mark.parametrize('external_edit', [False, True])
def test_restore_recovers_after_rollback_rename_before_capture(project, monkeypatch, external_edit):
    op = operation(project, monkeypatch)
    _, repo, _, original = project
    candidate = original.replace('theorem b : True := by sorry', 'theorem b : True := by trivial')
    external = candidate + '-- observed external edit during restoration\n'
    (Path(op['manifest']['repo']) / 'Shared.lean').write_text(candidate)
    assessment = publication.assess(op['id'], selection())
    real_rename = publication.os.rename
    class ProcessDeath(BaseException):
        pass
    def stop_after_rollback_rename(source, destination):
        if '.candidate-' in str(destination):
            if external_edit:
                Path(source).write_text(external)
            real_rename(source, destination)
            raise ProcessDeath()
        return real_rename(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr(publication.os, 'rename', stop_after_rollback_rename)
        def fail_checks(*_):
            raise ValueError('Force restoration')
        patch.setattr(publication, '_canonical_checks', fail_checks)
        with pytest.raises(ProcessDeath):
            publication.apply(op['id'], selection(), 'proved', assessment=assessment)
    assert not (repo / 'Shared.lean').exists()
    publication.recover()
    publication.recover()
    assert (repo / 'Shared.lean').read_text() == (external if external_edit else original)
    retained = {store.blob_content(v['blob_id']) for v in ops.versions(op['id'])}
    assert {original, candidate} <= retained
    if external_edit:
        assert external in retained


@pytest.mark.parametrize('same_identity', [True, False])
def test_overlapping_continuation_admissions_share_one_durable_successor(project, monkeypatch, same_identity):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, local
    from fastapi import HTTPException
    from app import runregistry
    op, old = paused_operation(project, monkeypatch, reason='dependency_wait')
    registry = runregistry.RunRegistry(max_concurrent=1)
    monkeypatch.setattr(runregistry, 'registry', registry)
    assert registry.try_admit('occupied', 'occupied-chat').ok
    barrier, calls = Barrier(2), local()
    real_fresh = ops.fresh
    def simultaneous_capture(*args, **kwargs):
        real_fresh(*args, **kwargs)
        if not getattr(calls, 'captured', False):
            calls.captured = True
            barrier.wait(timeout=5)
    monkeypatch.setattr(ops, 'fresh', simultaneous_capture)
    def submit(index):
        try:
            return ops.queue_continuation(op, old, {'idempotency_key': 'overlap-' + str(0 if same_identity else index), 'dependency_resume': True})
        except HTTPException as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [0, 1]))
    assert sum(isinstance(r, dict) for r in results) == (2 if same_identity else 1)
    assert all(r['run_id'] is None for r in results if isinstance(r, dict))
    assert ops.get(op['id'])['result']['continuation']['old_run_id'] == old['id']
    with db.connect() as conn:
        assert conn.execute('select count(*) from runs').fetchone()[0] == 1
    ops.cancel(op['id'])
    registry.release('occupied')
