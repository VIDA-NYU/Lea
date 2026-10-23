"""Public SQL/admission contract. Private operations are in app.routes.settings."""
from __future__ import annotations
from .. import peripheral_views
from .. import workspace_commands
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from .. import admission_policy as settings_service
router = APIRouter()
logger = logging.getLogger('lea-interface.settings')

class ApiKeyUpdateRequest(BaseModel):
    value: str | None = None
    clear: bool = False

class SettingsRequest(BaseModel):
    model: str | None = None
    permission_tier: str | None = None
    max_turns: int | None = None
    max_spend_usd: float | None = None
    api_keys: dict[str, ApiKeyUpdateRequest] | None = None
    provider_endpoints: dict[str, ApiKeyUpdateRequest] | None = None
    force_clear_keys: bool = False
    github_token: ApiKeyUpdateRequest | None = None

@router.get('/api/settings')
@peripheral_views.view('get_settings')
def get_settings() -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.get('/api/models')
@peripheral_views.view('models')
def models() -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.post('/api/models/refresh', status_code=202)
@workspace_commands.intake('peripheral.refresh_models')
def refresh_models() -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/models/requirements')
def model_requirements(model: str) -> dict:
    """Which API key(s) a model needs and whether they're configured."""
    current = get_settings()
    catalog = models()
    record = next((item for item in catalog.get('models', []) if item.get('value') == model), {})
    family = record.get('provider') or ('portkey' if model.startswith(('portkey/', '@')) else 'openai' if model.startswith(('openai/', 'gpt-', 'o1', 'o3', 'o4', 'responses/')) else 'anthropic' if model.startswith(('anthropic/', 'claude-')) else 'gemini' if model.startswith(('gemini/', 'google/')) else model.split('/')[0])
    from ..admission_policy import _PROVIDER_KEYS
    required = _PROVIDER_KEYS.get('gemini' if family == 'google' else family, [family.upper().replace('-', '_') + '_API_KEY'] if family and '/' in model else [])
    keys = [{'env': key, 'label': settings_service._key_label(key), 'configured': bool(current['api_keys'].get(key, {}).get('configured'))} for key in required]
    return {'model': model, 'provider': family, 'required_keys': keys, 'satisfied': not required or any((k['configured'] for k in keys))}

class CredentialReference(BaseModel):
    ticket: str

@workspace_commands.intake('settings.apply')
def apply_settings(request: CredentialReference):
    raise RuntimeError('An intake contract cannot execute')

@router.put('/api/settings', status_code=202)
def update_settings(request: SettingsRequest) -> dict:
    try:
        from .. import settings_policy
        ticket = settings_policy.stage_credentials(request.model_dump(exclude_unset=True))
        try:
            return apply_settings(CredentialReference(ticket=ticket))
        except BaseException:
            (settings_policy.db.DB_PATH.parent / 'credential-intake' / ticket).unlink(missing_ok=True)
            raise
    except settings_service.SettingsValidationError as exc:
        logger.warning('Settings validation failed: field=%s message=%s', exc.field, str(exc))
        raise HTTPException(status_code=422, detail={'message': str(exc), 'field': exc.field}) from exc
    except ValueError as exc:
        logger.warning('Settings update failed: %s', str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc
