"""Public SQL/admission contract. Private operations are in app.routes.editor."""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
import json
from .. import editor_service as service, commands
router = APIRouter()

def credential(request):
    value = request.headers.get('authorization', '')
    if not value.startswith('Bearer '):
        raise HTTPException(401, 'Editor credential required')
    return value[7:]

def invoke(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except commands.QueueError as exc:
        raise HTTPException(exc.status, detail={'error': exc.code}, headers={'Retry-After': '1'} if exc.status in {429, 503} else None) from exc

async def body(request, model, cap):
    value = bytearray()
    async for part in request.stream():
        value.extend(part)
        if len(value) > cap:
            raise HTTPException(413, 'Editor payload too large')
    try:
        return model.model_validate(json.loads(value))
    except (ValueError, ValidationError):
        raise HTTPException(422, 'Invalid editor payload')

@router.get('/api/sessions/{session_id}/editor')
def info(session_id: str, path: str | None=None):
    return invoke(service.file_info, session_id, path)

@router.post('/api/sessions/{session_id}/editor/clients', status_code=201)
def client(session_id: str):
    return invoke(service.create_client, session_id)

@router.delete('/api/editor/clients/current')
def close_client(request: Request):
    return invoke(service.close_client, credential(request))

@router.post('/api/editor/buffers', status_code=201)
async def publish(request: Request):
    key = credential(request)
    value = await body(request, service.Buffer, service.BUFFER_BYTES * 6 + 4096)
    return await run_in_threadpool(invoke, service.publish, key, value)

@router.post('/api/editor/requests', status_code=202)
async def submit(request: Request):
    key = credential(request)
    value = await body(request, service.LanguageRequest, 64 * 1024)
    return await run_in_threadpool(invoke, service.submit, key, value)

@router.get('/api/editor/receipts')
def lookup(request: Request, idempotency_key: str):
    return invoke(service.get, credential(request), idempotency_key=idempotency_key)

@router.get('/api/editor/requests/{command_id}')
def result(command_id: str, request: Request):
    return invoke(service.get, credential(request), command_id)
