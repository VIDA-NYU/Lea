"""Database-only formalization, review and target projections."""
import hashlib
import json
from collections import Counter
from .. import db
from . import core,sessions


@core.read_query
def decorate(rows):
    result=[]
    for raw in rows:
        row=dict(raw);fid=row['id'];scope=core.scope_for(formalization_id=fid)
        observed=core.fact(scope,'formalization',fid)
        with db.connect() as conn:
            files=[dict(r) for r in conn.execute('select * from formalization_files where formalization_id=? order by case role when \'primary\' then 0 else 1 end,path',(fid,))]
            active=conn.execute("select id,status,pending_approval from runs where focus_formalization_id=? and status in ('pending','running') order by created_at desc,id desc limit 1",(fid,)).fetchone()
            conversations=[dict(r) for r in conn.execute('''select s.id,s.title,s.updated_at from sessions s
                join session_formalizations sf on sf.session_id=s.id where sf.formalization_id=? and s.parent_id is null order by s.updated_at desc''',(fid,))]
        activity={'status':('queued' if active['status']=='pending' else 'waiting_for_approval' if active['pending_approval'] else 'running') if active else 'idle',
                  'run_id':active['id'] if active else None}
        if observed:
            item={**observed,**row}
        else:
            item={**row,'validity_status':'unchecked' if files else 'planned' if row.get('declaration_name') else 'draft',
                'validity_reason':'Current proof observation is unavailable: '+core.metadata(scope)['state'],
                'check_current':False,'certification':None,'completion_run_id':None,'declaration_kind':None,
                'review':{'eligible':False,'approval':None,'reason':'Wait for a current worker observation.','allowed_outcomes':[]},
                'safe_verify':None,'latest_check':None,'latest_run':None,'artifact':None,'reformalization':None,
                'primary_path':next((f['path'] for f in files if f['role']=='primary'),None)}
        item.update(files=files,activity=activity,sessions=conversations,projection=core.metadata(scope))
        result.append(item)
    return result


@core.read_query
def get(formalization_id):
    with db.connect() as conn: row=conn.execute('select * from formalizations where id=?',(formalization_id,)).fetchone()
    return decorate([dict(row)])[0] if row else None


@core.read_query
def for_project(project_id): return decorate(sessions.list_raw_project_formalizations(project_id))
@core.read_query
def for_session(session_id): return decorate(sessions.list_raw_session_formalizations(session_id))


def summary(items):
    return {'formalization_count':len(items),**dict(Counter(i.get('validity_status','unknown') for i in items)),
            'active_run_count':sum(i.get('activity',{}).get('status')!='idle' for i in items)}


def _revision_token(steps):
    parts=[f"{s['path']}:{s.get('blob_sha256') or s.get('blob_id') or s['id']}" for s in sorted(steps,key=lambda s:s['path'])]
    return hashlib.sha256('\n'.join(parts).encode()).hexdigest() if parts else None


@core.read_query
def current_snapshot(formalization_id,*,conversation_session_id=None):
    form=get(formalization_id)
    if not form:return None
    if conversation_session_id:
        with db.connect() as conn:
            associated=conn.execute('select 1 from session_formalizations where session_id=? and formalization_id=?',
                                    (conversation_session_id,formalization_id)).fetchone()
        if not associated: raise ValueError('Conversation does not belong to this formalization')
    steps=sessions.current_code_steps_for_formalization(formalization_id)
    local=sessions.current_code_steps_for_formalization(formalization_id,session_id=conversation_session_id) if conversation_session_id else []
    newest=max(steps,key=lambda s:(s['created_at'],int(s['id'])),default=None)
    return {'formalization_id':formalization_id,'project_id':form.get('project_id'),'revision_token':_revision_token(steps),
        'files':steps,'last_updated_session':{'id':newest['session_id'],'title':newest.get('updating_session_title')} if newest else None,
        'last_updated_at':newest['created_at'] if newest else None,'validity_status':form['validity_status'],
        'review':form['review'],'certification':form['certification'],'safe_verify':form.get('safe_verify'),'projection':form['projection'],
        'conversation':({'session_id':conversation_session_id,'revision_token':_revision_token(local),'files':local,
            'last_updated_at':max((s['created_at'] for s in local),default=None),
            'is_current':bool(steps and _revision_token(steps)==_revision_token(local))} if conversation_session_id else None)}


