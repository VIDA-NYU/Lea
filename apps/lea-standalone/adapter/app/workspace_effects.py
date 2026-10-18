"""Recovery envelopes for multi-file operations without a publication journal.

Uncertain operations retain before-images and become explicit conflicts. They
are never replayed after a lease loss. Existing staged publication journals own
their rollback; deletion uses a reversible directory move before SQL deletion.
"""
import base64
import json
import os
from pathlib import Path
from uuid import uuid4
from fastapi import HTTPException
from . import commands, db, store, workspace_commands as intake

SNAPSHOT_ACTIONS = {'project.identity', 'project.update', 'project.default_name', 'project.retire',
                    'project.restore', 'project.generate_blueprint', 'project.generate_blueprint_slug',
                    'project.sync_formalizations', 'project.delete_file', 'project.upload'}


def prepare(token):
    body = json.loads(token['payload_json'])
    if body['action'] in intake.EDIT_ACTIONS or body['action'] == 'project.delete':
        return None
    effect_id = uuid4().hex
    manifest = {'action': body['action'], 'files': {}, 'project': store.get_project(token['project_id']) if token['project_id'] else None}
    if body['action'] in SNAPSHOT_ACTIONS and manifest['project']:
        from .config import load_config
        from .projects import project_repo_dir
        config = load_config()
        if config.lea_root:
            repo = project_repo_dir(manifest['project'], config.lea_root / 'workspace/proofs')
            manifest['repo'] = str(repo)
            total = 0
            files = {}
            for root, directories, names in os.walk(repo, followlinks=False):
                directories[:] = [d for d in directories if d not in {'.git', '.lake', '.lea-trash'}
                    and not (Path(root).name=='.lea' and d=='displaced') and not (Path(root) / d).is_symlink()]
                for name in names:
                    path = Path(root) / name
                    if path.is_symlink():
                        raise HTTPException(409, 'Resolve project symlinks before a multi-file workspace operation')
                    raw = path.read_bytes()
                    total += len(raw)
                    if total > 64 * 1024 * 1024 or len(files) >= 10000:
                        raise HTTPException(413, 'Workspace recovery snapshot exceeds its 64 MiB / 10000 file limit')
                    files[path.relative_to(repo).as_posix()] = raw
            with db.write() as conn:
                intake.authorize(conn)
                for path, raw in files.items():
                    manifest['files'][path] = {'blob_id': store._put_blob(conn, base64.b64encode(raw).decode()), 'encoding': 'base64'}
    with db.write() as conn:
        intake.authorize(conn)
        conn.execute('insert into workspace_effects values(?,?,?,?,?,?,?,?,?)',
            (effect_id, token['id'], token['project_id'], token['session_id'], 'operation', 'prepared', commands._dump(manifest), db.utc_now(), db.utc_now()))
    return effect_id


def finish(effect_id, *, error=None):
    if effect_id:
        with db.write() as conn:
            intake.authorize(conn)
            effect = conn.execute('select * from workspace_effects where id=?', (effect_id,)).fetchone()
        state = 'published'
        manifest=json.loads(effect['manifest_json'])
        if not error and manifest.get('installations') and effect['project_id']:
            from . import workspace_files
            from .config import load_config
            from .projects import project_repo_dir
            project=store.get_project(effect['project_id'])
            repo=project_repo_dir(project,load_config().lea_root/'workspace/proofs')
            for relative,entry in manifest['installations'].items():
                if entry['after_blob']:
                    expected=store.blob_content(entry['after_blob']).encode()
                    workspace_files.adopt_materialized(project['id'],[(relative,repo/relative)],expected={relative:expected})
                else:
                    if (repo/relative).exists():
                        raise HTTPException(409,detail={'code':'external_conflict','message':'A removed destination was recreated before publication.'})
                    before=manifest['files'].get(relative)
                    if before:workspace_files.mark_deleted(project['id'],relative,base64.b64decode(store.blob_content(before['blob_id'])))
        if error:
            changed = store.get_project(effect['project_id']) != manifest.get('project') if effect['project_id'] else False
            if manifest.get('repo'):
                repo = Path(manifest['repo'])
                for path, entry in manifest['files'].items():
                    absolute = repo / path
                    expected = base64.b64decode(store.blob_content(entry['blob_id']))
                    if not absolute.is_file() or absolute.is_symlink() or absolute.read_bytes() != expected:
                        changed = True
                        break
            state = 'conflict' if changed else 'failed'
        with db.write() as conn:
            intake.authorize(conn)
            conn.execute('update workspace_effects set state=?,updated_at=? where id=?', (state, db.utc_now(), effect_id))


def assert_available(conn, project_id):
    if project_id and conn.execute("select 1 from workspace_effects where project_id=? and state='conflict' limit 1", (project_id,)).fetchone():
        raise HTTPException(409, detail={'code': 'workspace_reconciliation_required', 'message': 'An interrupted workspace operation has retained before-images. Reconcile it before starting more project work.'})


