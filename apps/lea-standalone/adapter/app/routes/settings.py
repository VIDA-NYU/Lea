"""Settings + model-catalog endpoints. Thin wrappers over `app.settings`, which
owns the config read/write and provider-key validation."""

from __future__ import annotations

from .. import peripheral_views
from .. import workspace_commands

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
@peripheral_views.query('get_settings')
def get_settings() -> dict:
    return settings_service.settings_payload()


@router.get("/api/models")
@peripheral_views.query('models')
def models() -> dict:
    from ..catalog_service import service
    from ..config import load_config
    return service().payload(load_config().model)


@router.post("/api/models/refresh", status_code=202)
@workspace_commands.register('peripheral.refresh_models')
def refresh_models() -> dict:
    from ..catalog_service import service
    from ..config import load_config
    service().request_refresh()
    return service().payload(load_config().model)


@router.get("/api/models/requirements")
def model_requirements(model: str) -> dict:
    """Which API key(s) a model needs and whether they're configured."""
    current=get_settings()
    catalog=models()
    record=next((item for item in catalog.get('models',[]) if item.get('value')==model),{})
    family=record.get('provider') or ('portkey' if model.startswith(('portkey/','@')) else
        'openai' if model.startswith(('openai/','gpt-','o1','o3','o4','responses/')) else
        'anthropic' if model.startswith(('anthropic/','claude-')) else 'gemini' if model.startswith(('gemini/','google/')) else model.split('/')[0])
    from ..models_catalog import _PROVIDER_KEYS
    required=_PROVIDER_KEYS.get('gemini' if family=='google' else family,
        [family.upper().replace('-','_')+'_API_KEY'] if family and '/' in model else [])
    keys=[{'env':key,'label':settings_service._key_label(key),'configured':bool(current['api_keys'].get(key,{}).get('configured'))} for key in required]
    return {'model':model,'provider':family,'required_keys':keys,'satisfied':not required or any(k['configured'] for k in keys)}


class CredentialReference(BaseModel):
    ticket:str


@workspace_commands.register('settings.apply')
def apply_settings(request:CredentialReference):
    from .. import settings_policy,peripheral_views
    values,path=settings_policy.consume(request.ticket)
    try:
        result=settings_service.update_settings(values,database=True)
        peripheral_views.capture('get_settings',{})
        return result
    except (settings_service.SettingsValidationError,ValueError) as exc:
        # Providers may echo request data. Never persist submitted secrets in an
        # error receipt, even though the infrastructure ticket is private.
        message=str(exc)
        def strings(value):
            if isinstance(value,dict):
                for item in value.values():yield from strings(item)
            elif isinstance(value,str) and len(value)>4:yield value
        for secret in strings({k:v for k,v in values.items() if k in {'api_keys','github_token'}}):message=message.replace(secret,'[redacted]')
        raise HTTPException(422,detail={'message':message,'field':getattr(exc,'field',None)}) from exc
    finally:path.unlink(missing_ok=True)


@router.put("/api/settings",status_code=202)
def update_settings(request: SettingsRequest) -> dict:
    try:
        from .. import settings_policy
        ticket=settings_policy.stage_credentials(request.model_dump(exclude_unset=True))
        try:return apply_settings(CredentialReference(ticket=ticket))
        except BaseException:
            (settings_policy.db.DB_PATH.parent/'credential-intake'/ticket).unlink(missing_ok=True)
            raise
    except settings_service.SettingsValidationError as exc:
        logger.warning("Settings validation failed: field=%s message=%s", exc.field, str(exc))
        raise HTTPException(status_code=422, detail={"message": str(exc), "field": exc.field}) from exc
    except ValueError as exc:
        logger.warning("Settings update failed: %s", str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc
