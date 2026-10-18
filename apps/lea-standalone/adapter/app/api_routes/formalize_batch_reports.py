"""Public SQL/admission contract. Private operations are in app.routes.formalize_batch_reports."""
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from ..queries import reports as service
from .. import workspace_commands
router = APIRouter()
BASE = '/api/projects/by-slug/{slug}/formalize-batch-reports'

@router.post(BASE)
@workspace_commands.intake('report.create')
def create(slug: str, payload: dict):
    raise RuntimeError('An intake contract cannot execute')

@router.get(BASE)
def list_reports(slug: str, limit: int=20, before: str | None=None):
    try:
        return service.list_reports(slug, limit, before)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

@router.get(BASE + '/{batch_id}')
def get(slug: str, batch_id: str):
    try:
        return service.get(slug, batch_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc

@router.post(BASE + '/{batch_id}/retry')
@workspace_commands.intake('report.retry')
def retry(slug: str, batch_id: str):
    raise RuntimeError('An intake contract cannot execute')
