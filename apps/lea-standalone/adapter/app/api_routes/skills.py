"""Public SQL/admission contract. Private operations are in app.routes.skills."""
from __future__ import annotations
from .. import workspace_commands
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from .. import store
router = APIRouter()

class SkillCreate(BaseModel):
    name: str
    body: str = ''
    authoring: dict | None = None
    is_global: bool = False
    project_ids: list[str] = []

class SkillUpdate(BaseModel):
    name: str | None = None
    body: str | None = None
    authoring: dict | None = None

class SkillAssignment(BaseModel):
    is_global: bool = False
    project_ids: list[str] = []

class SkillImport(BaseModel):
    url: str
    is_global: bool = False
    project_ids: list[str] = []

@router.get('/api/skills')
def list_skills() -> dict:
    return {'skills': store.list_skills()}

@router.post('/api/skills', status_code=201)
def create_skill(request: SkillCreate) -> dict:
    """Author a skill. Scope is applied in the same call (D58: "Add → choose
    scope"): when `is_global` or `project_ids` are given, the assignment is set
    right after create so the row comes back fully scoped."""
    try:
        skill = store.create_skill(request.name, request.body, authoring=request.authoring)
        if request.is_global or request.project_ids:
            skill = store.set_skill_assignment(skill['id'], is_global=request.is_global, project_ids=request.project_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return skill

@router.post('/api/skills/import', status_code=201)
@workspace_commands.intake('peripheral.import_skill')
def import_skill(request: SkillImport) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/skills/{skill_id}')
def get_skill(skill_id: str) -> dict:
    skill = store.get_skill(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail='Skill not found')
    return skill

@router.put('/api/skills/{skill_id}')
def update_skill(skill_id: str, request: SkillUpdate) -> dict:
    try:
        updated = store.update_skill(skill_id, name=request.name, body=request.body, authoring=request.authoring)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail='Skill not found')
    return updated

@router.put('/api/skills/{skill_id}/assignment')
def set_skill_assignment(skill_id: str, request: SkillAssignment) -> dict:
    """Set a skill's scope (D47): `is_global` plus the explicit per-project join.
    Replaces the join wholesale; unknown project ids are a 400."""
    try:
        updated = store.set_skill_assignment(skill_id, is_global=request.is_global, project_ids=request.project_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail='Skill not found')
    return updated

@router.delete('/api/skills/{skill_id}')
def delete_skill(skill_id: str) -> dict:
    if not store.delete_skill(skill_id):
        raise HTTPException(status_code=404, detail='Skill not found')
    return {'deleted': True, 'id': skill_id}
