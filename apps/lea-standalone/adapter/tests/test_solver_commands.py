"""Stage 4: real SQL admission/host, scripted prover, no paid calls."""
import ast
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path
import threading
import time
import pytest
from fastapi import HTTPException
from app import bridge,commands,command_host,command_execution,db,migration_limits,recovery,run_commands,runregistry,store
from app.config import LeaConfig
from app.routes import runs
from lea.events import Finished,TurnStarted,ToolApprovalRequested
from lea.providers import Usage


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'data/solver.sqlite');db.init_db()
    cfg=LeaConfig(model='fixture',max_turns=3,lea_root=tmp_path,max_spend_usd=None)
    monkeypatch.setattr(bridge,'load_config',lambda:cfg);monkeypatch.setattr(runs,'load_config',lambda:cfg)
    monkeypatch.setattr('app.config.load_config',lambda:cfg)
    monkeypatch.setattr(runregistry,'registry',runregistry.RunRegistry(max_concurrent=2))
    return cfg


def wait(predicate,timeout=8):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        value=predicate()
        if value:return value
        time.sleep(.03)
    pytest.fail('Timed out waiting for committed worker state')


def submit(message='Task',key='one',**values):
    return runs.create_run(runs.RunRequest(message=message,idempotency_key=key,**values))


def test_run_and_command_admission_roll_back_together_at_capacity(fixture,monkeypatch):
    monkeypatch.setattr(migration_limits,'COMPUTE_QUEUED_GLOBAL',0)
    with pytest.raises(HTTPException) as error:submit()
    assert error.value.status_code==429
    with db.connect() as conn:
        for table in ('runs','sessions','timeline','commands','command_capacity','run_commands'):
            assert conn.execute('select count(*) from '+table).fetchone()[0]==0,table


def test_duplicate_submission_has_one_run_message_and_command(fixture):
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(lambda _:submit(),range(12)))
    assert len({r['run_id'] for r in results})==len({r['command']['id'] for r in results})==1
    with db.connect() as conn:
        for table in ('runs','sessions','timeline','commands','run_commands'):
            assert conn.execute('select count(*) from '+table).fetchone()[0]==1,table


def test_cancel_before_claim_never_starts_solver_and_receipt_is_independent(fixture,monkeypatch):
    called=[]
    monkeypatch.setattr(bridge,'run_events',lambda *a,**kw:called.append(True))
    first=submit()
    result=runs.interrupt_run(first['run_id'])
    assert result['status']=='interrupt_requested' and result['worker_observed'] is False
    assert store.get_run(first['run_id'])['status']=='pending'
    assert runs.interrupt_run(first['run_id'])['command']['id']==result['command']['id']
    command_host.start()
    wait(lambda:store.get_run(first['run_id'])['status']=='cancelled')
    wait(lambda:run_commands.for_run(first['run_id'])['state']=='succeeded')
    assert not called
    assert runs.interrupt_run(first['run_id'])['status']=='cancelled'


def test_controls_progress_with_every_solver_parked_and_cancel_beats_approval(fixture,monkeypatch):
    answers={}
    def fake(cfg,messages,*,session_id=None,**options):
        yield TurnStarted(1)
        answers[session_id]=yield ToolApprovalRequested('bash',{'command':'fixture'},'invoke-'+session_id)
        yield Finished('completed','done',1,session_id,'fixture',Usage(),0,{'messages':[]})
    monkeypatch.setattr(bridge,'run_events',fake)
    monkeypatch.setattr(migration_limits,'COMPUTE_QUEUED_GLOBAL',2)
    a=submit('First','first');b=submit('Second','second')
    command_host.start()
    gate_a=wait(lambda:(store.get_run(a['run_id']) or {}).get('pending_approval'))
    gate_b=wait(lambda:(store.get_run(b['run_id']) or {}).get('pending_approval'))
    accepted=runs.resolve_approval(b['run_id'],gate_b['approval_id'],runs.ApprovalDecisionRequest(decision='allow'))
    assert accepted['status']=='accepted'
    runs.interrupt_run(a['run_id'])
    with pytest.raises(HTTPException) as cancelled:
        runs.resolve_approval(a['run_id'],gate_a['approval_id'],runs.ApprovalDecisionRequest(decision='allow'))
    assert cancelled.value.status_code==409
    wait(lambda:len(answers)==2)
    assert answers=={a['session_id']:'cancelled',b['session_id']:'allow'}
    assert runs.resolve_approval(b['run_id'],gate_b['approval_id'],runs.ApprovalDecisionRequest(decision='allow'))['command']['id']==accepted['command']['id']
    with pytest.raises(HTTPException):runs.resolve_approval(b['run_id'],gate_b['approval_id'],runs.ApprovalDecisionRequest(decision='deny'))


