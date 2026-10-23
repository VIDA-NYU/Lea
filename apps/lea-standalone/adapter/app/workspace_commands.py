"""SQL admission and receipts. Only the worker may enter registered actions.

The compatibility routes capture a SQL base when an old client omits it. The
versioned endpoint requires the caller's base on edits; neither path reads disk.
"""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
import inspect
import json
from pathlib import PurePosixPath
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel
from . import commands, db, migration_limits, run_commands, store

_current = ContextVar('workspace_command', default=None)
_actions = {}
_contracts = {}
KINDS = {'workspace.execute'}


def current():
    return _current.get()


@contextmanager
def executing(token):
    marker = _current.set(token)
    try:
        yield
    finally:
        _current.reset(marker)


def authorize(conn=None):
    token = current()
    if token is None:
        return
    if conn is None:
        with db.connect() as connection:
            return authorize(connection)
    if not commands._owned(conn, token):
        raise HTTPException(409, detail={'code': 'command_fenced', 'message': 'Workspace command ownership was lost; the submitted draft is retained.'})


def assert_available(conn,project_id):
    if project_id and conn.execute("select 1 from workspace_effects where project_id=? and state='conflict' limit 1",(project_id,)).fetchone():
        raise HTTPException(409,detail={'code':'workspace_reconciliation_required','message':'Reconcile the interrupted workspace operation before starting more project work.'})


def normalize_path(path):
    path = str(path or '')
    parts = PurePosixPath(path).parts
    if not parts or path.startswith('/') or '\\' in path or '..' in parts or any(p in {'.git', '.lake'} for p in parts):
        raise HTTPException(422, detail='A safe repository-relative path is required')
    return PurePosixPath(path).as_posix()


def file_revision(conn, scope, path):
    """A content/base identity from SQL, independent of filesystem observations."""
    row = conn.execute('select revision_id,blob_id,materialization from workspace_files where scope=? and path=?', (scope, path)).fetchone()
    # A solver may have superseded a prior human binding. Always incorporate its
    # current timeline identity rather than treating our cache as a second authority.
    clause = 's.project_id=?' if scope.startswith('p:') else 's.id=?'
    step = conn.execute(f'''select t.id,t.after_blob_id from timeline t join sessions s on s.id=t.session_id
        where {clause} and t.kind='code' and t.path=? and t.after_blob_id is not null
        order by t.id desc limit 1''', (scope[2:], path)).fetchone()
    if row and row['materialization']=='deleted':
        deleted_base=conn.execute('select base_revision from workspace_file_revisions where id=?',(row['revision_id'],)).fetchone()
        if not step or (deleted_base and deleted_base[0]==f'proof:{step[0]}:{step[1]}'):
            return 'deleted:'+row['revision_id']
    if step:
        return f'proof:{step[0]}:{step[1]}'
    if row:
        return 'file:' + row['revision_id']
    observed = conn.execute('''select b.blob_id from current_file_bindings b join projection_enrollment e
        on e.project_id=b.project_id and e.observation_id=b.observation_id where b.project_id=? and b.path=?''', (scope[2:], path)).fetchone() if scope.startswith('p:') else None
    return 'observed:' + observed[0] if observed and observed[0] else 'missing'


def revision_for(scope, path):
    with db.connect() as conn:
        return file_revision(conn, scope, path)


def blocks_solver(conn, project_id, session_id):
    scope='project_id=?' if project_id else 'project_id is null and session_id=?'
    value=project_id or session_id
    if value and conn.execute("select 1 from commands where kind='workspace.execute' and state='claimed' and "+scope+' limit 1',(value,)).fetchone():
        return True
    if value and conn.execute("select 1 from workspace_effects where state in ('prepared','installed','conflict') and "+scope+' limit 1',(value,)).fetchone():
        return True
    return False


def heartbeat(owner, epoch):
    with db.connect() as conn:
        tokens = [dict(r) for r in conn.execute("select * from commands where owner=? and worker_epoch=? and state='claimed' and kind='workspace.execute'", (owner, epoch))]
    for token in tokens:
        commands.heartbeat(token)


