"""Project-scoped Formalize all report API."""
from fastapi import APIRouter, BackgroundTasks, HTTPException

from .. import formalize_batch_reports as service

router = APIRouter()
BASE = "/api/projects/by-slug/{slug}/formalize-batch-reports"


@router.post(BASE)
def create(slug: str, payload: dict, background_tasks: BackgroundTasks):
    try:
        report, created = service.create(slug, payload)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if created:
        background_tasks.add_task(service.generate, report["batch_id"])
    return report


@router.get(BASE)
def list_reports(slug: str, limit: int = 20, before: str | None = None):
    try:
        return service.list_reports(slug, limit, before)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get(BASE + "/{batch_id}")
def get(slug: str, batch_id: str):
    try:
        return service.get(slug, batch_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post(BASE + "/{batch_id}/retry")
def retry(slug: str, batch_id: str, background_tasks: BackgroundTasks):
    try:
        report = service.retry(slug, batch_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    background_tasks.add_task(service.generate, batch_id)
    return report