def install_from_snapshot(absolute, content):
    """Replace/remove only the exact destination captured in durable intent."""
    from .workspace_files import _bytes, _install
    token=intake.current()
    if token is None:
        if content is None:absolute.unlink()
        else:absolute.write_text(content)
        return
    intake.authorize()
    with db.connect() as conn:
        effect=conn.execute("select * from workspace_effects where command_id=? and kind='operation' and state='prepared' order by created_at desc limit 1",(token['id'],)).fetchone()
    if not effect:raise HTTPException(409,'Workspace effect has no durable intent')
    manifest=json.loads(effect['manifest_json']);repo=Path(manifest['repo'])
    relative=absolute.resolve().relative_to(repo.resolve()).as_posix()
    entry=manifest['files'].get(relative)
    expected=base64.b64decode(store.blob_content(entry['blob_id'])) if entry else None
    def conflict(raw):
        with db.write() as conn:
            if raw is not None:
                blob=store._put_blob(conn,base64.b64encode(raw).decode())
                conn.execute('insert or ignore into workspace_blob_refs values(?,?)',(token['id'],blob))
                manifest.setdefault('external',{})[relative]={'blob_id':blob,'encoding':'base64'}
            manifest['failure']='An external edit raced the workspace operation at '+relative
            conn.execute("update workspace_effects set state='conflict',manifest_json=?,updated_at=? where id=?",(commands._dump(manifest),db.utc_now(),effect['id']))
        raise HTTPException(409,detail={'code':'external_conflict','message':'An external edit was retained instead of overwritten.','effect_id':effect['id']})
    observed=_bytes(absolute)
    if observed!=expected:conflict(observed)
    with db.write() as conn:
        intake.authorize(conn)
        after=store._put_blob(conn,content) if content is not None else None
        if after:conn.execute('insert or ignore into workspace_blob_refs values(?,?)',(token['id'],after))
        manifest.setdefault('installations',{})[relative]={'after_blob':after}
        conn.execute('update workspace_effects set manifest_json=?,updated_at=? where id=?',(commands._dump(manifest),db.utc_now(),effect['id']))
    if observed is not None:
        displaced=repo/'.lea/displaced'/token['id']/relative
        displaced.parent.mkdir(parents=True,exist_ok=True)
        os.rename(absolute,displaced)
        moved=_bytes(displaced)
        if moved!=expected:
            if not absolute.exists():os.link(displaced,absolute)
            conflict(moved)
    if content is not None:
        try:_install(absolute,content.encode())
        except FileExistsError:conflict(_bytes(absolute))
    elif absolute.exists():conflict(_bytes(absolute))


def delete_project(project, proofs_root):
    from . import projects, project_coordination
    token = intake.current()
    repo = projects.project_repo_dir(project, proofs_root)
    tombstone = proofs_root / '.lea-trash' / token['id']
    effect_id = uuid4().hex
    manifest = {'project': project, 'repo': str(repo), 'tombstone': str(tombstone)}
    with project_coordination.deletion(project['id']):
        if store.project_has_active_run(project['id']) or store.project_has_active_import(project['id']):
            raise HTTPException(409, 'Wait for active project work before deleting')
        with db.write() as conn:
            intake.authorize(conn)
            conn.execute('insert into workspace_effects values(?,?,?,?,?,?,?,?,?)',
                (effect_id, token['id'], project['id'], None, 'deletion', 'prepared', commands._dump(manifest), db.utc_now(), db.utc_now()))
        if repo.exists():
            tombstone.parent.mkdir(parents=True, exist_ok=True)
            if tombstone.exists():
                raise HTTPException(409, 'Deletion recovery directory already exists')
            os.rename(repo, tombstone)
        intake.authorize()
        deleted = store.delete_project_cascade(project['id'])
        with db.write() as conn:
            intake.authorize(conn)
            conn.execute("update workspace_effects set state='published',updated_at=? where id=?", (db.utc_now(), effect_id))
        if deleted:
            project_coordination.mark_deleted(project['id'])
        # Retain the tombstone. Purging accepted deletions is a separate bounded
        # maintenance policy; a request must not recursively delete the only copy.
        return deleted


def recover():
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("select * from workspace_effects where kind in ('operation','deletion') and state='prepared'")]
    for effect in rows:
        manifest = json.loads(effect['manifest_json'])
        state = 'conflict'
        if effect['kind'] == 'deletion':
            repo, tombstone = Path(manifest['repo']), Path(manifest['tombstone'])
            if not store.get_project(effect['project_id']):
                state = 'published'  # SQL deletion committed, retained directory is provenance.
            elif tombstone.exists() and not repo.exists():
                repo.parent.mkdir(parents=True, exist_ok=True)
                os.rename(tombstone, repo)
                state = 'restored'
            elif repo.exists() and not tombstone.exists():
                state = 'restored'  # Interrupted before the move.
        with db.write() as conn:
            conn.execute('update workspace_effects set state=?,updated_at=? where id=?', (state, db.utc_now(), effect['id']))
