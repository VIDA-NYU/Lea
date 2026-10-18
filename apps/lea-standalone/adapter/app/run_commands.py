"""SQL-only solver/control admission. Execution lives in command_host/bridge.

The existing local API assigns its compatibility producer behind the Host/Origin
boundary. Its random credential is never returned; external producers cannot
submit internal lineage references through the generic command endpoint.
"""
import json
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from datetime import datetime,timedelta,timezone
from uuid import uuid4
from fastapi import HTTPException
from . import commands,db

PRODUCER='compatibility:local'
GATED_TOOLS={'bash','write_file','edit_file'}
SOLVER_KINDS={'run.execute','operation.dispatch'}
SYNTHETIC_KINDS={k for k in commands.REGISTRY if k.startswith('synthetic.')}
_execution_token=ContextVar('solver_command_token',default=None)


@contextmanager
def executing(token):
    marker=_execution_token.set(token)
    try:yield
    finally:_execution_token.reset(marker)


def current_token():return _execution_token.get()


def admission_errors(fn):
    @wraps(fn)
    def call(*args,**kwargs):
        try:return fn(*args,**kwargs)
        except commands.QueueError as exc:
            raise HTTPException(exc.status,detail={'error':exc.code},
                headers={'Retry-After':'5'} if exc.status in {429,503} else None) from exc
    return call


def _enqueue(conn,kind,payload,*,key,project_id=None,session_id=None,formalization_id=None,deadline=None,already_accepted=False):
    handler=commands.REGISTRY[kind]
    old=conn.execute('select * from commands where producer_id=? and idempotency_key=?',(PRODUCER,key)).fetchone()
    semantic=commands._dump({'kind':kind,'payload':payload,'project':project_id,'session':session_id,'formalization':formalization_id})
    digest=commands._hash(semantic)
    if old:
        if old['request_hash']!=digest:raise commands.QueueError('idempotency_conflict')
        return commands.receipt(old)
    if not already_accepted and handler.lane!='control' and not commands.capability()['admission_enabled']:
        raise commands.QueueError('admission_disabled',503)
    if not already_accepted and handler.lane=='compute' and drain_requested(conn):
        raise commands.QueueError('adapter_draining',503)
    if not already_accepted and handler.lane=='compute' and project_id and kind!='companion.query':
        if conn.execute("select 1 from workspace_effects where project_id=? and state='conflict' limit 1",(project_id,)).fetchone():
            raise HTTPException(409,detail={'code':'workspace_reconciliation_required','message':'Reconcile the interrupted workspace operation before starting more project work.'})
        from .workspace_commands import current as workspace_command
        actor=workspace_command()
        if conn.execute("select 1 from workspace_effects where project_id=? and state in ('prepared','installed') and command_id is not ? limit 1",
            (project_id,actor['id'] if actor else None)).fetchone():
            raise commands.QueueError('project_busy',503)
    conn.execute('insert or ignore into command_producers values (?,?,?,?,1,?)',
        (PRODUCER,'compatibility',commands._hash(secrets.token_urlsafe(48)),None,db.utc_now()))
    producer=conn.execute('select enabled from command_producers where id=?',(PRODUCER,)).fetchone()
    if not producer[0]:raise commands.QueueError('producer_revoked',403)
    commands._reserve(conn,handler.lane,PRODUCER,project_id)
    now=datetime.now(timezone.utc);cid=uuid4().hex
    end=deadline or (now+timedelta(days=1)).isoformat()
    parent=db.caller_parent_command.get()
    if parent:
        accepted=conn.execute('select deadline,project_id from commands where id=?',(parent,)).fetchone()
        if not accepted or accepted[0]<=now.isoformat():raise commands.QueueError('parent_deadline_expired')
        if accepted[1] and project_id and accepted[1]!=project_id:raise commands.QueueError('parent_scope_mismatch',403)
        end=min(end,accepted[0])
    conn.execute('''insert into commands(id,producer_id,idempotency_key,request_hash,kind,version,lane,
        project_id,session_id,formalization_id,payload_json,state,created_at,deadline)
        values (?,?,?,?,?,1,?,?,?,?,?,'queued',?,?)''',
        (cid,PRODUCER,key,digest,kind,handler.lane,project_id,session_id,formalization_id,commands._dump(payload),now.isoformat(),end))
    return commands.receipt(conn.execute('select * from commands where id=?',(cid,)).fetchone())


