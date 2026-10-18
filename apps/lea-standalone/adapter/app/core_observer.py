"""Worker-only, resumable capture of existing proof/review semantics.

Only observed facts are materialized. Live conversation/accounting rows remain
SQL projections. No compiler, provider or GET-triggered refresh runs here.
"""
import hashlib
import json
from datetime import datetime,timedelta,timezone
from uuid import uuid4
from . import db,store,formalizations,proof_evidence,read_projections,observation
from .queries.core import revision,scope_for

MAX_VALUES=10000
MAX_INPUT_BYTES=32*1024*1024
LIFETIME_SECONDS=120


def enroll(scope,enabled=True):
    if not scope or scope[:2] not in {'p:','s:'}: raise ValueError('Invalid projection scope')
    table='projects' if scope.startswith('p:') else 'sessions'
    with db.write() as conn:
        if not conn.execute(f'select 1 from {table} where id=?',(scope[2:],)).fetchone(): raise ValueError('Unknown scope')
        conn.execute('''insert into core_projection_enrollment(scope,enabled) values (?,?)
            on conflict(scope) do update set enabled=excluded.enabled''',(scope,int(enabled)))
    if enabled and scope.startswith('p:'):
        from . import workspace_observer
        workspace_observer.enroll(scope[2:])


def _work(conn,scope):
    value=scope[2:]
    condition='s.project_id=?' if scope.startswith('p:') else "(s.id=? or exists(select 1 from session_formalizations sf join formalizations f on f.id=sf.formalization_id where sf.session_id=s.id and f.project_id is null and f.loose_session_id=?))"
    params=(value,) if scope.startswith('p:') else (value,value)
    items=[('step',str(r[0])) for r in conn.execute(f"select t.id from timeline t join sessions s on s.id=t.session_id where {condition} and t.kind='code' limit {MAX_VALUES+1}",params)]
    items += [('session',r[0]) for r in conn.execute(f'select s.id from sessions s where {condition} limit {MAX_VALUES+1}',params)]
    items += [('run',r[0]) for r in conn.execute(f'select r.id from runs r join sessions s on s.id=r.session_id where {condition} limit {MAX_VALUES+1}',params)]
    forms=conn.execute('select id from formalizations where '+('project_id=?' if scope.startswith('p:') else 'project_id is null and loose_session_id=?')+f' limit {MAX_VALUES+1}',(value,)).fetchall()
    items += [('formalization',r[0]) for r in forms]
    if scope.startswith('p:'):
        items += [('dependency',r[0]) for r in conn.execute(f'select id from project_dependencies where project_id=? limit {MAX_VALUES+1}',(value,))]
        items += [('library',scope[2:])]
    if len(items)>MAX_VALUES: raise ValueError('Core backfill exceeds the 10000-entity migration limit')
    return sorted((kind+':'+identity,kind,identity) for kind,identity in items)


