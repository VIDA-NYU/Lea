"""Versioned local workspace command intake and SQL-only result/content reads."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from .. import db, store, commands, workspace_commands as service

router = APIRouter()


class WorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: StrictInt = 1
    action: str = Field(max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=180)
    draft_id: str | None = Field(default=None, max_length=200)
    expected_revision: str | None = Field(default=None,max_length=512)
    arguments: dict


@router.post('/api/workspace/commands', status_code=202)
def submit(request: WorkspaceRequest):
    if request.version != 1:
        raise HTTPException(422, 'Unsupported workspace command version')
    try:
        return service.admit(request.action, request.arguments, idempotency_key=request.idempotency_key,
            expected_revision=request.expected_revision, draft_id=request.draft_id, strict=True)
    except commands.QueueError as exc:
        raise HTTPException(exc.status, detail={'error': exc.code}, headers={'Retry-After': '5'} if exc.status in {429, 503} else None) from exc


@router.get('/api/workspace/receipts')
def lookup(idempotency_key: str):
    return service.get(idempotency_key=idempotency_key)


@router.get('/api/workspace/commands/{command_id}')
def receipt(command_id: str):
    return service.get(command_id)


@router.get('/api/workspace/file')
def file(path: str, project_id: str | None = None, session_id: str | None = None):
    path = service.normalize_path(path)
    with db.read_snapshot() as conn:
        project_id, session_id, _ = service._scope(conn, {'project_id': project_id, 'session_id': session_id})
        if project_id and not conn.execute('select 1 from projects where id=?', (project_id,)).fetchone():
            raise HTTPException(404, 'Project not found')
        scope = 'p:' + project_id if project_id else 's:' + session_id if session_id else None
        if not scope:
            raise HTTPException(422, 'A file scope is required')
        revision = service.file_revision(conn, scope, path)
        binding = conn.execute('select * from workspace_files where scope=? and path=?', (scope, path)).fetchone()
        step = store.current_proof_step(session_id, path, conn=conn) if session_id else None
        if not step and project_id:
            row = conn.execute("select t.after_blob_id from timeline t join sessions s on s.id=t.session_id where s.project_id=? and t.path=? and t.kind='code' order by t.id desc limit 1", (project_id, path)).fetchone()
            blob = row[0] if row else None
        else:
            blob = step['blob_id'] if step else None
        blob = blob or (binding['blob_id'] if binding else None)
        if not blob and revision.startswith('observed:'):
            blob = revision[9:]
        return {'path': path, 'file_revision': revision, 'content': store.blob_content(blob) if not revision.startswith('deleted:') and (not binding or binding['encoding']=='utf8') else None,
            'encoding':binding['encoding'] if binding else 'utf8',
            'materialization': binding['materialization'] if binding else 'observed' if blob else 'unobserved'}
