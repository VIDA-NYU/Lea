"""Ordinary source formalization retains an explicitly selected, checked disproof."""
from dataclasses import replace
from pathlib import Path
from queue import Queue
import shutil

import pytest

from app import bridge, commands, db, formalizations, formalize_batch_reports, projects, run_commands, store
from app.config import LeaConfig
from app.source_context import source_hash
from published_source_fixtures import publish_for
from lea import agent, lsp_daemon
from lea.providers import Done, TextDelta, ToolCall, Usage
from lea.registry import build_toolset


@pytest.mark.parametrize('tools', [None, ['write_file', 'lean_check']])
@pytest.mark.parametrize('statement,kind,selection', [
    ('Every natural number equals zero.', 'theorem', {}),
    ('theorem target : True', 'theorem', {'declaration': 'target', 'kind': 'proved'}),
    ('def target : Nat', 'definition', {'declaration': 'target', 'kind': 'defined'}),
])
def test_ordinary_targets_offer_selection_without_pinning_natural_language_helpers(tools, statement, kind, selection):
    cfg = LeaConfig(model='test', max_turns=3, tools=tools)
    target = dict(declaration_name='target', statement=statement, kind=kind)
    configured = bridge._result_selection_config(cfg, target)
    assert configured.target_declaration == 'target'
    assert configured.selected_result == selection
    assert 'select_result' in build_toolset(configured.tools, configured.extra_tools)[1]
    assert bridge._result_selection_config(replace(cfg, stub_generation=True), target).selected_result == {}
    configured.selected_result['declaration'] = 'candidate'
    assert cfg.selected_result == {}  # Another run must not inherit this run's selection.


@pytest.mark.parametrize('recovering', [True, False])
def test_disproof_selection_survives_checkpoint_configuration(monkeypatch, recovering):
    from app import recovery
    selection = dict(path='/tmp/Counterexample.lean', declaration='target_disproof',
                     target_declaration='target', kind='disproved')
    checkpoint = ({'proof_state': {'selection': selection}}, {})
    monkeypatch.setattr(recovery, 'load_checkpoint', lambda _: checkpoint)
    monkeypatch.setattr(recovery, 'continuation_checkpoint', lambda *args: (*checkpoint, 1))
    run = {'id': 'run', 'recovery_state': 'recovering'} if recovering else {
        'resumes_run_id': 'previous', 'resume_checkpoint_id': 1}
    cfg = bridge._result_selection_config(LeaConfig(model='test', max_turns=4),
        dict(declaration_name='target', statement='Every natural number equals zero.', kind='theorem'), run)
    assert cfg.selected_result == selection
    assert cfg.selected_result is not selection


