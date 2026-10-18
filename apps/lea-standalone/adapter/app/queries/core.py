"""Revision-fenced SQL observations; uncertainty never becomes verification."""
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import re
from .. import db

_cache=ContextVar('core_query_cache',default=None)


def read_query(fn):
    @wraps(fn)
    def read(*args,**kwargs):
        if _cache.get() is not None:
            return fn(*args,**kwargs)
        token=_cache.set({})
        try:
            with db.read_snapshot(): return fn(*args,**kwargs)
        finally: _cache.reset(token)
    return read


def scope_for(session_id=None,project_id=None,formalization_id=None):
    with db.connect() as conn:
        if formalization_id:
            row=conn.execute('select project_id,loose_session_id from formalizations where id=?',(formalization_id,)).fetchone()
            return ('p:'+row['project_id'] if row['project_id'] else 's:'+row['loose_session_id']) if row else None
        if project_id: return 'p:'+project_id
        row=conn.execute('select project_id from sessions where id=?',(session_id,)).fetchone()
        if row and not row[0]:
            owner=conn.execute('''select f.loose_session_id from session_formalizations sf join formalizations f on f.id=sf.formalization_id
                join core_projection_enrollment e on e.scope='s:'||f.loose_session_id and e.enabled=1
                where sf.session_id=? and f.project_id is null order by f.loose_session_id limit 1''',(session_id,)).fetchone()
            if owner:return 's:'+owner[0]
        return ('p:'+row[0] if row[0] else 's:'+session_id) if row else None


def enrolled(scope=None):
    with db.connect() as conn:
        if scope is None:
            return bool(conn.execute('select 1 from core_projection_enrollment where enabled=1 limit 1').fetchone())
        return bool(conn.execute('select 1 from core_projection_enrollment where scope=? and enabled=1',(scope,)).fetchone())


def revision(conn,scope):
    row=conn.execute('select revision from core_scope_revisions where scope=?',(scope,)).fetchone()
    return row[0] if row else 0


def observation(scope):
    cache=_cache.get()
    if cache is not None and scope in cache: return cache[scope]
    with db.connect() as conn:
        row=conn.execute('''select o.* from core_projection_enrollment e join core_observations o
            on o.id=e.observation_id and o.scope=e.scope where e.scope=? and e.enabled=1''',(scope,)).fetchone()
        reason='synchronizing'
        if row:
            reason=(None if row['state']=='observed' else row['state'])
            if row['input_revision']!=revision(conn,scope): reason='inputs_changed'
            elif datetime.fromisoformat(row['expires_at'])<=datetime.now(timezone.utc): reason='observation_expired'
        if scope:
            applying=conn.execute("""select a.state from reformalization_applications a join reformalization_operations o
                on o.id=a.operation_id join formalizations f on f.id=o.formalization_id
                where ((?='p:' and o.project_id=?) or (?='s:' and f.project_id is null and f.loose_session_id=?))
                and a.state in ('prepared','applying','restoring','conflict','conflicted') limit 1""",
                (scope[:2],scope[2:],scope[:2],scope[2:])).fetchone()
            if applying:reason='conflicted' if applying[0] in {'conflict','conflicted'} else 'reconciling' if applying[0]=='restoring' else 'applying'
            effect=conn.execute("select state from workspace_effects where ((?='p:' and project_id=?) or (?='s:' and session_id=?)) and state in ('prepared','installed','conflict') limit 1",
                (scope[:2],scope[2:],scope[:2],scope[2:])).fetchone()
            if effect:reason='conflicted' if effect[0]=='conflict' else 'applying'
        value=(dict(row) if row else None,reason)
        if cache is not None: cache[scope]=value
        return value


@read_query
def fact(scope,kind,entity_id):
    observed,reason=observation(scope)
    if reason: return None
    with db.connect() as conn:
        row=conn.execute('''select payload from core_projection_values
            where observation_id=? and scope=? and kind=? and entity_id=?''',
            (observed['id'],scope,kind,str(entity_id))).fetchone()
    return json.loads(row[0]) if row else None


def metadata(scope):
    value,reason=observation(scope)
    return {'version':1,'state':reason or 'observed','observation_id':value['id'] if value else None,
            'observed_at':value['observed_at'] if value else None,
            'expires_at':value['expires_at'] if value else None,
            'environment_revision':value['environment_revision'] if value else None}


def apply_step(step,scope=None):
    scope=scope or scope_for(session_id=step['session_id'])
    current=fact(scope,'step',step['id']) or {}
    exact=(current.get('blob_id')==step.get('blob_id') and
           current.get('check_evidence_id')==step.get('check_evidence_id') and
           current.get('sha256')==hashlib.sha256(step.get('code','').encode()).hexdigest())
    valid=bool(exact and current.get('check_current'))
    return {**step,'check_current':valid,'projection':metadata(scope),
        'check_stale_reason':(current.get('check_stale_reason') if exact else None) or
            (None if valid else metadata(scope)['state'] if metadata(scope)['state']!='observed' else 'missing_evidence'),
        **({'selected_result':current['selected_result']} if exact and current.get('selected_result') else {})}


def recovery_action(run):
    return (fact(scope_for(session_id=run['session_id']),'run',run['id']) or {}).get('available_recovery_action')


def safe_verify(run):
    if not run.get('safe_verify_status'): return None
    scope=scope_for(session_id=run['session_id'])
    value=fact(scope,'run',run['id']) or {}
    return {'run_id':run['id'],'status':run['safe_verify_status'],'detail':run.get('safe_verify_detail'),
            'current':bool((value.get('safe_verify') or {}).get('current'))}


def run_detail(row):
    # Credential-aware sanitization belongs to writers/worker import. Query code
    # never loads bootstrap configuration or provider credentials.
    if row and row.get('result_detail'):
        row={**row,'result_detail':re.sub(r'(?:sk-[A-Za-z0-9_./+\-=]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})',
                                        '[redacted]',row['result_detail'])[:2000]}
    return row


def approval_events_for_session(session_id):
    with db.connect() as conn:
        legacy=conn.execute('select approvals_json from core_legacy_records where session_id=?',(session_id,)).fetchone()
        events=conn.execute('''select r.id as run_id,e.type,e.payload from run_observation_records e
            join runs r on r.id=e.run_id where r.session_id=? and e.type in ('approval_requested','approval_resolved')
            order by e.event_id''',(session_id,)).fetchall()
    approvals={a['id']:a for a in json.loads(legacy[0])} if legacy else {}
    for event in events:
        p=json.loads(event['payload']);key=event['run_id']+':'+str(p.get('approval_id',''))
        if event['type']=='approval_requested':
            approvals[key]={**p,'id':key,'session_id':session_id,'run_id':event['run_id'],'decision':None}
        elif key in approvals:
            approvals[key].update(decision=p.get('decision'),feedback=p.get('feedback'),resolved_at=p.get('created_at'))
    return list(approvals.values())