def claimed_gate():
    value=submit()
    epoch=commands.start_worker('fixture')
    token=commands.claim('fixture',epoch,'compute',kinds={'run.execute'})
    assert store.claim_pending_run(value['run_id'])
    attempt=recovery.start_attempt(value['run_id'])
    card=run_commands.bind_approval(value['run_id'],'gate','invocation','bash',{'command':'fixture'})
    return value,epoch,token,card


def test_decision_is_bound_to_exact_attempt_invocation_and_cancellation(fixture):
    value,epoch,token,card=claimed_gate()
    run_commands.approve(value['run_id'],'gate','allow')
    control=commands.claim('fixture',epoch,'control')
    assert command_execution.control(control)['applied']
    assert commands.complete(control,{})
    assert run_commands.consume_approval(value['run_id'],'gate')=='allow'
    good={'id':'invocation','kind':'tool','name':'bash','input':{'command':'fixture'}}
    with db.write() as conn:run_commands.authorize_invocation(conn,value['run_id'],token,good)
    for bad in ({**good,'id':'different'},{**good,'input':{'command':'other'}}):
        with pytest.raises(RuntimeError):
            with db.write() as conn:run_commands.authorize_invocation(conn,value['run_id'],token,bad)
    run_commands.stop(value['run_id'])
    with pytest.raises(RuntimeError):
        with db.write() as conn:run_commands.authorize_invocation(conn,value['run_id'],token,good)


def test_waiting_approval_and_empty_poll_do_not_take_a_write_lock(fixture,monkeypatch):
    value,epoch,token,card=claimed_gate()
    def denied(*a,**kw):raise AssertionError('An unchanged observation tried to reserve the SQLite writer')
    monkeypatch.setattr(db,'write',denied)
    assert not commands.has_ready('control')
    assert not commands.has_ready('compute',kinds={'run.execute'})
    assert run_commands.consume_approval(value['run_id'],'gate') is None


def test_live_lease_expiry_never_retries_solver_or_accepts_stale_completion(fixture):
    value=submit();epoch=commands.start_worker('fixture')
    old=commands.claim('fixture',epoch,'compute',kinds={'run.execute'})
    assert commands.recover(now=datetime.now(timezone.utc)+timedelta(seconds=31))['suspended']==1
    assert commands.claim('fixture',epoch,'compute',kinds={'run.execute'}) is None
    assert not commands.complete(old,{'late':True})
    assert store.get_run(value['run_id'])['stop_requested_reason']=='command_fenced'


def test_late_domain_result_is_retained_without_a_current_success_claim(fixture):
    value,epoch,token,card=claimed_gate()
    other=submit(key='other')
    with pytest.raises(ValueError,match='another run'):
        store.update_run(other['run_id'],'proved',result_kind='proved',command_token=token)
    assert store.get_run(other['run_id'])['status']=='pending'
    commands.recover(now=datetime.now(timezone.utc)+timedelta(seconds=31))
    result=store.update_run(value['run_id'],'proved',result_kind='proved',cost_usd=.25,
        input_tokens=3,usage_status='final',command_token=token)
    assert result['status']=='cancelled' and result['result_kind']=='interrupted'
    assert result['recovery_state']=='review' and result['cost_usd']==.25
    assert not commands.complete(token,{'late':True})


def test_reaped_startup_reclaims_unstarted_command_without_new_run_or_message(fixture):
    value=submit();epoch=commands.start_worker(command_host.OWNER)
    old=commands.claim(command_host.OWNER,epoch,'compute',kinds={'run.execute'})
    run_commands.recover_at_startup()
    epoch=commands.start_worker(command_host.OWNER)
    new=commands.claim(command_host.OWNER,epoch,'compute',kinds={'run.execute'})
    assert new['id']==old['id'] and new['generation']>old['generation']
    assert not commands.complete(old,{})
    with db.connect() as conn:
        assert conn.execute('select count(*) from runs').fetchone()[0]==1
        assert conn.execute("select count(*) from timeline where author='user'").fetchone()[0]==1


