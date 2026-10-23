"""Bounded, restartable legacy import. Original files are never modified/deleted."""
import hashlib
import json
from pathlib import Path
import re
import os
from uuid import uuid4
from . import db

VERSION=1
MAX_FILE=32*1024*1024
FILES={'jobs.json':'jobs.json','chatSessions.json':'chatSessions.json',
       'generated-targets.json':'generated-targets.json','jobs.json.batches.json':'jobs.json.batches.json','settings.json':'settings.json'}


def redact(value):
    if isinstance(value,list):return [redact(v) for v in value]
    if isinstance(value,dict):return {k:redact(v) for k,v in value.items() if not re.fullmatch(r'(?:.*(?:api_?key|auth_?token|access_?token|refresh_?token|password|secret|credential)s?|authorization|github_?token)',k,re.I)}
    if isinstance(value,str):return re.sub(r'\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,})\b','[redacted]',value)
    return value


def _nonfinite(value):
    raise ValueError('Non-finite legacy number: '+value)


def import_directory(root:Path):
    counts={};total=0;processed=False
    for name,document in FILES.items():
        path=root/name
        if not path.is_file() or path.is_symlink():continue
        processed=True
        with path.open('rb') as source:raw=source.read(MAX_FILE+1)
        total+=len(raw)
        if len(raw)>MAX_FILE or total>64*1024*1024:raise ValueError('Legacy companion import exceeds 32 MiB/file or 64 MiB aggregate')
        digest=hashlib.sha256(raw).hexdigest()
        archive=_archive(name,raw,digest)
        with db.write() as conn:
            if conn.execute('select 1 from companion_imports where path=? and digest=? and version=?',(str(path),digest,VERSION)).fetchone():continue
            identity=uuid4().hex;now=db.utc_now()
            try:
                value=json.loads(raw,parse_constant=_nonfinite)
                if not isinstance(value,dict) or len(value)>10000:raise ValueError('Expected at most 10000 records in a legacy object')
            except (ValueError,UnicodeError) as exc:
                conn.execute('insert into companion_imports values(?,?,?,?,?,?,?,?)',(identity,str(path),digest,VERSION,0,None,now,str(archive)))
                conn.execute('insert into companion_issues values(?,?,?,?,?,?)',(uuid4().hex,identity,name,'invalid_legacy_json',json.dumps({'error':str(exc),'path':str(path),'digest':digest}),now))
                counts[name]={'imported':0,'issues':1};continue
            value=redact(value)
            if name=='settings.json':value={k:v for k,v in value.items() if k in {'leaModel','leaMaxTurns','leaMaxSpendUsd','leaJobTimeoutSeconds','leaTexMirrorEnabled','leaPauseOnSourceIssue','leaUiBaseUrl'}}
            encoded=json.dumps(value,ensure_ascii=False)
            conn.execute('insert into companion_imports values(?,?,?,?,?,?,?,?)',(identity,str(path),digest,VERSION,len(value),encoded,now,str(archive)))
            # Freeze checkpoint: after initial adoption, a changed legacy file is
            # evidence requiring reconciliation, never permission to overwrite SQL.
            old=conn.execute('select payload from companion_documents where name=?',(document,)).fetchone()
            if old:
                if json.loads(old[0])!=value:
                    conn.execute('insert into companion_issues values(?,?,?,?,?,?)',(uuid4().hex,identity,name,'legacy_delta_after_freeze',encoded,now))
                counts[name]={'imported':0,'retained':len(value)};continue
            operational=json.loads(encoded)
            if name=='jobs.json.batches.json':
                for batch in operational.values():
                    if isinstance(batch,dict) and not batch.get('done'):
                        batch['migrationBlocked']=True
                        batch['pausedOn']={'reason':'migration_review','detail':'Review the imported batch and explicitly continue it before starting any further work.'}
            conn.execute('insert into companion_documents values(?,?,1,?)',(document,json.dumps(operational),now))
            issues=0
            for record_id,record in value.items() if name in {'jobs.json','chatSessions.json'} else []:
                if not isinstance(record,dict):
                    issues+=1
                    conn.execute('insert into companion_issues values(?,?,?,?,?,?)',(uuid4().hex,identity,record_id,'invalid_legacy_record',json.dumps(record),now))
                    continue
                sid=record.get('leaSessionId') or record.get('recorderSessionId') or record.get('sessionId')
                rid=record.get('apiRunId');fid=record.get('formalizationId')
                identifiers=[sid,rid,fid,*(record.get(k) for k in ('jobKey','overleafProjectId','projectSlug','recordedProofPath','logPath'))]
                if any(value is not None and not isinstance(value,str) for value in identifiers):
                    issues+=1
                    conn.execute('insert into companion_issues values(?,?,?,?,?,?)',(uuid4().hex,identity,record_id,'invalid_legacy_reference',json.dumps(record),now))
                    continue
                run=conn.execute('select * from runs where id=?',(rid,)).fetchone() if rid else None
                session=conn.execute('select * from sessions where id=?',(sid,)).fetchone() if sid else None
                form=conn.execute('select * from formalizations where id=?',(fid,)).fetchone() if fid else None
                reason=None
                if rid and not run or sid and not session or fid and not form:reason='missing_domain_reference'
                elif run and ((sid and run['session_id']!=sid) or (fid and run['focus_formalization_id']!=fid)):reason='conflicting_domain_reference'
                sid=sid or (run['session_id'] if run else None);fid=fid or (run['focus_formalization_id'] if run else None)
                session=session or (conn.execute('select * from sessions where id=?',(sid,)).fetchone() if sid else None)
                form=form or (conn.execute('select * from formalizations where id=?',(fid,)).fetchone() if fid else None)
                pid=run['project_id'] if run else session['project_id'] if session else form['project_id'] if form else None
                if not reason and session and form and session['project_id']!=form['project_id']:reason='conflicting_domain_reference'
                if not reason and session and form and not pid and form['loose_session_id']!=sid and not conn.execute(
                    'select 1 from session_formalizations where session_id=? and formalization_id=?',(sid,fid)).fetchone():reason='conflicting_domain_reference'
                job_key=record.get('jobKey') or (record_id if name=='chatSessions.json' else '')
                document=record.get('overleafProjectId') or job_key.split(':',1)[0]
                if not reason and pid and document:
                    project=conn.execute('select slug from projects where id=?',(pid,)).fetchone()
                    slug=re.sub('[^A-Za-z0-9_-]','_',str(document))[:80]
                    if slug and not slug[0].isalnum():slug=('project_'+slug)[:80]
                    if not project or project[0]!=slug:reason='conflicting_document_reference'
                if not reason and form and record.get('targetLabel'):
                    binding=conn.execute('select target_kind,target_key from overleaf_bindings where formalization_id=?',(fid,)).fetchone()
                    if binding and (binding['target_key']!=record['targetLabel'] or (record.get('targetKind') and binding['target_kind']!=record['targetKind'])):reason='conflicting_target_reference'
                if reason:
                    issues+=1
                    conn.execute('insert into companion_issues values(?,?,?,?,?,?)',(uuid4().hex,identity,record_id,reason,json.dumps(record),now))
                    # Preserve raw history above; do not grant an invalid identity
                    # operational authority through a normalized association.
                    continue
                sid=sid or (run['session_id'] if run else None);fid=fid or (run['focus_formalization_id'] if run else None)
                pid=run['project_id'] if run else session['project_id'] if session else form['project_id'] if form else None
                if name=='jobs.json':
                    conn.execute('insert or ignore into companion_jobs values(?,?,?,?,?,?,?,?,?)',
                        (record_id,record.get('overleafProjectId'),record.get('jobKey'),pid,sid,rid,fid,json.dumps(record),now))
                else:
                    conn.execute('insert or ignore into companion_associations values(?,?,?,?,?,?,?)',
                        (record_id,record_id.split(':')[0],pid,sid,fid,json.dumps(record),now))
            counts[name]={'imported':len(value),'issues':issues}
    logs=root/'jobs'
    if logs.is_dir() and not logs.is_symlink():
        for index,path in enumerate(sorted(logs.iterdir())):
            if index>=1000:break
            if not path.is_file() or path.is_symlink():continue
            with path.open('rb') as source:
                source.seek(max(0,path.stat().st_size-65536));raw=source.read(65536)
            name='log:'+path.name
            digest='tail:'+hashlib.sha256(raw).hexdigest();archive=_archive(path.name,raw,digest[5:])
            with db.write() as conn:
                now=db.utc_now();payload=json.dumps(redact(raw.decode('utf8','replace')))
                conn.execute('insert or ignore into companion_documents values(?,?,1,?)',(name,payload,now))
                conn.execute('insert or ignore into companion_imports values(?,?,?,?,?,?,?,?)',(uuid4().hex,str(path),digest,VERSION,1,payload,now,str(archive)))
    if processed and root.exists():
        # New/compatibility writers honor this checkpoint. Preserve all original
        # files and private read-only snapshots; no operational fallback reopens
        # either JSON ledger after the database cutover.
        marker=root/'.lea-db-migration.json'
        if not marker.exists():
            with open(marker,'x') as target:
                with db.connect() as conn:
                    receipts=[dict(row) for row in conn.execute('select path,digest,record_count,archive_path from companion_imports where path like ?',(str(root)+'/%',))]
                os.chmod(marker,0o600);json.dump({'version':VERSION,'state':'retired','database_import_verified':True,'receipts':receipts},target);target.flush();os.fsync(target.fileno())
    return counts