def recover_at_startup():
    """Only the startup data-lock owner reconciles a dead worker's effects."""
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("select c.*,r.action from commands c join workspace_requests r on r.command_id=c.id where c.state in ('claimed','suspended')")]
    for row in rows:
        with db.connect() as conn:
            effects = [dict(r) for r in conn.execute('select * from workspace_effects where command_id=?',(row['id'],))]
        result, success = None, False
        if not effects:
            # No handler crossed its durable intent boundary. Safe to claim once
            # again after the prior process was reaped, preserving the same draft.
            with db.write() as conn:
                if row['state']=='suspended':commands._reserve(conn,row['lane'],row['producer_id'],row['project_id'])
                conn.execute("update commands set state='queued',owner=null,worker_epoch=null,attempt_id=null,lease_until=null,generation=generation+1,error=null where id=?",(row['id'],))
                conn.execute('delete from command_reservations where command_id=?',(row['id'],))
                if row['attempt_id']:
                    conn.execute("update command_attempts set finished_at=?,outcome='startup_before_intent' where id=?",(db.utc_now(),row['attempt_id']))
            continue
        if row['action']=='session.save' and all(e['state']=='published' for e in effects):
            manifest=json.loads(effects[-1]['manifest_json'])
            with db.connect() as conn:
                step=conn.execute(store.TIMELINE_WITH_BLOB+' where t.id=?',(manifest.get('step_id'),)).fetchone()
                note=conn.execute('select * from timeline where id=?',(manifest.get('note_id'),)).fetchone()
            result={'unchanged':False,'code_step':store._code_step_from_row(step) if step else None,
                'note':store._message_from_row(note) if note else None,'effect_id':effects[-1]['id'],
                'draft_id':manifest.get('draft_id'),'file_revision':revision_for(manifest['scope'],manifest['path']),
                'recovered':True,'materialization':'provenance_pending'}
            success=True
        elif row['action']=='project.delete' and all(e['state']=='published' for e in effects) and not store.get_project(row['project_id']):
            result={'deleted':True,'id':row['project_id'],'recovered':True}
            success=True
        with db.write() as conn:
            if result is not None:
                blob=store._put_blob(conn,commands._dump(result))
                conn.execute('update workspace_requests set result_blob_id=? where command_id=?',(blob,row['id']))
            outcome='succeeded' if success else 'suspended'
            error=None if success else 'workspace_reconciliation_required'
            if row['state']=='claimed':
                commands._settle(conn,row,outcome,{'workspace_request':row['id']} if success else None,error)
            else:
                conn.execute('update commands set state=?,error=?,result_json=? where id=?',(outcome,error,commands._dump({'workspace_request':row['id']}) if success else None,row['id']))


def _still_owned(token):
    with db.connect() as conn:
        return bool(commands._owned(conn,token))


