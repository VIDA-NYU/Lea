"""Local source refresh transport. It never starts a prover or a model request."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from .. import overleaf_source as service

router = APIRouter()


class SourceResponse(BaseModel):
    document_id: str
    source_bundle: dict | None = None
    mirrored_files: dict[str, str] | None = None
    error: str | None = Field(default=None, max_length=2000)


@router.post("/api/formalizations/{formalization_id}/overleaf-source/prepare")
async def prepare(formalization_id: str):
    try:
        return await service.prepare(formalization_id)
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(504, detail="Open this document in Overleaf with the extension connected, then send again.") from exc


@router.get("/api/overleaf/source-requests")
def pending():
    return {"requests": service.pending()}


@router.post("/api/overleaf/source-requests/{request_id}")
def respond(request_id: str, request: SourceResponse):
    try:
        service.respond(request_id, request.source_bundle, request.error, request.document_id, request.mirrored_files)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
