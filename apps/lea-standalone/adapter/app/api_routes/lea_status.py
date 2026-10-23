"""Public SQL/admission contract. Private operations are in app.routes.lea_status."""
from __future__ import annotations
from ..api_contracts import sql_query
from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Literal
router = APIRouter()

class SourceComparison(BaseModel):
    source_bundle: dict
    coverage: Literal['complete', 'partial'] = 'partial'

class AssessmentComparison(SourceComparison):
    formalization_id: str
    alignment_source_bundle_hash: str | None = None

class AssessmentComparisons(BaseModel):
    comparisons: list[AssessmentComparison] = Field(max_length=100)

@router.post('/api/lea-status/compare')
@sql_query('assessments')
def compare_assessments(request: AssessmentComparisons):
    raise RuntimeError('A SQL query cannot execute')

@router.post('/api/formalizations/{formalization_id}/lea-status/compare')
@sql_query('status_compare')
def compare(formalization_id: str, request: SourceComparison):
    raise RuntimeError('A SQL query cannot execute')

@router.get('/api/formalizations/{formalization_id}/lea-status')
@sql_query('status')
def current(formalization_id: str, source_identity_hash: str | None=None, source_bundle_hash: str | None=None):
    raise RuntimeError('A SQL query cannot execute')

@router.get('/api/formalizations/{formalization_id}/lea-status/updates')
@sql_query('status_history')
def history(formalization_id: str, after: str | None=None, limit: int=50):
    raise RuntimeError('A SQL query cannot execute')