def test_uncertain_model_recovery_suspends_without_new_paid_attempt(fixture):
    value,epoch,token,card=claimed_gate()
    with db.write() as conn:
        conn.execute("insert into run_invocations(id,run_id,attempt_id,kind,name,turn,input_json,started_at) values ('paid',?,?,'model','stream',1,'{}',?)",
            (value['run_id'],card['attempt_id'],db.utc_now()))
    assert recovery.recover_running()['paused']==1
    run_commands.recover_at_startup()
    run=store.get_run(value['run_id'])
    assert run['recovery_state']=='review' and run['status']=='cancelled'
    with db.connect() as conn:assert conn.execute('select count(*) from run_invocations').fetchone()[0]==1


def test_external_credentials_cannot_enqueue_internal_lineage_refs(fixture):
    value=submit();credential=commands.register_producer('outside','standalone')
    with pytest.raises(commands.QueueError,match='producer_kind_denied'):
        commands.admit(credential,{'kind':'run.execute','idempotency_key':'forged','session_id':value['session_id']})


def test_admission_kill_switch_keeps_controls_available(fixture,monkeypatch):
    first=submit()
    monkeypatch.setenv('LEA_COMMAND_ADMISSION_ENABLED','0')
    with pytest.raises(HTTPException) as blocked:submit(key='disabled')
    assert blocked.value.status_code==503
    assert runs.interrupt_run(first['run_id'])['command']['lane']=='control'
    with db.connect() as conn:assert conn.execute('select count(*) from runs').fetchone()[0]==1


def test_legacy_adoption_is_bounded_and_capacity_failure_does_not_lose_runs(fixture,monkeypatch):
    monkeypatch.setattr(migration_limits,'COMPUTE_QUEUED_GLOBAL',1)
    monkeypatch.setenv('LEA_COMMAND_ADMISSION_ENABLED','0')
    ids=[]
    for i in range(3):
        session=store.create_session(str(i));run=store.create_run(session['id'],'fixture',None,3)
        store.add_message(session['id'],'user','Existing accepted task',run['id']);ids.append(run['id'])
    run_commands.recover_at_startup()
    with db.connect() as conn:
        assert conn.execute('select count(*) from run_commands').fetchone()[0]==1
        assert conn.execute('select count(*) from runs').fetchone()[0]==3
        assert conn.execute("select count from command_capacity where key='compute:global'").fetchone()[0]==1
        row=conn.execute('select * from commands').fetchone()
    # Settling one command makes capacity available to rotating adoption.
    with db.write() as conn:commands._settle(conn,row,'cancelled',error='fixture')
    run_commands.adopt_pending();run_commands.adopt_pending()
    with db.connect() as conn:assert conn.execute('select count(*) from run_commands').fetchone()[0]==2


def test_child_stop_is_durable_and_does_not_cancel_parent(fixture,monkeypatch):
    parent=submit();child=store.create_session('Child',parent_id=parent['session_id'])
    run=store.create_run(child['id'],'fixture',None,3);store.claim_pending_run(run['id'])
    monkeypatch.setattr(bridge,'_child_session_to_result',{child['id']:'child-result'})
    delivered=[];monkeypatch.setattr(bridge,'request_child_stop',lambda rid:delivered.append(rid) or True)
    accepted=run_commands.child_stop(child['id'])
    assert run_commands.child_stop(child['id'])['command']['id']==accepted['command']['id']
    assert not delivered
    epoch=commands.start_worker('child-fixture');token=commands.claim('child-fixture',epoch,'control')
    assert command_execution.control(token)['delivered']
    assert delivered==['child-result']
    assert store.get_run(parent['run_id'])['stop_requested_reason'] is None


