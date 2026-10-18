"""Public SQL/admission contract. Private operations are in app.routes.formalizations."""
from __future__ import annotations
from ..api_contracts import sql_query
from .. import workspace_commands
from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Literal
router = APIRouter()

class FormalizationCreate(BaseModel):
    display_title: str
    kind: str = 'theorem'
    declaration_name: str | None = None
    statement: str | None = None
    origin: str = 'ui'
    origin_key: str | None = None
    source_hash: str | None = None

class FormalizationUpdate(BaseModel):
    display_title: str | None = None
    kind: str | None = None
    declaration_name: str | None = None
    statement: str | None = None
    source_hash: str | None = None

class ReviewDecision(BaseModel):
    action: Literal['approve', 'withdraw']
    outcome: Literal['proved', 'defined', 'disproved'] | None = None
    revision_token: str | None = None
    approval_id: str | None = None
    source_context_ref: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)

@router.get('/api/formalizations/{formalization_id}/review')
@sql_query('review')
def review_context(formalization_id: str, source_context_ref: str | None=None) -> dict:
    raise RuntimeError('A SQL query cannot execute')

@router.post('/api/formalizations/{formalization_id}/review-decisions', status_code=202)
@workspace_commands.intake('formalization.review')
def review_decision(formalization_id: str, request: ReviewDecision) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/projects/{project_id}/formalizations')
@sql_query('project_forms')
def project_formalizations(project_id: str) -> dict:
    raise RuntimeError('A SQL query cannot execute')

@router.get('/api/projects/by-slug/{slug}/formalizations')
@sql_query('slug_forms')
def project_formalizations_by_slug(slug: str) -> dict:
    raise RuntimeError('A SQL query cannot execute')

@router.post('/api/projects/{project_id}/formalizations', status_code=202)
@workspace_commands.intake('formalization.create')
def create_project_formalization(project_id: str, request: FormalizationCreate) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/sessions/{session_id}/formalizations')
@sql_query('session_forms')
def session_formalizations(session_id: str) -> dict:
    raise RuntimeError('A SQL query cannot execute')

@router.get('/api/formalizations/{formalization_id}')
@sql_query('formalization')
def formalization_detail(formalization_id: str) -> dict:
    raise RuntimeError('A SQL query cannot execute')

@router.get('/api/formalizations/{formalization_id}/current')
@sql_query('current')
def formalization_current(formalization_id: str, session_id: str | None=None) -> dict:
    raise RuntimeError('A SQL query cannot execute')

@router.patch('/api/formalizations/{formalization_id}', status_code=202)
@workspace_commands.intake('formalization.update')
def update_formalization(formalization_id: str, request: FormalizationUpdate) -> dict:
    raise RuntimeError('An intake contract cannot execute')
