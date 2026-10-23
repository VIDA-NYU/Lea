"""Public SQL/admission contract. Private operations are in app.routes.custom_tools."""
from __future__ import annotations
from .. import peripheral_views
from .. import workspace_commands
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from .. import store
router = APIRouter()

class ToolPayload(BaseModel):
    name: str
    url: str
    description: str = ''
    authoring: dict | None = None
    method: str = 'GET'
    params: dict = {}
    headers: dict = {}
    auth_key_name: str | None = None
    auth_header: str | None = None
    timeout: int | None = None
    enabled: bool = True
    is_global: bool = False
    project_ids: list[str] = []

class ToolAssignment(BaseModel):
    is_global: bool = False
    project_ids: list[str] = []

@router.get('/api/custom-tools')
def list_tools() -> dict:
    return {'tools': store.list_custom_tools()}

@router.post('/api/custom-tools', status_code=201)
def create_tool(request: ToolPayload) -> dict:
    try:
        tool = store.create_custom_tool(name=request.name, url=request.url, description=request.description, method=request.method, params=request.params, headers=request.headers, auth_key_name=request.auth_key_name, auth_header=request.auth_header, timeout=request.timeout, enabled=request.enabled, authoring=request.authoring)
        if request.is_global or request.project_ids:
            tool = store.set_custom_tool_assignment(tool['id'], is_global=request.is_global, project_ids=request.project_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return tool

@router.get('/api/custom-tools/catalog')
@peripheral_views.view('list_tool_catalog')
def list_tool_catalog() -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.post('/api/custom-tools/catalog/{entry_id}', status_code=201)
@workspace_commands.intake('peripheral.install_tool_entry')
def install_tool_entry(entry_id: str) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/custom-tools/{tool_id}')
def get_tool(tool_id: str) -> dict:
    tool = store.get_custom_tool(tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail='Tool not found')
    return tool

@router.put('/api/custom-tools/{tool_id}/assignment')
def set_assignment(tool_id: str, request: ToolAssignment) -> dict:
    try:
        updated = store.set_custom_tool_assignment(tool_id, is_global=request.is_global, project_ids=request.project_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail='Tool not found')
    return updated

@router.delete('/api/custom-tools/{tool_id}')
def delete_tool(tool_id: str) -> dict:
    if not store.delete_custom_tool(tool_id):
        raise HTTPException(status_code=404, detail='Tool not found')
    return {'deleted': True, 'id': tool_id}
