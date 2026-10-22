"""A conversational affirmation must retain the agreed target through completion."""
import hashlib
import json

import pytest

from app import bridge, db, store, target_contexts
from app.target_completion import checked_target
from lea import agent
from lea.config import LeaConfig
from lea.providers import Done, TextDelta, Usage

pytestmark = pytest.mark.usefixtures('certified_unit_inputs')

SIGNATURE = 'theorem multiple_of_even_is_even (n m : ℕ) (hn : Even n) (hdiv : n ∣ m) : Even m'
PROPOSAL = 'Does this natural-number formulation match?\n```lean\n' + SIGNATURE + ' := by\n```'
CODE = 'import Mathlib.Algebra.Group.Even\nnamespace Lea.Misc\n' + SIGNATURE + ''' := by
  rcases hn with ⟨k, rfl⟩
  rcases hdiv with ⟨c, rfl⟩
  exact ⟨k * c, Nat.add_mul k k c⟩
end Lea.Misc
'''


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.sqlite3')
    db.init_db()
    session = store.create_session('Every multiple of an even number is even')
    target = store.create_formalization(project_id=None, loose_session_id=session['id'],
        display_title=session['title'])
    store.link_session_formalization(session['id'], target['id'])
    first = store.create_run(session['id'], 'test', None, 10, focus_formalization_id=target['id'])
    original = store.add_message(session['id'], 'user', 'Prove every multiple of an even number is even',
        first['id'], formalization_id=target['id'])
    proposal = store.add_message(session['id'], 'assistant', PROPOSAL, first['id'], formalization_id=target['id'])
    target_contexts.record_proposal(first['id'], proposal['id'])
    store.update_run(first['id'], 'answered')
    return session, target, first, original, proposal


def confirm(conversation, reply='Yes, it does'):
    from app import recovery
    session, target, *_ = conversation
    run = store.create_run(session['id'], 'test', None, 10, focus_formalization_id=target['id'])
    message = store.add_message(session['id'], 'user', reply, run['id'], formalization_id=target['id'])
    store.update_run(run['id'], 'running')
    recovery.start_attempt(run['id'])
    return run, message, target_contexts.prepare(run['id'])


@pytest.mark.parametrize('reply', ['Yes, it does','Yes, it does. Please proceed.','Okay','Sounds good, go ahead'])
def test_confirmation_persists_statement_identity_and_exact_message_provenance(conversation, reply):
    run, message, context = confirm(conversation, reply)
    session, target, first, original, proposal = conversation
    assert context['request'] == original['content']
    assert str(context['request_message_id']) == original['id']
    assert str(context['proposal_message_id']) == proposal['id']
    assert str(context['confirmation_message_id']) == message['id']
    assert context['statement'] == SIGNATURE
    assert context['declaration_name'] == 'multiple_of_even_is_even'
    assert store.get_formalization(target['id'])['statement'] == SIGNATURE
    assert target_contexts.prepare(run['id']) == context
    assert target_contexts.load(run['id']) == context
    # Retried execution keeps the agreed context even if mutable metadata changes.
    store.update_formalization(target['id'], statement='theorem different : True')
    assert target_contexts.prepare(run['id']) == context
    assert not target_contexts.unchanged(context)


@pytest.mark.parametrize('reply', ['Yes, but use integers', 'No', 'Prove a different theorem', 'Why?'])
def test_qualified_or_unrelated_replies_never_confirm_an_old_proposal(conversation, reply):
    run, _, context = confirm(conversation, reply)
    assert context is None
    assert target_contexts.load(run['id']) is None
    assert store.get_formalization(conversation[1]['id'])['declaration_name'] is None


def test_confirmation_only_applies_to_latest_assistant_message(conversation):
    session, target, first, *_ = conversation
    store.add_message(session['id'], 'assistant', 'Would you like an explanation?', first['id'], formalization_id=target['id'])
    assert confirm(conversation)[2] is None