def attach_run(conn,run,*,already_accepted=False):
    existing=conn.execute('select c.* from run_commands r join commands c on c.id=r.command_id where r.run_id=?',(run['id'],)).fetchone()
    if existing:return commands.receipt(existing)
    if run.get('reformalization_id'):
        launch=conn.execute("select c.* from commands c join operation_commands o on o.command_id=c.id where o.operation_id=? and c.state='claimed' order by c.admission_id limit 1",
                            (run['reformalization_id'],)).fetchone()
        if launch:
            commands._settle(conn,launch,'succeeded',{'run_id':run['id'],'operation_id':run['reformalization_id']})
            already_accepted=True
    receipt=_enqueue(conn,'run.execute',{'run_id':run['id']},key='run:'+run['id'],project_id=run['project_id'],
        session_id=run['session_id'],formalization_id=run['focus_formalization_id'],deadline=run.get('deadline_at'),already_accepted=already_accepted)
    conn.execute('insert into run_commands values (?,?)',(run['id'],receipt['id']))
    return receipt


def for_run(run_id):
    with db.connect() as conn:
        row=conn.execute('select c.* from run_commands r join commands c on c.id=r.command_id where r.run_id=?',(run_id,)).fetchone()
    return commands.receipt(row) if row else None


def ensure_run(run_id):
    with db.write() as conn:
        row=conn.execute('select * from runs where id=?',(run_id,)).fetchone()
        if row:return attach_run(conn,dict(row))


def owns(conn,run_id,token):
    return bool(conn.execute('select 1 from run_commands l join runs r on r.id=l.run_id where l.run_id=? and l.command_id=?',
        (run_id,token['id'])).fetchone() and commands._owned(conn,token))


def owns_operation(conn,operation_id,token):
    return bool(conn.execute('select 1 from operation_commands where operation_id=? and command_id=?',
        (operation_id,token['id'])).fetchone() and commands._owned(conn,token))


def attach_operation(conn,op,key='initial',*,already_accepted=False):
    receipt=_enqueue(conn,'operation.dispatch',{'operation_id':op['id'],'predecessor_run_id':op.get('root_run_id')},key='operation:'+op['id']+':'+key,
        project_id=op['project_id'],session_id=op['session_id'],formalization_id=op['formalization_id'],already_accepted=already_accepted)
    conn.execute('insert or ignore into operation_commands values (?,?,?)',(op['id'],receipt['id'],key))
    return receipt


def _run(conn,run_id):
    run=conn.execute('select * from runs where id=?',(run_id,)).fetchone()
    if not run:raise HTTPException(404,'Run not found')
    return dict(run)


def stop(run_id,reason='user_stop',source='user'):
    with db.write() as conn:
        run=_run(conn,run_id)
        if run['status'] not in {'pending','running'}:
            return {'status':run['status'],'stop_reason':run['stop_reason'],'usage_status':run['usage_status']}
        # Cancellation intent and the independent control receipt commit together.
        # Gates consult this row before the consumer gets CPU time.
        conn.execute('''update runs set stop_requested_reason=?,stop_requested_by=?,stop_requested_at=?,updated_at=?
            where id=? and stop_requested_reason is null and status in ('pending','running')''',
            (reason,source,db.utc_now(),db.utc_now(),run_id))
        run=_run(conn,run_id)
        receipt=_enqueue(conn,'run.stop',{'run_id':run_id,'reason':run['stop_requested_reason'],'source':run['stop_requested_by']},
            key='stop:'+run_id,project_id=run['project_id'],session_id=run['session_id'],formalization_id=run['focus_formalization_id'])
    return {'status':'interrupt_requested','stop_reason':run['stop_requested_reason'],
            'usage_status':run['usage_status'],'command':receipt,'worker_observed':False}


def child_stop(session_id):
    with db.write() as conn:
        child=conn.execute('select * from sessions where id=? and parent_id is not null',(session_id,)).fetchone()
        run=conn.execute("select * from runs where session_id=? and status='running' order by created_at desc limit 1",(session_id,)).fetchone()
        if not child or not run:raise HTTPException(404,'No running sub-agent for that session')
        receipt=_enqueue(conn,'child.stop',{'session_id':session_id,'run_id':run['id']},key='child-stop:'+run['id'],
            project_id=run['project_id'],session_id=session_id,formalization_id=run['focus_formalization_id'])
        conn.execute('''update runs set stop_requested_reason='user_stop',stop_requested_by='user',stop_requested_at=?
            where id=? and stop_requested_reason is null''',(db.utc_now(),run['id']))
    return {'status':'interrupt_requested','command':receipt,'worker_observed':False}


