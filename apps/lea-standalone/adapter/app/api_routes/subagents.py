"""Public SQL/admission contract. Private operations are in app.routes.subagents."""
from __future__ import annotations
from .. import peripheral_views
from .. import workspace_commands
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from .. import store
router = APIRouter()

class OverrideRequest(BaseModel):
    model: str | None = None
    max_turns: int | None = None
    max_cost: float | None = None
    system_prompt: str | None = None
    tools: list[str] | None = None

class RoleCreate(BaseModel):
    name: str
    system_prompt: str = ''
    authoring: dict | None = None
    description: str | None = None
    model: str | None = None
    tools: list[str] | None = None
    max_turns: int | None = None

class RoleUpdate(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    authoring: dict | None = None
    description: str | None = None
    model: str | None = None
    tools: list[str] | None = None
    max_turns: int | None = None

@router.get('/api/sub-agents/profiles')
@peripheral_views.view('list_profiles')
def list_profiles() -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.post('/api/sub-agents/roles', status_code=201)
@workspace_commands.intake('role.create')
def create_role(request: RoleCreate) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.put('/api/sub-agents/roles/{role_id}')
@workspace_commands.intake('role.update')
def update_role(role_id: str, request: RoleUpdate) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.delete('/api/sub-agents/roles/{role_id}')
def delete_role(role_id: str) -> dict:
    if not store.delete_agent_role(role_id):
        raise HTTPException(status_code=404, detail='Role not found')
    return {'deleted': True, 'id': role_id}

@router.put('/api/sub-agents/profiles/{name}')
@workspace_commands.intake('peripheral.update_profile')
def update_profile(name: str, request: OverrideRequest) -> dict:
    raise RuntimeError('An intake contract cannot execute')
