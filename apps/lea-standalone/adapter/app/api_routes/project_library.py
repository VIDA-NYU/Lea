"""Public SQL/admission contract. Private operations are in app.routes.project_library."""
from __future__ import annotations
from ..api_contracts import sql_query
from .. import workspace_commands
from fastapi import APIRouter
from pydantic import BaseModel, Field
router = APIRouter()
PREFIX = '/api/projects/by-slug/{slug}'

class EnsureRequest(BaseModel):
    specification: dict
    run_id: str | None = None
    requester_id: str | None = None
    mode: str = 'deferred'

class WorkerRequest(BaseModel):
    initiating_run_id: str | None = None
    model: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)

@router.get(PREFIX + '/library')
@sql_query('library')
def search(slug: str, q: str='', limit: int=20, offset: int=0, validate: bool=True):
    raise RuntimeError('A SQL query cannot execute')

@router.get(PREFIX + '/dependencies')
@sql_query('dependencies')
def list_dependencies(slug: str, requester_id: str | None=None):
    raise RuntimeError('A SQL query cannot execute')

@router.get(PREFIX + '/library/declarations/{name:path}')
@sql_query('declaration')
def inspect_declaration(slug: str, name: str):
    raise RuntimeError('A SQL query cannot execute')

@router.get(PREFIX + '/dependencies/{dependency_id}')
@sql_query('dependency')
def get_dependency(slug: str, dependency_id: str):
    raise RuntimeError('A SQL query cannot execute')

@router.post(PREFIX + '/dependencies/ensure', status_code=202)
@workspace_commands.intake('dependency.ensure')
def ensure(slug: str, request: EnsureRequest):
    raise RuntimeError('An intake contract cannot execute')

@router.post(PREFIX + '/dependencies/{dependency_id}/retry', status_code=202)
@workspace_commands.intake('dependency.retry')
def retry(slug: str, dependency_id: str):
    raise RuntimeError('An intake contract cannot execute')

@router.post(PREFIX + '/dependencies/{dependency_id}/run', status_code=202)
@workspace_commands.intake('dependency.run')
def start_worker(slug: str, dependency_id: str, request: WorkerRequest):
    raise RuntimeError('An intake contract cannot execute')

@router.post(PREFIX + '/dependencies/{dependency_id}/resume', status_code=202)
@workspace_commands.intake('dependency.resume')
def resume_worker(slug: str, dependency_id: str):
    raise RuntimeError('An intake contract cannot execute')