def _target(form,steps,snapshot):
    """Observe display facts without replacing immutable proof history."""
    path=form.get('primary_path') or (steps[0]['path'] if steps else None)
    step=next((s for s in steps if s['path']==path),None)
    result={'path':path,'exists':False,'declaration_present':False,'has_sorry':None,
            'shared_file':False,'content_sha256':None}
    if not step:return result
    with db.connect() as conn:
        owner=conn.execute('select p.namespace,s.parent_id from sessions s left join projects p on p.id=s.project_id where s.id=?',
                           (step['session_id'],)).fetchone()
        evidence=conn.execute('select evidence_json from proof_check_evidence where id=?',(step.get('check_evidence_id'),)).fetchone()
        linked=conn.execute('''select count(distinct f.id) from formalization_files ff join formalizations f on f.id=ff.formalization_id
            where f.project_id is ? and (? is not null or f.loose_session_id=?) and ff.path=?''',
            (form.get('project_id'),form.get('project_id'),form.get('loose_session_id'),path)).fetchone()[0]
        artifacts=[dict(r) for r in conn.execute('select * from artifacts where scope=? and path=?',
            (form.get('project_id') or form.get('loose_session_id'),path))]
    execution=json.loads(evidence[0]) if evidence else {'source_path':''}
    absolute=proof_evidence.canonical_path(step,execution,owner['namespace'],owner['parent_id'])
    sha,_=snapshot.observation(absolute)
    if sha is None:return result
    result.update(exists=True,content_sha256=sha)
    # Match the file capture budget; over-limit/binary content stays unavailable.
    with absolute.open('rb') as source:raw=source.read(4*1024*1024+1)
    if len(raw)>4*1024*1024:return result
    if hashlib.sha256(raw).hexdigest()!=sha:raise OSError('Target changed during observation')
    try:content=raw.decode('utf-8')
    except UnicodeError:return result
    from .target_completion import target_declaration
    from .artifacts import declaration_contains_sorry,has_unrelated_declarations
    declared=target_declaration(content,form.get('declaration_name'),form.get('kind') or 'theorem')
    result.update(declaration_present=bool(declared),declaration_kind=declared.kind if declared else None,
        has_sorry=declaration_contains_sorry(content,form.get('declaration_name')),
        shared_file=len(artifacts)>1 or linked>1 or has_unrelated_declarations(content,artifacts[0]['declaration_name'] if len(artifacts)==1 else None))
    return result


def _capture(kind,identity,snapshot):
    if kind=='step':
        with db.connect() as conn: row=conn.execute(store.TIMELINE_WITH_BLOB+' where t.id=?',(identity,)).fetchone()
        step=store._code_step_from_row(row)
        return {'step':{k:step.get(k) for k in ('blob_id','check_evidence_id','check_current','check_stale_reason','selected_result')} |
                {'sha256':hashlib.sha256(step['code'].encode()).hexdigest()}}
    if kind=='session':
        # Import once into durable SQL instead of query-time raw-log fallbacks.
        with db.connect() as conn: old=conn.execute('select * from core_legacy_records where session_id=?',(identity,)).fetchone()
        usage=json.loads(old['usage_json']) if old else store._usage_breakdown_from_raw_logs(identity)
        if not old:
            with db.connect() as conn:
                runs=[r[0] for r in conn.execute('select id from runs where session_id=? order by created_at,id',(identity,))]
            for row in usage:
                number=row.get('run_number',0)
                if 0<number<=len(runs):row['run_id']=runs[number-1]
        return {'session':{'approvals':json.loads(old['approvals_json']) if old else store.approval_events_for_session(identity),'usage':usage}}
    if kind=='run':
        from . import recovery
        run=store.get_run(identity)
        return {'run':{'available_recovery_action':recovery.available_action(run),'safe_verify':store._safe_verify_summary(run)}}
    if kind=='dependency':
        from . import project_dependencies
        return {'dependency':project_dependencies.get(identity,validate=False)}
    if kind=='library':
        from . import project_library
        return {'library':project_library.entries(store.get_project(identity))}
    form=formalizations.get(identity)
    from .artifact_snapshots import _snapshot
    captured=_snapshot(identity)
    context={'review':form['review'],'artifacts':[],
             'source_snapshot':{'statement':form.get('statement') or ''}}
    steps=store.current_code_steps_for_formalization(identity)
    for step in steps:
        context['artifacts'].append({'path':step['path'],'blob_id':step['blob_id']})
    if form.get('completion_run_id'):
        with db.connect() as conn:
            source=conn.execute('select source_bundle_json from lea_status_run_contexts where run_id=?',(form['completion_run_id'],)).fetchone()
        if source:context['source_snapshot']=json.loads(source[0])
    from . import reformalizations,reformalization_review
    attempt=reformalization_review.latest(identity)
    if attempt and attempt['state']!='settled':
        drafts=reformalizations.drafts(attempt['id'])
        if drafts:
            # Display retained candidates without calling assess(): assess can
            # compile and modify a stage. The decision handler revalidates effects.
            token,bindings=reformalization_review.binding(attempt)
            eligible=attempt['state']=='review'
            if eligible:
                try:reformalizations.fresh(attempt,snapshot=snapshot)
                except (ValueError,OSError):eligible=False
            from pathlib import Path
            from lea.check_evidence import freshness
            for draft in drafts:
                execution=(draft.get('evidence') or {}).get('execution') or {}
                eligible=eligible and freshness(execution,Path(attempt['manifest']['repo'])/draft['path'],snapshot=snapshot)[0]
            selection=(attempt.get('result') or {}).get('selection') or {}
            outcome='defined' if form.get('kind')=='definition' else 'disproved' if selection.get('kind')=='disproved' else 'proved'
            context['review']={'eligible':bool(eligible and selection),'revision_token':token,'allowed_outcomes':[outcome],
                'reason':'Confirm correspondence with the source.' if eligible else 'The draft needs reconciliation.',
                'approval':None,'run_id':attempt['root_run_id'],'artifact_scope':'attempt','reformalization_id':attempt['id'],
                'candidate_path':selection.get('path'),'candidate_declaration':selection.get('declaration'),'evidence':bindings}
            context['artifacts']=[{'path':d['path'],'blob_id':d['blob_id'],'artifact_scope':'attempt'} for d in drafts]
    return {'formalization':form,'artifact_snapshot':captured[0] if captured else None,'review_context':context,
            'target':_target(form,steps,snapshot)}


