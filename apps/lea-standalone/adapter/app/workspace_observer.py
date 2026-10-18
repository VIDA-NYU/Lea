"""Worker-only bounded file backfill. GET handlers never import or invoke this.

File inventory enrollment is independent of proof-status enrollment. These
observations do not establish that an old compiler/review result is current.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from . import db
from .migration_limits import SOURCE_FILE_BYTES, SOURCE_BUNDLE_BYTES
from .queries.files import input_revision

IGNORED = {'.git','.lake','node_modules','__pycache__'}
MAX_ENTRIES = 10000
OBSERVATION_SECONDS = 120


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _manifest(root):
    """Bounded metadata scan, excluding symlinks and build internals."""
    found=[];entries=0
    if not root.is_dir() or root.is_symlink():
        raise ValueError('Workspace is unavailable')
    pending=[root]
    while pending:
        directory=pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                entries+=1
                if entries>MAX_ENTRIES:
                    raise ValueError('Workspace inventory exceeds the 10000-entry migration limit')
                info=child.stat(follow_symlinks=False)
                path=Path(child.path)
                if stat.S_ISDIR(info.st_mode) and child.name not in IGNORED:
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    found.append((path.relative_to(root).as_posix(),info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_ino))
    return sorted(found)


def _read(root,relative):
    # Open each component relative to an owned directory descriptor. An external
    # rename to a symlink cannot redirect a read outside the project tree.
    descriptor=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        parts=relative.split('/')
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            os.close(descriptor);descriptor=child
        file=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=descriptor)
        with os.fdopen(file,'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError('Unsupported workspace entry')
            data=handle.read(SOURCE_FILE_BYTES+1)
        if len(data)>SOURCE_FILE_BYTES:
            return None,'file_too_large'
        try:
            return data.decode('utf-8'),None
        except UnicodeError:
            return None,'binary'
    finally:
        os.close(descriptor)


def _environment(root):
    records=[]
    # Configuration bytes supplement the full project manifest. The caller owns
    # the project path; no client-supplied path reaches this worker service.
    for base in (root,*list(root.parents)[:4]):
        for name in ('lean-toolchain','lakefile.lean','lakefile.toml','lake-manifest.json'):
            path=base/name
            if path.is_file() and not path.is_symlink():
                with path.open('rb') as handle:
                    data=handle.read(SOURCE_FILE_BYTES+1)
                if len(data)>SOURCE_FILE_BYTES:
                    raise ValueError('Environment input exceeds migration byte limit')
                records.append((str(path),hashlib.sha256(data).hexdigest()))
    return _digest(records)


def enroll(project_id):
    """Explicit worker/operator opt-in; never called during a GET."""
    with db.write() as conn:
        if not conn.execute('select 1 from projects where id=?',(project_id,)).fetchone():
            raise ValueError('Unknown project')
        conn.execute('''insert into projection_enrollment(project_id,version) values (?,1)
            on conflict(project_id) do update set enabled=1''',(project_id,))


def scan(project_id, root, *, limit=100):
    if not 1<=limit<=200:
        raise ValueError('Invalid backfill batch size')
    root=Path(root).absolute()
    manifest=_manifest(root)
    environment=_environment(root)
    identity=_digest([str(root),manifest,environment])
    with db.write() as conn:
        if not conn.execute('select 1 from projection_enrollment where project_id=? and enabled=1',(project_id,)).fetchone():
            raise ValueError('Project is not enrolled for file projections')
        revision=input_revision(conn,project_id)
        state=conn.execute('select * from workspace_backfills where project_id=?',(project_id,)).fetchone()
        if not state:
            previous=conn.execute('''select o.* from projection_enrollment e join workspace_observations o
                on o.id=e.observation_id where e.project_id=?''',(project_id,)).fetchone()
            floor=conn.execute('select floor from change_retention where id=1').fetchone()[0]
            if (previous and previous['manifest_revision']==identity and
                previous['input_cursor']>=max(revision,floor)):
                now=datetime.now(timezone.utc)
                conn.execute('update workspace_observations set observed_at=?,expires_at=? where id=?',
                    (now.isoformat(),(now+timedelta(seconds=OBSERVATION_SECONDS)).isoformat(),previous['id']))
                return {'state':'observed','processed':0,'observation_id':previous['id']}
        if state and (state['input_revision']!=revision or state['manifest_revision']!=identity):
            conn.execute("update workspace_observations set state='superseded' where id=?",(state['observation_id'],))
            conn.execute('delete from workspace_backfills where project_id=?',(project_id,))
            state=None
        if not state:
            observation_id=uuid4().hex
            now=datetime.now(timezone.utc)
            from .observation import watermark
            conn.execute('insert into workspace_observations values (?,?,?,?,?,?,?,?,?)',
                (observation_id,project_id,environment,watermark(conn),now.isoformat(),
                 (now+timedelta(seconds=OBSERVATION_SECONDS)).isoformat(),'capturing','worker_scan_v1',identity))
            conn.execute('insert into workspace_backfills(project_id,observation_id,checkpoint,input_revision,manifest_revision) values (?,?,?,?,?)',(project_id,observation_id,'',revision,identity))
            state={'observation_id':observation_id,'checkpoint':'','phase':'capture'}
        state=dict(state)
    # No filesystem read is performed while holding a database transaction.
    batch=[];used=0
    for path,size,*_ in manifest:
        if state['phase']!='capture':break
        if path<=state['checkpoint']:
            continue
        if len(batch)>=limit or (batch and used+min(size,SOURCE_FILE_BYTES)>SOURCE_BUNDLE_BYTES):
            break
        content,reason=_read(root,path)
        batch.append((path,size,content,reason));used+=min(size,SOURCE_FILE_BYTES)
    # If an external writer raced any captured byte, discard rather than publish
    # a generation assembled from visibly different workspace manifests.
    if _digest([str(root),_manifest(root),_environment(root)])!=identity:
        return {'state':'retry','processed':0}
    with db.write() as conn:
        current=conn.execute('select * from workspace_backfills where project_id=?',(project_id,)).fetchone()
        if (not current or current['observation_id']!=state['observation_id'] or
            current['checkpoint']!=state['checkpoint'] or input_revision(conn,project_id)!=revision):
            return {'state':'retry','processed':0}
        for path,size,content,reason in batch:
            blob_id=None
            if content is not None:
                digest=hashlib.sha256(content.encode()).hexdigest()
                conn.execute('insert or ignore into artifact_blobs values (?,?,?,?)',
                             (digest,digest,content,db.utc_now()))
                blob_id=conn.execute('select id from artifact_blobs where sha256=?',(digest,)).fetchone()[0]
            conn.execute('insert into current_file_bindings values (?,?,?,?,?,?,?)',
                (project_id,state['observation_id'],path,blob_id,size,'unavailable' if reason else 'present',reason))
        checkpoint=batch[-1][0] if batch else state['checkpoint']
        conn.execute('update workspace_backfills set checkpoint=? where project_id=?',(checkpoint,project_id))
        captured=not manifest or checkpoint==manifest[-1][0]
        done=False
        if state['phase']=='deleting':
            deleted=conn.execute('''select f.path from current_file_bindings f
                join projection_enrollment e on e.project_id=f.project_id and e.observation_id=f.observation_id
                where f.project_id=? and not exists(select 1 from current_file_bindings n
                    where n.observation_id=? and n.path=f.path) order by f.path limit ?''',
                (project_id,state['observation_id'],limit)).fetchall()
            for item in deleted:
                conn.execute("insert into current_file_bindings values (?,?,?,null,0,'deleted',null)",
                             (project_id,state['observation_id'],item['path']))
            done=len(deleted)<limit
        elif captured:
            pending=conn.execute('''select 1 from current_file_bindings f
                join projection_enrollment e on e.project_id=f.project_id and e.observation_id=f.observation_id
                where f.project_id=? and not exists(select 1 from current_file_bindings n
                    where n.observation_id=? and n.path=f.path) limit 1''',(project_id,state['observation_id'])).fetchone()
            done=not pending
            if pending:conn.execute("update workspace_backfills set phase='deleting' where project_id=?",(project_id,))
        if done:
            # Tombstones retain former path identity; immutable previous generations
            # and existing SQL proof blobs remain accessible as history.
            now=datetime.now(timezone.utc)
            conn.execute("update workspace_observations set state='observed',observed_at=?,expires_at=? where id=?",
                         (now.isoformat(),(now+timedelta(seconds=OBSERVATION_SECONDS)).isoformat(),state['observation_id']))
            conn.execute('update projection_enrollment set observation_id=? where project_id=?',(state['observation_id'],project_id))
            conn.execute('delete from workspace_backfills where project_id=?',(project_id,))
    return {'state':'observed' if done else 'capturing','processed':len(batch),'observation_id':state['observation_id']}


def maintain():
    """One bounded batch for the least recently observed enrolled project."""
    with db.write() as conn:
        project=conn.execute('''select p.* from projection_enrollment e join projects p on p.id=e.project_id
            left join workspace_observations o on o.id=e.observation_id where e.enabled=1
            order by e.last_scan_at,e.project_id limit 1''').fetchone()
        if project:
            conn.execute('update projection_enrollment set last_scan_at=? where project_id=?',(db.utc_now(),project['id']))
    if not project:
        return {}
    from .config import load_config
    from .projects import project_repo_dir
    config=load_config()
    if not config.lea_root:
        return {}
    return scan(project['id'],project_repo_dir(dict(project),config.lea_root/'workspace/proofs'))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Enroll a project for worker-maintained database file reads')
    parser.add_argument('--project',required=True)
    parser.add_argument('--core',action='store_true',help='Enroll session/proof queries and worker freshness observations')
    parser.add_argument('--disable',action='store_true',help='Restore legacy file reads; retain indexed history')
    args=parser.parse_args()
    db.init_db()
    if args.core:
        from . import core_observer
        core_observer.enroll('p:'+args.project,enabled=not args.disable)
    if args.disable:
        with db.write() as conn:
            conn.execute('update projection_enrollment set enabled=0 where project_id=?',(args.project,))
        print('Legacy file reads selected; indexed history retained.')
    else:
        enroll(args.project)
        # The existing maintenance host resumes bounded batches after this opt-in.
        print('Enrolled; workspace maintenance will publish the initial observation.')