def _scope(conn, args):
    project_id = args.get('project_id')
    if args.get('slug'):
        row = conn.execute('select id from projects where slug=?', (args['slug'],)).fetchone()
        project_id = row[0] if row else None
    session_id = args.get('session_id')
    fid = args.get('formalization_id') or (args.get('request') or {}).get('formalization_id')
    if session_id:
        row = conn.execute('select project_id from sessions where id=?', (session_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Session not found')
        if project_id and project_id != row[0]:
            raise HTTPException(422, 'Scope mismatch')
        project_id = row[0]
    if fid:
        row = conn.execute('select project_id,loose_session_id from formalizations where id=?', (fid,)).fetchone()
        if not row:
            raise HTTPException(404, 'Formalization not found')
        if session_id and not conn.execute('select 1 from session_formalizations where session_id=? and formalization_id=?', (session_id, fid)).fetchone():
            raise HTTPException(409, 'Formalization is not associated with this session')
        if project_id and project_id != row[0]:
            raise HTTPException(422, 'Scope mismatch')
        project_id = row[0]
        session_id = session_id or row[1]
    return project_id, session_id, fid


def register(action):
    """Register an existing domain operation, replacing its HTTP callable by intake.

    The original guarded function is private worker code; nested domain calls in
    that worker keep their existing sequencing. No handler runs during admission.
    """
    def decorate(fn):
        import os
        if os.environ.get('LEA_PROCESS_ROLE')=='api':raise RuntimeError('The API cannot register an executor')
        signature = inspect.signature(fn, eval_str=True)
        _actions[action] = (fn, signature)
        _contracts[action] = (fn, signature)
        @wraps(fn)
        def route(*args, **kwargs):
            if current() is not None:
                authorize()
                return fn(*args, **kwargs)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            values = {k: v.model_dump(mode='json') if isinstance(v, BaseModel) else v for k, v in bound.arguments.items()}
            try:
                selected='session.edit' if action=='session.save' and (values.get('request') or {}).get('check_and_cascade') else action
                return admit(selected, values)
            except commands.QueueError as exc:
                raise HTTPException(exc.status, detail={'error': exc.code}, headers={'Retry-After': '5'} if exc.status in {429, 503} else None) from exc
        # Explicit domain execution seam. Production calls use _actions under a
        # claimed token; unit callers also need private worker read semantics.
        @wraps(fn)
        def worker(*args,**kwargs):
            from .peripheral_views import capturing
            with capturing():return fn(*args,**kwargs)
        route.worker = worker
        route.__signature__ = signature
        return route
    return decorate



def intake(action):
    """Public validation contract. This registry never contains an executor."""
    def decorate(fn):
        signature=inspect.signature(fn,eval_str=True)
        _contracts[action]=(fn,signature)
        @wraps(fn)
        def route(*args,**kwargs):
            bound=signature.bind(*args,**kwargs);bound.apply_defaults()
            values={k:v.model_dump(mode='json') if isinstance(v,BaseModel) else v for k,v in bound.arguments.items()}
            selected='session.edit' if action=='session.save' and (values.get('request') or {}).get('check_and_cascade') else action
            try:return admit(selected,values)
            except commands.QueueError as exc:
                raise HTTPException(exc.status,detail={'error':exc.code},headers={'Retry-After':'5'} if exc.status in {429,503} else None) from exc
        route.__signature__=signature
        return route
    return decorate

EDIT_ACTIONS = {'session.save', 'session.edit', 'project.save', 'project.instructions', 'project.memory', 'project.blueprint'}
CHECK_ACTIONS = {'session.check','session.rebuild','session.verify'}
DOC_PATHS = {'project.instructions': '.lea/instructions.md', 'project.memory': '.lea/memory.md', 'project.blueprint': '.lea/blueprint.md'}


class EditRequest(BaseModel):
    file_revision: str | None = None
    idempotency_key: str | None = None
    draft_id: str | None = None


def admit(action, arguments, *, idempotency_key=None, expected_revision=None, draft_id=None, strict=False, connection=None):
    if action not in _contracts:
        raise HTTPException(422, 'Unsupported workspace action')
    if strict and action=='project.upload':
        raise HTTPException(422,'Use the capped multipart upload endpoint to submit attachment bytes')
    arguments = json.loads(json.dumps(arguments))
    fn, signature = _contracts[action]
    try:
        bound = signature.bind(**arguments)
        bound.apply_defaults()
        import typing
        hints = typing.get_type_hints(inspect.unwrap(fn))
        for name, value in bound.arguments.items():
            annotation = hints.get(name)
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                bound.arguments[name] = annotation.model_validate(value).model_dump(mode='json')
        arguments = dict(bound.arguments)
        if action=='peripheral.test_server':
            store._validate_mcp_env((arguments.get('request') or {}).get('env'))
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, 'Invalid workspace action arguments') from exc
    request = arguments.get('request') or {}
    idempotency_key = idempotency_key or request.get('idempotency_key') or uuid4().hex
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 180:
        raise HTTPException(422, 'Invalid idempotency key')
    draft_id = draft_id or request.get('draft_id')
    if draft_id is not None and (not isinstance(draft_id, str) or len(draft_id) > 200):
        raise HTTPException(422, 'Invalid draft ID')
    with nullcontext(connection) if connection is not None else db.write() as conn:
        previous=conn.execute('''select r.*,c.idempotency_key from workspace_requests r join commands c on c.id=r.command_id
            where c.producer_id=? and c.idempotency_key=?''',(run_commands.PRODUCER,'workspace:'+idempotency_key)).fetchone()
        prior=json.loads(previous['arguments_json']) if previous else None
        project_id, session_id, fid = (previous['project_id'],previous['session_id'],previous['formalization_id']) if previous else _scope(conn, arguments)
        if not conn.execute('select 1 from commands where producer_id=? and idempotency_key=?',(run_commands.PRODUCER,'workspace:'+idempotency_key)).fetchone():
            assert_available(conn, project_id)
            if action in EDIT_ACTIONS and project_id:
                if conn.execute("select 1 from reformalization_operations where project_id=? and state in ('active','applying') limit 1",(project_id,)).fetchone():
                    raise HTTPException(409,detail={'error':'project_busy','message':'A reformalization owns this project; wait before editing its canonical files.'})
        scope = 'p:' + project_id if project_id else 's:' + session_id if session_id else None
        path = None
        if action in EDIT_ACTIONS:
            path = normalize_path(DOC_PATHS.get(action) or request.get('path'))
            if scope is None:
                raise HTTPException(422, 'A file scope is required')
            supplied = expected_revision or request.pop('file_revision', None)
            if strict and supplied is None:
                raise HTTPException(422, 'expected_revision is required for an edit')
            base = supplied if supplied is not None else prior['file_base'] if prior else file_revision(conn, scope, path)
        elif action in CHECK_ACTIONS:
            raw_path=request.get('path')
            if not raw_path and prior:raw_path=prior['path']
            if not raw_path:
                latest=conn.execute("select path from timeline where session_id=? and kind='code' order by id desc limit 1",(session_id,)).fetchone()
                raw_path=latest[0] if latest else None
            path=normalize_path(raw_path) if raw_path else None
            base=(expected_revision or request.pop('file_revision',None) or (prior['file_base'] if prior else file_revision(conn,scope,path))) if path else None
        else:
            base = None
        # Large file contents are immutable blobs, never command envelopes.
        if 'content' in request:
            if not isinstance(request['content'], str) or len(request['content'].encode()) > migration_limits.SOURCE_FILE_BYTES:
                raise HTTPException(413, 'File content exceeds the 4 MiB limit')
            request['content_blob_id'] = store._put_blob(conn, request.pop('content'))
        body = {'action': action, 'arguments': arguments, 'scope': scope, 'path': path, 'file_base': base, 'draft_id': draft_id,
            'requires_absence':bool(strict and action in EDIT_ACTIONS and base=='missing')}
        refs = [request[key] for key in ('content_blob_id','upload_blob_id') if request.get(key)]
        argument_text=commands._dump(arguments)
        if len(argument_text.encode())>32*1024*1024:
            raise HTTPException(413,'Workspace arguments exceed the 32 MiB input limit')
        if len(argument_text.encode())>migration_limits.COMMAND_BYTES//2:
            argument_blob=store._put_blob(conn,argument_text)
            refs.append(argument_blob)
            body.pop('arguments')
            body['arguments_blob_id']=argument_blob
        if len(commands._dump(body).encode()) > migration_limits.COMMAND_BYTES:
            raise HTTPException(413, 'Workspace request exceeds the 64 KiB envelope limit')
        receipt = run_commands._enqueue(conn, 'workspace.execute', body, key='workspace:' + idempotency_key,
            project_id=project_id, session_id=session_id, formalization_id=fid)
        conn.execute('''insert or ignore into workspace_requests(command_id,action,project_id,session_id,formalization_id,arguments_json,created_at)
            values(?,?,?,?,?,?,?)''', (receipt['id'], action, project_id, session_id, fid, commands._dump(body), db.utc_now()))
        for blob in refs:
            conn.execute('insert or ignore into workspace_blob_refs values(?,?)',(receipt['id'],blob))
        if action in EDIT_ACTIONS:
            conn.execute('''insert or ignore into workspace_file_revisions values(?,?,?,?,?,?,?,?)''',
                (uuid4().hex, receipt['id'], scope, path, request['content_blob_id'], base, draft_id, db.utc_now()))
        if previous and previous['result_blob_id']:
            receipt['result']=_expand(json.loads(store.blob_content(previous['result_blob_id'])))
            receipt['error_status']=previous['error_status']
        return {'workspace_command': True, 'command': receipt, 'draft_id': draft_id}


def get(command_id=None, *, idempotency_key=None):
    with db.connect() as conn:
        row = conn.execute('''select c.*,r.result_blob_id,r.error_status from commands c join workspace_requests r on r.command_id=c.id
            where c.producer_id=? and ''' + ('c.id=?' if command_id else 'c.idempotency_key=?'),
            (run_commands.PRODUCER, command_id or 'workspace:' + str(idempotency_key))).fetchone()
        if not row:
            raise HTTPException(404, 'Workspace command not found')
        value = commands.receipt(row)
        if row['result_blob_id']:
            value['result'] = _expand(json.loads(store.blob_content(row['result_blob_id'])))
        if row['state']=='suspended' and value['result'] is None:
            effects=[dict(r) for r in conn.execute('select id,kind,state,created_at,updated_at from workspace_effects where command_id=? order by created_at limit 50',(row['id'],))]
            value['result']={'detail':{'code':'workspace_reconciliation_required',
                'message':'This workspace operation was interrupted. Its submitted content and recovery copies are retained; reconcile it before retrying.',
                'effects':effects}}
        value['error_status'] = row['error_status']
        return {'workspace_command': True, 'command': value}


def _compact(value, refs):
    if isinstance(value,list):return [_compact(item,refs) for item in value]
    if not isinstance(value,dict):return value
    result={key:_compact(item,refs) for key,item in value.items()}
    if isinstance(value.get('code'),str) and value.get('blob_id'):
        result['code']={'$workspace_blob':value['blob_id']}
        refs.add(value['blob_id'])
    return result


def _expand(value):
    if isinstance(value,list):return [_expand(item) for item in value]
    if not isinstance(value,dict):return value
    if set(value)=={'$workspace_blob'}:return store.blob_content(value['$workspace_blob'])
    return {key:_expand(item) for key,item in value.items()}


def execute(token):
    try:
        return _execute(token)
    except Exception as exc:
        # Version/schema/serialization failures before or after a domain handler
        # must not leave a claimed receipt renewing forever with no live task.
        with db.write() as conn:
            owned=commands._owned(conn,token)
            if not owned:return False
            result={'detail':{'code':'workspace_execution_failed','message':str(exc)}}
            blob=store._put_blob(conn,commands._dump(result))
            conn.execute('update workspace_requests set result_blob_id=?,error_status=500 where command_id=?',(blob,token['id']))
            commands._settle(conn,owned,'failed',{'workspace_request':token['id']},'workspace_execution_failed')
        return True


def defer_before_execution(token):
    with db.write() as conn:
        if not commands._owned(conn,token):return
        conn.execute("update commands set state='queued',owner=null,worker_epoch=null,attempt_id=null,lease_until=null,generation=generation+1 where id=?",(token['id'],))
        conn.execute('delete from command_reservations where command_id=?',(token['id'],))
        conn.execute("update command_attempts set finished_at=?,outcome='drain_before_execution' where id=?",(db.utc_now(),token['attempt_id']))


def _execute(token):
    body = json.loads(token['payload_json'])
    action = body['action']
    fn, signature = _actions[action]
    args = body.get('arguments') or json.loads(store.blob_content(body['arguments_blob_id']))
    request = args.get('request')
    if request and 'content_blob_id' in request:
        request['content'] = store.blob_content(request.pop('content_blob_id'))
    if request is not None:
        # Resolve postponed annotations in the declaring module (and unwrap guards).
        import typing
        original = inspect.unwrap(fn)
        model = typing.get_type_hints(original).get('request')
        if model and issubclass(model, BaseModel):
            args['request'] = model.model_validate(request)
    result, error, status = None, None, None
    with executing(token):
        effect_id = None
        try:
            authorize()
            from . import workspace_effects
            effect_id = workspace_effects.prepare(token)
            if body['path']:
                with db.connect() as conn:
                    actual = file_revision(conn, body['scope'], body['path'])
                if actual != body['file_base'] and not (actual=='missing' and body['file_base'].startswith('untracked:')):
                    raise HTTPException(409, detail={'code': 'revision_conflict', 'message': 'The file changed before this save executed. Your draft is retained.', 'current_revision': actual})
            result = fn(**args)
            workspace_effects.finish(effect_id)
        except HTTPException as exc:
            result, error, status = {'detail': exc.detail}, 'workspace_rejected', exc.status_code
            # Rejection before an effect is safe. A partially executed operation
            # retains its snapshots for explicit reconciliation.
            if effect_id and _still_owned(token):
                workspace_effects.finish(effect_id, error=error)
        except Exception as exc:
            result, error, status = {'detail': {'code': 'workspace_failed', 'message': str(exc)}}, 'workspace_failed', 500
            if effect_id and _still_owned(token):
                workspace_effects.finish(effect_id, error=error)
        with db.write() as conn:
            owned = commands._owned(conn, token)
            if not owned:
                return False
            refs=set()
            blob = store._put_blob(conn, commands._dump(_compact(result,refs)))
            for reference in refs:
                conn.execute('insert or ignore into workspace_blob_refs values(?,?)',(token['id'],reference))
            conn.execute('update workspace_requests set result_blob_id=?,error_status=? where command_id=?', (blob, status, token['id']))
            commands._settle(conn, owned, 'failed' if error else 'succeeded', {'workspace_request': token['id']}, error)
    return True
