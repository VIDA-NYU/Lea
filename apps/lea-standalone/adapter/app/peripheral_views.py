"""SQL-only UI views. Registered legacy readers execute only in worker capture."""
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from functools import wraps
import hashlib
import inspect
import json
from pathlib import Path
from uuid import uuid4
from urllib.parse import quote
from email.message import Message
from fastapi import HTTPException
from fastapi.responses import Response,FileResponse
from pydantic import BaseModel
from . import db

_capturing=ContextVar('peripheral_capture',default=False)
_queries={}
MAX_RESULT=64*1024*1024


def disposition(filename):
    value=str(filename).replace('\r','').replace('\n','').replace('"','').replace('\\','_')
    return "attachment; filename=\""+value.encode('ascii','replace').decode()+"\"; filename*=UTF-8''"+quote(value,safe='')


def dump(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),default=lambda v:v.model_dump(mode='json') if isinstance(v,BaseModel) else str(v))


def key(kind,args):return kind+':'+hashlib.sha256(dump(args).encode()).hexdigest()


@contextmanager
def capturing():
    token=_capturing.set(True)
    try:yield
    finally:_capturing.reset(token)


def query(kind,*,automatic=True,lifetime=120):
    def decorate(fn):
        import os
        if os.environ.get('LEA_PROCESS_ROLE')=='api':raise RuntimeError('The API cannot register an observer')
        signature=inspect.signature(fn,eval_str=True)
        _queries[kind]=(fn,signature,automatic,lifetime)
        @wraps(fn)
        def read(*args,**kwargs):
            bound=signature.bind(*args,**kwargs);bound.apply_defaults()
            from .workspace_commands import current
            if _capturing.get() or current() is not None:return fn(*args,**kwargs)
            return get(kind,dict(bound.arguments))
        read.__signature__=signature
        @wraps(fn)
        def worker(*args,**kwargs):
            from .peripheral_views import capturing
            with capturing():return fn(*args,**kwargs)
        read.worker=worker
        return read
    return decorate



def view(kind,**_):
    """SQL-only public counterpart to worker query registration."""
    def decorate(contract):
        signature=inspect.signature(contract,eval_str=True)
        @wraps(contract)
        def read(*args,**kwargs):
            bound=signature.bind(*args,**kwargs);bound.apply_defaults()
            return get(kind,dict(bound.arguments))
        read.__signature__=signature
        return read
    return decorate


def input_revision(conn,project_id=None,session_id=None):
    ignored=('peripheral_views','command_workers','worker_process','commands','command_attempts','companion_documents','companion_jobs','companion_associations','source_publishers','core_projection_values','core_observations')
    clause='entity_type not in ('+','.join('?' for _ in ignored)+')'
    values=list(ignored)
    if project_id:clause+=' and project_id=?';values.append(project_id)
    elif session_id:clause+=' and session_id=?';values.append(session_id)
    retained=conn.execute('select coalesce(max(id),0) from domain_changes where '+clause,values).fetchone()[0]
    # Once an old input delta leaves replay, conservatively invalidate the view.
    # A compaction floor can never make an old observation appear current again.
    return max(retained,conn.execute('select floor from change_retention where id=1').fetchone()[0])


def get(kind,args):
    with db.read_snapshot() as conn:
        row=conn.execute('select * from peripheral_views where key=?',(key(kind,args),)).fetchone()
        if not row:raise HTTPException(503,detail={'error':'projection_unavailable','kind':kind,'message':'The worker has not published this view yet.'},headers={'Retry-After':'2'})
        value=json.loads(row['payload']) if row['payload'] is not None else None
        reason='observation_expired' if row['expires_at']<=db.utc_now() else 'inputs_changed' if input_revision(conn,row['project_id'],row['session_id'])>row['input_revision'] else 'observed'
        metadata={'state':reason,
                  'observed_at':row['observed_at'],'expires_at':row['expires_at'],'version':1}
        if row['status']>=400:raise HTTPException(row['status'],detail=value.get('detail',value) if isinstance(value,dict) else value,headers=json.loads(row['headers_json']))
        if isinstance(value,dict) and '$download' in value:return download(value['$download'])
        if isinstance(value,dict):
            if kind in {'health','readiness','lean_check_runtime'} and row['expires_at']<=db.utc_now():value={**value,'ready':False,'state':'unavailable','lifecycle_state':'worker_unavailable'}
            return {**value,'projection':metadata}
        return value


def download(identity):
    with db.connect() as conn:row=conn.execute('select * from download_artifacts where id=?',(identity,)).fetchone()
    if not row:raise HTTPException(404,'Download not found')
    if row['expires_at']<=db.utc_now():raise HTTPException(410,'Download expired; request a new export')
    return Response(bytes(row['content']),media_type=row['media_type'],headers={'Content-Disposition':disposition(row['filename'])})


