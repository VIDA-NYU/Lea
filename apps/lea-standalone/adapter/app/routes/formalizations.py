"""First-class formalization CRUD and read models."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import formalizations as service
from .. import github_import_service
from ..config import load_config
from .. import store


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


@router.get("/api/projects/{project_id}/formalizations")
def project_formalizations(project_id: str) -> dict:
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    items = service.for_project(project_id)
    return {"formalizations": items, "summary": service.summary(items)}


@router.get("/api/projects/by-slug/{slug}/formalizations")
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


@router.post("/api/projects/{project_id}/formalizations", status_code=201)
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
def session_formalizations(session_id: str) -> dict:
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    items = service.for_session(session_id)
    return {"formalizations": items, "summary": service.summary(items)}


@router.get("/api/formalizations/{formalization_id}")
def formalization_detail(formalization_id: str) -> dict:
    item = service.get(formalization_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Formalization not found")
    return item


@router.get("/api/formalizations/{formalization_id}/current")
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


@router.patch("/api/formalizations/{formalization_id}")
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