def bind_approval(run_id,approval_id,invocation_id,tool,args):
    with db.write() as conn:
        run=_run(conn,run_id)
        conn.execute('insert into tool_approval_requests values (?,?,?,?,?,?,\'pending\',null,null,?,null)',
            (approval_id,run_id,run['attempt_id'],invocation_id,tool,commands._dump(args),db.utc_now()))
        card={'approval_id':approval_id,'invocation_id':invocation_id,'attempt_id':run['attempt_id'],'tool_name':tool,'args':args}
        conn.execute('update runs set pending_approval=?,updated_at=? where id=?',(json.dumps(card),db.utc_now(),run_id))
    return card


def approve(run_id,approval_id,decision):
    with db.write() as conn:
        run=_run(conn,run_id)
        gate=conn.execute('select * from tool_approval_requests where id=? and run_id=?',(approval_id,run_id)).fetchone()
        if not gate or gate['attempt_id']!=run['attempt_id']:raise HTTPException(409,'No pending approval matches this run/approval id')
        if gate['decision']:
            if gate['decision']!=decision:raise HTTPException(409,'Approval already has a different decision')
            receipt=commands.receipt(conn.execute('select * from commands where id=?',(gate['command_id'],)).fetchone())
            return {'status':'accepted','decision':decision,'command':receipt}
        if run['status']!='running' or run['stop_requested_reason'] or gate['state']!='pending':
            raise HTTPException(409,'This approval is no longer pending')
        payload={'run_id':run_id,'approval_id':approval_id,'attempt_id':run['attempt_id'],
                 'invocation_id':gate['invocation_id'],'decision':decision}
        receipt=_enqueue(conn,'run.approve',payload,key='approval:'+approval_id,project_id=run['project_id'],
            session_id=run['session_id'],formalization_id=run['focus_formalization_id'])
        conn.execute("update tool_approval_requests set state='accepted',decision=?,command_id=? where id=?",
                     (decision,receipt['id'],approval_id))
    return {'status':'accepted','decision':decision,'command':receipt}


def consume_approval(run_id,approval_id):
    # Pending gates need no write lock. This hint can only delay consumption;
    # the authoritative decision/cancellation checks below repeat atomically.
    with db.connect() as conn:
        run=conn.execute('select * from runs where id=?',(run_id,)).fetchone()
        gate=conn.execute('select state,attempt_id from tool_approval_requests where id=? and run_id=?',(approval_id,run_id)).fetchone()
        parent=conn.execute('select c.* from run_commands r join commands c on c.id=r.command_id where r.run_id=?',(run_id,)).fetchone()
        if (run and gate and parent and run['status']=='running' and not run['stop_requested_reason']
            and gate['attempt_id']==run['attempt_id'] and gate['state'] in {'pending','accepted'}
            and commands._owned(conn,dict(parent))):return None
    with db.write() as conn:
        run=_run(conn,run_id)
        gate=conn.execute('select * from tool_approval_requests where id=? and run_id=?',(approval_id,run_id)).fetchone()
        parent=conn.execute('select c.* from run_commands r join commands c on c.id=r.command_id where r.run_id=?',(run_id,)).fetchone()
        revoked=parent and not commands._owned(conn,dict(parent))
        if run['stop_requested_reason'] or run['status']!='running' or revoked or not gate or gate['attempt_id']!=run['attempt_id']:
            if gate:conn.execute("update tool_approval_requests set state='cancelled',resolved_at=? where id=?",(db.utc_now(),approval_id))
            return 'cancelled'
        if gate['state']=='delivered':
            conn.execute("update tool_approval_requests set state='consumed',resolved_at=? where id=?",(db.utc_now(),approval_id))
            return gate['decision']
    return None


def close_approval(approval_id):
    with db.write() as conn:
        conn.execute("update tool_approval_requests set state='expired',resolved_at=? where id=? and state in ('pending','accepted','delivered')",
                     (db.utc_now(),approval_id))


def authorize_invocation(conn,run_id,token,payload):
    run=_run(conn,run_id)
    if run['stop_requested_reason'] or not owns(conn,run_id,token):raise RuntimeError('Solver command is cancelled or no longer owned')
    gate=conn.execute('select * from tool_approval_requests where run_id=? and attempt_id=? and invocation_id=?',
        (run_id,run['attempt_id'],payload['id'])).fetchone()
    if (payload.get('kind')=='tool' and payload['name'] in GATED_TOOLS and run['approval_mode']=='stepwise'
        and not gate and not allowlisted(run['session_id'],payload['name'],conn=conn)):
        raise RuntimeError('Tool invocation has no matching approval')
    if gate and (gate['state']!='consumed' or gate['decision'] not in {'allow','always_session'}
        or gate['tool_name']!=payload['name'] or gate['args_json']!=commands._dump(payload.get('input'))):
        raise RuntimeError('Tool invocation does not match an authorized approval')


def allowlisted(session_id,tool,*,conn=None):
    if conn is None:
        with db.connect() as conn:return allowlisted(session_id,tool,conn=conn)
    return bool(conn.execute('''select 1 from tool_approval_requests a join runs r on r.id=a.run_id
        where r.session_id=? and a.tool_name=? and a.state='consumed' and a.decision='always_session' limit 1''',
        (session_id,tool)).fetchone())


def operation_cancel(operation_id):
    with db.write() as conn:
        op=conn.execute('select * from reformalization_operations where id=?',(operation_id,)).fetchone()
        if not op:raise HTTPException(404,'Reformalization not found')
        if op['state']=='settled':return {'reformalization_id':operation_id,'state':'settled'}
        request=json.loads(op['request_json']);request['_cancelled']=True
        conn.execute('update reformalization_operations set request_json=? where id=?',(json.dumps(request),operation_id))
        if op['root_run_id']:
            conn.execute("update runs set stop_requested_reason=coalesce(stop_requested_reason,'user_stop') where id=? and status in ('pending','running')",(op['root_run_id'],))
        receipt=_enqueue(conn,'operation.cancel',{'operation_id':operation_id},key='operation-cancel:'+operation_id,
            project_id=op['project_id'],session_id=op['session_id'],formalization_id=op['formalization_id'])
    return {'reformalization_id':operation_id,'state':op['state'],'command':receipt,'cancellation_requested':True}


def runtime_control(*,force=False,cancel=False,shutdown=False):
    from . import process_state as runtime
    selected=runtime.snapshot()
    if not cancel and not selected["restart_supported"]:raise HTTPException(409,detail={'code':'restart_unavailable','message':'Start the workspace worker through run_worker.py or the shared supervisor to enable restart control.'})
    with db.write() as conn:
        receipt=_enqueue(conn,'runtime.control',{'force':force,'cancel':cancel,'shutdown':shutdown,'target_runtime_id':selected.get('runtime_id')},key='runtime:'+uuid4().hex)
    return {**runtime.snapshot(),'command':receipt,'control_requested':True}


def drain_requested(conn=None):
    if conn is None:
        with db.connect() as conn:return drain_requested(conn)
    return bool(conn.execute("""select 1 from commands where kind='runtime.control'
        and state in ('queued','claimed') and json_extract(payload_json,'$.cancel')=0 limit 1""").fetchone())


def settled(conn,row,state,error):
    """Keep deadline/revocation/lease outcomes visible to the existing run gate."""
    if state=='succeeded':return
    if row['kind']=='operation.dispatch':
        oid=json.loads(row['payload_json'])['operation_id']
        conn.execute("""update reformalization_operations set state='retained',result_json=?,updated_at=?
            where id=? and state not in ('settled','retained')""",
            (json.dumps({'outcome':'interrupted','detail':error or 'Command ownership was lost; retained work requires reconciliation.'}),db.utc_now(),oid))
        return
    if row['kind']!='run.execute':return
    rid=json.loads(row['payload_json'])['run_id'];now=db.utc_now()
    conn.execute('''update runs set stop_requested_reason=coalesce(stop_requested_reason,?),
        stop_requested_by=coalesce(stop_requested_by,'command'),stop_requested_at=coalesce(stop_requested_at,?)
        where id=? and status in ('pending','running')''',('timeout' if state=='expired' else 'command_fenced',now,rid))
    conn.execute("update runs set recovery_state='review',recovery_reason=?,recoverable=1 where id=? and status='running'",
                 (error or 'Solver command ownership was lost; review before retrying.',rid))
    conn.execute("""update runs set status='cancelled',stop_reason=stop_requested_reason,usage_status='final',recoverable=1,
        result_kind='interrupted',result_detail=?,updated_at=? where id=? and status='pending'""",(error or state,now,rid))


