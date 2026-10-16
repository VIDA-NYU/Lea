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
    # Provider endpoint overrides (e.g. PORTKEY_BASE_URL), same {value, clear} shape.
    provider_endpoints: dict[str, ApiKeyUpdateRequest] | None = None
    # D3: acknowledge the "this key is still in use" warning and clear it regardless.
    force_clear_keys: bool = False
    # Same {value, clear} shape as a provider key; redacted on read (D34).
    github_token: ApiKeyUpdateRequest | None = None


@router.get("/api/settings")
def get_settings() -> dict:
    return settings_service.settings_payload()


@router.get("/api/models")
def models() -> dict:
    from ..catalog_service import service
    from ..config import load_config
    return service().payload(load_config().model)


@router.post("/api/models/refresh", status_code=202)
def refresh_models() -> dict:
    from ..catalog_service import service
    from ..config import load_config
    service().request_refresh()
    return service().payload(load_config().model)


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