def test_multiple_proposed_declarations_are_ambiguous(conversation):
    session, target, first, *_ = conversation
    proposal = store.add_message(session['id'], 'assistant',
        '```lean\ntheorem a : True := by\ntheorem b : True := by\n```', first['id'], formalization_id=target['id'])
    target_contexts.record_proposal(first['id'], proposal['id'])
    assert confirm(conversation)[2] is None


def test_historical_unannotated_proposal_can_be_confirmed_after_upgrade(conversation):
    with db.write() as conn:
        conn.execute('update timeline set data=null where id=?', (conversation[-1]['id'],))
    assert confirm(conversation)[2]['statement'] == SIGNATURE


def test_next_activation_recovers_an_old_confirmation_without_rewriting_its_result(conversation):
    session, target, *_ = conversation
    old = store.create_run(session['id'], 'test', None, 10, focus_formalization_id=target['id'])
    agreement = store.add_message(session['id'], 'user', 'Yes, it does', old['id'], formalization_id=target['id'])
    store.add_message(session['id'], 'assistant', 'The proof compiles.', old['id'], formalization_id=target['id'])
    store.update_run(old['id'], 'needs_review', result_kind='needs_review')
    _, _, context = confirm(conversation, 'Please try again')
    assert context['statement'] == SIGNATURE
    assert str(context['confirmation_message_id']) == agreement['id']
    assert store.get_run(old['id'])['status'] == 'needs_review'


@pytest.mark.parametrize('revision', ['Yes, but use integers', 'Prove a different theorem'])
def test_new_request_blocks_recovery_of_an_old_confirmation(conversation, revision):
    session, target, *_ = conversation
    old = store.create_run(session['id'], 'test', None, 10, focus_formalization_id=target['id'])
    store.add_message(session['id'], 'user', 'Yes, it does', old['id'], formalization_id=target['id'])
    store.add_message(session['id'], 'user', revision, old['id'], formalization_id=target['id'])
    assert confirm(conversation, 'Please try again')[2] is None


def test_obsolete_command_cannot_confirm_or_modify_target(conversation):
    session, target, *_ = conversation
    run = store.create_run(session['id'], 'test', None, 10, focus_formalization_id=target['id'])
    store.add_message(session['id'], 'user', 'Yes', run['id'], formalization_id=target['id'])
    with pytest.raises(RuntimeError, match='Obsolete'):
        target_contexts.prepare(run['id'], token={'id':'revoked'})
    assert store.get_formalization(target['id'])['declaration_name'] is None


def test_queued_confirmation_requires_its_live_claim_even_on_replay(conversation):
    from app import commands, recovery, run_commands
    session, target, *_ = conversation
    run = store.create_run(session['id'], 'test', None, 10, focus_formalization_id=target['id'])
    store.add_message(session['id'], 'user', 'Yes', run['id'], formalization_id=target['id'])
    run_commands.ensure_run(run['id'])
    epoch = commands.start_worker('confirmation-test')
    token = commands.claim('confirmation-test', epoch, 'compute', kinds={'run.execute'})
    assert store.claim_pending_run(run['id'])
    recovery.start_attempt(run['id'])
    with pytest.raises(RuntimeError, match='owning'):
        target_contexts.prepare(run['id'])
    context = target_contexts.prepare(run['id'], token=token)
    assert context['statement'] == SIGNATURE
    with db.write() as conn:
        conn.execute("update commands set state='suspended' where id=?", (token['id'],))
    with pytest.raises(RuntimeError, match='Obsolete'):
        target_contexts.prepare(run['id'], token=token)