def recover_at_startup():
    """Only after process reaping, journal reconciliation and checkpoint recovery.

    A pending run is new or explicitly approved safe by recovery.recover_running.
    Lease expiry in a live process never authorizes retry of real work.
    """
    with db.write() as conn:
        conn.execute("delete from command_workers where id='lea-command-host'")
        linked=conn.execute('select c.*,r.status as run_status from commands c join run_commands l on l.command_id=c.id join runs r on r.id=l.run_id where c.state not in (\'succeeded\',\'failed\',\'cancelled\',\'expired\')').fetchall()
        for row in linked:
            if row['run_status']=='pending' and row['state'] in {'claimed','suspended'}:
                if row['state']=='suspended':commands._reserve(conn,row['lane'],row['producer_id'],row['project_id'])
                conn.execute("update commands set state='queued',owner=null,worker_epoch=null,attempt_id=null,lease_until=null,generation=generation+1,error=null where id=?",(row['id'],))
                if row['attempt_id']:conn.execute("update command_attempts set finished_at=?,outcome='startup_reconciled' where id=?",(db.utc_now(),row['attempt_id']))
            elif row['run_status'] not in {'pending','running'} and row['state'] in {'queued','claimed'}:
                commands._settle(conn,row,'succeeded',{'status':row['run_status']})
    adopt_pending()


def adopt_pending(limit=20):
    """Bounded, rotating adoption of previously accepted legacy runs.

    Capacity rejection rolls back its entire transaction; it cannot lose the old
    pending row or prevent the adapter starting. Sub-agent rows keep their parent
    executor. Persisted progress avoids starving later scopes at a project cap.
    """
    with db.connect() as conn:
        cursor=conn.execute('select cursor from command_adoption_checkpoint where id=1').fetchone()[0]
        rows=[dict(r) for r in conn.execute("""select * from (
            select r.created_at||':r:'||r.id as checkpoint,'run' as kind,r.id as id,'initial' as request_key
                from runs r join sessions s on s.id=r.session_id where r.status='pending' and s.parent_id is null
                and not exists(select 1 from run_commands c where c.run_id=r.id)
            union all
            select coalesce(nullif(o.queued_at,''),o.created_at)||':o:'||o.id,'operation',o.id,
                case when json_extract(o.result_json,'$.continuation.request.idempotency_key') is not null
                    then 'continuation:'||json_extract(o.result_json,'$.continuation.request.idempotency_key') else 'initial' end as request_key
                from reformalization_operations o where (o.state='queued' or
                    (o.state in ('active','worker') and json_extract(o.result_json,'$.continuation') is not null))
                and not exists(select 1 from operation_commands c where c.operation_id=o.id and c.request_key=
                    case when json_extract(o.result_json,'$.continuation.request.idempotency_key') is not null
                        then 'continuation:'||json_extract(o.result_json,'$.continuation.request.idempotency_key') else 'initial' end)
            ) where checkpoint>? order by checkpoint limit ?""",(cursor,limit))]
    if not rows:
        if cursor:
            with db.write() as conn:conn.execute("update command_adoption_checkpoint set cursor='' where id=1")
        return
    for item in rows:
        try:
            with db.write() as conn:
                if item['kind']=='run':
                    run=conn.execute("select * from runs where id=? and status='pending'",(item['id'],)).fetchone()
                    if run:attach_run(conn,dict(run),already_accepted=True)
                else:
                    op=conn.execute('select * from reformalization_operations where id=?',(item['id'],)).fetchone()
                    if op:attach_operation(conn,dict(op),item['request_key'],already_accepted=True)
                conn.execute('update command_adoption_checkpoint set cursor=? where id=1',(item['checkpoint'],))
        except commands.QueueError:
            with db.write() as conn:conn.execute('update command_adoption_checkpoint set cursor=? where id=1',(item['checkpoint'],))
