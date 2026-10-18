"""Public SQL/admission contract. Private operations are in app.routes.mcp_servers."""
from __future__ import annotations
from .. import peripheral_views
from .. import workspace_commands
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from .. import store
router = APIRouter()

class ServerPayload(BaseModel):
    name: str
    transport: str = 'stdio'
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    env_from: list[str] = []
    url: str | None = None
    api_key_name: str | None = None
    enabled: bool = True
    is_global: bool = False
    project_ids: list[str] = []

class ServerUpdate(BaseModel):
    name: str | None = None
    transport: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    env_from: list[str] | None = None
    url: str | None = None
    api_key_name: str | None = None
    enabled: bool | None = None

class ServerAssignment(BaseModel):
    is_global: bool = False
    project_ids: list[str] = []

class ServerTest(BaseModel):
    """A candidate server to dry-run. Accepts an unsaved draft so the form can test
    BEFORE saving — the common case is "did I type this right?", which is worth
    answering before a row exists."""
    transport: str = 'stdio'
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    env_from: list[str] = []
    url: str | None = None
    api_key_name: str | None = None

@router.get('/api/mcp-servers')
def list_servers() -> dict:
    return {'servers': store.list_mcp_servers()}

@router.post('/api/mcp-servers', status_code=201)
def create_server(request: ServerPayload) -> dict:
    """Create + scope in one call, matching the Skill Factory's "Add → choose scope"
    (D58). A shape or secret-in-env problem is a 400 the form renders inline."""
    try:
        server = store.create_mcp_server(name=request.name, transport=request.transport, command=request.command, args=request.args, env=request.env, env_from=request.env_from, url=request.url, api_key_name=request.api_key_name, enabled=request.enabled)
        if request.is_global or request.project_ids:
            server = store.set_mcp_server_assignment(server['id'], is_global=request.is_global, project_ids=request.project_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return server

@router.get('/api/mcp-servers/defaults')
@peripheral_views.view('server_defaults')
def server_defaults() -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.get('/api/mcp-servers/key-requirements')
@peripheral_views.view('key_requirements')
def key_requirements() -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.get('/api/mcp-servers/catalog')
@peripheral_views.view('list_catalog')
def list_catalog() -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.post('/api/mcp-servers/catalog/{entry_id}', status_code=201)
@workspace_commands.intake('peripheral.install_catalog_entry')
def install_catalog_entry(entry_id: str) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/mcp-servers/{server_id}')
def get_server(server_id: str) -> dict:
    server = store.get_mcp_server(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail='MCP server not found')
    return server

@router.put('/api/mcp-servers/{server_id}')
def update_server(server_id: str, request: ServerUpdate) -> dict:
    try:
        updated = store.update_mcp_server(server_id, **request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail='MCP server not found')
    return updated

@router.put('/api/mcp-servers/{server_id}/assignment')
def set_assignment(server_id: str, request: ServerAssignment) -> dict:
    try:
        updated = store.set_mcp_server_assignment(server_id, is_global=request.is_global, project_ids=request.project_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail='MCP server not found')
    return updated

@router.delete('/api/mcp-servers/{server_id}')
def delete_server(server_id: str) -> dict:
    if not store.delete_mcp_server(server_id):
        raise HTTPException(status_code=404, detail='MCP server not found')
    return {'deleted': True, 'id': server_id}

class SkillMcpToggle(BaseModel):
    kind: str
    item_id: str
    action: str | None

@router.get('/api/sessions/{session_id}/skills-mcp')
@peripheral_views.view('session_skills_mcp')
def session_skills_mcp(session_id: str) -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.put('/api/sessions/{session_id}/skills-mcp')
@workspace_commands.intake('peripheral.update_session_skills_mcp')
def update_session_skills_mcp(session_id: str, request: SkillMcpToggle) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.post('/api/mcp-servers/test')
@workspace_commands.intake('peripheral.test_server')
def test_server(request: ServerTest) -> dict:
    raise RuntimeError('An intake contract cannot execute')