def capture(scope,limit=20):
    if not 1<=limit<=200: raise ValueError('Invalid capture batch size')
    with db.connect() as conn:
        active=conn.execute('''select o.* from core_observations o join core_projection_enrollment e
            on e.observation_id=o.id where e.scope=?''',(scope,)).fetchone()
    if active:
        prior=json.loads(active['inputs_json']);check=read_projections.ReadSnapshot()
        check.files=prior.get('files',{});check.topology=prior.get('topology',{});check.artifact_sets=prior.get('artifact_sets',{})
        if not check.unchanged():
            with db.write() as conn:
                conn.execute("update core_observations set state='reconciling' where id=?",(active['id'],))
    with db.write() as conn:
        enrollment=conn.execute('select * from core_projection_enrollment where scope=? and enabled=1',(scope,)).fetchone()
        if not enrollment: raise ValueError('Core scope is not enrolled')
        rev=revision(conn,scope)
        state=conn.execute('select * from core_backfills where scope=?',(scope,)).fetchone()
        old=conn.execute('select * from core_observations where id=?',(state['observation_id'],)).fetchone() if state else None
        if old and old['input_revision']!=rev:
            conn.execute("update core_observations set state='superseded' where id=?",(old['id'],))
            conn.execute('delete from core_backfills where scope=?',(scope,));state=None;old=None
        if not state:
            now=datetime.now(timezone.utc);oid=uuid4().hex
            conn.execute('insert into core_observations values (?,?,?,?,?,?,?,?,?)',
                (oid,scope,rev,observation.watermark(conn),now.isoformat(),now.isoformat(),'capturing','','{}'))
            conn.execute('insert into core_backfills values (?,?,?)',(scope,oid,''))
            state={'scope':scope,'observation_id':oid,'checkpoint':''}
        state=dict(state);items=_work(conn,scope)
    snapshot=read_projections.ReadSnapshot()
    previous=json.loads(old['inputs_json']) if old else {}
    snapshot.files=previous.get('files',{})
    snapshot.topology=previous.get('topology',{})
    snapshot.artifact_sets=previous.get('artifact_sets',{})
    from . import config
    snapshot.observation(config.ROOT/'config/lea.local.toml')
    if not snapshot.unchanged():
        with db.write() as conn:
            conn.execute("update core_observations set state='superseded' where id=?",(state['observation_id'],))
            conn.execute('delete from core_backfills where scope=? and observation_id=?',(scope,state['observation_id']))
        return {'state':'retry'}
    batch=[item for item in items if item[0]>state['checkpoint']][:limit]
    token=read_projections._active.set(snapshot);artifact_token=read_projections._artifacts.set({})
    try:
        values=[];used=0
        for key,kind,identity in batch:
            value=_capture(kind,identity,snapshot)
            size=len(json.dumps(value,ensure_ascii=False).encode())
            if size>MAX_INPUT_BYTES:raise ValueError('Core value exceeds capture byte budget')
            if values and used+size>MAX_INPUT_BYTES:break
            values.append((key,kind,identity,value));used+=size
        batch=batch[:len(values)]
        inputs=json.dumps({'files':snapshot.files,'topology':snapshot.topology,'artifact_sets':snapshot.artifact_sets},sort_keys=True)
        if len(inputs.encode())>MAX_INPUT_BYTES: raise ValueError('Core observation exceeds input byte budget')
        if not snapshot.unchanged():
            with db.write() as conn:
                conn.execute("update core_observations set state='superseded' where id=?",(state['observation_id'],))
                conn.execute('delete from core_backfills where scope=? and observation_id=?',(scope,state['observation_id']))
            return {'state':'retry'}
    finally:
        read_projections._active.reset(token);read_projections._artifacts.reset(artifact_token)
    with db.write() as conn:
        current=conn.execute('select * from core_backfills where scope=?',(scope,)).fetchone()
        if (not current or current['observation_id']!=state['observation_id'] or current['checkpoint']!=state['checkpoint']
            or revision(conn,scope)!=rev): return {'state':'retry'}
        for _,kind,identity,payloads in values:
            for name,payload in payloads.items():
                if name=='session':continue  # Imported audit data has one durable owner below.
                encoded=json.dumps(payload,ensure_ascii=False)
                if len(encoded.encode())>MAX_INPUT_BYTES: raise ValueError('Core projection value exceeds byte budget')
                conn.execute('insert or replace into core_projection_values values (?,?,?,?,?)',
                    (state['observation_id'],scope,name,identity,encoded))
            if kind=='session':
                payload=payloads['session']
                conn.execute('insert or ignore into core_legacy_records values (?,?,?,?)',
                             (identity,scope,json.dumps(payload['approvals']),json.dumps(payload['usage'])))
        checkpoint=batch[-1][0] if batch else state['checkpoint']
        done=not items or checkpoint==items[-1][0]
        now=datetime.now(timezone.utc)
        conn.execute('update core_observations set inputs_json=?,environment_revision=?,observed_at=?,expires_at=?,state=? where id=?',
            (inputs,hashlib.sha256(inputs.encode()).hexdigest(),now.isoformat(),
             (now+timedelta(seconds=LIFETIME_SECONDS)).isoformat(),'observed' if done else 'capturing',state['observation_id']))
        if done:
            conn.execute('update core_projection_enrollment set observation_id=? where scope=?',(state['observation_id'],scope))
            conn.execute('delete from core_backfills where scope=?',(scope,))
        else: conn.execute('update core_backfills set checkpoint=? where scope=?',(checkpoint,scope))
    return {'state':'observed' if done else 'capturing','observation_id':state['observation_id'],'processed':len(batch)}


def maintain():
    with db.write() as conn:
        now=datetime.now(timezone.utc)
        cutoff=(now-timedelta(seconds=30)).isoformat()
        expiring=(now+timedelta(seconds=30)).isoformat()
        scope=conn.execute('''select e.scope from core_projection_enrollment e left join core_observations o on o.id=e.observation_id
            left join core_scope_revisions r on r.scope=e.scope
            where e.enabled=1 and (o.id is null or o.observed_at<? or exists(select 1 from core_backfills b where b.scope=e.scope)
                or o.input_revision!=coalesce(r.revision,0))
            order by case when o.id is null or o.expires_at<=? or o.input_revision!=coalesce(r.revision,0)
                or exists(select 1 from core_backfills b where b.scope=e.scope) then 0 else 1 end,
                last_scan_at,e.scope limit 1''',(cutoff,expiring)).fetchone()
        if not scope:
            return {'state':'waiting'} if conn.execute('select 1 from core_projection_enrollment where enabled=1 limit 1').fetchone() else {}
        conn.execute('update core_projection_enrollment set last_scan_at=? where scope=?',(db.utc_now(),scope[0]))
    return capture(scope[0])
