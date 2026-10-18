"""Database compatibility API and adapter-owned business-worker lifecycle."""
import json
import re
import secrets
from fastapi import HTTPException
from . import db,commands,run_commands

QUERY_PATHS={'/statuses','/lean-pane/manifest','/targets/resolve','/source-context/manifest',
    '/lean-pane/chat/session','/lean-pane/review/context','/lean-pane/repair/status','/project/identity/preview'}
ACTION_PATHS={'/formalize','/formalize/all','/stub','/stub/all','/lean-pane/chat/message','/lean-pane/chat/interrupt',
    '/lean-pane/edit/start','/lean-pane/edit/save','/lean-pane/repair/start','/lean-pane/repair/all',
    '/lean-pane/repair/all/continue','/lean-pane/repair/all/cancel','/lean-pane/review/decision',
    '/lean-pane/dependencies/retry','/lean-pane/dependencies/wait','/lean-check/retry','/project/identity',
    '/project/identity/default-name','/project/blueprint/generate','/share/github/remote','/share/github/push',
    '/project/github-import/preview','/project/github-import/confirm','/formalize/all/reports/save'}
SOURCE_PATHS={'/formalize','/formalize/all','/stub','/stub/all','/lean-pane/chat/message',
    '/lean-pane/repair/start','/lean-pane/repair/all','/lean-pane/repair/all/continue',
    '/lean-pane/review/decision','/lean-pane/dependencies/retry'}



def admit(path,payload,key,method='POST'):
    if path not in QUERY_PATHS|ACTION_PATHS:raise HTTPException(404,'Unknown companion operation')
    encoded=json.dumps(payload,ensure_ascii=False)
    if len(encoded.encode())>32*1024*1024:raise HTTPException(413,'Companion input exceeds 32 MiB')
    from .companion_import import redact
    if redact(payload)!=payload:raise HTTPException(422,'Credentials must use the dedicated infrastructure settings intake')
    with db.write() as conn:
        previous=conn.execute('select * from commands where producer_id=? and idempotency_key=?',(run_commands.PRODUCER,'companion:'+key)).fetchone()
        if previous:
            original=conn.execute('select method,path from companion_requests where command_id=?',(previous['id'],)).fetchone()
            accepted=json.loads(previous['payload_json'])
            if not original or (original['method'],original['path'])!=(method,path) or accepted.get('input_hash')!=commands._hash(encoded):
                raise commands.QueueError('idempotency_conflict')
            # A first run can resolve and attach its project after acceptance.
            # That routing update must not change the original request identity.
            return receipt(previous['id'])
        stored=dict(payload)
        if not previous and path=='/lean-pane/chat/interrupt':
            if not stored.get('runId') and stored.get('sessionId'):
                active=conn.execute("select id from runs where session_id=? and status in ('pending','running') order by created_at desc limit 1",(stored['sessionId'],)).fetchone()
                stored['runId']=active[0] if active else None
            if not stored.get('runId'):raise HTTPException(409,'No active run was selected at admission')
            run_commands.stop(stored['runId'])
        target=payload.get('target')
        document=payload.get('overleafProjectId') or (target.get('overleafProjectId') if isinstance(target,dict) else None)
        if not document and payload.get('batchId'):
            record=conn.execute("select payload from companion_documents where name='jobs.json.batches.json'").fetchone()
            document=(json.loads(record[0]).get(payload['batchId']) or {}).get('overleafProjectId') if record else None
        slug=re.sub('[^A-Za-z0-9_-]','_',str(document or ''))[:80]
        if slug and not slug[0].isalnum():slug=('project_'+slug)[:80]
        row=conn.execute('select id from projects where slug=?',(slug,)).fetchone()
        kind='companion.control' if path in {'/lean-pane/chat/interrupt','/lean-pane/repair/all/cancel','/lean-pane/dependencies/wait'} else 'companion.query' if path in QUERY_PATHS else 'companion.dispatch'
        project_id=row[0] if row else None
        if path=='/lean-pane/chat/interrupt' and stored.get('runId'):
            selected=conn.execute('select project_id from runs where id=?',(stored['runId'],)).fetchone()
            project_id=selected[0] if selected else None
        references=set()
        def collect(value):
            if isinstance(value,dict):
                for name,item in value.items():
                    if name in {'source_context_ref','sourceContextRef'} and isinstance(item,str) and item:references.add(item)
                    elif isinstance(item,(dict,list)):collect(item)
            elif isinstance(value,list):
                for item in value:collect(item)
        if not previous and kind=='companion.dispatch' and path in SOURCE_PATHS:
            collect(payload)
            if not references and document:
                references={r[0] for r in conn.execute('select publication_id from source_heads where document_id=? limit 1001',(document,))}
            if len(references)>1000:raise HTTPException(413,'Business operation exceeds 1000 frozen source bindings')
            from .source_publications import resolve
            for reference in references:
                try:resolve(conn,reference)
                except ValueError as exc:raise HTTPException(409,str(exc)) from exc
        admitted=run_commands._enqueue(conn,kind,{'path':path,'input_hash':commands._hash(encoded)},key='companion:'+key,
            project_id=project_id)
        conn.execute('insert or ignore into companion_requests values(?,?,?,?,null)',(admitted['id'],method,path,json.dumps(stored,ensure_ascii=False)))
        for reference in references:conn.execute('insert or ignore into companion_source_bindings values(?,?)',(admitted['id'],reference))
        if not previous and path=='/lean-pane/repair/all/cancel' and payload.get('batchId'):
            record=conn.execute("select payload from companion_documents where name='jobs.json.batches.json'").fetchone()
            batches=json.loads(record[0]) if record else {}
            if payload['batchId'] in batches:
                batches[payload['batchId']]['cancelRequested']=True
                conn.execute("update companion_documents set payload=?,revision=revision+1,updated_at=? where name='jobs.json.batches.json'",(json.dumps(batches),db.utc_now()))
    return {'workspace_command':True,'command':admitted}


