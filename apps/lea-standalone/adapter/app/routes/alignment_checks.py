"""HTTP API for starting, polling, retrying, and inspecting Lea Check reports."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import alignment_checks as service, alignment_store, store


router = APIRouter()


@router.post("/api/formalizations/{formalization_id}/alignment-checks", status_code=202)
def start_alignment_check(formalization_id: str, request: dict | None = None) -> dict:
    raise HTTPException(status_code=410, detail={"code": "lea_check_retired", "message": "Lea Status is maintained during formalization; independent checks are retired."})


@router.get("/api/formalizations/{formalization_id}/alignment-checks/current")
def current_alignment_check(formalization_id: str, source_bundle_hash: str | None = None) -> dict:
    if store.get_formalization(formalization_id) is None:
        raise HTTPException(status_code=404, detail="Formalization not found")
    return service.current(formalization_id, source_bundle_hash)


@router.get("/api/formalizations/{formalization_id}/alignment-checks")
def alignment_check_history(formalization_id: str) -> dict:
    if store.get_formalization(formalization_id) is None:
        raise HTTPException(status_code=404, detail="Formalization not found")
    return {
        "alignment_checks": [service.public(item, current=False)["lea_check"] for item in alignment_store.history(formalization_id)]
    }


@router.post("/api/alignment-checks/{check_id}/retry", status_code=202)
def retry_alignment_check(check_id: str) -> dict:
    raise HTTPException(status_code=410, detail={"code": "lea_check_retired", "message": "Resume a formalization to receive live Lea Status updates."})
