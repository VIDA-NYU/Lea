"""Public SQL/admission contract. Private operations are in app.routes.search."""
from __future__ import annotations
from ..api_contracts import sql_query
from fastapi import APIRouter
router = APIRouter()

@router.get('/api/search')
@sql_query('search')
def search(q: str='') -> dict:
    raise RuntimeError('A SQL query cannot execute')