def test_cancellation_during_tool_preserves_result_but_fences_next_invocation(fixture,monkeypatch):
    entered=threading.Event();release=threading.Event();effects=[]
    def fake(cfg,messages,*,session_id=None,checkpoint_callback=None,should_stop=None,**options):
        yield TurnStarted(1)
        checkpoint_callback('invocation_start',{'id':'first','kind':'tool','name':'read_file','input':{}})
        entered.set();release.wait(5);effects.append('first')
        checkpoint_callback('invocation_result',{'id':'first','result':'observed'})
        assert should_stop()
        with pytest.raises(recovery.CheckpointWriteError):
            checkpoint_callback('invocation_start',{'id':'second','kind':'tool','name':'read_file','input':{}})
        yield Finished('interrupted','Stopped',1,session_id,'fixture',Usage(input_tokens=2),0,{'messages':[]})
    monkeypatch.setattr(bridge,'run_events',fake)
    first=submit(autonomous=True);command_host.start()
    try:
        assert entered.wait(5)
        runs.interrupt_run(first['run_id']);release.set()
        wait(lambda:run_commands.for_run(first['run_id'])['state']=='succeeded')
        assert effects==['first']
        with db.connect() as conn:
            assert conn.execute('select count(*) from run_invocations where completed_at is not null').fetchone()[0]==1
    finally:release.set()


def test_drain_intent_blocks_new_admission_before_worker_observes_it(fixture,monkeypatch):
    from app import runtime
    from app import process_state
    monkeypatch.setattr(process_state,'snapshot',lambda:{'restart_supported':True})
    accepted=run_commands.runtime_control(shutdown=True)
    assert accepted['control_requested']
    with pytest.raises(HTTPException) as blocked:submit()
    assert blocked.value.status_code==503 and blocked.value.detail['error']=='adapter_draining'


def test_reformalization_admission_rolls_back_conversation_and_linkage(fixture,monkeypatch):
    from app import reformalizations
    monkeypatch.setattr(reformalizations,'capability',lambda:{'admission_enabled':True})
    monkeypatch.setattr(reformalizations.execution,'capability',lambda:{'available':True})
    project=store.create_project('atomic')
    form=store.create_formalization(project_id=project['id'],loose_session_id=None,display_title='Target')
    store.link_formalization_file(form['id'],'Proof.lean','primary')
    monkeypatch.setattr(migration_limits,'COMPUTE_QUEUED_GLOBAL',0)
    with pytest.raises(commands.QueueError,match='queue_full'):
        reformalizations.enqueue({'message':'Retry','focus_formalization_id':form['id']},project['id'])
    with db.connect() as conn:
        for table in ('sessions','session_formalizations','reformalization_operations','operation_commands','commands'):
            assert conn.execute('select count(*) from '+table).fetchone()[0]==0,table


def test_late_source_preparation_cannot_create_a_solver_after_lease_loss(fixture):
    from app import reformalizations
    project=store.create_project('prepare');session=store.create_session('Prepare',project_id=project['id'])
    form=store.create_formalization(project_id=project['id'],loose_session_id=None,display_title='Target')
    with db.write() as conn:
        conn.execute("""insert into reformalization_operations(id,project_id,formalization_id,session_id,
            request_hash,request_json,state,created_at,updated_at) values ('op',?,?,?,'fixture','{}','active',?,?)""",
            (project['id'],form['id'],session['id'],db.utc_now(),db.utc_now()))
        run_commands.attach_operation(conn,{'id':'op','project_id':project['id'],'session_id':session['id'],'formalization_id':form['id']})
    epoch=commands.start_worker('prepare-fixture')
    token=commands.claim('prepare-fixture',epoch,'compute',kinds={'operation.dispatch'})
    commands.recover(now=datetime.now(timezone.utc)+timedelta(seconds=31))
    with reformalizations.acting('op'),reformalizations.claiming(token),pytest.raises(ValueError,match='lost its command claim'):
        store.create_run_bundle(message='Late callback',session_id=session['id'],project_id=project['id'],
            session_origin='ui',session_origin_url=None,model='fixture',provider=None,max_turns=3,autonomous=True,
            focus_formalization_id=form['id'])
    with db.connect() as conn:
        assert conn.execute('select count(*) from runs').fetchone()[0]==0
        assert conn.execute('select count(*) from timeline').fetchone()[0]==0


def test_http_controls_do_not_reference_runner_signals_or_fifo():
    root=Path(runs.__file__).parents[1]
    for path in (Path(runs.__file__),root/'main.py'):
        tree=ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute):
                assert node.func.attr not in {'enqueue_run','request_stop','request_subagent_stop','resolve_approval','begin_drain','cancel_drain'},(path,node.lineno)
    assert not hasattr(bridge,'_run_queue')