def _archive(name,raw,digest):
    directory=db.DB_PATH.parent/'legacy-companion'/digest
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    archive=directory/name
    if not archive.exists():
        with open(archive,'xb') as target:
            os.chmod(archive,0o600);target.write(raw);target.flush();os.fsync(target.fileno())
        os.chmod(archive,0o400)
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=digest:raise ValueError('Legacy archive integrity mismatch; import refused')
    return archive


def import_registry(project,root):
    """Retain the old generated document in SQL before workers render successors."""
    path=root/'workspace/projects'/(project['slug']+'.md')
    if not path.is_file() or path.is_symlink():return
    with path.open('rb') as source:raw=source.read(4*1024*1024+1)
    if len(raw)>4*1024*1024:raise ValueError('Legacy project registry exceeds its 4 MiB import bound; the original is retained')
    digest=hashlib.sha256(raw).hexdigest();archive=_archive(path.name,raw,digest)
    with db.write() as conn:
        if conn.execute('select 1 from companion_imports where path=? and digest=? and version=?',(str(path),digest,VERSION)).fetchone():return
        now=db.utc_now();identity=uuid4().hex;payload=json.dumps({'content':redact(raw.decode('utf8','replace')),'sha256':digest})
        conn.execute('insert into companion_imports values(?,?,?,?,?,?,?,?)',(identity,str(path),digest,VERSION,1,payload,now,str(archive)))
        conn.execute('insert or ignore into companion_documents values(?,?,1,?)',('project-registry-import:'+project['id'],payload,now))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Import a retired companion ledger without starting any run')
    parser.add_argument('directory',type=Path)
    args=parser.parse_args();db.init_db();print(json.dumps(import_directory(args.directory),indent=2))
