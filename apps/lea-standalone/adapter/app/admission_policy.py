"""Non-secret policy and published provider availability for admission."""
from types import SimpleNamespace
from . import db,settings_policy

class SettingsValidationError(ValueError):
    def __init__(self,message,field=None):super().__init__(message);self.field=field


def load_config():
    value=settings_policy.read() or {}
    return SimpleNamespace(model=value.get('model','gemini/gemini-3.1-pro-preview'),max_turns=value.get('max_turns'),
        max_spend_usd=value.get('max_spend_usd'),max_cost_usd=None)


def permission_tier():return (settings_policy.read() or {}).get('permission_tier','stepwise')


def spend_limit_reached(limit):
    if limit is None:return False
    from .store import total_spend_usd
    return total_spend_usd()>=limit


def _key_label(name):
    return {'OPENAI_API_KEY':'OpenAI','ANTHROPIC_API_KEY':'Anthropic','GOOGLE_API_KEY':'Google','PORTKEY_API_KEY':'Portkey'}.get(name,name)

_PROVIDER_KEYS={'openai':['OPENAI_API_KEY'],'anthropic':['ANTHROPIC_API_KEY'],'gemini':['GOOGLE_API_KEY'],'portkey':['PORTKEY_API_KEY']}


def admission_enabled():
    from .process_state import health
    return health().get('capabilities',{}).get('lea_status',{}).get('admission_enabled',False)