@core.read_query
def review_context(fid,source_context_ref=None):
    form=get(fid)
    if not form:return None
    scope=core.scope_for(formalization_id=fid)
    source=None
    if source_context_ref:
        with db.connect() as conn:
            from .sources import resolve
            published=conn.execute('select 1 from source_publications where id=?',(source_context_ref,)).fetchone()
            if published:source=resolve(conn,source_context_ref,fid)
            row=conn.execute('select * from overleaf_source_requests where id=?',(source_context_ref,)).fetchone()
            if not published and (not row or row['formalization_id']!=fid or row['state']!='ready' or row['expires_at']<=db.utc_now()):
                raise ValueError('Current source is unavailable or expired')
            if not published:source=json.loads(row['source_bundle_json'])
            latest=conn.execute("select source_bundle_json from overleaf_source_requests where formalization_id=? and state='ready' order by captured_at desc limit 1",(fid,)).fetchone()
            if form.get('source_hash')!=source['sourceIdentityHash'] or (not published and latest and json.loads(latest[0])['bundleHash']!=source['bundleHash']):
                raise ValueError('The selected source changed')
            if row and row['mirror_files_json']:
                from . import files
                indexed=files.listing(form['project_id'])
                actual={i['path'].split('/overleaf/',1)[-1]:i for i in indexed['files'] if '/overleaf/' in i['path'] and i['state']!='deleted'}
                expected=json.loads(row['mirror_files_json'])
                hashes={path:conn.execute('select sha256 from artifact_blobs where id=?',(i['blob_id'],)).fetchone()[0]
                        for path,i in actual.items() if i['blob_id']}
                if hashes!=expected:raise ValueError('Published source mirror observation changed')
    observed=core.fact(scope,'review_context',fid)
    if observed:
        observed['formalization']={**form,'review':observed['review']}
        with db.connect() as conn:
            for artifact in observed['artifacts']:
                blob=conn.execute('select content from artifact_blobs where id=?',(artifact.get('blob_id'),)).fetchone()
                artifact['content']=blob[0] if blob else None
                artifact['content_state']='present' if blob else 'unavailable'
        if source:observed['source_snapshot']=source
        return observed
    current=current_snapshot(fid)
    return {'formalization':form,'review':form['review'],'source_snapshot':source or {'statement':form.get('statement') or ''},
            'artifacts':[{'path':s['path'],'blob_id':s.get('blob_id'),'content':s['code']} for s in current['files']]}


@core.read_query
def search(q,limit=30):
    q=(q or '').strip()
    if not q:return []
    like='%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
    with db.connect() as conn:
        rows=conn.execute(r"""select f.*,p.title as project_title,p.namespace as project_namespace from formalizations f
            left join projects p on p.id=f.project_id where f.display_title like ? escape '\' or f.declaration_name like ? escape '\'
            order by f.updated_at desc limit ?""",(like,like,limit)).fetchall()
    return decorate([dict(r) for r in rows])