def receipt(identity):
    with db.connect() as conn:
        row=conn.execute('select c.*,r.result_json as response from commands c join companion_requests r on r.command_id=c.id where c.id=?',(identity,)).fetchone()
        if not row:raise HTTPException(404,'Companion receipt not found')
        value=commands.receipt(row)
        if row['response']:value['result']=json.loads(row['response'])
        return {'workspace_command':True,'command':value}


def lookup(key):
    with db.connect() as conn:row=conn.execute('select id from commands where producer_id=? and idempotency_key=?',(run_commands.PRODUCER,'companion:'+key)).fetchone()
    if not row:raise HTTPException(404,'Companion receipt not found')
    return receipt(row[0])


def authenticate(epoch,proof):
    if not epoch or not proof:return False
    with db.connect() as conn:
        row=conn.execute("select p.bridge_hash,p.expires_at,w.epoch,w.lease_until from worker_process p join command_workers w on w.id='lea-command-host' where p.id=1 and p.lifecycle_state='ready'").fetchone()
    return bool(row and row['epoch']==epoch and row['expires_at']>db.utc_now() and row['lease_until']>db.utc_now()
        and row['bridge_hash'] and secrets.compare_digest(commands._hash(proof),row['bridge_hash']))


def retain(limit=25):
    from datetime import datetime,timedelta,timezone
    cutoff=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
    with db.write() as conn:
        rows=conn.execute("""select r.command_id from companion_requests r join commands c on c.id=r.command_id
            where c.state in ('succeeded','failed','cancelled','expired') and c.created_at<? and r.payload!='{}'
            and not exists(select 1 from companion_jobs j where json_extract(j.payload,'$.acceptedCommandId')=c.id
                and json_extract(j.payload,'$.status') in ('in_progress','paused')) limit ?""",(cutoff,limit)).fetchall()
        for row in rows:
            conn.execute('update companion_requests set payload=\'{}\',result_json=? where command_id=?',
                (json.dumps({'statusCode':410,'body':{'error':'input_retention_expired','message':'This operational input has expired; canonical run history remains.'}}),row[0]))
            conn.execute('delete from companion_source_bindings where command_id=?',(row[0],))
    return len(rows)
