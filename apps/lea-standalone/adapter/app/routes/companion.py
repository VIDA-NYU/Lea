"""Optional companion transport endpoints. Every response is database-backed."""
import json
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,Field,ConfigDict,StrictBool
from .. import db,commands,companion_service

router=APIRouter()


class Dispatch(BaseModel):
    path:str=Field(max_length=200)
    method:str='POST'
    idempotency_key:str=Field(min_length=1,max_length=180)
    payload:dict


@router.post('/api/companion/commands',status_code=202)
def dispatch(request:Dispatch):
    if request.method not in {'POST','PUT'}:raise HTTPException(422,'Unsupported operation method')
    try:return companion_service.admit(request.path,request.payload,request.idempotency_key,request.method)
    except commands.QueueError as exc:raise HTTPException(exc.status,detail={'error':exc.code}) from exc


@router.get('/api/companion/receipts')
def lookup(idempotency_key:str):return companion_service.lookup(idempotency_key)


@router.get('/api/companion/commands/{identity}')
def receipt(identity:str):return companion_service.receipt(identity)


@router.get('/api/companion/jobs/{identity}')
def job(identity:str):
    with db.read_snapshot() as conn:
        row=conn.execute('select * from companion_jobs where id=?',(identity,)).fetchone()
        if not row:raise HTTPException(404,'Unknown job')
        value=json.loads(row['payload'])
        run=conn.execute('select * from runs where id=?',(row['run_id'],)).fetchone() if row['run_id'] else None
        if row['run_id'] and not run:value={**value,'legacy_status':value.get('status'),'status':'unknown','connectionState':'history_without_run'}
        # This is an operational history projection. Current proof validity is
        # supplied separately by the proof projections, never by this label.
        if run and run['status'] in {'proved','defined','disproved','needs_review','failed'}:
            value={**value,'resultKind':run['result_kind'],'domain_run_status':run['status']}
        if run and not value.get('recordedProofPath'):
            step=conn.execute("select path from timeline where run_id=? and kind='code' and formalization_id is ? order by id desc limit 1",(run['id'],row['formalization_id'])).fetchone()
            project=conn.execute('select repo_path,namespace from projects where id=?',(run['project_id'],)).fetchone() if run['project_id'] else None
            if step:
                value={**value,'recordedProofPath':'workspace/'+((project['repo_path'] or 'proofs/'+project['namespace'].replace('.','/')) if project else 'proofs/'+run['session_id'])+'/'+step[0]}
        if run and run['status'] not in {'pending','running'} and value.get('status')=='in_progress':
            value={**value,'settling':True,'connectionState':'reconciling','runStatus':run['status']}
        log=conn.execute('select payload from companion_documents where name=?',('log:'+str(value.get('logPath','')).split('/')[-1],)).fetchone()
        return {**value,'logTail':json.loads(log[0])[-4000:] if log else ''}


@router.get('/api/companion/jobs')
def jobs():
    with db.read_snapshot() as conn:
        return {'jobs':[{'id':r['id'],'document_id':r['document_id'],'session_id':r['session_id'],'run_id':r['run_id'],
                         'formalization_id':r['formalization_id']} for r in conn.execute('select * from companion_jobs order by updated_at desc limit 100')]}


@router.get('/api/companion/settings')
def settings():
    from .settings import get_settings
    current=get_settings()
    with db.connect() as conn:row=conn.execute("select payload from companion_documents where name='settings.json'").fetchone()
    preferences=json.loads(row[0]) if row else {}
    from .settings import model_requirements
    return {**{k:v for k,v in preferences.items() if k in {'leaUiBaseUrl','leaJobTimeoutSeconds','leaTexMirrorEnabled','leaPauseOnSourceIssue'}},
        'ok':True,'leaModel':current['model'],'leaMaxTurns':current['max_turns'],'leaMaxSpendUsd':current['max_spend_usd'],
        'leaCurrentSpendUsd':current['current_spend_usd'],'leaApiKeys':current['api_keys'],'leaModelOptions':current['model_options'],
        'leaApiKeyConfigured':any(v['configured'] for v in current['api_keys'].values()),
        'githubTokenConfigured':current['github_token']['configured'],
        'leaProviderKeys':{family:current['api_keys'].get(key,{'configured':False}) for family,key in {'openai':'OPENAI_API_KEY','google':'GOOGLE_API_KEY','anthropic':'ANTHROPIC_API_KEY','portkey':'PORTKEY_API_KEY'}.items()},
        'leaModelRequirements':model_requirements(current['model']),
        'leaStatusCapability':{'version':1,'admission_enabled':True,'source_pause_policy':1},'transport_only':True}


class Preferences(BaseModel):
    model_config=ConfigDict(extra='forbid')
    leaJobTimeoutSeconds:int|None=Field(default=None,ge=10,le=86400)
    leaTexMirrorEnabled:StrictBool|None=None
    leaPauseOnSourceIssue:StrictBool|None=None


@router.put('/api/companion/preferences')
def preferences(request:Preferences):
    with db.write() as conn:
        row=conn.execute("select payload from companion_documents where name='settings.json'").fetchone()
        value=json.loads(row[0]) if row else {}
        value.update(request.model_dump(exclude_unset=True,exclude_none=True))
        conn.execute("insert into companion_documents values('settings.json',?,1,?) on conflict(name) do update set payload=excluded.payload,revision=revision+1,updated_at=excluded.updated_at",(json.dumps(value),db.utc_now()))
    return {'saved':True}


@router.get('/api/companion/imports')
def imports():
    with db.read_snapshot() as conn:
        return {'imports':[dict(r) for r in conn.execute('select id,path,digest,version,record_count,created_at,archive_path from companion_imports order by created_at')],
            'issues':[dict(r) for r in conn.execute('select id,import_id,record_id,reason,created_at from companion_issues order by created_at limit 1000')],
            'counts':{table:conn.execute('select count(*) from '+table).fetchone()[0] for table in ('companion_jobs','companion_associations','companion_documents')}}


@router.get('/api/downloads/{identity}')
def download(identity:str):
    from ..peripheral_views import download
    return download(identity)
