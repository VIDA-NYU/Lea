"""Credential-bound command intake; result observation reads only SQL receipts."""
import json
from fastapi import APIRouter,Request,HTTPException
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from .. import commands
from ..migration_limits import COMMAND_BYTES

router=APIRouter()


def credential(request):
    value=request.headers.get('authorization','')
    if not value.startswith('Bearer '):raise HTTPException(401,detail={'error':'unauthorized'})
    return value[7:]


@router.post('/api/commands',status_code=202)
async def submit(request:Request):
    key=credential(request);data=bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data)>COMMAND_BYTES:raise HTTPException(413,detail={'error':'envelope_too_large'})
    try:
        value=json.loads(data)
        return await run_in_threadpool(commands.admit,key,value)
    except (ValidationError,json.JSONDecodeError,UnicodeDecodeError):raise HTTPException(422,detail={'error':'invalid_command'})
    except commands.QueueError as exc:
        raise HTTPException(exc.status,detail={'error':exc.code},
            headers={'Retry-After':'5'} if exc.status in {429,503} else None) from exc


@router.get('/api/commands/{command_id}')
def receipt(command_id:str,request:Request):
    try:return commands.get(credential(request),command_id)
    except commands.QueueError as exc:raise HTTPException(exc.status,detail={'error':exc.code}) from exc
