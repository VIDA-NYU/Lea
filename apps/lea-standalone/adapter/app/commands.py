"""Regulated SQL command queue and worker ownership.

No bridge, workspace, provider, compiler or UI imports. Scope/admission/ownership
checks commit before handlers execute; receipts are database records.
"""
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import hashlib
import hmac
import json
import os
import secrets
from uuid import uuid4
from pydantic import BaseModel,ConfigDict,Field,StrictInt
from . import db,migration_limits as limits
from .queries.core import revision


class QueueError(ValueError):
    def __init__(self,code,status=409):self.code=code;self.status=status;super().__init__(code)


class Payload(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    text:str=Field(default='',max_length=16000)
    blob_id:str|None=None


class Envelope(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    kind:str
    version:StrictInt=1
    idempotency_key:str=Field(min_length=1,max_length=200)
    project_id:str|None=None
    session_id:str|None=None
    formalization_id:str|None=None
    expected_revision:StrictInt|None=Field(default=None,ge=0)
    deadline:str|None=None
    payload:Payload=Field(default_factory=Payload)


@dataclass(frozen=True)
class Handler:
    lane:str
    recovery:str
    producers:tuple=('operator',)


REGISTRY={'synthetic.echo':Handler('compute','retry'),'synthetic.control':Handler('control','retry'),
          'synthetic.editor':Handler('editor','retry'),'synthetic.uncertain':Handler('compute','suspend'),
          **{k:Handler('compute','suspend',('compatibility',)) for k in ('run.execute','operation.dispatch','workspace.execute','companion.dispatch')},
          **{k:Handler('editor','retry',('editor',)) for k in ('editor.analyze','editor.hover','editor.complete','editor.goals','editor.term_goal')},
          'companion.query':Handler('editor','retry',('compatibility',)),
          'companion.control':Handler('control','retry',('compatibility',)),
          **{k:Handler('control','retry',('compatibility',)) for k in ('run.stop','run.approve','child.stop','operation.cancel','runtime.control')}}
TERMINAL={'succeeded','failed','cancelled','suspended','expired'}


def capability():
    if os.environ.get('LEA_PROCESS_ROLE')=='api':
        from .process_state import health
        return health()['capabilities'].get('commands',{'version':1,'admission_enabled':False})
    return {'version':1,'admission_enabled':os.environ.get('LEA_COMMAND_ADMISSION_ENABLED','1').lower() not in {'0','false','no'},
            'synthetic_only':False,'producer_kinds':['operator','compatibility','editor'],'kinds':sorted(REGISTRY),
            'external_kinds':sorted(k for k in REGISTRY if k.startswith('synthetic.')),
            'solver_admission':'local_compatibility_translator','solver_version':1,'controls_enabled':True,
            'workspace_version':1,'workspace_admission':'local_typed_translator','workspace_edits_require_base':True,
            'source_publication_version':1,'companion_state':'database','companion_transport':'stateless',
            'application_reads':'database','editor_version':1,'direct_lsp':False}


def _now():return datetime.now(timezone.utc)
def _dump(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def _hash(value):return hashlib.sha256(value.encode()).hexdigest()


def register_producer(producer_id,kind='operator',project_id=None):
    if kind not in {'operator','standalone','overleaf'}:raise QueueError('invalid_producer',422)
    credential=secrets.token_urlsafe(32)
    with db.write() as conn:
        if project_id and not conn.execute('select 1 from projects where id=?',(project_id,)).fetchone():raise QueueError('unknown_project',404)
        conn.execute('insert into command_producers values (?,?,?,?,1,?)',
                     (producer_id,kind,_hash(credential),project_id,db.utc_now()))
    return credential


def authenticate(credential,conn=None):
    if not credential or len(credential)>512:raise QueueError('unauthorized',401)
    if conn is None:
        with db.connect() as connection:return authenticate(credential,connection)
    row=conn.execute('select * from command_producers where credential_hash=?',(_hash(credential),)).fetchone()
    if not row or not row['enabled'] or not hmac.compare_digest(row['credential_hash'],_hash(credential)):
        raise QueueError('unauthorized',401)
    return dict(row)


def _scope(conn,request,producer):
    project=request.project_id;session=request.session_id;fid=request.formalization_id
    if session:
        row=conn.execute('select project_id from sessions where id=?',(session,)).fetchone()
        if not row:raise QueueError('unknown_session',404)
        session_project=row[0]
        if project is not None and project!=row[0]:raise QueueError('scope_mismatch',422)
        project=row[0]
    if fid:
        form=conn.execute('select * from formalizations where id=?',(fid,)).fetchone()
        if not form:raise QueueError('unknown_formalization',404)
        if session and form['project_id']!=session_project:raise QueueError('scope_mismatch',422)
        if project is not None and form['project_id']!=project:raise QueueError('scope_mismatch',422)
        if session and not conn.execute('select 1 from session_formalizations where session_id=? and formalization_id=?',(session,fid)).fetchone():
            raise QueueError('scope_mismatch',422)
        project=form['project_id']
        if not project:session=session or form['loose_session_id']
    if project and not conn.execute('select 1 from projects where id=?',(project,)).fetchone():raise QueueError('unknown_project',404)
    if producer['project_id'] and project!=producer['project_id']:raise QueueError('producer_scope_denied',403)
    scope='p:'+project if project else 's:'+session if session else None
    if request.expected_revision is not None and (not scope or revision(conn,scope)!=request.expected_revision):
        raise QueueError('revision_conflict')
    blob=request.payload.blob_id
    if blob:
        if not scope:raise QueueError('blob_scope_required',422)
        owned=conn.execute('''select 1 from timeline t join sessions s on s.id=t.session_id where t.after_blob_id=?
            and ((? is not null and s.project_id=?) or (? is null and s.id=?)) union all
            select 1 from current_file_bindings where blob_id=? and project_id=? limit 1''',
            (blob,project,project,project,session,blob,project)).fetchone()
        if not owned:raise QueueError('blob_scope_denied',403)
    return project,session,fid


def _capacity(lane,producer,project):
    if lane=='compute':
        return [(lane+':global',limits.COMPUTE_QUEUED_GLOBAL),(lane+':producer:'+producer,limits.COMPUTE_QUEUED_PRODUCER)]+(
            [(lane+':project:'+project,limits.COMPUTE_QUEUED_PROJECT)] if project else [])
    if lane=='control':return [('control:global',limits.CONTROL_QUEUED_GLOBAL)]
    return [('editor:global',limits.EDITOR_QUEUED_GLOBAL),('editor:producer:'+producer,limits.EDITOR_QUEUED_PRODUCER)]


def _reserve(conn,lane,producer,project):
    for key,cap in _capacity(lane,producer,project):
        conn.execute('insert or ignore into command_capacity values (?,0)',(key,))
        if not conn.execute('update command_capacity set count=count+1 where key=? and count<?',(key,cap)).rowcount:
            raise QueueError('queue_full',429)


def _release(conn,row):
    for key,_ in _capacity(row['lane'],row['producer_id'],row['project_id']):
        conn.execute('update command_capacity set count=count-1 where key=?',(key,))
    conn.execute('delete from command_reservations where command_id=?',(row['id'],))


def receipt(row):
    return {k:row[k] for k in ('id','admission_id','producer_id','kind','version','lane','state','project_id','session_id','formalization_id',
                               'generation','attempt_id','created_at','deadline','error')} | {
        'result':json.loads(row['result_json']) if row['result_json'] else None}


def admit(credential,value):
    if not capability()['admission_enabled']:
        raise QueueError('admission_disabled',503)
    encoded=_dump(value)
    if len(encoded.encode())>limits.COMMAND_BYTES:raise QueueError('envelope_too_large',413)
    request=Envelope.model_validate(value)
    if request.kind.startswith('editor.'):
        raise QueueError('typed_editor_endpoint_required',422)
    handler=REGISTRY.get(request.kind)
    if handler is None or request.version!=1:raise QueueError('unsupported_command',422)
    now=_now()
    with db.write() as conn:
        producer=authenticate(credential,conn)
        if producer['kind'] not in handler.producers:raise QueueError('producer_kind_denied',403)
        # Resolve scope before hashing; project inference cannot create a different
        # semantic command under an idempotency key.
        old=conn.execute('select * from commands where producer_id=? and idempotency_key=?',(producer['id'],request.idempotency_key)).fetchone()
        if old:
            semantic=request.model_dump()
            for name in ('project_id','session_id','formalization_id'):
                if semantic[name] is not None and semantic[name]!=old[name]:raise QueueError('idempotency_conflict')
                semantic[name]=old[name]
            digest=_hash(_dump(semantic))
            if old['request_hash']!=digest:raise QueueError('idempotency_conflict')
            return receipt(old)
        project,session,fid=_scope(conn,request,producer)
        semantic=request.model_dump();semantic.update(project_id=project,session_id=session,formalization_id=fid)
        digest=_hash(_dump(semantic))
        try: deadline=datetime.fromisoformat(request.deadline.replace('Z','+00:00')) if request.deadline else now+timedelta(seconds=5 if handler.lane=='editor' else 86400)
        except ValueError:raise QueueError('invalid_deadline',422)
        if deadline.tzinfo is None or deadline<=now or deadline>now+timedelta(days=1):raise QueueError('invalid_deadline',422)
        deadline=deadline.astimezone(timezone.utc)
        if handler.lane=='editor':deadline=min(deadline,now+timedelta(seconds=limits.EDITOR_DEADLINE_SECONDS))
        _reserve(conn,handler.lane,producer['id'],project)
        command_id=uuid4().hex
        conn.execute('''insert into commands(id,producer_id,idempotency_key,request_hash,kind,version,lane,project_id,
            session_id,formalization_id,expected_revision,payload_json,state,created_at,deadline)
            values (?,?,?,?,?,1,?,?,?,?,?,?,'queued',?,?)''',
            (command_id,producer['id'],request.idempotency_key,digest,request.kind,handler.lane,project,session,fid,
             request.expected_revision,_dump(request.payload.model_dump()),now.isoformat(),deadline.isoformat()))
        return receipt(conn.execute('select * from commands where id=?',(command_id,)).fetchone())


def get(credential,command_id):
    with db.connect() as conn:
        producer=authenticate(credential,conn)
        row=conn.execute('select * from commands where id=? and producer_id=?',(command_id,producer['id'])).fetchone()
        if not row:raise QueueError('command_not_found',404)
        return receipt(row)


def start_worker(owner):
    now=_now();epoch=uuid4().hex
    with db.write() as conn:
        old=conn.execute('select * from command_workers where id=?',(owner,)).fetchone()
        if old and old['lease_until']>now.isoformat():raise QueueError('worker_already_active')
        conn.execute('insert into command_workers values (?,?,?,?) on conflict(id) do update set epoch=excluded.epoch,lease_until=excluded.lease_until,heartbeat_at=excluded.heartbeat_at',
                     (owner,epoch,(now+timedelta(seconds=limits.CLAIM_LEASE_SECONDS)).isoformat(),now.isoformat()))
    return epoch


def heartbeat_worker(owner,epoch):
    now=_now()
    with db.write() as conn:
        return bool(conn.execute('''update command_workers set lease_until=?,heartbeat_at=?
            where id=? and epoch=? and lease_until>?''',
            ((now+timedelta(seconds=limits.CLAIM_LEASE_SECONDS)).isoformat(),now.isoformat(),owner,epoch,now.isoformat())).rowcount)


def has_ready(lane,*,kinds=None):
    if kinds is not None and not kinds:return False
    extra=' and c.kind in ('+','.join('?' for _ in kinds)+')' if kinds is not None else ''
    with db.connect() as conn:
        return bool(conn.execute("select 1 from commands c join command_producers p on p.id=c.producer_id where c.lane=? and c.state='queued' and p.enabled=1"+extra+' limit 1',
            (lane,*(tuple(kinds) if kinds is not None else ()))).fetchone())


@contextmanager
def readiness_reader():
    """Worker-local autocommit reads, with no snapshot or lock between polls.

    Reusing schema parsing avoids making idle consumers repeatedly load the
    growing migration schema. Claims still repeat all checks in db.write().
    """
    conn=db._open(isolation_level=None)
    conn.execute('pragma query_only=on')
    def ready(lane,*,kinds=None):
        if kinds is not None and not kinds:return False
        extra=' and c.kind in ('+','.join('?' for _ in kinds)+')' if kinds is not None else ''
        cursor=conn.execute("select 1 from commands c join command_producers p on p.id=c.producer_id where c.lane=? and c.state='queued' and p.enabled=1"+extra+' limit 1',
            (lane,*(tuple(kinds) if kinds is not None else ())))
        try:return bool(cursor.fetchone())
        finally:cursor.close()
    try:yield ready
    finally:conn.close()


def _settle(conn,row,state,result=None,error=None):
    conn.execute('update commands set state=?,result_json=?,error=?,lease_until=null where id=?',
        (state,_dump(result) if result is not None else None,error,row['id']))
    if row['attempt_id']:conn.execute('update command_attempts set finished_at=?,outcome=? where id=?',(db.utc_now(),state,row['attempt_id']))
    _release(conn,row)
    if row['kind'].startswith('editor.'):
        from .editor_service import settled
        settled(conn,row,state,error)
    if row['kind'] in {'run.execute','operation.dispatch'}:
        from .run_commands import settled
        settled(conn,row,state,error)


def claim(owner,epoch,lane,*,kinds=None):
    if lane not in {'compute','control','editor'}:raise QueueError('invalid_lane',422)
    now=_now();stamp=now.isoformat()
    with db.write() as conn:
        worker=conn.execute('select * from command_workers where id=? and epoch=? and lease_until>?',(owner,epoch,stamp)).fetchone()
        if not worker:return None
        if datetime.fromisoformat(worker['heartbeat_at'])<=now-timedelta(seconds=limits.CLAIM_HEARTBEAT_SECONDS):
            conn.execute('update command_workers set lease_until=?,heartbeat_at=? where id=? and epoch=?',
                         ((now+timedelta(seconds=limits.CLAIM_LEASE_SECONDS)).isoformat(),stamp,owner,epoch))
        rows=conn.execute('''select c.* from commands c join command_producers p on p.id=c.producer_id
            where c.state='queued' and c.lane=? and p.enabled=1 order by c.admission_id limit 200''',(lane,)).fetchall()
        for row in rows:
            if row['version']!=1:
                _settle(conn,row,'failed',error='unsupported_command_version');continue
            if kinds is not None and row['kind'] not in kinds:continue
            if row['deadline']<=stamp:
                _settle(conn,row,'expired',error='deadline_expired');continue
            if row['kind'] not in REGISTRY:continue
            scope='p:'+row['project_id'] if row['project_id'] else 's:'+row['session_id'] if row['session_id'] else None
            if row['expected_revision'] is not None and revision(conn,scope)!=row['expected_revision']:
                _settle(conn,row,'failed',error='revision_conflict');continue
            # Real solver project/dependency reservations remain with existing
            # operation journals and registry. A waiting parent must release its
            # slot so helpers in the same project can execute.
            key='compute:project:'+row['project_id'] if row['kind'].startswith('synthetic.') and lane=='compute' and row['project_id'] else None
            if row['kind']=='workspace.execute':
                key='workspace:'+scope if scope else 'workspace:global'
            if key and conn.execute('select 1 from command_reservations where key=?',(key,)).fetchone():continue
            attempt=uuid4().hex;generation=row['generation']+1
            conn.execute("update commands set state='claimed',owner=?,worker_epoch=?,generation=?,attempt_id=?,lease_until=? where id=? and state='queued'",
                (owner,epoch,generation,attempt,min(row['deadline'],(now+timedelta(seconds=limits.CLAIM_LEASE_SECONDS)).isoformat()),row['id']))
            conn.execute('insert into command_attempts values (?,?,?,?,?,?,null,null)',(attempt,row['id'],owner,epoch,generation,stamp))
            if key:conn.execute('insert into command_reservations values (?,?,?)',(key,row['id'],attempt))
            return dict(conn.execute('select * from commands where id=?',(row['id'],)).fetchone())
    return None


def _owned(conn,token):
    stamp=db.utc_now()
    return conn.execute('''select c.* from commands c join command_producers p on p.id=c.producer_id
        join command_workers w on w.id=c.owner and w.epoch=c.worker_epoch
        where c.id=? and c.state='claimed' and c.owner=? and c.worker_epoch=? and c.generation=?
        and c.attempt_id=? and c.lease_until>? and c.deadline>? and w.lease_until>? and p.enabled=1''',
        (token['id'],token['owner'],token['worker_epoch'],token['generation'],token['attempt_id'],stamp,stamp,stamp)).fetchone()


def heartbeat(token):
    now=_now()
    with db.write() as conn:
        row=_owned(conn,token)
        if not row:return False
        end=(now+timedelta(seconds=limits.CLAIM_LEASE_SECONDS)).isoformat()
        conn.execute('update command_workers set lease_until=?,heartbeat_at=? where id=? and epoch=?',(end,now.isoformat(),token['owner'],token['worker_epoch']))
        conn.execute('update commands set lease_until=? where id=?',(min(end,row['deadline']),row['id']))
        return True


def complete(token,result=None,error=None):
    if len(_dump(result).encode())>limits.CHANGE_PAGE_BYTES:raise QueueError('result_too_large',413)
    with db.write() as conn:
        row=_owned(conn,token)
        if not row:return False
        _settle(conn,row,'failed' if error else 'succeeded',result,error)
        return True


def recover(now=None):
    stamp=(now or _now()).isoformat();counts={'retried':0,'suspended':0,'expired':0}
    with db.write() as conn:
        rows=conn.execute("select * from commands where (state='claimed' and lease_until<=?) or (state='queued' and deadline<=?) order by admission_id limit 200",(stamp,stamp)).fetchall()
        for row in rows:
            if row['deadline']<=stamp:_settle(conn,row,'expired',error='deadline_expired');counts['expired']+=1
            elif REGISTRY.get(row['kind'],Handler('compute','suspend')).recovery=='retry':
                conn.execute("update commands set state='queued',owner=null,worker_epoch=null,attempt_id=null,lease_until=null,generation=generation+1 where id=?",(row['id'],))
                conn.execute('delete from command_reservations where command_id=?',(row['id'],))
                conn.execute("update command_attempts set finished_at=?,outcome='lease_expired' where id=?",(stamp,row['attempt_id']))
                counts['retried']+=1
            else:_settle(conn,row,'suspended',error='uncertain_effect_requires_review');counts['suspended']+=1
    return counts


def revoke(producer_id):
    with db.write() as conn:
        conn.execute('update command_producers set enabled=0 where id=?',(producer_id,))
        for row in conn.execute("select * from commands where producer_id=? and state in ('queued','claimed')",(producer_id,)).fetchall():
            _settle(conn,row,'suspended',error='producer_revoked')


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Register or revoke a local synthetic command producer')
    parser.add_argument('--producer',required=True)
    parser.add_argument('--project')
    parser.add_argument('--revoke',action='store_true')
    args=parser.parse_args();db.init_db()
    if args.revoke:
        revoke(args.producer)
        print('Producer revoked; active receipts retained.')
    else:
        print(register_producer(args.producer,project_id=args.project))
