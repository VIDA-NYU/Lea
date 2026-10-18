"""Public SQL/admission contract. Private operations are in app.routes.overleaf_source."""
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from .. import overleaf_source as service
from .. import source_publications as publications, db
router = APIRouter()

@router.post('/api/overleaf/publishers')
def publisher(request: publications.PublisherRequest):
    return publications.publisher(request)

@router.post('/api/overleaf/publications', status_code=201)
def publish(request: publications.PublicationRequest):
    try:
        return publications.publish(request)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

class PublishSubmit(BaseModel):
    publication: publications.PublicationRequest
    run: dict

@router.post('/api/overleaf/publications/submit', status_code=202)
def publish_submit(request: PublishSubmit):
    from .runs import RunRequest, create_run
    try:
        with db.write():
            source = publications.publish(request.publication)
            run = create_run(RunRequest.model_validate({**request.run, 'source_context_ref': source['source_context_ref']}))
            return {**run, 'publication': source}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

@router.get('/api/formalizations/{formalization_id}/published-source')
def published(formalization_id: str):
    try:
        return publications.latest(formalization_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

class SourceResponse(BaseModel):
    document_id: str
    source_bundle: dict | None = None
    mirrored_files: dict[str, str] | None = None
    error: str | None = Field(default=None, max_length=2000)

@router.post('/api/formalizations/{formalization_id}/overleaf-source/prepare')
async def prepare(formalization_id: str):
    try:
        return await service.prepare(formalization_id)
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(504, detail='Open this document in Overleaf with the extension connected, then send again.') from exc

@router.get('/api/overleaf/source-requests')
def pending():
    return {'requests': [], 'retired': True, 'publication_version': 1}

@router.post('/api/overleaf/source-requests/{request_id}')
def respond(request_id: str, request: SourceResponse):
    raise HTTPException(410, 'Source callbacks are retired; publish an immutable source snapshot.')