def capture(kind,args):
    fn,signature,_,lifetime=_queries[kind]
    bound=signature.bind(**args);bound.apply_defaults();args=dict(bound.arguments)
    status=200;headers={}
    with db.read_snapshot() as conn:
        project_id=args.get('project_id')
        if args.get('slug'):
            row=conn.execute('select id from projects where slug=?',(args['slug'],)).fetchone();project_id=row[0] if row else None
        revision=input_revision(conn,project_id,args.get('session_id'))
    try:
        with capturing():value=fn(**args)
        if isinstance(value,Response):
            status=value.status_code;headers=dict(value.headers)
            if isinstance(value,FileResponse):
                with Path(value.path).open('rb') as source:content=source.read(MAX_RESULT+1)
            else:content=value.body
            if len(content)>MAX_RESULT:raise HTTPException(413,'Download exceeds its 64 MiB bound')
            identity=uuid4().hex
            message=Message();message['Content-Disposition']=headers.get('content-disposition','')
            filename=message.get_filename() or 'download.bin'
            with db.write() as conn:
                from .workspace_commands import authorize
                authorize(conn)
                now=datetime.now(timezone.utc)
                conn.execute('insert into download_artifacts values(?,?,?,?,?,?,?,?)',
                    (identity,args.get('project_id'),args.get('session_id'),content,headers.get('content-type','application/octet-stream'),filename,
                     now.isoformat(),(now+timedelta(hours=24)).isoformat()))
            value={'$download':identity,'download_url':'/api/downloads/'+identity}
    except HTTPException as exc:value={'detail':exc.detail};status=exc.status_code;headers=dict(exc.headers or {})
    encoded=dump(value)
    if len(encoded.encode())>MAX_RESULT:raise ValueError('Worker view exceeds its 64 MiB bound')
    now=datetime.now(timezone.utc)
    with db.write() as conn:
        from .workspace_commands import authorize
        authorize(conn)
        conn.execute('''insert into peripheral_views values(?,?,?,?,?,?,?,?,?,?,?) on conflict(key) do update set
            payload=excluded.payload,status=excluded.status,headers_json=excluded.headers_json,observed_at=excluded.observed_at,
            expires_at=excluded.expires_at,input_revision=excluded.input_revision''',
            (key(kind,args),kind,dump(args),project_id,args.get('session_id'),encoded,status,dump(headers),
             now.isoformat(),(now+timedelta(seconds=lifetime)).isoformat(),revision))
    if status>=400:raise HTTPException(status,detail=value['detail'],headers=headers)
    return value


def maintain(limit=3):
    """Bounded autonomous refresh; requests never enqueue view maintenance."""
    with db.connect() as conn:
        projects=[dict(r) for r in conn.execute("select p.id,p.slug from projects p left join peripheral_views v on v.project_id=p.id group by p.id order by coalesce(max(v.observed_at),'') asc,p.id limit 100")]
        sessions=[r[0] for r in conn.execute("select s.id from sessions s left join peripheral_views v on v.session_id=s.id group by s.id order by coalesce(max(v.observed_at),'') asc,s.id limit 100")]
        attachments=[dict(r) for r in conn.execute('select id,project_id from project_files where kind!=? order by created_at desc limit 100',('overleaf',))]
    with db.write() as conn:
        conn.execute('delete from download_artifacts where id in (select id from download_artifacts where expires_at<? limit 20)',(db.utc_now(),))
    candidates=[]
    for kind,(_,signature,automatic,_) in list(_queries.items()):
        if not automatic:continue
        required={n for n,p in signature.parameters.items() if p.default is inspect.Parameter.empty}
        if not required:variants=[{}]
        elif required=={'project_id'}:variants=[{'project_id':p['id']} for p in projects]
        elif required=={'slug'}:variants=[{'slug':p['slug']} for p in projects]
        elif required=={'session_id'}:variants=[{'session_id':s} for s in sessions]
        elif required=={'project_id','file_id'}:variants=[{'project_id':r['project_id'],'file_id':r['id']} for r in attachments]
        else:continue
        for args in variants:
            bound=signature.bind(**args);bound.apply_defaults();args=dict(bound.arguments)
            with db.connect() as conn:old=conn.execute('select observed_at from peripheral_views where key=?',(key(kind,args),)).fetchone()
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(old[0])).total_seconds() if old else 1e12
            interval=2 if kind in {'health','readiness','lean_check_runtime'} else 20
            priority=3 if kind in {'health','readiness'} else 2 if not required else 1
            if age>=interval:candidates.append((priority,age,kind,args))
    completed=0
    for _,_,kind,args in sorted(candidates,key=lambda i:(i[0],i[1]),reverse=True)[:limit]:
        try:capture(kind,args)
        except (HTTPException,ValueError,OSError):pass
        completed+=1
    return completed
