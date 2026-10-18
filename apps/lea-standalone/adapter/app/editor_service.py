"""SQL-only editor admission and results. No process, workspace or prover access."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
import secrets
from uuid import uuid4
from pydantic import BaseModel,ConfigDict,Field,StrictInt
from . import commands,db,workspace_commands,observation,run_commands,migration_limits as limits
from .queries import core

KINDS={'editor.analyze','editor.hover','editor.complete','editor.goals','editor.term_goal'}
BUFFER_BYTES=4*1024*1024
OWNER_BYTES=8*1024*1024
MAX_BUFFERS=256
MAX_CLIENTS=32
MAX_REQUESTS=2048
RESULT_BYTES=256*1024
RESULT_OWNER_BYTES=8*1024*1024
LIFETIME=limits.EDITOR_RESULT_SECONDS


class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class Buffer(StrictModel):
    version:StrictInt=Field(ge=1,le=9007199254740991)
    path:str=Field(min_length=1,max_length=1024)
    base_revision:str=Field(min_length=1,max_length=512)
    environment_revision:str=Field(min_length=1,max_length=128)
    content:str=Field(max_length=BUFFER_BYTES)


class Position(StrictModel):
    line:StrictInt=Field(ge=0,le=1000000)
    character:StrictInt=Field(ge=0,le=1000000)


class LanguageRequest(StrictModel):
    version:StrictInt=1
    kind:str=Field(max_length=40)
    buffer_id:str=Field(min_length=1,max_length=80)
    environment_revision:str=Field(min_length=1,max_length=128)
    idempotency_key:str=Field(min_length=1,max_length=180)
    sequence:StrictInt=Field(ge=1,le=9007199254740991)
    supersession_key:str=Field(default='main',min_length=1,max_length=80)
    deadline:str=Field(max_length=64)
    position:Position|None=None


def _now():return datetime.now(timezone.utc)
def _expiry():return (_now()+timedelta(seconds=LIFETIME)).isoformat()
def _error(code,status=409):raise commands.QueueError(code,status)


def capability():
    return {'version':1,'transport':'database','direct_lsp':False,'kinds':sorted(KINDS),
        'unsupported':['interactive_widgets','server_rpc','definition_navigation','code_actions','semantic_tokens'],
        'deadline_seconds':limits.EDITOR_DEADLINE_SECONDS,'queued_per_producer':limits.EDITOR_QUEUED_PRODUCER,
        'result_bytes':RESULT_BYTES,'retention_seconds':LIFETIME,'diagnostics_are_proof_evidence':False}


def _session(conn,session_id):
    row=conn.execute('select * from sessions where id=?',(session_id,)).fetchone()
    if not row:_error('unknown_session',404)
    return row


def _scope(session):return core.scope_for(session_id=session['id'])


def environment(conn,session):
    scope=core.scope_for(session_id=session['id'])
    value,reason=core.observation(scope)
    if reason:_error('environment_'+reason,503)
    return commands._hash(commands._dump({'scope':scope,'revision':core.revision(conn,scope),
        'environment':value['environment_revision']}))


def file_info(session_id,path=None):
    with db.read_snapshot() as conn:
        session=_session(conn,session_id)
        if path is None:
            row=conn.execute("select path from timeline where session_id=? and kind='code' and path like '%.lean' order by id desc limit 1",(session_id,)).fetchone()
            if not row:_error('no_published_lean_file',404)
            path=row[0]
        path=workspace_commands.normalize_path(path)
        if not path.endswith('.lean'):_error('lean_file_required',422)
        scope=_scope(session)
        where='s.project_id=?' if session['project_id'] else 's.id=?'
        row=conn.execute(f'''select t.after_blob_id from timeline t join sessions s on s.id=t.session_id
            where {where} and t.path=? and t.kind='code' and t.after_blob_id is not null order by t.id desc limit 1''',
            (scope[2:],path)).fetchone()
        revision=workspace_commands.file_revision(conn,scope,path)
        if revision.startswith('deleted:'):_error('file_deleted',404)
        binding=conn.execute('select blob_id,encoding from workspace_files where scope=? and path=?',(scope,path)).fetchone()
        blob=row[0] if row else binding['blob_id'] if binding and binding['encoding']=='utf8' else revision[9:] if revision.startswith('observed:') else None
        if not blob:_error('file_not_indexed',503)
        value=conn.execute('select content from artifact_blobs where id=?',(blob,)).fetchone()
        if not value:_error('file_not_indexed',503)
        try:env=environment(conn,session);state='observed'
        except commands.QueueError as exc:env=None;state=exc.code
        worker=conn.execute('select epoch,lease_until from command_workers where id=?',('lea-command-host',)).fetchone()
        available=bool(worker and worker['lease_until']>db.utc_now())
        return {'worker':{'state':'draining' if run_commands.drain_requested(conn) else 'available' if available else 'unavailable','epoch':worker['epoch'] if available else None},
            'version':1,'session_id':session_id,'project_id':session['project_id'],'path':path,
            'fileName':path,'content':value[0],'file_revision':revision,'environment_revision':env,
            'analysis_state':state,'capabilities':capability(),'cursor':observation.watermark(conn)}


def create_client(session_id):
    with db.write() as conn:
        session=_session(conn,session_id)
        if conn.execute('select count(*) from editor_clients where expires_at>?',(db.utc_now(),)).fetchone()[0]>=MAX_CLIENTS:_error('editor_clients_full',429)
        owner='editor:'+uuid4().hex;credential=secrets.token_urlsafe(32)
        conn.execute('insert into command_producers values (?,?,?,?,1,?)',(owner,'editor',commands._hash(credential),session['project_id'],db.utc_now()))
        conn.execute('insert into editor_clients values (?,?,?,?,?,?)',(owner,session_id,session['project_id'],_scope(session),db.utc_now(),_expiry()))
        return {'owner_id':owner,'credential':credential,'capabilities':capability()}


def authenticate(conn,credential):
    producer=commands.authenticate(credential,conn)
    row=conn.execute('select * from editor_clients where id=? and expires_at>?',(producer['id'],db.utc_now())).fetchone()
    if not row:_error('editor_owner_expired',401)
    session=_session(conn,row['session_id'])
    if session['project_id']!=row['project_id'] or _scope(session)!=row['scope']:_error('editor_scope_changed')
    return row,session


def close_client(credential):
    with db.write() as conn:
        owner,_=authenticate(conn,credential)
        commands.revoke(owner['id'])
        conn.execute('update editor_clients set expires_at=? where id=?',(db.utc_now(),owner['id']))
        return {'closed':True}


def _cancel(conn,owner,path,*,except_buffer=None):
    rows=conn.execute('''select c.* from commands c join editor_requests r on r.command_id=c.id
        join editor_buffers b on b.id=r.buffer_id where r.owner_id=? and b.path=? and c.state in ('queued','claimed')
        and (? is null or b.id!=?)''',(owner,path,except_buffer,except_buffer)).fetchall()
    for row in rows:commands._settle(conn,row,'cancelled',error='superseded')


def publish(credential,value):
    try:raw=value.content.encode()
    except UnicodeEncodeError:_error('invalid_editor_text',422)
    if len(raw)>BUFFER_BYTES:_error('editor_buffer_too_large',413)
    path=workspace_commands.normalize_path(value.path)
    with db.write() as conn:
        owner,session=authenticate(conn,credential)
        old=conn.execute('select * from editor_buffers where owner_id=? and path=? and version=?',(owner['id'],path,value.version)).fetchone()
        sha=hashlib.sha256(value.content.encode()).hexdigest()
        if old:
            if (old['sha256'],old['base_revision'],old['environment_revision'])!=(sha,value.base_revision,value.environment_revision):_error('buffer_version_conflict')
            return _buffer_receipt(old)
        info=file_info(session['id'],path)
        if info['file_revision']!=value.base_revision:_error('buffer_base_changed')
        if info['environment_revision']!=value.environment_revision:_error('environment_changed')
        head=conn.execute('''select b.version from editor_heads h join editor_buffers b on b.id=h.buffer_id
            where h.owner_id=? and h.path=?''',(owner['id'],path)).fetchone()
        if head and value.version<=head[0]:_error('buffer_superseded')
        _prune_buffers(conn,owner['id'])
        count,size=conn.execute('''select (select count(*) from editor_buffers where owner_id=?),coalesce(sum(length(cast(content as blob))),0) from editor_contents
            where id in (select blob_id from editor_buffers where owner_id=?)''',(owner['id'],owner['id'])).fetchone()
        reused=conn.execute('select 1 from editor_buffers where owner_id=? and blob_id=?',(owner['id'],sha)).fetchone()
        if count>=MAX_BUFFERS or size+(0 if reused else len(value.content.encode()))>OWNER_BYTES:_error('editor_buffer_quota',429)
        bid=uuid4().hex;blob=sha
        conn.execute('insert or ignore into editor_contents values (?,?)',(blob,value.content))
        conn.execute('insert into editor_buffers values (?,?,?,?,?,?,?,?,?,?,?,?)',
            (bid,owner['id'],session['id'],session['project_id'],path,value.version,value.base_revision,value.environment_revision,blob,sha,db.utc_now(),_expiry()))
        conn.execute('insert into editor_heads values (?,?,?,?,?) on conflict(owner_id,path) do update set buffer_id=excluded.buffer_id',
            (owner['id'],path,bid,session['id'],session['project_id']))
        conn.execute('update editor_clients set expires_at=? where id=?',(_expiry(),owner['id']))
        _cancel(conn,owner['id'],path,except_buffer=bid)
        return _buffer_receipt(conn.execute('select * from editor_buffers where id=?',(bid,)).fetchone())


def _buffer_receipt(row):
    return {k:row[k] for k in ('id','owner_id','session_id','project_id','path','version','base_revision','environment_revision','sha256','expires_at')}


def current(conn,buffer):
    if run_commands.drain_requested(conn):return 'worker_draining'
    head=conn.execute('select buffer_id from editor_heads where owner_id=? and path=?',(buffer['owner_id'],buffer['path'])).fetchone()
    if not head or head[0]!=buffer['id']:return 'superseded'
    if buffer['expires_at']<=db.utc_now():return 'buffer_expired'
    session=_session(conn,buffer['session_id'])
    client=conn.execute('select project_id,scope from editor_clients where id=?',(buffer['owner_id'],)).fetchone()
    if not client or session['project_id']!=client[0] or _scope(session)!=client[1]:return 'scope_changed'
    if workspace_commands.file_revision(conn,_scope(session),buffer['path'])!=buffer['base_revision']:return 'saved_file_changed'
    try:
        if environment(conn,session)!=buffer['environment_revision']:return 'environment_changed'
    except commands.QueueError:return 'environment_unavailable'
    return None


def submit(credential,value):
    if value.version!=1 or value.kind not in KINDS:_error('unsupported_editor_request',422)
    if value.kind!='editor.analyze' and value.position is None:_error('position_required',422)
    encoded=commands._dump(value.model_dump())
    with db.write() as conn:
        owner,session=authenticate(conn,credential)
        old=conn.execute('''select r.* from editor_requests r join commands c on c.id=r.command_id
            where r.owner_id=? and c.idempotency_key=?''',(owner['id'],value.idempotency_key)).fetchone()
        if old:
            if old['request_hash']!=commands._hash(encoded):_error('idempotency_conflict')
            return _result(conn,old)
        if not commands.capability()['admission_enabled']:_error('admission_disabled',503)
        if run_commands.drain_requested(conn):_error('adapter_draining',503)
        buffer=conn.execute('select * from editor_buffers where id=? and owner_id=?',(value.buffer_id,owner['id'])).fetchone()
        if not buffer:_error('editor_buffer_not_found',404)
        if value.environment_revision!=buffer['environment_revision']:_error('environment_changed')
        reason=current(conn,buffer)
        if reason:_error(reason)
        if value.position:
            lines=conn.execute('select content from editor_contents where id=?',(buffer['blob_id'],)).fetchone()[0].split('\n')
            if value.position.line>=len(lines) or value.position.character>len(lines[value.position.line].encode('utf-16-le'))//2:_error('position_outside_buffer',422)
        try:end=datetime.fromisoformat(value.deadline.replace('Z','+00:00'))
        except ValueError:_error('invalid_deadline',422)
        if end.tzinfo is None or end<=_now() or end>_now()+timedelta(seconds=limits.EDITOR_DEADLINE_SECONDS+.1):_error('invalid_deadline',422)
        end=end.astimezone(timezone.utc)
        slot=conn.execute('''select * from editor_request_slots where owner_id=? and path=? and kind=? and supersession_key=?''',
            (owner['id'],buffer['path'],value.kind,value.supersession_key)).fetchone()
        if slot:
            if slot['sequence']>=value.sequence:_error('request_superseded')
            previous=conn.execute("select * from commands where id=? and state in ('queued','claimed')",(slot['command_id'],)).fetchone()
            if previous:commands._settle(conn,previous,'cancelled',error='superseded')
            conn.execute("update editor_requests set result_json=null,result_state='superseded' where command_id=?",(slot['command_id'],))
        if conn.execute('select count(*) from editor_requests where owner_id=?',(owner['id'],)).fetchone()[0]>=MAX_REQUESTS:_error('editor_request_quota',429)
        conn.execute('update editor_clients set expires_at=? where id=?',(_expiry(),owner['id']))
        commands._reserve(conn,'editor',owner['id'],session['project_id'])
        cid=uuid4().hex
        conn.execute('''insert into commands(id,producer_id,idempotency_key,request_hash,kind,version,lane,project_id,session_id,
            payload_json,state,created_at,deadline) values (?,?,?,?,?,1,'editor',?,?,?,'queued',?,?)''',
            (cid,owner['id'],value.idempotency_key,commands._hash(encoded),value.kind,session['project_id'],session['id'],commands._dump({'editor_request':cid}),db.utc_now(),end.isoformat()))
        conn.execute('insert into editor_requests values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (cid,owner['id'],session['id'],session['project_id'],buffer['id'],value.kind,value.sequence,value.supersession_key,commands._hash(encoded),
             commands._dump(value.position.model_dump()) if value.position else None,None,'pending',db.utc_now(),_expiry()))
        conn.execute('insert into editor_request_slots values (?,?,?,?,?,?) on conflict(owner_id,path,kind,supersession_key) do update set sequence=excluded.sequence,command_id=excluded.command_id',
            (owner['id'],buffer['path'],value.kind,value.supersession_key,value.sequence,cid))
        return _result(conn,conn.execute('select * from editor_requests where command_id=?',(cid,)).fetchone())


def settled(conn,command,state,error):
    # Called in the same settlement transaction, including expiry and revocation.
    conn.execute("update editor_requests set result_state=? where command_id=? and result_state='pending'",(error or state,command['id']))


def _result(conn,row):
    command=conn.execute('select * from commands where id=?',(row['command_id'],)).fetchone()
    buffer=conn.execute('select * from editor_buffers where id=?',(row['buffer_id'],)).fetchone()
    reason=current(conn,buffer) if buffer else 'buffer_expired'
    if row['expires_at']<=db.utc_now():reason='result_expired'
    slot=conn.execute('select command_id from editor_request_slots where owner_id=? and path=? and kind=? and supersession_key=?',
        (row['owner_id'],buffer['path'] if buffer else '',row['kind'],row['supersession_key'])).fetchone()
    if not slot or slot[0]!=row['command_id']:reason=reason or 'superseded'
    return {**commands.receipt(command),'buffer':_buffer_receipt(buffer) if buffer else None,'kind':row['kind'],
        'sequence':row['sequence'],'result_state':reason or row['result_state'],
        'current':not reason and row['result_state']=='available','expires_at':row['expires_at'],
        'result':json.loads(row['result_json']) if row['result_json'] and not reason else None,'cursor':observation.watermark(conn)}


def get(credential,command_id=None,idempotency_key=None):
    with db.read_snapshot() as conn:
        owner,_=authenticate(conn,credential)
        row=conn.execute('select * from editor_requests where owner_id=? and command_id=?',(owner['id'],command_id)).fetchone() if command_id else conn.execute('''select r.* from editor_requests r join commands c on c.id=r.command_id
            where r.owner_id=? and c.idempotency_key=?''',(owner['id'],idempotency_key)).fetchone()
        if not row:_error('editor_request_not_found',404)
        return _result(conn,row)


def _prune_buffers(conn,owner=None):
    rows=conn.execute('''select b.id,b.blob_id from editor_buffers b where (? is null or b.owner_id=?)
        and (b.expires_at<=? or not exists(select 1 from editor_heads h where h.buffer_id=b.id))
        and not exists(select 1 from editor_requests r join commands c on c.id=r.command_id where r.buffer_id=b.id and c.state in ('queued','claimed')) limit 100''',(owner,owner,db.utc_now())).fetchall()
    for row in rows:
        conn.execute('delete from editor_heads where buffer_id=?',(row['id'],))
        conn.execute('delete from editor_buffers where id=?',(row['id'],))
        conn.execute('delete from editor_contents where id=? and not exists(select 1 from editor_buffers where blob_id=?)',(row['blob_id'],row['blob_id']))
    return len(rows)


def retain():
    with db.write() as conn:
        expired=[r[0] for r in conn.execute('''select r.command_id from editor_requests r join commands c on c.id=r.command_id
            where r.expires_at<=? and c.state not in ('queued','claimed') order by r.expires_at limit 100''',(db.utc_now(),))]
        for cid in expired:
            conn.execute('delete from editor_request_slots where command_id=?',(cid,))
            conn.execute('delete from editor_requests where command_id=?',(cid,))
            conn.execute('delete from command_attempts where command_id=?',(cid,))
            conn.execute('delete from commands where id=?',(cid,))
        removed=_prune_buffers(conn)
        for row in conn.execute('''select id from editor_clients c where expires_at<=? and not exists(select 1 from editor_buffers b where b.owner_id=c.id)
            and not exists(select 1 from editor_requests r where r.owner_id=c.id) limit 32''',(db.utc_now(),)).fetchall():
            conn.execute('delete from editor_clients where id=?',(row[0],))
            conn.execute('delete from command_producers where id=?',(row[0],))
            conn.execute('delete from command_capacity where key=?',('editor:producer:'+row[0],))
        return len(expired)+removed