def test_continued_run_retains_exact_context_and_checkpoint_configuration(conversation, tmp_path):
    from app import recovery
    old, _, context = confirm(conversation)
    session, target, *_ = conversation
    new = store.create_run(session['id'], 'test', None, 10, focus_formalization_id=target['id'])
    store.add_message(session['id'], 'user', 'Continue', new['id'], formalization_id=target['id'])
    with db.write() as conn:
        conn.execute("update runs set resumes_run_id=?,status='running' where id=?", (old['id'],new['id']))
    recovery.start_attempt(new['id'])
    inherited = target_contexts.prepare(new['id'])
    assert inherited == context
    old_config = LeaConfig(model='test',max_turns=3,mathematical_request=context)
    new_config = LeaConfig(model='test',max_turns=10,mathematical_request=inherited)
    before = recovery.manifest(run=old,config=old_config,repo=tmp_path)
    after = recovery.manifest(run=new,config=new_config,repo=tmp_path)
    assert before['execution_digest'] == after['execution_digest']
    active = store.get_run(old['id'])
    state = {'stage':'turn_boundary','turn':2,'messages':[{'role':'user','content':'Yes',
                                                        'mathematical_request':context}]}
    recovery.Recorder(old['id'],active['attempt_id'],before)('checkpoint',state)
    _, _, checkpoint_id = recovery.continuation_checkpoint(old['id'])
    with db.write() as conn:
        conn.execute('update runs set resume_checkpoint_id=? where id=?',(checkpoint_id,new['id']))
    restored, changed = recovery.prepare_continuation(store.get_run(new['id']),new_config,tmp_path,{},'Continue')
    assert not changed and restored['messages'][0]['mathematical_request'] == context


def test_child_tasks_do_not_inherit_the_coordinators_confirmed_target(tmp_path):
    from lea.runctx import run_context
    from lea.subagents import prepare_spawn, _child_stops
    parent = LeaConfig(model='test', max_turns=1,
        mathematical_request={'statement':SIGNATURE}, selected_result={'declaration':'multiple_of_even_is_even'})
    with run_context(config=parent, working_dir=str(tmp_path), run_key='context-isolation'):
        plan = prepare_spawn({'prompt':'Find a useful helper'})
    try:
        assert plan.child_config.mathematical_request == {}
        assert plan.child_config.selected_result == {}
        assert parent.mathematical_request == {'statement':SIGNATURE}
    finally:
        _child_stops.pop(plan.result_id, None)




def test_target_check_requires_exact_confirmed_statement_and_current_evidence(conversation):
    _, _, context = confirm(conversation)
    target = store.get_formalization(conversation[1]['id'])
    step = {'code':CODE, 'check_status':'ok', 'check_current':True,
            'execution':{'content_sha256':hashlib.sha256(CODE.encode()).hexdigest(),
                         'selected_result':{'full_name':'Lea.Misc.multiple_of_even_is_even',
                             'user_name':'Lea.Misc.multiple_of_even_is_even','kind':'theorem','axioms':[]}}}
    assert checked_target(target, [step])[0] == step
    assert checked_target(target, [{**step, 'execution':{}}])[0] is None
    weakened = CODE.replace(': Even m := by', ': True := by')
    assert 'confirmed' in checked_target(target, [{**step, 'code':weakened}])[1]
    assert checked_target(target, [{**step, 'check_current':False}])[0] is None
    assert checked_target(target, [{**step, 'code':'theorem helper : True := by trivial'}])[0] is None


def test_statement_fence_preserves_string_contents_and_binder_defaults():
    from app.artifacts import declaration_type, declaration_signature, scan_lean_declarations
    first = 'theorem t (n : Nat := 0) : "a b" = "a b" := by rfl'
    declaration = scan_lean_declarations(first)[0]
    header = declaration_signature(declaration)
    assert '(n : Nat := 0)' in header and 'by rfl' not in header
    assert declaration_type(header) != declaration_type(header.replace('a b','ab'))
    assert declaration_type(header) != declaration_type(header.replace('a b','-- different'))
    application = 'theorem t (f : Nat → Nat) (x fx : Nat) : f x = fx'
    assert declaration_type(application) != declaration_type(application.replace(': f x =', ': fx ='))
    assert declaration_type(application) == declaration_type(application.replace(': f x =', ':\n f /- formatting -/ x ='))


