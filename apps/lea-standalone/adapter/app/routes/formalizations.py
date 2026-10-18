"""First-class formalization CRUD and read models."""

from __future__ import annotations

import sqlite3

from ..core_query_dispatch import database_query
from .. import workspace_commands
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Literal

from .. import formalizations as service
from .. import github_import_service
from ..config import load_config
from .. import store, project_coordination, formalization_reviews


def _scope_project(arguments):
    if "project_id" in arguments:
        return arguments["project_id"]
    if "slug" in arguments:
        project = store.get_project_by_slug(arguments["slug"])
        return project["id"] if project else None
    if "formalization_id" in arguments:
        item = store.get_formalization(arguments["formalization_id"])
        return item.get("project_id") if item else None
    session = store.get_session(arguments["session_id"])
    return session.get("project_id") if session else None


router = APIRouter()


class FormalizationCreate(BaseModel):
    display_title: str
    kind: str = "theorem"
    declaration_name: str | None = None
    statement: str | None = None
    origin: str = "ui"
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
@database_query('review')
@project_coordination.guarded(_scope_project, metadata=True)
def review_context(formalization_id: str, source_context_ref: str | None = None) -> dict:
    return formalization_reviews.context(formalization_id, source_context_ref)


@router.post('/api/formalizations/{formalization_id}/review-decisions', status_code=202)
@workspace_commands.register('formalization.review')
@project_coordination.guarded(_scope_project)
def review_decision(formalization_id: str, request: ReviewDecision) -> dict:
    # The service owns the mutation reservation, before its environment lease.
    return formalization_reviews.decide(formalization_id, request.model_dump())


@router.get("/api/projects/{project_id}/formalizations")
@database_query('project_forms')
@project_coordination.guarded(_scope_project, metadata=True)
def project_formalizations(project_id: str) -> dict:
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    items = service.for_project(project_id)
    return {"formalizations": items, "summary": service.summary(items)}


@router.get("/api/projects/by-slug/{slug}/formalizations")
@database_query('slug_forms')
@project_coordination.guarded(_scope_project, metadata=True)
def project_formalizations_by_slug(slug: str) -> dict:
    project = store.get_project_by_slug(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    items = service.for_project(project["id"])
    return {
        "project_id": project["id"],
        "slug": project["slug"],
        "formalizations": items,
        "summary": service.summary(items),
    }


@router.post('/api/projects/{project_id}/formalizations', status_code=202)
@workspace_commands.register('formalization.create')
@project_coordination.guarded(_scope_project, write=True)
def create_project_formalization(
    project_id: str, request: FormalizationCreate
) -> dict:
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not request.display_title.strip():
        raise HTTPException(status_code=400, detail="Display title is required")
    try:
        row = store.create_formalization(
            project_id=project_id,
            loose_session_id=None,
            display_title=request.display_title,
            kind=request.kind,
            declaration_name=request.declaration_name,
            statement=request.statement,
            origin=request.origin,
            origin_key=request.origin_key,
            source_hash=request.source_hash,
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    config = load_config()
    if config.lea_root:
        project = store.get_project(project_id)
        if project:
            github_import_service.try_adopt_imported_declaration(
                project, row, config.lea_root / "workspace" / "proofs"
            )
    return service.decorate([row])[0]


@router.get("/api/sessions/{session_id}/formalizations")
@database_query('session_forms')
@project_coordination.guarded(_scope_project, metadata=True)
def session_formalizations(session_id: str) -> dict:
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    items = service.for_session(session_id)
    return {"formalizations": items, "summary": service.summary(items)}


@router.get("/api/formalizations/{formalization_id}")
@database_query('formalization')
@project_coordination.guarded(_scope_project, metadata=True)
def formalization_detail(formalization_id: str) -> dict:
    item = service.get(formalization_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Formalization not found")
    return item


@router.get("/api/formalizations/{formalization_id}/current")
@database_query('current')
@project_coordination.guarded(_scope_project, metadata=True)
def formalization_current(
    formalization_id: str,
    session_id: str | None = None,
) -> dict:
    if session_id is not None and store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    item = service.current_snapshot(
        formalization_id,
        conversation_session_id=session_id,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Formalization not found")
    return item


@router.patch("/api/formalizations/{formalization_id}",status_code=202)
@workspace_commands.register('formalization.update')
@project_coordination.guarded(_scope_project, write=True)
def update_formalization(
    formalization_id: str, request: FormalizationUpdate
) -> dict:
    try:
        row = store.update_formalization(
            formalization_id,
            display_title=request.display_title,
            declaration_name=request.declaration_name,
            statement=request.statement,
            kind=request.kind,
            source_hash=request.source_hash,
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Formalization not found")
    return service.decorate([row])[0]
