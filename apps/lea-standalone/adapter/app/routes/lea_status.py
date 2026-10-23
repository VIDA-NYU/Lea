"""Read-only current and historical solver assessments."""
from ..core_query_dispatch import database_query
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Literal
from .. import lea_status as service, alignment_checks
from ..read_projections import read_projection

router = APIRouter()


class SourceComparison(BaseModel):
    source_bundle: dict
    coverage: Literal["complete", "partial"] = "partial"


class AssessmentComparison(SourceComparison):
    formalization_id: str
    alignment_source_bundle_hash: str | None = None


class AssessmentComparisons(BaseModel):
    comparisons: list[AssessmentComparison] = Field(max_length=100)


@router.post("/api/lea-status/compare")
@database_query('assessments')
@read_projection
def compare_assessments(request: AssessmentComparisons):
    """One fresh read for inventory assessments; never starts checks or a model.

    Sharing the response's filesystem observations avoids repeatedly traversing
    the same imported proof inputs for every live and historical assessment.
    The projection validates those observations before returning the response.
    """
    assessments = {}
    for item in request.comparisons:
        try:
            status = service.current(item.formalization_id, source_bundle=item.source_bundle,
                                     coverage=item.coverage)
            legacy = alignment_checks.current(item.formalization_id, item.alignment_source_bundle_hash)
            assessments[item.formalization_id] = {"lea_status": status, **legacy}
        except (LookupError, ValueError) as exc:
            # An unavailable/invalid target must not hide other targets' results.
            assessments[item.formalization_id] = {"error": str(exc)}
    return {"assessments": assessments}


@router.post("/api/formalizations/{formalization_id}/lea-status/compare")
@database_query('status_compare')
def compare(formalization_id: str, request: SourceComparison):
    try:
        return {"lea_status": service.current(formalization_id, source_bundle=request.source_bundle,
                                             coverage=request.coverage)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/formalizations/{formalization_id}/lea-status")
@database_query('status')
def current(formalization_id: str, source_identity_hash: str | None = None, source_bundle_hash: str | None = None):
    try:
        return {"lea_status": service.current(formalization_id, source_identity_hash, source_bundle_hash)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/formalizations/{formalization_id}/lea-status/updates")
@database_query('status_history')
def history(formalization_id: str, after: str | None = None, limit: int = 50):
    try:
        return service.history(formalization_id, after, limit)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