@core.read_query
def target_status(project,declarations='',formalization_ids=''):
    wanted=[s.strip() for s in declarations.split(',') if s.strip()]
    ids={s.strip() for s in formalization_ids.split(',') if s.strip()}
    forms=for_project(project['id']);byname={f.get('declaration_name') or f['display_title']:f for f in forms}
    for form in forms:
        if form['id'] in ids and (name:=form.get('declaration_name') or form['display_title']) not in wanted:wanted.append(name)
    targets=[]
    with db.connect() as conn:
        artifacts={r['declaration_name']:dict(r) for r in conn.execute('select * from artifacts where scope=?',(project['id'],))}
        reports=conn.execute('select batch_id,started_at,finished_at,facts_json from formalize_batch_reports where project_id=? order by finished_at desc,batch_id desc',
                             (project['id'],)).fetchall()
    outcomes={}
    for report in reports:
        for item in json.loads(report['facts_json']).get('items',[]):
            outcomes.setdefault((item['targetKind'],item['targetLabel']),
                {k:report[k] for k in ('batch_id','started_at','finished_at')} |
                {k:item.get(k) for k in ('state','reason','detail','recoverable','runId')})
    for name in wanted:
        form=byname.get(name)
        if not form:
            targets.append({'declaration_name':name,'recorded':False,'validity_status':'unavailable',
                'validity_reason':'No formalization identity is available.','check_current':False,
                'completion_run_id':None,'declaration_kind':None,
                'last_batch_outcome':outcomes.get(('theorem',name)) or outcomes.get(('definition',name))});continue
        steps=sessions.current_code_steps_for_formalization(form['id'])
        path=form.get('primary_path') or (steps[0]['path'] if steps else None)
        step=next((s for s in steps if s['path']==path),None)
        observed=core.fact(core.scope_for(formalization_id=form['id']),'target',form['id']) or {}
        artifact=artifacts.get(name) or {}
        outcome=outcomes.get((form['kind'],(form.get('origin_key') or '').rsplit(':',1)[-1])) or outcomes.get((form['kind'],name))
        with db.connect() as conn:
            blob=conn.execute('select content from artifact_blobs where sha256=?',(observed.get('content_sha256'),)).fetchone()
            if outcome:
                run=conn.execute('select created_at from runs where id=?',(outcome.get('runId'),)).fetchone()
                since=run[0] if run else outcome['started_at']
                if conn.execute('select 1 from runs where focus_formalization_id=? and created_at>? and id!=? limit 1',
                    (form['id'],since,outcome.get('runId') or '')).fetchone():outcome=None
        source_hash=form.get('source_hash');artifact_hash=artifact.get('source_hash')
        targets.append({'declaration_name':name,'formalization_id':form['id'],'recorded':bool(artifact),
            'path':path,'session_id':step['session_id'] if step else (form['sessions'][0]['id'] if form['sessions'] else None),
            'exists':observed.get('exists'),'content':blob[0][:24000] if blob and observed.get('exists') else None,
            'content_state':'observed' if blob else 'unavailable',
            'shared_file':observed.get('shared_file'),'has_sorry':observed.get('has_sorry'),
            'module_name':artifact.get('module_name') if artifact.get('path')==path else None,'kind':artifact.get('kind'),
            'check_status':step.get('check_status') if step else None,'check_detail':step.get('check_detail') if step else None,
            'check_author':step.get('author') if step else None,'check_created_at':step.get('created_at') if step else None,
            'check_current':bool(form.get('check_current') and step and step.get('check_current')),
            'validity_status':form['validity_status'],'validity_reason':form['validity_reason'],
            'review':form['review'],'certification':form['certification'],'completion_run_id':form.get('completion_run_id'),
            'declaration_kind':observed.get('declaration_kind') or form.get('declaration_kind'),
            'declaration_present':observed.get('declaration_present'),'artifact_updated_at':step.get('created_at') if step else None,
            'reformalization':form.get('reformalization'),'current_source_hash':source_hash,
            'artifact_source_hash':artifact_hash,'stale':bool(source_hash and artifact_hash and source_hash!=artifact_hash),
            'last_batch_outcome':outcome,'projection':form['projection']})
    return {'project_id':project['id'],'slug':project['slug'],'project_namespace':project['namespace'],'namespace_revision':project['namespace_generation'],'targets':targets}
