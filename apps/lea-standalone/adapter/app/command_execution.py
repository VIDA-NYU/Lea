"""Worker-only handlers. No request handler calls these execution services."""
from fastapi import HTTPException
import json
import time
from . import commands,db,run_commands


def control(token):
    from . import bridge,store,reformalizations,runtime
    value=json.loads(token['payload_json']);kind=token['kind']
    if kind=='run.stop':
        run=bridge.request_stop(value['run_id'],reason=value['reason'],source=value['source'])
        if store.fail_pending_run(value['run_id'],'Interrupted before the run started.'):
            bridge.publish_terminal_from_row(value['run_id'])
        return {'worker_observed':True,'run_id':value['run_id'],'status':(store.get_run(value['run_id']) or {}).get('status')}
    if kind=='child.stop':
        delivered=bridge.request_subagent_stop(value['session_id'])
        return {'worker_observed':True,'delivered':delivered,'status':(store.get_run(value['run_id']) or {}).get('status')}
    if kind=='run.approve':
        with db.write() as conn:
            if not commands._owned(conn,token):return {'applied':False,'reason':'obsolete_owner'}
            run=conn.execute('select * from runs where id=?',(value['run_id'],)).fetchone()
            gate=conn.execute('select * from tool_approval_requests where id=?',(value['approval_id'],)).fetchone()
            valid=bool(run and gate and run['status']=='running' and not run['stop_requested_reason']
                and run['attempt_id']==value['attempt_id']==gate['attempt_id']
                and gate['invocation_id']==value['invocation_id'] and gate['command_id']==token['id'])
            if valid and gate['state']=='accepted':
                conn.execute("update tool_approval_requests set state='delivered' where id=?",(gate['id'],))
            return {'applied':valid,'decision':value['decision'] if valid else 'cancelled'}
    if kind=='operation.cancel':
        result=reformalizations.cancel(value['operation_id'])
        return {'operation_id':value['operation_id'],'state':result['state'],'run_id':result.get('run_id'),'worker_observed':True}
    if kind=='runtime.control':
        if value.get('target_runtime_id')!=runtime.IDENTITY:
            return {'worker_observed':True,'already_transitioned':True,'runtime_id':runtime.IDENTITY}
        result=runtime.cancel_drain() if value['cancel'] else runtime.begin_drain(force=value['force'],restart=not value['shutdown'])
        # Lifecycle has one SQL authority, including immediately after a control.
        from . import process_state
        with db.connect() as conn:
            row=conn.execute('select capabilities_json from worker_process where id=1 and runtime_id=?',(runtime.IDENTITY,)).fetchone()
        if row:process_state.publish(runtime.IDENTITY,runtime.snapshot(),json.loads(row[0]))
        return result
    raise ValueError('Unknown control handler')


def solver_tick(owner,epoch,superseded):
    """SQL is the waiting set. Local supersede caching is disposable."""
    from . import bridge,runregistry,reformalizations,runtime,store
    with db.connect() as conn:
        rows=[dict(r) for r in conn.execute("select * from commands where owner=? and worker_epoch=? and state='claimed' and kind in ('run.execute','operation.dispatch') order by admission_id limit 200",(owner,epoch))]
    for token in rows:
        with db.connect() as conn:
            if not commands._owned(conn,token):continue
            from .workspace_commands import blocks_solver
            if blocks_solver(conn,token['project_id'],token['session_id']):continue
        value=json.loads(token['payload_json'])
        if token['kind']=='run.execute':
            run=store.get_run(value['run_id'])
            if not run:
                commands.complete(token,error='run_deleted');continue
            if run['stop_requested_reason'] and run['status']=='pending':
                store.fail_pending_run(run['id'],'Interrupted before the run started.')
                bridge.publish_terminal_from_row(run['id']);run=store.get_run(run['id'])
            if run['status']=='pending':
                if bridge._try_dispatch(run['id'],superseded,command_token=token)==bridge._CAPACITY:break
            elif run['status']!='running' and not runregistry.registry.is_active(run['id']):
                bridge.publish_terminal_from_row(run['id'])
                commands.complete(token,{'run_id':run['id'],'status':run['status'],'result_kind':run.get('result_kind')})
                superseded.pop(run['id'],None)
        else:
            op=reformalizations.get(value['operation_id'])
            if not op:commands.complete(token,error='operation_deleted');continue
            predecessor=value.get('predecessor_run_id')
            if (op.get('root_run_id') and op['root_run_id']!=predecessor) or op['state'] in {'retained','settled','paused'}:
                commands.complete(token,{'operation_id':op['id'],'run_id':op.get('root_run_id'),'state':op['state']})
            elif op['request'].get('_cancelled'):
                reformalizations.cancel(op['id'])
            elif runtime.accepting_work():reformalizations.wake(op['id'],command_token=token)
    # Claim from SQL, independently of run thread availability. The bounded
    # registry admits drivers; project-specific blockers do not strand strangers.
    if runtime.accepting_work() and commands.has_ready('compute',kinds=run_commands.SOLVER_KINDS):
        commands.claim(owner,epoch,'compute',kinds=run_commands.SOLVER_KINDS)


def heartbeat_solvers(owner,epoch):
    with db.connect() as conn:
        tokens=[dict(r) for r in conn.execute("select * from commands where owner=? and worker_epoch=? and state='claimed' and kind in ('run.execute','operation.dispatch')",(owner,epoch))]
    for token in tokens:commands.heartbeat(token)
