"""Worker-owned publication of SQL file content, with recoverable before-images.

Intent, immutable bytes, timeline and unavailable evidence commit together. File
installation uses create-only links after retaining the displaced destination;
Git is provenance and never the only surviving copy of an accepted edit.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from fastapi import HTTPException
from . import commands, db, store, workspace_commands as intake


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _bytes(path):
    try:
        if path.is_symlink():
            raise HTTPException(409, detail={'code': 'external_conflict', 'message': 'An editor replaced the destination with a symlink.'})
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _install(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.lea-save-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)  # A newly created external destination wins.
    finally:
        os.unlink(temporary)


def _update(effect, state, manifest=None):
    with db.write() as conn:
        conn.execute('update workspace_effects set state=?,manifest_json=coalesce(?,manifest_json),updated_at=? where id=?',
            (state, commands._dump(manifest) if manifest is not None else None, db.utc_now(), effect['id']))


def _conflict(effect, manifest, observed, detail):
    with db.write() as conn:
        if observed is not None:
            # Preserve binary divergence too; text is stored directly when possible.
            import base64
            manifest['external_blob'] = store._put_blob(conn, base64.b64encode(observed).decode())
            manifest['external_encoding'] = 'base64'
        manifest['failure'] = detail
        conn.execute("update workspace_effects set state='conflict',manifest_json=?,updated_at=? where id=?", (commands._dump(manifest), db.utc_now(), effect['id']))
        conn.execute("update workspace_files set materialization='conflict',detail=? where scope=? and path=?", (detail, manifest['scope'], manifest['path']))
    raise HTTPException(409, detail={'code': 'external_conflict', 'message': detail,
        'draft_id': manifest.get('draft_id'), 'effect_id': effect['id']})


def materialize(effect, *, recovery=False):
    manifest = json.loads(effect['manifest_json'])
    absolute, displaced = Path(manifest['absolute']), Path(manifest['displaced'])
    after = store.blob_content(manifest['blob_id']).encode()
    before = store.blob_content(manifest['before_blob']).encode() if manifest['before_blob'] else None
    if not recovery:
        intake.authorize()
    observed = _bytes(absolute)
    if observed != after:
        if observed != before:
            _conflict(effect, manifest, observed, 'The working file changed outside this save. Its bytes and the submitted draft are retained.')
        absolute.parent.mkdir(parents=True, exist_ok=True)
        if observed is not None:
            if displaced.exists():
                if _bytes(displaced) != before:
                    _conflict(effect, manifest, _bytes(displaced), 'The retained before-image changed outside this operation.')
                displaced.unlink()
            os.rename(absolute, displaced)
            moved = _bytes(displaced)
            if moved != before:
                if not absolute.exists():
                    os.link(displaced, absolute)
                _conflict(effect, manifest, moved, 'An external edit raced file installation; it was retained instead of overwritten.')
        try:
            _install(absolute, after)
        except FileExistsError:
            _conflict(effect, manifest, _bytes(absolute), 'An external editor created the destination during installation.')
    if _bytes(absolute) != after:
        _conflict(effect, manifest, _bytes(absolute), 'The installed file changed before publication.')
    with db.write() as conn:
        if not recovery:
            intake.authorize(conn)
        conn.execute("update workspace_files set materialization='installed',detail=null where scope=? and path=? and revision_id=?",
            (manifest['scope'], manifest['path'], manifest['revision_id']))
        conn.execute("update workspace_effects set state='installed',updated_at=? where id=?", (db.utc_now(), effect['id']))
    if displaced.exists() and _bytes(displaced) == before:
        displaced.unlink()
    return manifest


def save(session_id, path, absolute, content, *, formalization_id=None, base_revision=None, note=None, project_id=None):
    from . import proof_files, project_coordination, formalizations
    from lea.check_evidence import mutation
    token = intake.current()
    if token is None:
        raise RuntimeError('Workspace publication requires a claimed command')
    body = json.loads(token['payload_json'])
    session = store.get_session(session_id) if session_id else None
    project_id = project_id or (session or {}).get('project_id')
    scope = 'p:' + project_id if project_id else 's:' + session_id
    with mutation(absolute), project_coordination.file_edit(project_id), proof_files.file_guard(absolute):
        if (session_id and store.has_active_run(session_id)) or (project_id and (
                store.project_has_active_run(project_id) or store.project_has_active_import(project_id))):
            raise HTTPException(409, detail={'error': 'project_busy', 'message': 'Wait for active project work before editing files.'})
        snapshot = formalizations.current_snapshot(formalization_id, conversation_session_id=session_id) if formalization_id else None
        if base_revision is not None and snapshot and base_revision != snapshot.get('revision_token'):
            raise HTTPException(409, detail={'code': 'revision_conflict', 'message': 'This formalization changed before the save.', 'current_revision': snapshot.get('revision_token')})
        before = _bytes(absolute)
        if before is not None and len(before) > 4 * 1024 * 1024:
            raise HTTPException(413, 'Working file exceeds the 4 MiB publication limit')
        with db.write() as conn:
            intake.authorize(conn)
            actual=intake.file_revision(conn, scope, path)
            if body['path'] and actual != body['file_base'] and not (actual=='missing' and body['file_base'].startswith('untracked:')):
                raise HTTPException(409, detail={'code': 'revision_conflict', 'message': 'The file changed before publication; the draft is retained.'})
            current = store.current_proof_step(session_id, path, conn=conn) if session_id else None
            binding = conn.execute('select * from workspace_files where scope=? and path=?', (scope, path)).fetchone()
            expected = store.blob_content(current['blob_id']) if current else store.blob_content(binding['blob_id']) if binding else None
            if actual.startswith('deleted:'):
                current=None;expected=None
            if expected is None and body['file_base'] and body['file_base'].startswith('observed:'):
                expected=store.blob_content(body['file_base'][9:])
            # Nested store reads must use this transaction, not open another writer.
            if expected is not None and before != expected.encode():
                # The admission record already retains the draft; record divergence
                # as a durable effect before rejecting, outside this transaction.
                mismatch = True
            else:
                mismatch = False
            if body.get('requires_absence') and before is not None:mismatch=True
            if body['file_base'] and body['file_base'].startswith('untracked:') and (before is None or digest(before)!=body['file_base'][10:]):mismatch=True
            revision = conn.execute('select * from workspace_file_revisions where command_id=? and path=?', (token['id'], path)).fetchone()
            blob = store._put_blob(conn, content)
            if not mismatch and before==content.encode() and current and current['blob_id']==blob:
                return {'unchanged':True,'code_step':None,'note':None,
                    'revision_token':(snapshot or {}).get('revision_token'),
                    'file_revision':intake.file_revision(conn,scope,path),'draft_id':body.get('draft_id'),
                    'effect_id':None}
            revision_id = revision['id'] if revision else uuid4().hex
            if not revision:
                conn.execute('insert into workspace_file_revisions values(?,?,?,?,?,?,?,?)', (revision_id, token['id'], scope, path, blob, intake.file_revision(conn, scope, path), body.get('draft_id'), db.utc_now()))
            effect_id = uuid4().hex
            manifest = {'scope': scope, 'path': path, 'absolute': str(absolute), 'blob_id': blob,
                'before_blob': store._put_blob(conn, before.decode()) if before is not None else None,
                'displaced': str(absolute.with_name('.lea-before-' + effect_id)), 'revision_id': revision_id,
                'draft_id': body.get('draft_id'), 'formalization_id': formalization_id}
            step_id = None
            if not mismatch and session_id and path.endswith('.lean'):
                cursor = conn.execute('''insert into timeline(session_id,kind,author,path,after_blob_id,formalization_id,created_at,content_lost)
                    values(?,'code','user',?,?,?,?,0)''', (session_id, path, blob, formalization_id, db.utc_now()))
                step_id = str(cursor.lastrowid)
                manifest['step_id'] = step_id
                if formalization_id:
                    conn.execute('insert or ignore into formalization_files(formalization_id,path,role) values(?,?,?)', (formalization_id, path, 'generated'))
            if not mismatch:
                conn.execute('''insert into workspace_files(scope,path,project_id,session_id,revision_id,blob_id,materialization)
                    values(?,?,?,?,?,?,'pending') on conflict(scope,path) do update set revision_id=excluded.revision_id,
                    blob_id=excluded.blob_id,materialization='pending',commit_sha=null,detail=null''',
                    (scope, path, project_id, session_id, revision_id, blob))
                if project_id:
                    conn.execute('update runs set safe_verify_status=null,safe_verify_detail=null where project_id=?', (project_id,))
                elif session_id:
                    conn.execute('update runs set safe_verify_status=null,safe_verify_detail=null where session_id=?', (session_id,))
                if session_id and note and note.strip():
                    note_cursor = conn.execute("insert into timeline(session_id,kind,author,content,formalization_id,created_at) values(?,'edit_note','user',?,?,?)", (session_id,note.strip(),formalization_id,db.utc_now()))
                    manifest['note_id'] = str(note_cursor.lastrowid)
                if session_id:
                    conn.execute('update sessions set updated_at=? where id=?',(db.utc_now(),session_id))
            conn.execute('insert into workspace_effects values(?,?,?,?,?,?,?,?,?)',
                (effect_id, token['id'], project_id, session_id, 'file', 'prepared', commands._dump(manifest), db.utc_now(), db.utc_now()))
            effect = dict(conn.execute('select * from workspace_effects where id=?', (effect_id,)).fetchone())
        if mismatch:
            _conflict(effect, manifest, before, 'The working file differs from its current database revision. Refresh before saving; both versions are retained.')
        materialize(effect)
        with db.connect() as conn:
            note_row = conn.execute('select * from timeline where id=?',(manifest.get('note_id'),)).fetchone()
        note_message = store._message_from_row(note_row) if note_row else None
        step = store.current_proof_step(session_id, path) if step_id else None
        updated = formalizations.current_snapshot(formalization_id, conversation_session_id=session_id) if formalization_id else None
        return {'unchanged': before == content.encode(), 'code_step': step, 'note': note_message,
            'revision_token': (updated or {}).get('revision_token'), 'file_revision': intake.revision_for(scope, path),
            'draft_id': body.get('draft_id'), 'effect_id': effect_id}


def provenance(effect_id, commit_sha=None, detail=None):
    if not effect_id:
        return
    with db.write() as conn:
        intake.authorize(conn)
        effect = conn.execute('select * from workspace_effects where id=?', (effect_id,)).fetchone()
        manifest = json.loads(effect['manifest_json'])
        conn.execute('update workspace_files set commit_sha=?,materialization=?,detail=? where scope=? and path=? and revision_id=?',
            (commit_sha, 'provenance_pending' if detail else 'materialized', detail, manifest['scope'], manifest['path'], manifest['revision_id']))
        conn.execute("update workspace_effects set state='published',updated_at=? where id=?", (db.utc_now(), effect_id))


def recover():
    """Reconcile committed SQL intent without invoking compilers, models or Git."""
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("select * from workspace_effects where kind='file' and state in ('prepared','installed') order by created_at")]
    for row in rows:
        manifest = json.loads(row['manifest_json'])
        try:
            # A crash after rename leaves the before-image at its retained path.
            absolute, displaced = Path(manifest['absolute']), Path(manifest['displaced'])
            if _bytes(absolute) is None and displaced.exists():
                before = store.blob_content(manifest['before_blob']).encode() if manifest['before_blob'] else None
                if _bytes(displaced) == before:
                    os.link(displaced, absolute)
            materialize(row, recovery=True)
            with db.write() as conn:
                conn.execute("update workspace_files set materialization='provenance_pending',detail='Recovered content; Git provenance must be recorded separately.' where scope=? and path=? and revision_id=?",
                    (manifest['scope'], manifest['path'], manifest['revision_id']))
                conn.execute("update workspace_effects set state='published',updated_at=? where id=?", (db.utc_now(), row['id']))
        except (HTTPException, OSError) as exc:
            _update(row, 'conflict', {**manifest, 'failure': str(exc)})


def adopt_materialized(project_id, paths, *, commit_sha=None, expected=None):
    """Bind worker-produced attachments/docs without making Git their authority."""
    import base64
    token=intake.current()
    if token is None:return
    scope='p:'+project_id
    values=[]
    for relative,absolute in paths:
        raw=_bytes(absolute)
        if expected is not None and raw!=expected[relative]:
            raise HTTPException(409,detail={'code':'external_conflict','message':'The produced file changed before its database binding was published.'})
        if raw is None:continue
        if len(raw)>25*1024*1024:raise HTTPException(413,'File exceeds the retained attachment limit')
        try:
            content=raw.decode('utf-8');encoding='utf8'
        except UnicodeDecodeError:
            content=base64.b64encode(raw).decode();encoding='base64'
        values.append((relative,content,encoding))
    with db.write() as conn:
        intake.authorize(conn)
        for relative,content,encoding in values:
            blob=store._put_blob(conn,content)
            revision=conn.execute('select id from workspace_file_revisions where command_id=? and path=?',(token['id'],relative)).fetchone()
            rid=revision[0] if revision else uuid4().hex
            if not revision:
                conn.execute('insert into workspace_file_revisions values(?,?,?,?,?,?,?,?)',(rid,token['id'],scope,relative,blob,intake.file_revision(conn,scope,relative),None,db.utc_now()))
            conn.execute('''insert into workspace_files(scope,path,project_id,revision_id,blob_id,materialization,commit_sha,encoding)
                values(?,?,?,?,?,'materialized',?,?) on conflict(scope,path) do update set
                revision_id=excluded.revision_id,blob_id=excluded.blob_id,materialization='materialized',commit_sha=excluded.commit_sha,encoding=excluded.encoding,detail=null''',
                (scope,relative,project_id,rid,blob,commit_sha,encoding))


def mark_deleted(project_id, relative, before):
    import base64
    token=intake.current();scope='p:'+project_id
    try:content=before.decode();encoding='utf8'
    except UnicodeDecodeError:content=base64.b64encode(before).decode();encoding='base64'
    with db.write() as conn:
        intake.authorize(conn)
        base=intake.file_revision(conn,scope,relative)
        blob=store._put_blob(conn,content);rid=uuid4().hex
        conn.execute('insert into workspace_file_revisions values(?,?,?,?,?,?,?,?)',(rid,token['id'],scope,relative,blob,base,None,db.utc_now()))
        conn.execute('''insert into workspace_files(scope,path,project_id,revision_id,blob_id,materialization,encoding)
            values(?,?,?,?,?,'deleted',?) on conflict(scope,path) do update set revision_id=excluded.revision_id,
            blob_id=excluded.blob_id,materialization='deleted',encoding=excluded.encoding,detail=null''',(scope,relative,project_id,rid,blob,encoding))
