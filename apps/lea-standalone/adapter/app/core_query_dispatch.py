"""Explicit migration adapter: enrolled scopes bypass every legacy read wrapper."""
from functools import wraps
import inspect
from fastapi import HTTPException
from . import db,observation
from .queries import core,sessions,proofs,status,alignment


def project(slug):
    with db.connect() as conn: row=conn.execute('select * from projects where slug=?',(slug,)).fetchone()
    if not row: raise HTTPException(404,'Project not found')
    return dict(row)


def database_query(kind):
    def decorate(legacy):
        signature=inspect.signature(legacy)
        @wraps(legacy)
        def read(*args,**kwargs):
            args=dict(signature.bind(*args,**kwargs).arguments)
            scope=None
            if args.get('slug'): scope=core.scope_for(project_id=project(args['slug'])['id'])
            elif args.get('formalization_id'): scope=core.scope_for(formalization_id=args['formalization_id'])
            elif args.get('project_id'): scope=core.scope_for(project_id=args['project_id'])
            elif args.get('session_id'): scope=core.scope_for(session_id=args['session_id'])
            from .peripheral_views import _capturing
            if _capturing.get():return legacy(**args)
            try:
                with db.read_snapshot():
                    return _query(kind,args)
            except LookupError as exc: raise HTTPException(404,str(exc)) from exc
            except ValueError as exc: raise HTTPException(422,str(exc)) from exc
        @wraps(legacy)
        def worker(*args,**kwargs):
            from .peripheral_views import capturing
            with capturing():return legacy(*args,**kwargs)
        read.worker=worker
        return read
    return decorate


@core.read_query
def _query(kind,a):
    sid=a.get('session_id');fid=a.get('formalization_id');pid=a.get('project_id')
    if kind=='sessions': return {'sessions':sessions.list_sessions(),'cursor':observation.watermark(_connection())}
    if kind=='stats': return sessions.usage_stats(include_proof_status=a.get('include_proof_status',True))
    if kind=='projects': return {'projects':sessions.list_projects()}
    if kind=='project':
        with db.connect() as conn:row=conn.execute('select * from projects where id=?',(pid,)).fetchone()
        if not row:raise LookupError('Project not found')
        return {**dict(row),'sessions':sessions.list_project_sessions(pid)}
    if kind=='search':
        q=a.get('q','')
        session_hits=[{**r,'result_type':'session'} for r in sessions.search_sessions(q)]
        form_hits=[{**f,'title':f['display_title'],'status':f['validity_status'],'result_type':'formalization',
                    'session_id':f['sessions'][0]['id'] if f['sessions'] else None} for f in proofs.search(q)]
        return {'results':sorted([*form_hits,*session_hits],key=lambda i:i.get('updated_at',''),reverse=True)[:30]}
    if kind=='session':
        detail=sessions.session_detail(sid)
        if not detail: raise LookupError('Session not found')
        forms=proofs.for_session(sid)
        with db.connect() as conn:
            queued=[{'id':r[0]} for r in conn.execute("select id from reformalization_operations where session_id=? and state='queued'",(sid,))]
        return {**detail,'formalizations':forms,'formalization_summary':proofs.summary(forms),'queued_reformalizations':queued,
                'lean_check_runtime':{'available':False,'reason':'worker_observation_required'},
                'latest_focus_formalization_id':next((r['focus_formalization_id'] for r in reversed(detail['runs']) if r.get('focus_formalization_id')),None),
                'projection':core.metadata(core.scope_for(session_id=sid)),'cursor':observation.watermark(_connection())}
    if kind=='current':
        value=proofs.current_snapshot(fid,conversation_session_id=sid)
    elif kind=='formalization': value=proofs.get(fid)
    elif kind=='review': value=proofs.review_context(fid,a.get('source_context_ref'))
    elif kind in {'project_forms','session_forms','slug_forms'}:
        if kind=='slug_forms': pid=project(a['slug'])['id']
        items=proofs.for_session(sid) if kind=='session_forms' else proofs.for_project(pid)
        return {'formalizations':items,'summary':proofs.summary(items),**({'project_id':pid,'slug':a['slug']} if kind=='slug_forms' else {})}
    elif kind=='target_status': return proofs.target_status(project(a['slug']),a.get('declarations',''),a.get('formalization_ids',''))
    elif kind=='status': return {'lea_status':status.current(fid,a.get('source_identity_hash'),a.get('source_bundle_hash'))}
    elif kind=='status_history': return status.history(fid,a.get('after'),a.get('limit',50))
    elif kind=='status_compare': return {'lea_status':status.current(fid,source_bundle=a['request'].source_bundle,coverage=a['request'].coverage)}
    elif kind=='assessments':
        result={}
        for item in a['request'].comparisons:
            try: result[item.formalization_id]={'lea_status':status.current(item.formalization_id,source_bundle=item.source_bundle,coverage=item.coverage),
                **alignment.current(item.formalization_id,item.alignment_source_bundle_hash)}
            except (LookupError,ValueError) as exc: result[item.formalization_id]={'error':str(exc)}
        return {'assessments':result}
    elif kind in {'dependencies','dependency','library','declaration'}:
        return _library(kind,a)
    else: raise ValueError('Unknown database query adapter')
    if value is None: raise LookupError('Formalization not found')
    return value


def _connection():
    # Called only within read_snapshot; nested connect returns that same handle.
    with db.connect() as conn:return conn


def _library(kind,a):
    p=project(a['slug']);scope='p:'+p['id']
    with db.connect() as conn:
        rows=[dict(r) for r in conn.execute('select * from project_dependencies where project_id=?',(p['id'],))]
        if a.get('requester_id'):
            allowed={r[0] for r in conn.execute('select dependency_id from project_dependency_edges where requester_id=?',(a['requester_id'],))}
            rows=[r for r in rows if r['id'] in allowed]
    if kind in {'dependencies','dependency'}:
        items=[]
        for row in rows:
            value=core.fact(scope,'dependency',row['id'])
            items.append(value or {**row,'ready':False,'state':'stale' if row['state']=='ready' else row['state'],'projection':core.metadata(scope)})
        if kind=='dependencies':return {'dependencies':items}
        value=next((i for i in items if i['id']==a['dependency_id']),None)
        if value is None:raise LookupError('Dependency not found in this project')
        return value
    items=core.fact(scope,'library',p['id']) or []
    if kind=='declaration':
        value=next((i for i in items if a['name'] in {i['full_name'],i['user_name']}),None)
        if value is None:raise LookupError('Declaration is unindexed or unavailable')
        return value
    q=a.get('q','').lower();items=[i for i in items if not q or q in str(i).lower()]
    offset=a.get('offset',0);limit=a.get('limit',20)
    if not 1<=limit<=100 or offset<0:raise ValueError('Invalid page')
    return {'declarations':items[offset:offset+limit],'total':len(items),'limit':limit,'offset':offset,'projection':core.metadata(scope)}
