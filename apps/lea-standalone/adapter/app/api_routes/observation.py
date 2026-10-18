"""Public SQL/admission contract. Private operations are in app.routes.observation."""
from __future__ import annotations
import asyncio
import json
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from .. import observation
from ..migration_limits import CHANGE_PAGE_DEFAULT, CHANGE_PAGE_MAX
router = APIRouter()

def _reset(exc):
    return {'error': 'snapshot_required', 'floor': exc.floor, 'watermark': exc.watermark, 'message': str(exc)}

@router.get('/api/changes')
async def changes(request: Request, after: int=Query(0, ge=0), limit: int=Query(CHANGE_PAGE_DEFAULT, ge=1, le=CHANGE_PAGE_MAX), project_id: str | None=None, session_id: str | None=None, formalization_id: str | None=None, run_id: str | None=None, stream: bool=False):
    if request.headers.get('last-event-id') is not None and stream:
        try:
            after = int(request.headers['last-event-id'])
            if after < 0:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, detail={'error': 'invalid_cursor'})
    scope = dict(limit=limit, project_id=project_id, session_id=session_id, formalization_id=formalization_id, run_id=run_id)
    try:
        first = await run_in_threadpool(observation.changes, after, **scope)
    except observation.CursorReset as exc:
        raise HTTPException(409, detail=_reset(exc))
    if not stream:
        return first

    async def frames():
        page = first
        while not await request.is_disconnected():
            yield f'id: {page['cursor']}\nevent: changes\ndata: {json.dumps(page)}\n\n'
            cursor = page['cursor']
            if not page['has_more']:
                await asyncio.sleep(1)
            try:
                page = await run_in_threadpool(observation.changes, cursor, **scope)
            except observation.CursorReset as exc:
                yield f'event: resync\ndata: {json.dumps(_reset(exc))}\n\n'
                return
    return StreamingResponse(frames(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@router.get('/api/snapshots/sessions/{session_id}')
def session_snapshot(session_id: str):
    result = observation.session_snapshot(session_id)
    if result is None:
        raise HTTPException(404, detail='Session not found')
    return result
