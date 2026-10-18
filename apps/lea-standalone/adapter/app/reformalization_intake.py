"""Shared SQL/admission functions. No workspace or executor dependency."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from uuid import uuid4
from fastapi import HTTPException
from . import process_state as execution
from . import db, store
_actor = ContextVar('reformalization_actor', default=None)
_operation_claim = ContextVar('reformalization_command_claim', default=None)

def capability():
    return execution.health().get('capabilities', {}).get('reformalization', {'version': 1, 'admission_enabled': False, 'staging': False})

def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()

@contextmanager
def acting(operation_id):
    token = _actor.set(operation_id)
    try:
        yield
    finally:
        _actor.reset(token)

def actor():
    return _actor.get()

def registered_dependency(initiating_run_id, dependency_id, project_id):
    """Only a recorded consumer edge can bypass its foreground owner."""
    if not initiating_run_id or not dependency_id:
        return False
    with db.connect() as conn:
        return bool(conn.execute('select 1 from runs r join project_dependency_edges e\n            on e.requester_id=r.focus_formalization_id and e.project_id=r.project_id\n            where r.id=? and r.project_id=? and e.dependency_id=?', (initiating_run_id, project_id, dependency_id)).fetchone())

def owner(project_id):
    if not project_id:
        return None
    with db.connect() as conn:
        try:
            row = conn.execute("select * from reformalization_operations where project_id=? and state in ('active','applying')", (project_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                return None
            raise
    return dict(row) if row else None

def assert_writer(project_id):
    unsettled = unfinished_application(project_id)
    if unsettled and unsettled['operation_id'] != actor():
        raise HTTPException(409, detail={'error': 'publication_recovery_required', 'message': 'This project has an interrupted publication to recover before new writes.'})
    current = owner(project_id)
    if current and current['id'] != actor():
        child = get(actor()) if actor() else None
        if child and child['project_id'] == project_id and ((child.get('request') or {}).get('_foreground_owner') == current['id']):
            run = store.get_run(child.get('root_run_id')) or {}
            parent = for_run(run.get('initiating_run_id')) if run.get('initiating_run_id') else None
            if run.get('purpose') == 'project_dependency' and parent and registered_dependency(run.get('initiating_run_id'), run.get('dependency_id'), project_id) and (parent['id'] == current['id'] or parent['request'].get('_foreground_owner') == current['id']):
                return
        raise HTTPException(409, detail={'error': 'project_busy', 'message': "A reformalization owns this project's mathematics. Wait for it to settle.", 'reformalization_id': current['id']})

def unfinished_application(project_id):
    if not project_id:
        return None
    with db.connect() as conn:
        try:
            row = conn.execute("select a.operation_id from reformalization_applications a\n                join reformalization_operations o on o.id=a.operation_id where o.project_id=?\n                and a.state in ('prepared','applying','restoring') limit 1", (project_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                return None
            raise
    return dict(row) if row else None

def assert_readable(project_id):
    unsettled = unfinished_application(project_id)
    if unsettled and unsettled['operation_id'] != actor():
        raise HTTPException(503, detail={'error': 'publication_recovery_required', 'message': 'Current project views are waiting for publication recovery.'}, headers={'Retry-After': '1'})

def get(operation_id):
    with db.connect() as conn:
        row = conn.execute('select * from reformalization_operations where id=?', (operation_id,)).fetchone()
    if not row:
        return None
    value = dict(row)
    for column, key in (('request_json', 'request'), ('manifest_json', 'manifest'), ('result_json', 'result')):
        value[key] = json.loads(value.pop(column)) if value.get(column) else None
    return value

def for_run(run_id):
    run = store.get_run(run_id)
    return get(run['reformalization_id']) if run and run.get('reformalization_id') else None

def enqueue(request, project_id):
    if not capability()['admission_enabled']:
        raise HTTPException(503, detail={'error': 'reformalization_unavailable', 'message': execution.capability().get('reason') or 'New reformalizations are disabled; retained work is available.'})
    if not execution.capability()['available']:
        raise HTTPException(503, detail={'error': 'reformalization_executor_unavailable', 'message': execution.capability().get('reason') or 'The filesystem executor is unavailable.'})
    form = store.get_formalization(request.get('focus_formalization_id')) if request.get('focus_formalization_id') else None
    if not form and project_id and (request.get('new_formalization') or {}).get('origin_key'):
        with db.connect() as conn:
            row = conn.execute('select * from formalizations where project_id=? and origin=? and origin_key=?', (project_id, request['new_formalization'].get('origin', 'overleaf'), request['new_formalization']['origin_key'])).fetchone()
        form = dict(row) if row else None
    if not form or form.get('project_id') != project_id:
        raise HTTPException(422, 'Reformalization requires an existing target in this project.')
    if not store.list_formalization_files(form['id']):
        raise HTTPException(422, 'This target has no current Lean work to reformalize.')
    normalized = {**request, 'focus_formalization_id': form['id'], 'new_formalization': None}
    request_hash = digest(dumps({k: v for k, v in normalized.items() if k not in {'idempotency_key', 'session_id'}}))
    with db.connect() as conn:
        prior = conn.execute('select * from reformalization_operations where request_key=?', (request.get('idempotency_key'),)).fetchone() if request.get('idempotency_key') else None
        duplicate = conn.execute("select * from reformalization_operations where formalization_id=? and request_hash=? and state in ('queued','active','applying') order by rowid desc limit 1", (form['id'], request_hash)).fetchone()
    if prior:
        if prior['request_hash'] != request_hash or (request.get('session_id') and request['session_id'] != prior['session_id']):
            raise HTTPException(409, 'Idempotency key belongs to a different reformalization request')
        return get_after_transaction(dict(prior))
    if duplicate:
        return get_after_transaction(dict(duplicate))
    session_id = request.get('session_id')
    if not session_id:
        sessions = store.session_ids_for_formalization(form['id'])
        session_id = sessions[0] if sessions else None
    request = {**request, 'session_id': session_id, 'focus_formalization_id': form['id'], 'new_formalization': None}
    request_hash = digest(dumps({k: v for k, v in request.items() if k not in {'idempotency_key', 'session_id'}}))
    now, operation_id = (db.utc_now(), str(uuid4()))
    with db.write() as conn:
        key = request.get('idempotency_key')
        old = conn.execute('select * from reformalization_operations where request_key=?', (key,)).fetchone() if key else None
        if old:
            if old['request_hash'] != request_hash or (request.get('session_id') and request['session_id'] != old['session_id']):
                raise HTTPException(409, 'Idempotency key belongs to another reformalization request.')
            return get_after_transaction(dict(old))
        duplicate = conn.execute("select * from reformalization_operations where formalization_id=? and request_hash=? and state in ('queued','active','applying') order by rowid desc limit 1", (form['id'], request_hash)).fetchone()
        if duplicate:
            return get_after_transaction(dict(duplicate))
        if not session_id:
            session_id = str(uuid4())
            conn.execute("insert into sessions(id,project_id,title,origin,created_at,updated_at) values (?,?,?,'ui',?,?)", (session_id, project_id, ('Re-formalize ' + form['display_title'])[:120], now, now))
        session = conn.execute('select project_id from sessions where id=?', (session_id,)).fetchone()
        if not session or session[0] != project_id:
            raise HTTPException(409, 'Conversation belongs to another project.')
        conn.execute('insert or ignore into session_formalizations values (?,?,?)', (session_id, form['id'], now))
        request = {**request, 'session_id': session_id}
        conn.execute("insert into reformalization_operations(id,project_id,formalization_id,session_id,\n            request_key,request_hash,request_json,state,queued_at,created_at,updated_at) values (?,?,?,?,?,?,?,'queued',?,?,?)", (operation_id, project_id, form['id'], session_id, key, request_hash, dumps(request), now, now, now))
        from . import run_commands
        run_commands.attach_operation(conn, {'id': operation_id, 'project_id': project_id, 'session_id': session_id, 'formalization_id': form['id']})
    return get(operation_id)

def get_after_transaction(value):
    for column, key in (('request_json', 'request'), ('manifest_json', 'manifest'), ('result_json', 'result')):
        value[key] = json.loads(value.pop(column)) if value.get(column) else None
    return value

def response(op):
    awaiting_segment = bool((op.get('result') or {}).get('continuation'))
    run = store.get_run(op.get('root_run_id')) if op.get('root_run_id') and (not awaiting_segment) else None
    bundle = store.get_run_submission(run['client_request_id']) if run and run.get('client_request_id') else None
    from .api_routes.runs import _creation_response
    details = _creation_response(bundle) if bundle else {}
    if not details.get('command'):
        from . import commands
        with db.connect() as conn:
            receipt = conn.execute('select c.* from operation_commands o join commands c on c.id=o.command_id where o.operation_id=? order by c.admission_id desc limit 1', (op['id'],)).fetchone()
        if receipt:
            details['command'] = commands.receipt(receipt)
    return {**details, 'reformalization_id': op['id'], 'run_id': None if awaiting_segment else op.get('root_run_id'), 'session_id': op['session_id'], 'focus_formalization_id': op['formalization_id'], 'project_id': op['project_id'], 'state': op['state'], 'result': op.get('result'), 'artifact_scope': 'attempt' if op['state'] != 'settled' else 'canonical'}

def bind_run_command(conn, operation_id, run):
    """Bind a staged run before the command can become visible to a consumer."""
    raw = conn.execute('select * from reformalization_operations where id=?', (operation_id,)).fetchone()
    parent = get_after_transaction(dict(raw)) if raw else None
    token = _operation_claim.get()
    if token:
        from . import run_commands
        if not run_commands.owns_operation(conn, operation_id, token):
            raise ValueError('Source preparation lost its command claim before solver admission')
    if not parent or parent['state'] not in {'active', 'worker'} or parent['request'].get('_cancelled'):
        raise ValueError('This reformalization was cancelled before solver admission')
    if run['purpose'] == 'project_dependency':
        op_id, now = (str(uuid4()), db.utc_now())
        request = {'_foreground_owner': parent['request'].get('_foreground_owner') or operation_id, '_parent_operation': operation_id}
        conn.execute("insert into reformalization_operations(id,project_id,formalization_id,session_id,\n            request_hash,request_json,state,root_run_id,created_at,updated_at) values (?,?,?,?,?,?,'worker',?,?,?)", (op_id, run['project_id'], run['focus_formalization_id'], run['session_id'], digest(dumps(request)), dumps(request), run['id'], now, now))
    else:
        op_id = operation_id
        conn.execute('update reformalization_operations set root_run_id=?,updated_at=? where id=?', (run['id'], db.utc_now(), op_id))
    conn.execute('update runs set reformalization_id=? where id=?', (op_id, run['id']))
    return op_id

def drafts(operation_id):
    with db.connect() as conn:
        rows=conn.execute('''select d.*,b.content as code from reformalization_drafts d
            join artifact_blobs b on b.id=d.blob_id where d.operation_id=?
            and d.id in (select max(id) from reformalization_drafts where operation_id=? group by path)
            order by d.path''',(operation_id,operation_id)).fetchall()
    op=get(operation_id)
    before=(op.get('manifest') or {}).get('files',{})
    return [{**dict(row),'id':'draft:'+str(row['id']),'author':'agent','session_id':op['session_id'],
        'formalization_id':op['formalization_id'],'baseline_blob_id':before.get(row['path'],{}).get('blob_id'),
        'artifact_scope':'attempt','evidence':json.loads(row['evidence_json']) if row['evidence_json'] else None} for row in rows]


def versions(operation_id):
    op = get(operation_id)
    if not op:
        raise HTTPException(404, 'Reformalization not found')
    items = [{'path': p, 'kind': 'baseline', 'blob_id': info['blob_id']} for p, info in (op.get('manifest') or {}).get('files', {}).items()]
    items.extend(({**d, 'kind': 'index'} for d in (op.get('manifest') or {}).get('index_before', [])))
    with db.connect() as conn:
        journals = [json.loads(r[0]) for r in conn.execute('select manifest_json from reformalization_applications where operation_id=?', (operation_id,))]
        items.extend(({'path': r['path'], 'kind': 'draft', 'blob_id': r['blob_id']} for r in conn.execute('select path,blob_id from reformalization_drafts where operation_id=?', (operation_id,))))
    for journal in journals:
        for path, info in journal['files'].items():
            for field in ('before_blob', 'after_blob', 'displaced_blob', 'conflicting_blob', 'rollback_blob'):
                if info.get(field):
                    items.append({'path': path, 'kind': field, 'blob_id': info[field]})
    return items

def continuation_hash(run_id, request):
    return digest(json.dumps({'predecessor': run_id, **request}, sort_keys=True))

def queue_continuation(op, old, request, *, recovery_review=False):
    """Reacquire FIFO ownership before creating another accounting segment."""
    request_hash = continuation_hash(old['id'], request)
    op = get(op['id'])
    foreground = get(op['request'].get('_foreground_owner')) if op['request'].get('_foreground_owner') else op
    if request.get('dependency_resume') and (op['request'].get('_cancelled') or foreground['request'].get('_cancelled')):
        raise HTTPException(409, 'This attempt was stopped; automatic continuation is disabled')
    previous = store.get_run_submission(request.get('idempotency_key')) if request.get('idempotency_key') else None
    if previous:
        if previous['run'].get('client_request_hash') != request_hash:
            raise HTTPException(409, 'Continuation identity was reused with different inputs')
        from .api_routes.runs import _creation_response
        return {**response(op), **_creation_response(previous)}
    waiting = (op.get('result') or {}).get('continuation')
    if waiting:
        if waiting['request'].get('idempotency_key') != request.get('idempotency_key'):
            raise HTTPException(409, 'A continuation of this attempt is already waiting')
        if waiting['request'] != request or waiting['old_run_id'] != old['id']:
            raise HTTPException(409, 'Continuation identity was reused with different inputs')
        return response(op)
    if old['id'] != op['root_run_id']:
        raise HTTPException(409, 'A newer segment of this attempt already exists')
    if op['state'] not in {'paused', 'active', 'worker', 'retained'}:
        raise HTTPException(409, 'This attempt is not paused for continuation')
    if request.get('dependency_resume') and (op['state'] not in {'active', 'worker'} or old.get('stop_reason') != 'dependency_wait'):
        raise HTTPException(409, 'Automatic continuation requires an owned dependency wait')
    if not old.get('recoverable') or old['status'] not in {'cancelled', 'max_turns'}:
        raise HTTPException(409, 'This run is not a settled recoverable pause')
    if request.get('best_effort') and old.get('stop_reason') != 'source_obstruction':
        raise HTTPException(409, 'Best-effort continuation requires a source-obstruction pause')
    from . import checkpoint_queries as recovery
    if recovery_review:
        if old['status'] != 'cancelled' or old.get('recovery_state') != 'review':
            raise HTTPException(409, 'Run is not awaiting recovery review')
        if old.get('deadline_at') and datetime.fromisoformat(old['deadline_at']) <= datetime.now(timezone.utc):
            raise HTTPException(409, 'The original run deadline has expired')
        state = (recovery.load_checkpoint(old['id']) or ({}, {}))[0]
        checkpoint_id = None
        if old.get('max_turns') is not None and int(old['max_turns']) <= int(state.get('turn', 0)):
            raise HTTPException(409, 'The original turn allowance is exhausted')
    else:
        state, _, checkpoint_id = recovery.continuation_checkpoint(old['id'])
    source = None
    if request.get('source_context_ref'):
        from . import overleaf_source
        with db.connect() as conn:
            source = overleaf_source.resolve(conn, request['source_context_ref'], op['formalization_id'], bind_source=False)
    elif request.get('source_bundle'):
        from .source_context import validate_source
        source = validate_source(request['source_bundle'])
    if source and source['sourceIdentityHash'] != op['manifest']['source_hash']:
        raise HTTPException(409, 'The source changed. The paused draft is retained; start a new reformalization against the current source.')
    if request.get('best_effort'):
        from . import lea_status_store
        prior_context = lea_status_store.context(old['id'])
        effective_source = source or (prior_context or {}).get('source_bundle') or {}
        if str(effective_source.get('proof') or '').strip() or effective_source.get('proofAssociation', {}).get('status') not in {'missing', 'not_applicable'}:
            raise HTTPException(409, 'Best-effort continuation requires a proofless source-obstruction pause')
    payload = {'old_run_id': old['id'], 'checkpoint_id': checkpoint_id, 'request': request, 'source': source, 'saved_turn': int(state.get('turn', 0)), 'recovery_review': recovery_review}
    dependent_worker = bool(op['request'].get('_foreground_owner') and (owner(op['project_id']) or {}).get('id') == op['request']['_foreground_owner'] and registered_dependency(old.get('initiating_run_id'), old.get('dependency_id'), op['project_id']))
    keep_owner = request.get('dependency_resume') or dependent_worker
    if keep_owner:
        from .dependency_intake import get as dependency
        if any((not (dependency(key) or {}).get('ready') for key in json.loads(old.get('waiting_dependencies_json') or '[]'))):
            raise HTTPException(409, 'The shared dependencies are not verified yet')
    joined = False
    with db.write() as conn:
        current = get_after_transaction(dict(conn.execute('select * from reformalization_operations where id=?', (op['id'],)).fetchone()))
        admitted = conn.execute('select client_request_hash from runs where client_request_id=?', (request.get('idempotency_key'),)).fetchone()
        pending = (current.get('result') or {}).get('continuation')
        if admitted:
            if admitted['client_request_hash'] != request_hash:
                raise HTTPException(409, 'Continuation identity was reused with different inputs')
            joined = True
        elif pending:
            if pending['old_run_id'] != old['id'] or pending['request'] != request:
                raise HTTPException(409, 'This attempt already has a successor request')
            joined = True
        else:
            if current['root_run_id'] != old['id'] or current['state'] not in {'paused', 'active', 'worker', 'retained'}:
                raise HTTPException(409, 'This attempt already has a successor request')
            if current['request'].get('_cancelled') and (request.get('dependency_resume') or not op['request'].get('_cancelled')):
                raise HTTPException(409, 'This attempt was stopped during continuation admission')
            current_request = current['request'] if request.get('dependency_resume') else {k: v for k, v in current['request'].items() if k != '_cancelled'}
            state = ('worker' if dependent_worker else current['state']) if keep_owner else 'queued'
            result = {**(current.get('result') or {}), 'continuation': payload}
            conn.execute('update reformalization_operations set state=?,request_json=?,result_json=?,queued_at=?,updated_at=? where id=?', (state, dumps(current_request), dumps(result), current['queued_at'] if keep_owner else db.utc_now(), db.utc_now(), op['id']))
            from . import run_commands
            run_commands.attach_operation(conn, current, 'continuation:' + request['idempotency_key'])
    if joined:
        submission = store.get_run_submission(request.get('idempotency_key'))
        if submission:
            from .api_routes.runs import _creation_response
            return {**response(get(op['id'])), **_creation_response(submission)}
        return response(get(op['id']))
    return response(get(op['id']))