def test_confirmed_passing_target_is_registered_as_primary(conversation):
    run, _, context = confirm(conversation)
    session, target, *_ = conversation
    step = store.add_code_step(session['id'], run['id'], 'Main.lean', content=CODE,
        check_status='ok', artifact_kind='proof', formalization_id=target['id'])
    bridge._record_run_artifacts(session['id'], run['id'], None, None, {'Main.lean':step['id']},
        focus_formalization_id=target['id'], completion_verified=True)
    files = store.list_formalization_files(target['id'])
    assert [(f['path'], f['role']) for f in files] == [('Main.lean','primary')]
    with db.connect() as conn:
        artifact = conn.execute('select declaration_name from artifacts where formalization_id=?',(target['id'],)).fetchone()
    assert artifact[0] == context['declaration_name']


@pytest.mark.parametrize('retarget', [False, True])
def test_conversation_completes_through_worker_with_a_pinned_target(conversation, tmp_path, monkeypatch, retarget):
    from queue import Queue
    from evidence_fixtures import mock_execution
    from lea.events import FileChanged, CheckResult, Finished
    session, target, *_ = conversation
    run = store.create_run(session['id'], 'test', None, 5, focus_formalization_id=target['id'])
    store.add_message(session['id'], 'user', 'Yes, it does', run['id'], formalization_id=target['id'])
    def events(config, messages, *, working_dir, **kwargs):
        assert config.mathematical_request['request'].startswith('Prove every multiple')
        assert config.selected_result['declaration'] == 'multiple_of_even_is_even'
        path = __import__('pathlib').Path(working_dir) / 'Main.lean'
        path.write_text(CODE)
        yield FileChanged(str(path), content=CODE)
        evidence = mock_execution(path)
        evidence['selected_result'] = {'user_name':'Lea.Misc.multiple_of_even_is_even',
            'full_name':'Lea.Misc.multiple_of_even_is_even','kind':'theorem','axioms':[]}
        yield CheckResult(str(path), 'ok', execution=evidence, content=CODE)
        observed = store.latest_code_step_for_path(session['id'], 'Main.lean')
        assert observed['check_current'], {k:observed.get(k) for k in ('check_status','check_stale_reason','check_detail')}
        if retarget:
            store.update_formalization(target['id'], statement='theorem other : True')
        yield Finished('completed', 'The proof compiles.', 1, session['id'], 'test', Usage(), 0,
                       {'messages':messages}, result_kind='proved', candidate_path=str(path))
    monkeypatch.setattr(bridge, 'run_events', events)
    bridge.run_lea(bridge.RunnerContext(session_id=session['id'],run_id=run['id'], task='Yes, it does',
        config=LeaConfig(model='test', max_turns=5, lea_root=tmp_path), events=Queue(), autonomous=True))
    result = store.get_run(run['id'])
    assert result['status'] == ('answered' if retarget else 'proved'), result['result_detail']
    if retarget:
        assert result['result_kind'] == 'incomplete'
        assert 'changed during' in result['result_detail']
    else:
        assert store.list_formalization_files(target['id'])[0]['role'] == 'primary'


def test_new_runtime_does_not_inherit_historical_checkpoint_exceptions(conversation, tmp_path):
    from app import recovery
    run = conversation[2]
    config = LeaConfig(model='test', max_turns=5)
    current = recovery.manifest(run=run, config=config, repo=tmp_path)
    releases = json.loads(__import__('pathlib').Path(recovery.__file__).with_name('checkpoint_compatibility.json').read_text())
    for release in releases:
        old = {**current, 'tool_code_digests':release['predecessor']}
        assert not recovery.compatible_runtime(old, current, {'stage':'turn_boundary','tool_schema_digest':'set'})
