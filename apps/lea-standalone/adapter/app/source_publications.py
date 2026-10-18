"""Immutable, publisher-fenced source. Admission never contacts an editor or disk."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from . import db
from .source_context import validate_source
from .queries.sources import resolve,latest,receipt,_validate_binding

LEASE_SECONDS = 90
MAX_FILES = 10000


class PublisherRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    document_id: str = Field(min_length=1,max_length=300)
    client_id: str = Field(min_length=1,max_length=200)
    expected_generation: StrictInt = Field(default=0,ge=0,le=2**53-1)


class PublicationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: StrictInt = 1
    document_id: str = Field(min_length=1,max_length=300)
    client_id: str = Field(min_length=1,max_length=200)
    generation: StrictInt = Field(ge=1,le=2**53-1)
    sequence: StrictInt = Field(ge=1,le=2**53-1)
    formalization_id: str | None = None
    source_bundle: dict
    files: dict[str,str] = Field(default_factory=dict)
    archive_ref: str | None = Field(default=None,max_length=100)
    coverage: str = 'partial'


def dump(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))


def publisher(request):
    now=datetime.now(timezone.utc)
    with db.write() as conn:
        old=conn.execute('select * from source_publishers where document_id=?',(request.document_id,)).fetchone()
        if old and old['client_id']==request.client_id and old['lease_until']>now.isoformat():
            generation=old['generation']
        else:
            if old and (old['lease_until']>now.isoformat() or request.expected_generation!=old['generation']):
                raise HTTPException(409,detail={'code':'publisher_conflict','generation':old['generation'],
                    'message':'Another tab owns this document publication. Close it and wait for its lease to expire before claiming this generation.'})
            if not old and request.expected_generation!=0:raise HTTPException(409,'Publisher generation changed')
            generation=(old['generation'] if old else 0)+1
            conn.execute('''insert into source_publishers values(?,?,?,?,?) on conflict(document_id) do update set
                client_id=excluded.client_id,generation=excluded.generation,sequence=0,lease_until=excluded.lease_until''',
                (request.document_id,request.client_id,generation,0,(now+timedelta(seconds=LEASE_SECONDS)).isoformat()))
        return {'version':1,'document_id':request.document_id,'client_id':request.client_id,'generation':generation,
                'sequence':old['sequence'] if old and generation==old['generation'] else 0,'lease_seconds':LEASE_SECONDS}


def publish(request,conn=None):
    if conn is None:
        with db.write() as connection:return publish(request,connection)
    if request.version!=1 or request.coverage not in {'partial','complete'}:raise HTTPException(422,'Unsupported source version or coverage')
    if len(dump(request.model_dump()).encode())>32*1024*1024:raise HTTPException(413,'Source publication exceeds its 32 MiB aggregate input bound')
    try:source=validate_source(request.source_bundle)
    except (ValueError,TypeError) as exc:raise HTTPException(422,str(exc)) from exc
    if source['targetKind'] not in {'theorem','definition'} or not source['targetKey'] or len(source['targetKey'])>512:raise HTTPException(422,'Unsupported source target identity')
    if source.get('proof') and source['proofAssociation'].get('proofHash')!=hashlib.sha256(source['proof'].encode()).hexdigest():raise HTTPException(422,'Associated proof hash does not match its published text')
    from .workspace_commands import normalize_path
    inventory=request.files
    if request.archive_ref:
        if inventory:raise HTTPException(422,'Supply source bytes or an immutable archive reference')
        archive=conn.execute('select * from source_archives where id=? and document_id=? and mode=?',(request.archive_ref,request.document_id,'publication')).fetchone()
        if not archive:raise HTTPException(409,'The immutable document archive is unavailable; publish its bytes again')
        inventory=json.loads(archive['files_json'])
    files={normalize_path(path):content for path,content in inventory.items()}
    if len(files)!=len(inventory) or len(files)>MAX_FILES:raise HTTPException(413,'Source inventory exceeds its file bound')
    from .upload_intake import OVERLEAF_SOURCE_EXTS
    from pathlib import PurePosixPath
    if any(PurePosixPath(path).suffix.lower() not in OVERLEAF_SOURCE_EXTS for path in files):raise HTTPException(422,'Only supported Overleaf source files belong in a source publication')
    if any(len(content.encode())>4*1024*1024 for content in files.values()) or sum(len(p.encode())+len(c.encode()) for p,c in files.items())>32*1024*1024:
        raise HTTPException(413,'Source publication exceeds 4 MiB/file or 32 MiB aggregate')
    if sum(len(p.encode())+len(c.encode()) for p,c in files.items())+len(json.dumps(source,ensure_ascii=False).encode())>32*1024*1024:
        raise HTTPException(413,'Source bundle plus archive exceeds the 32 MiB bound')
    if (source.get('mirror') or {}).get('verified'):
        if request.coverage!='complete' or len(files)!=source['mirror']['mirroredFileCount']:
            raise HTTPException(422,'A verified mirror requires the complete immutable file inventory')
        revision=hashlib.sha256('\0'.join(path+'\0'+hashlib.sha256(content.encode()).hexdigest() for path,content in sorted(files.items())).encode()).hexdigest()
        if source['mirror'].get('revision')!=revision:raise HTTPException(422,'Source mirror hash does not match the published immutable inventory')
    for fragment in source.get('relevantSource',[]):
        path=fragment.get('path')
        if path in files:
            exact=files[path].replace('\r\n','\n').replace('\r','\n')
            if fragment.get('content','') not in exact:raise HTTPException(422,'Source excerpt does not match its published file')
        elif request.coverage=='complete' and path:raise HTTPException(422,'Complete source is missing a referenced excerpt file')
    fingerprint=hashlib.sha256(dump(request.model_dump()).encode()).hexdigest()
    previous=conn.execute('select * from source_publication_receipts where document_id=? and client_id=? and generation=? and sequence=?',
        (request.document_id,request.client_id,request.generation,request.sequence)).fetchone()
    if previous:
        if previous['request_hash']!=fingerprint:raise HTTPException(409,'Publication sequence was already used for different source')
        return receipt(conn.execute('select * from source_publications where id=?',(previous['publication_id'],)).fetchone())
    owner=conn.execute('select * from source_publishers where document_id=?',(request.document_id,)).fetchone()
    if not owner or owner['client_id']!=request.client_id or owner['generation']!=request.generation or owner['lease_until']<=db.utc_now():
        raise HTTPException(409,detail={'code':'publisher_conflict','message':'Claim the current document publisher before publishing.'})
    if request.sequence<=owner['sequence']:raise HTTPException(409,'Source sequence is older than the current publication')
    form=conn.execute('select * from formalizations where id=?',(request.formalization_id,)).fetchone() if request.formalization_id else None
    if request.formalization_id and not form:raise HTTPException(404,'Formalization not found')
    if not form:
        matches=conn.execute('select * from formalizations where origin=? and origin_key=?',
            ('overleaf',f"{request.document_id}:{source['targetKind']}:{source['targetKey']}")).fetchall()
        if len(matches)>1:raise HTTPException(409,'The published target has ambiguous database associations')
        form=matches[0] if matches else None
    if form:_validate_binding(conn,dict(form),request.document_id,source)
    pid=form['project_id'] if form else None
    now=db.utc_now();identity=uuid4().hex
    current=conn.execute('select p.* from source_heads h join source_publications p on p.id=h.publication_id where h.document_id=? and h.target_kind=? and h.target_key=?',
        (request.document_id,source['targetKind'],source['targetKey'])).fetchone()
    unchanged=bool(current and current['client_id']==request.client_id and current['generation']==request.generation and current['bundle_hash']==source['bundleHash']
                   and current['coverage']==request.coverage and file_inventory(conn,current)==files)
    if unchanged:identity=current['id']
    else:
        archive_hash=hashlib.sha256(dump(files).encode()).hexdigest()
        archive=conn.execute('select id from source_archives where document_id=? and sha256=?',(request.document_id,archive_hash)).fetchone()
        archive_id=archive[0] if archive else uuid4().hex
        if not archive:conn.execute('insert into source_archives(id,project_id,files_json,mode,created_at,document_id,sha256) values(?,?,?,?,?,?,?)',
            (archive_id,pid or '',dump(files),'publication',now,request.document_id,archive_hash))
        conn.execute('''insert into source_publications values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (identity,request.document_id,request.client_id,request.generation,request.sequence,pid,form['id'] if form else None,
             source['sourceIdentityHash'],source['bundleHash'],request.coverage,json.dumps(source,ensure_ascii=False,separators=(',',':')),dump({'$archive':archive_id}),fingerprint,now))
    conn.execute('insert into source_publication_receipts values(?,?,?,?,?,?)',
        (request.document_id,request.client_id,request.generation,request.sequence,fingerprint,identity))
    head=conn.execute('select * from source_heads where document_id=? and target_kind=? and target_key=?',
        (request.document_id,source['targetKind'],source['targetKey'])).fetchone()
    if not unchanged or (form and (head['formalization_id']!=form['id'] or head['project_id']!=pid)):
        conn.execute('''insert into source_heads values(?,?,?,?,?,?,?) on conflict(document_id,target_kind,target_key) do update set
            publication_id=excluded.publication_id,project_id=coalesce(excluded.project_id,source_heads.project_id),
            formalization_id=coalesce(excluded.formalization_id,source_heads.formalization_id),updated_at=excluded.updated_at''',
            (request.document_id,source['targetKind'],source['targetKey'],identity,pid,form['id'] if form else None,now))
    conn.execute('update source_publishers set sequence=?,lease_until=? where document_id=?',
        (request.sequence,(datetime.now(timezone.utc)+timedelta(seconds=LEASE_SECONDS)).isoformat(),request.document_id))
    # Source identity is a domain change, not proof/check/approval evidence.
    if form and form['source_hash']!=source['sourceIdentityHash']:conn.execute('update formalizations set source_hash=?,updated_at=? where id=?',(source['sourceIdentityHash'],now,form['id']))
    return receipt(conn.execute('select * from source_publications where id=?',(identity,)).fetchone())


def file_inventory(conn,row):
    value=json.loads(row['files_json'])
    if '$archive' not in value:return value
    archive=conn.execute('select files_json from source_archives where id=?',(value['$archive'],)).fetchone()
    if not archive:raise ValueError('The retained source archive is unavailable')
    return json.loads(archive[0])


def materialize_run(run_id,proofs_root):
    """Worker-only materialization of precisely the admitted immutable inventory."""
    from . import store,uploads,workspace_commands
    with db.connect() as conn:
        run=conn.execute('select * from runs where id=?',(run_id,)).fetchone()
        row=conn.execute('select * from source_publications where id=?',(run['source_context_ref'],)).fetchone() if run and run['source_context_ref'] else None
        if not row:return False
        resolve(conn,row['id'],run['focus_formalization_id'],admission=False)
        files=file_inventory(conn,row)
    if files or row['coverage']=='complete':
        project=store.get_project(run['project_id'])
        if not project:raise ValueError('The published source project is unavailable')
        from . import run_commands
        token=run_commands.current_token()
        with db.connect() as conn:
            if token and not run_commands.owns(conn,run_id,token):raise ValueError('Source materialization lost command ownership')
        if not token:raise ValueError('Source materialization requires an owned worker command')
        _materialize(token,project,proofs_root,files,row['coverage'])
    return True


def _materialize(token,project,proofs_root,files,coverage):
    """Retain before-images and install only captured destinations; no blind sync."""
    import base64
    from . import uploads,workspace_commands,workspace_effects,workspace_files,store,commands
    from .projects import project_repo_dir
    repo=project_repo_dir(project,proofs_root)
    base=uploads.overleaf_dir(project,proofs_root)
    before={};total=0
    if any(path.is_symlink() for path in (repo,repo/'.lea',repo/'.lea/files',base)):raise ValueError('Resolve source mirror symlinks before materialization')
    if base.exists():
        for path in base.rglob('*'):
            if path.is_symlink():raise ValueError('Resolve source mirror symlinks before materialization')
            if not path.is_file():continue
            with path.open('rb') as source:raw=source.read(32*1024*1024-total+1)
            total+=len(raw)
            if total>32*1024*1024 or len(before)>=10000:raise ValueError('Source recovery snapshot exceeds its bounds')
            before[path.relative_to(repo).as_posix()]=raw
    wanted={'.lea/files/overleaf/'+path:content for path,content in files.items()}
    # The SQL current binding is the expected base for already managed content.
    # Keep a racing external edit rather than adopting it as our overwrite base.
    for relative,raw in before.items():
        with db.connect() as conn:binding=conn.execute('select blob_id,encoding from workspace_files where scope=? and path=?',('p:'+project['id'],relative)).fetchone()
        if binding:
            expected=store.blob_content(binding['blob_id'])
            expected=base64.b64decode(expected) if binding['encoding']=='base64' else expected.encode()
            if expected!=raw:raise ValueError('An external edit changed the source mirror; retained publication needs reconciliation')
    effect=uuid4().hex
    manifest={'action':'source.materialize','project':project,'repo':str(repo),'files':{}}
    with db.write() as conn:
        if not commands._owned(conn,token):raise ValueError('Source materialization lost ownership')
        for relative,raw in before.items():manifest['files'][relative]={'blob_id':store._put_blob(conn,base64.b64encode(raw).decode()),'encoding':'base64'}
        conn.execute('insert into workspace_effects values(?,?,?,?,?,?,?,?,?)',(effect,token['id'],project['id'],token['session_id'],'operation','prepared',dump(manifest),db.utc_now(),db.utc_now()))
    with workspace_commands.executing(token):
        for relative,content in wanted.items():
            absolute=repo/relative;absolute.parent.mkdir(parents=True,exist_ok=True)
            workspace_effects.install_from_snapshot(absolute,content)
        if coverage=='complete':
            for relative in before:
                if relative not in wanted and not relative.endswith('/.gitignore'):workspace_effects.install_from_snapshot(repo/relative,None)
        workspace_effects.finish(effect)
        # Index source bytes for ordinary file/attachment projections; identity
        # came from the publication, never from this mutable materialization.
        for relative,content in wanted.items():
            existing=store.get_project_file_by_path(project['id'],relative)
            if not existing:store.create_project_file(project['id'],filename=relative.rsplit('/',1)[-1],stored_path=relative,kind='overleaf',mime='text/x-tex')
        if coverage=='complete':
            for entry in store.list_project_files_by_kind(project['id'],'overleaf'):
                if entry['stored_path'] not in wanted:
                    with db.write() as conn:
                        workspace_commands.authorize(conn)
                        store.delete_project_file(entry['id'])
        try:
            sha=uploads.commit_mirror(project,proofs_root)
            with db.write() as conn:
                workspace_commands.authorize(conn)
                conn.execute("update workspace_files set commit_sha=? where scope=? and path like '.lea/files/overleaf/%'",(sha,'p:'+project['id']))
        except Exception as exc:
            with db.write() as conn:
                workspace_commands.authorize(conn)
                conn.execute("update workspace_files set materialization='provenance_pending',detail=? where scope=? and path like '.lea/files/overleaf/%'",(str(exc),'p:'+project['id']))


def retain(limit=25):
    """Drop only unreferenced superseded input after thirty days, in short batches.

    Current heads, run provenance and accepted business bindings are never aged
    out here. Receipt deletion cannot replay a sequence: the publisher watermark
    still rejects it. Legacy raw archives are retained pending manual sign-off.
    """
    cutoff=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
    with db.write() as conn:
        conn.execute('''delete from source_publication_receipts where rowid in (
            select r.rowid from source_publication_receipts r join source_publishers p using(document_id)
            where r.generation<p.generation or (r.generation=p.generation and r.sequence<p.sequence-100000) limit ?)''',(limit,))
        ids=[r[0] for r in conn.execute('''select p.id from source_publications p where p.created_at<?
            and not exists(select 1 from source_heads h where h.publication_id=p.id)
            and not exists(select 1 from runs r where r.source_context_ref=p.id)
            and not exists(select 1 from companion_source_bindings b where b.publication_id=p.id)
            and not exists(select 1 from reformalization_operations o where o.request_json like '%'||p.id||'%') limit ?''',(cutoff,limit))]
        for identity in ids:
            conn.execute('delete from source_publication_receipts where publication_id=?',(identity,))
            conn.execute('delete from source_publications where id=?',(identity,))
        conn.execute('''delete from source_archives where id in (select a.id from source_archives a where a.created_at<?
            and not exists(select 1 from source_publications p where json_extract(p.files_json,'$.$archive')=a.id)
            and not exists(select 1 from source_archives newer where newer.project_id=a.project_id and newer.mode=a.mode and newer.created_at>a.created_at)
            limit ?)''',(cutoff,limit))
    return len(ids)
