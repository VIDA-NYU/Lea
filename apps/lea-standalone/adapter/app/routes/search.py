"""Global search endpoint (V1, D31/D41).

`GET /api/search?q=` returns sessions whose title — or whose project's title —
matches the query, each tagged with its project. This is the *only* path to a
project session: the sidebar hides in-project sessions (D30), so search is
load-bearing, not a nicety. The query work lives in `store.search_sessions`
(SQLite LIKE); this route is the thin HTTP layer.
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import formalizations, store

router = APIRouter()


@router.get("/api/search")
def search(q: str = "") -> dict:
    session_hits = [
        {**row, "result_type": "session"}
        for row in store.search_sessions(q)
    ]
    formalization_hits = [
        {
            "id": item["id"],
            "result_type": "formalization",
            "title": item["display_title"],
            "declaration_name": item.get("declaration_name"),
            "formalization_kind": item["kind"],
            "status": item["validity_status"],
            "activity": item["activity"],
            "updated_at": item["updated_at"],
            "project_id": item.get("project_id"),
            "project_title": item.get("project_title"),
            "project_namespace": item.get("project_namespace"),
            "session_id": (
                item["sessions"][0]["id"] if item.get("sessions") else None
            ),
            "primary_path": item.get("primary_path"),
        }
        for item in formalizations.search(q)
    ]
    results = sorted(
        [*formalization_hits, *session_hits],
        key=lambda item: (item.get("updated_at") or "", item["result_type"] == "formalization"),
        reverse=True,
    )[:30]
    return {"results": results}
