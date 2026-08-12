"""Settings + model-catalog endpoints. Thin wrappers over `app.settings`, which
owns the config read/write and provider-key validation."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import settings as settings_service

router = APIRouter()
logger = logging.getLogger("lea-interface.settings")


class ApiKeyUpdateRequest(BaseModel):
    value: str | None = None
    clear: bool = False


class SettingsRequest(BaseModel):
    model: str | None = None
    permission_tier: str | None = None
    max_turns: int | None = None
    max_spend_usd: float | None = None
    api_keys: dict[str, ApiKeyUpdateRequest] | None = None
    # D3: acknowledge the "this key is still in use" warning and clear it regardless.
    force_clear_keys: bool = False
    # Same {value, clear} shape as a provider key; redacted on read (D34).
    github_token: ApiKeyUpdateRequest | None = None


@router.get("/api/settings")
def get_settings() -> dict:
    return settings_service.settings_payload()


@router.get("/api/models")
def models() -> dict:
    """Full LiteLLM chat-model catalog for the searchable model picker.

    `warnings` (F3) is non-empty when this is the curated FALLBACK list rather than
    the live catalog — so the picker can say so instead of presenting a handful of
    models as the whole world."""
    return {
        "models": settings_service.model_catalog(),
        "warnings": settings_service.model_catalog_warnings(),
    }


@router.get("/api/models/requirements")
def model_requirements(model: str) -> dict:
    """Which API key(s) a model needs and whether they're configured."""
    return settings_service.model_requirements(model)


@router.put("/api/settings")
def update_settings(request: SettingsRequest) -> dict:
    try:
        return settings_service.update_settings(request.dict(exclude_unset=True))
    except settings_service.SettingsValidationError as exc:
        logger.warning("Settings validation failed: field=%s message=%s", exc.field, str(exc))
        raise HTTPException(status_code=422, detail={"message": str(exc), "field": exc.field}) from exc
    except ValueError as exc:
        logger.warning("Settings update failed: %s", str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc
