"""Public SQL/admission contract. Private operations are in app.routes.sessions."""
from __future__ import annotations
from ..api_contracts import sql_query
from .. import peripheral_views
import asyncio
import json
from .. import workspace_commands
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from .. import store
router = APIRouter()

class PathRequest(BaseModel):
    file_revision: str | None = None
    path: str | None = None
    author: str | None = None
    summary: str | None = None
    formalization_id: str | None = None

class FileWriteRequest(workspace_commands.EditRequest):
    path: str
    content: str
    note: str | None = None
    formalization_id: str | None = None
    base_revision: str | None = None
    check_and_cascade: bool = False

class SessionUpdate(BaseModel):
    title: str

@router.get('/api/sessions')
@sql_query('sessions')
def list_sessions() -> dict:
    raise RuntimeError('A SQL query cannot execute')
_SESSIONS_POLL_SECONDS = 1.0
_SESSIONS_MAX_TICKS = 10800

@router.get('/api/sessions/events')
async def session_list_events(request: Request) -> StreamingResponse:
    """One durable invalidation feed; query snapshots own the list contents.

    Resume a consistent list snapshot via ?after=N or Last-Event-ID. Expiry is
    an explicit refresh event, so EventSource never loops on an unreadable 409.
    No process token, digest, worker invocation or open DB transaction spans IO.
    """
    from starlette.concurrency import run_in_threadpool
    from .. import observation
    raw = request.headers.get('last-event-id') or request.query_params.get('after')
    initial = raw is None
    cursor = int(raw) if raw and raw.isascii() and raw.isdigit() and (len(raw) <= 19) else 0

    async def stream():
        nonlocal cursor, initial
        for _ in range(_SESSIONS_MAX_TICKS):
            if getattr(request.app.state, 'shutting_down', False) or await request.is_disconnected():
                return
            try:
                page = await run_in_threadpool(observation.changes, cursor)
                next_cursor = page['watermark'] if initial else page['cursor']
                noise = {'peripheral_views', 'commands', 'command_workers','worker_process', 'command_attempts', 'companion_documents', 'companion_jobs', 'companion_associations', 'download_artifacts', 'source_publishers'}
                changed = initial or any((change['entity_type'] not in noise for change in page['changes']))
                reset = False
            except observation.CursorReset as exc:
                next_cursor, changed, reset = (exc.watermark, True, True)
                page = {'has_more': False}
            cursor, initial = (next_cursor, False)
            if changed:
                payload = {'cursor': cursor, 'reset': reset}
                yield f'event: sessions_changed\nid: {cursor}\ndata: {json.dumps(payload)}\n\n'
            else:
                yield ': keep-alive\n\n'
            if not page['has_more']:
                await asyncio.sleep(_SESSIONS_POLL_SECONDS)
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@router.get('/api/stats')
@sql_query('stats')
def stats(include_proof_status: bool=True) -> dict:
    raise RuntimeError('A SQL query cannot execute')

@router.get('/api/sessions/{session_id}')
@sql_query('session')
def session_detail(session_id: str) -> dict:
    raise RuntimeError('A SQL query cannot execute')

@router.patch('/api/sessions/{session_id}')
def update_session(session_id: str, request: SessionUpdate) -> dict:
    title = request.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail='Title is required')
    updated = store.update_session_title(session_id, title)
    if updated is None:
        raise HTTPException(status_code=404, detail='Session not found')
    return updated

@router.post('/api/sessions/{session_id}/compact', status_code=202)
@workspace_commands.intake('session.compact')
def compact_session(session_id: str) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.post('/api/sessions/{session_id}/file', status_code=202)
@workspace_commands.intake('session.save')
def write_file_session(session_id: str, request: FileWriteRequest) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/sessions/{session_id}/lean-check-runtime')
@peripheral_views.view('lean_check_runtime')
def lean_check_runtime(session_id: str) -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.post('/api/sessions/{session_id}/edit', status_code=202)
@workspace_commands.intake('session.edit')
def edit_session(session_id: str, request: FileWriteRequest) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.post('/api/sessions/{session_id}/lean-check', status_code=202)
@workspace_commands.intake('session.check')
def lean_check_session(session_id: str, request: PathRequest) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.post('/api/sessions/{session_id}/rebuild', status_code=202)
@workspace_commands.intake('session.rebuild')
def rebuild_session_module(session_id: str, request: PathRequest) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.post('/api/sessions/{session_id}/verify', status_code=202)
@workspace_commands.intake('session.verify')
def verify_session(session_id: str, request: PathRequest) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/sessions/{session_id}/lsp-info')
def lsp_info(session_id: str, path: str | None=None) -> dict:
    """Legacy initial-content alias. Reads only the published SQL file."""
    from .editor import invoke
    from ..editor_service import file_info
    return invoke(file_info, session_id, path)

@router.websocket('/api/sessions/{session_id}/lsp')
async def lsp_socket(websocket: WebSocket, session_id: str) -> None:
    """Retired execution channel. Every client must use persisted requests."""
    await websocket.close(code=1008, reason='Direct LSP retired; use queued editor requests')

@router.get('/api/sessions/{session_id}/export')
@peripheral_views.view('export_session', automatic=False, lifetime=86400)
def export_session(session_id: str) -> Response:
    raise RuntimeError('A SQL view cannot execute')

@router.post('/api/sessions/{session_id}/export', status_code=202)
@workspace_commands.intake('session.export')
def prepare_session_export(session_id: str):
    raise RuntimeError('An intake contract cannot execute')