def test_overleaf_disproof_crosses_agent_compiler_and_database(tmp_path, monkeypatch):
    if not shutil.which('lake'):
        pytest.skip('Native Lean toolchain required')
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'state.sqlite3')
    db.init_db()
    root = tmp_path / 'prover'
    workspace = root / 'workspace'
    workspace.mkdir(parents=True)
    (workspace / 'lakefile.toml').write_text('name="disproof_regression"\n[[lean_lib]]\nname="Lea"\nsrcDir="proofs"\n')
    shutil.copyfile(Path(__file__).parents[2] / 'prover/workspace/lean-toolchain', workspace / 'lean-toolchain')
    cfg = LeaConfig(model='test', max_turns=4, lea_root=root)
    monkeypatch.setattr('app.config.load_config', lambda: cfg)
    project = projects.provision_project('Disproof', workspace / 'proofs')
    statement = 'Every natural number equals zero.'
    target = store.create_formalization(project_id=project['id'], loose_session_id=None,
        display_title='target', declaration_name='target', kind='theorem', statement=statement,
        origin='overleaf', origin_key='doc:theorem:target')
    source = dict(version=2, targetKey='target', targetKind='theorem', statement=statement, proof='',
        proofAssociation=dict(status='missing', method='none', sourceFile='', proofHash=''),
        statementLocation=dict(sourceFile='main.tex'), uses=[], context='', relevantSource=[], mirror=None)
    source['bundleHash'] = source_hash(source)
    source['sourceIdentityHash'] = source_hash(source, identity=True)
    reference = publish_for(target['id'], source, files={'main.tex': statement}, coverage='complete')
    bundle = store.create_run_bundle(message='Formalize the source claim.', session_id=None,
        project_id=project['id'], session_origin='overleaf', session_origin_url=None,
        model='test', provider=None, max_turns=4, autonomous=True,
        purpose='overleaf_solver', focus_formalization_id=target['id'],
        source_bundle=source, source_context_ref=reference)
    calls = []

    def provider(model, system, messages, tools, *args, **kwargs):
        assert 'select_result' in {tool['name'] for tool in tools}
        assert 'outcome=disproved' in system
        assert 'classify the mathematical outcome' not in system
        calls.append(messages)
        if len(calls) == 1:
            yield ToolCall('update_lea_status', dict(kind='initial', confidence='high', scope='source_only',
                summary='The claim is false at n = 1.', confidence_reason='One is not zero.'))
            yield ToolCall('write_file', dict(path='Counterexample.lean', content=(
                'namespace Lea.Disproof\n'
                'private theorem supportingFact : True := by trivial\n'
                'theorem target_disproof : ¬ (∀ n : Nat, n = 0) := by\n'
                '  intro h\n  cases h 1\n'
                'end Lea.Disproof\n')))
            yield ToolCall('select_result', dict(path='Counterexample.lean',
                declaration='Lea.Disproof.target_disproof', outcome='disproved'))
            yield ToolCall('lean_check', dict(path='Counterexample.lean'))
        elif len(calls) == 2:
            yield ToolCall('update_lea_status', dict(kind='final', confidence='high', scope='statement_and_proof',
                summary='The original universal claim has a checked disproof.',
                confidence_reason='Lean verified the negation using n = 1.',
                matches=[], caveats=[], limitations=[], remaining_obligations=[],
                recommended_next_action=dict(kind='inspect_result', detail='Inspect the checked disproof.')))
        else:
            yield TextDelta('The original claim was disproved by a checked counterexample.')
        yield Done(Usage(2, 1), 0)

    monkeypatch.setattr(agent, '_provider_stream', provider)
    queue = Queue()
    run_commands.ensure_run(bundle['run']['id'])
    epoch = commands.start_worker('disproof-test')
    token = commands.claim('disproof-test', epoch, 'compute', kinds={'run.execute'})
    assert token
    try:
        bridge.run_lea(bridge.RunnerContext(session_id=bundle['session']['id'], run_id=bundle['run']['id'],
            task='Formalize the source claim.', config=cfg, events=queue, autonomous=True,
            purpose='overleaf_solver', command_token=token))
        run = store.get_run(bundle['run']['id'])
        assert run['status'] == 'disproved', run.get('result_detail') or run.get('final_text')
        assert run['result_kind'] == 'disproved'
        assert len(calls) == 3
        current = formalizations.get(target['id'])
        assert current['validity_status'] == 'disproved'
        assert current['check_current'] is True
        assert current['declaration_name'] == 'target'
        step = store.session_detail(bundle['session']['id'])['code_steps'][-1]
        assert step['check_status'] == 'ok'
        assert 'theorem target_disproof' in step['code']
        assert 'theorem target :' not in step['code']
        events = list(queue.queue)
        assert events[-1]['type'] == 'done'
        assert events[-1]['payload']['result_kind'] == 'disproved'
        fact = formalize_batch_reports._item_fact(dict(targetKind='theorem', targetLabel='target',
            state=current['validity_status'], runId=run['id'], formalizationId=target['id']), project['id'])
        counts = formalize_batch_reports._counts([fact])
        assert counts['disproved'] == 1 and counts['incomplete'] == 0
        assert fact['reportingIncomplete'] is False
    finally:
        lsp_daemon._shutdown_all()
