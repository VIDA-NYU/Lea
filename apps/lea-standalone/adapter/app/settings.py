from __future__ import annotations

import os
import re
import socket
import urllib.error
import urllib.request
import json
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import diagnostics

from .config import (
    ROOT, LEGACY_KEY_ENV, configured_provider_keys, load_config,
    permission_tier as config_permission_tier, PERMISSION_TIERS,
    github_token as config_github_token, write_private_text,
)
from . import models_catalog
from . import store


CONFIG_PATH = ROOT / "config" / "lea.local.toml"
API_KEY_FIELDS = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "google": "google_api_key",
}
PROVIDER_LABELS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
}
# Display metadata for the approval tiers the live system actually supports
# (the gate vs. autonomous axis). Keyed by config.PERMISSION_TIERS.
PERMISSION_TIER_DETAILS = {
    "stepwise": {
        "label": "Approve each step",
        "description": "Ask before each agent action (bash / write / edit) during formalization.",
    },
    "none": {
        "label": "Fully autonomous",
        "description": "The agent runs without approval prompts.",
    },
}
# Curated shortlist of current models. Not exhaustive — the Settings model field
# is a searchable combobox that also accepts any custom model ID (provider is
# inferred from the ID prefix), so models not listed here can still be typed in.
MODEL_OPTIONS = [
    {"value": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "family": "openai"},
    {"value": "gpt-5.6-terra", "label": "GPT-5.6 Terra", "family": "openai"},
    {"value": "gpt-5.6-luna", "label": "GPT-5.6 Luna", "family": "openai"},
    {"value": "gpt-5.5", "label": "GPT-5.5", "family": "openai"},
    {"value": "gpt-5.5-mini", "label": "GPT-5.5 Mini", "family": "openai"},
    {"value": "claude-opus-4-8", "label": "Claude Opus 4.8", "family": "anthropic"},
    {"value": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "family": "anthropic"},
    {"value": "claude-haiku-4-5", "label": "Claude Haiku 4.5", "family": "anthropic"},
    {"value": "gemini/gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro", "family": "google"},
]
MODEL_FAMILY_BY_VALUE = {str(option["value"]): str(option["family"]) for option in MODEL_OPTIONS}
KEY_VALIDATORS = {
    "openai": re.compile(r"^sk-[A-Za-z0-9_-]{8,}$"),
    "anthropic": re.compile(r"^sk-ant-.+"),
    "google": re.compile(r"^AIza[A-Za-z0-9_-]{20,}$"),
}
# GitHub PAT shapes (D34): classic `ghp_…`/scoped `gho_`/`ghu_`/`ghs_`/`ghr_`,
# fine-grained `github_pat_…`, or a legacy 40-hex token. Lenient on length so valid
# tokens aren't rejected; this is a format sniff, not authentication (the push proves the token).
GITHUB_TOKEN_RE = re.compile(r"^(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}|[0-9a-fA-F]{40})$")

# The three "first-class" providers — their env vars route to legacy flat TOML
# keys and get live key verification. Everything else (Mistral, HuggingFace, …)
# is stored under its uppercase env var name and saved without live verification.
FAMILY_ENV = {family: env for env, family in {
    "GOOGLE_API_KEY": "google",
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENAI_API_KEY": "openai",
}.items()}
ENV_FAMILY = {env: family for family, env in FAMILY_ENV.items()}

# Display labels for common env vars; unknown ones derive a label from the name.
KNOWN_KEY_LABELS = {
    "OPENAI_API_KEY": "OpenAI",
    "ANTHROPIC_API_KEY": "Anthropic",
    "GOOGLE_API_KEY": "Google",
    "GEMINI_API_KEY": "Gemini",
    "MISTRAL_API_KEY": "Mistral",
    "HUGGINGFACE_API_KEY": "HuggingFace",
    "COHERE_API_KEY": "Cohere",
    "DEEPSEEK_API_KEY": "DeepSeek",
    "GROQ_API_KEY": "Groq",
    "TOGETHERAI_API_KEY": "Together AI",
    "XAI_API_KEY": "xAI",
    "PERPLEXITYAI_API_KEY": "Perplexity",
}


def _key_label(env_name: str) -> str:
    if env_name in KNOWN_KEY_LABELS:
        return KNOWN_KEY_LABELS[env_name]
    return env_name.removesuffix("_API_KEY").replace("_", " ").title() or env_name


class SettingsValidationError(ValueError):
    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class ApiKeyUpdate:
    value: str | None = None
    clear: bool = False


def settings_payload(path: Path | None = None) -> dict[str, Any]:
    config = load_config(path)
    return {
        "model": config.model,
        "max_turns": config.max_turns,
        "max_spend_usd": config.max_spend_usd,
        "current_spend_usd": current_spend_usd(),
        "api_keys": _api_keys_payload(configured_provider_keys(path)),
        "model_options": MODEL_OPTIONS,
        "permission_tier": config_permission_tier(path),
        "permission_tiers": [
            {"value": tier, **PERMISSION_TIER_DETAILS[tier]} for tier in PERMISSION_TIERS
        ],
        # Presence-only, like the provider keys — the raw token never reaches the client.
        "github_token": _masked_key(config_github_token(path)),
    }


def _api_keys_payload(keys: dict[str, str]) -> dict[str, Any]:
    """Env-var-keyed key status. Always includes the three first-class providers
    (so they show in the UI) plus any additional providers already configured."""
    env_names = list(ENV_FAMILY.keys())
    for env in keys:
        if env not in env_names:
            env_names.append(env)
    return {
        env: {**_masked_key(keys.get(env)), "label": _key_label(env)}
        for env in env_names
    }


def model_catalog() -> list[dict[str, str]]:
    """Full LiteLLM chat-model catalog for the searchable picker; falls back to
    the curated shortlist if LiteLLM is unavailable."""
    catalog = models_catalog.list_chat_models()
    if catalog:
        return catalog
    return [
        {"value": str(o["value"]), "label": str(o["label"]), "provider": str(o["family"])}
        for o in MODEL_OPTIONS
    ]


def model_catalog_warnings() -> list[dict]:
    """F3: non-empty when the picker is showing the curated FALLBACK list rather
    than the live catalog. The fallback is a handful of models against LiteLLM's
    ~2k, and it also disables the per-model key prompt — a user hunting for a model
    that isn't there deserves to know it's a stale list, not an exhaustive one."""
    reason = models_catalog.unavailable_reason()
    if not reason or models_catalog.list_chat_models():
        return []
    return [diagnostics.resolve(
        "degraded", "settings.catalog_unavailable",
        f"The full model catalog could not be loaded ({reason}); showing a built-in list.",
        source="settings",
    )]


def _required_env_keys(model: str) -> list[str]:
    """The env vars that can authenticate `model` — any ONE of them satisfies it.

    A provider LiteLLM recognises is authoritative, *including* when it needs no
    single env var (Vertex/Bedrock/Ollama authenticate through a credential chain).
    That empty answer is returned as-is rather than falling through to the family
    heuristic, which would invent a requirement the provider doesn't have. The
    heuristic is only for models LiteLLM can't place at all."""
    if models_catalog.is_available():
        requirements = models_catalog.requirements_for(model)
        if requirements.get("provider"):
            return [str(key) for key in (requirements.get("required_keys") or [])]
    family = _model_family(model)
    if family and family in FAMILY_ENV:
        return [FAMILY_ENV[family]]
    return []


def _key_available(env: str, saved: set[str]) -> bool:
    """Whether `env` can authenticate a run: saved in the config file, or already
    present in the process environment.

    Both count, because both work — `load_config` exports the saved keys into
    `os.environ` and LiteLLM reads them from there, so a key exported in the user's
    shell authenticates exactly as well as one typed into Settings. Counting only the
    file would refuse a model that demonstrably runs.

    Pairing this with the now environment-independent `required_keys` is what makes
    the check coherent (AUDIT-2026-07-24 C11). Previously the *requirement* was
    derived from the environment while *satisfaction* was judged against the file, so
    an exported key erased the requirement instead of meeting it — and every model
    passed."""
    return env in saved or bool(os.environ.get(env))


def model_requirements(model: str, path: Path | None = None) -> dict[str, Any]:
    """Which key(s) a model needs and whether they're configured — drives the
    dynamic API-key prompt in Settings."""
    required = _required_env_keys(model)
    saved = set(configured_provider_keys(path).keys())
    provider = models_catalog.provider_for(model) or _model_family(model)
    return {
        "model": model,
        "provider": provider,
        "required_keys": [
            {"env": env, "label": _key_label(env), "configured": _key_available(env, saved)}
            for env in required
        ],
        "satisfied": (not required) or any(_key_available(env, saved) for env in required),
    }


def validate_configured_model(model: str, path: Path | None = None) -> str:
    """Validate a model selected for a run against the currently configured keys.

    The Settings UI normally performs the same check while persisting its default,
    but an explicit per-run model must be safe even when a client bypasses that UI.
    Return the normalized model so callers can validate and snapshot it in one step.
    """
    normalized = str(model).strip()
    if not normalized:
        raise SettingsValidationError("model must not be empty", "model")
    requirements = model_requirements(normalized, path)
    if not requirements["satisfied"]:
        required = requirements["required_keys"][0]
        raise SettingsValidationError(
            f"An API key ({required['label']}) is required before starting a run with this model.",
            f"api_keys.{required['env']}",
        )
    return normalized


def update_settings(values: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    config_path = path or CONFIG_PATH
    current_config = load_config(config_path)
    updates: dict[str, Any] = {}

    if "model" in values and values["model"] is not None:
        model = str(values["model"]).strip()
        if not model:
            raise ValueError("model must not be empty")
        updates["model"] = model

    if "max_turns" in values:
        max_turns = values["max_turns"]
        if max_turns is None:
            updates["max_turns"] = None
        else:
            max_turns_int = int(max_turns)
            if max_turns_int < 1:
                raise ValueError("max_turns must be at least 1")
            updates["max_turns"] = max_turns_int

    if "permission_tier" in values and values["permission_tier"] is not None:
        tier = str(values["permission_tier"])
        if tier not in PERMISSION_TIERS:
            raise ValueError(
                f"permission_tier must be one of {', '.join(PERMISSION_TIERS)}"
            )
        updates["permission_tier"] = tier

    # GitHub token (D34): same {value, clear} shape as a provider key, redacted on read.
    github_update = values.get("github_token")
    if isinstance(github_update, dict):
        if github_update.get("clear"):
            updates["github_token"] = None
        else:
            gh_value = github_update.get("value")
            if gh_value is not None:
                gh_value = str(gh_value).strip()
                if gh_value:
                    if not GITHUB_TOKEN_RE.fullmatch(gh_value):
                        raise SettingsValidationError(
                            "That doesn't look like a GitHub token (expected ghp_…, github_pat_…, or a 40-char hex).",
                            "github_token",
                        )
                    updates["github_token"] = gh_value

    if "max_spend_usd" in values:
        max_spend_usd = values["max_spend_usd"]
        if max_spend_usd is None:
            updates["max_spend_usd"] = None
        else:
            max_spend_float = float(max_spend_usd)
            if max_spend_float < 0:
                raise ValueError("max_spend_usd must be greater than or equal to 0")
            updates["max_spend_usd"] = max_spend_float

    # api_keys is keyed by LiteLLM env var name (OPENAI_API_KEY, MISTRAL_API_KEY,
    # …). The three first-class providers route to their legacy flat TOML keys
    # and get live verification; any other provider is saved under its env var
    # name as-is.
    api_key_updates = values.get("api_keys") or {}
    if not isinstance(api_key_updates, dict):
        raise ValueError("api_keys must be an object")
    current_keys = configured_provider_keys(config_path)
    selected_model = str(updates.get("model", current_config.model))
    for env_name, raw_update in api_key_updates.items():
        if raw_update is None:
            continue
        if not isinstance(raw_update, dict):
            raise ValueError(f"api_keys.{env_name} must be an object")
        env_name = str(env_name)
        family = ENV_FAMILY.get(env_name)
        toml_key = API_KEY_FIELDS[family] if family else env_name
        if raw_update.get("clear"):
            # D3: a key an MCP server declares (`env_from` / `api_key_name`) is still in
            # use. Clearing it makes that server fail on FIRST USE with a 401 — a delayed,
            # confusing failure far from the action that caused it. Say so while the user
            # still holds the decision; `force` is the explicit "yes, anyway".
            if not values.get("force_clear_keys"):
                from . import store

                users = sorted(set(store.mcp_key_requirements().get(env_name, [])))
                if users:
                    raise SettingsValidationError(
                        f"{env_name} is still used by {', '.join(users)}. Remove it from "
                        f"those servers first, or clear it anyway to disable them.",
                        field=f"api_keys.{env_name}",
                    )
            updates[toml_key] = None
            continue
        value = raw_update.get("value")
        if value is None:
            continue
        value = str(value).strip()
        if not value:
            continue
        # A NUL cannot exist in a POSIX environment variable, and `load_config` exports
        # every saved key into `os.environ` for LiteLLM to read — so a key containing
        # one would persist to the file cleanly (the writer escapes it) and then raise
        # `ValueError: embedded null byte` on the next load, breaking every request.
        # Rejected at the boundary rather than escaped, because a credential with a NUL
        # in it is not a credential; the only way to get one here is a mangled paste.
        if "\x00" in value:
            raise SettingsValidationError(
                f"That {_key_label(env_name)} key contains a null byte — it looks like "
                "a copy/paste problem. Copy the key again.",
                f"api_keys.{env_name}",
            )
        if family:
            _validate_api_key_format(family, value)
            _verify_api_key_credentials(family, value, selected_model)
        elif not re.fullmatch(r"[A-Z][A-Z0-9_]*_API_KEY", env_name):
            raise SettingsValidationError(
                f"{env_name} is not a recognized API key name.",
                f"api_keys.{env_name}",
            )
        updates[toml_key] = value

    _validate_selected_model_has_key(str(current_config.model), current_keys, updates)
    _write_toml_updates(config_path, updates)
    return settings_payload(config_path)


def current_spend_usd() -> float:
    """Total persisted spend. Reads the dedicated scalar aggregate rather than
    `usage_stats()["global"]`, which was a sum over a 100-row page and therefore
    under-reported — see `store.total_spend_usd` (AUDIT-2026-07-24 C1)."""
    return store.total_spend_usd()


def spend_limit_reached(max_spend_usd: float | None, pending_cost_usd: float | None = None) -> bool:
    if max_spend_usd is None:
        return False
    return current_spend_usd() + float(pending_cost_usd or 0) >= float(max_spend_usd)


def _masked_key(value: str | None) -> dict[str, Any]:
    if not value:
        return {"configured": False, "last4": None}
    return {"configured": True, "last4": value[-4:] if len(value) >= 4 else value}


def _configured_env_keys_after(current_keys: set[str], updates: dict[str, Any]) -> set[str]:
    """Env-var key names that will be configured once `updates` are written."""
    keys = set(current_keys)
    for toml_key, value in updates.items():
        if toml_key in LEGACY_KEY_ENV:
            env = LEGACY_KEY_ENV[toml_key]
        elif re.fullmatch(r"[A-Z][A-Z0-9_]*_API_KEY", toml_key):
            env = toml_key
        else:
            continue
        keys.add(env) if value else keys.discard(env)
    return keys


def _validate_selected_model_has_key(
    current_model: str, current_keys: set[str], updates: dict[str, Any]
) -> None:
    model = str(updates.get("model", current_model))
    required = _required_env_keys(model)
    if not required:
        return
    # Same "saved OR already exported" rule `model_requirements` applies, so Settings
    # can't refuse to save a model that would in fact run (AUDIT-2026-07-24 C11).
    configured = _configured_env_keys_after(current_keys, updates)
    if any(_key_available(env, configured) for env in required):
        return
    env = required[0]
    raise SettingsValidationError(
        f"An API key ({_key_label(env)}) is required before saving this model.",
        f"api_keys.{env}",
    )


def _model_family(model: str) -> str | None:
    if model in MODEL_FAMILY_BY_VALUE:
        return MODEL_FAMILY_BY_VALUE[model]
    normalized = model.lower()
    if normalized.startswith(("gpt-", "openai/")) or re.match(r"^o\d", normalized):
        return "openai"
    if normalized.startswith(("claude-", "anthropic/")):
        return "anthropic"
    if normalized.startswith(("gemini", "google/")):
        return "google"
    return None


def _validate_api_key_format(family: str, value: str) -> None:
    validator = KEY_VALIDATORS.get(family)
    if validator and not validator.match(value):
        raise SettingsValidationError(
            f"The {_provider_label(family)} API key does not look valid. Check the key and try again.",
            f"api_keys.{family}",
        )


def _verify_api_key_credentials(
    family: str,
    value: str,
    model: str | None = None,
) -> None:
    request = _provider_verification_request(family, value, model)
    if request is None:
        return
    try:
        with urllib.request.urlopen(request, timeout=6, context=_ssl_context()) as response:
            if response.status >= 400:
                raise SettingsValidationError(
                    f"Could not verify the {_provider_label(family)} API key.",
                    f"api_keys.{family}",
                )
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        if exc.code in {401, 403}:
            raise SettingsValidationError(
                _provider_auth_error_message(family, detail),
                f"api_keys.{family}",
            ) from exc
        if family == "anthropic" and _looks_like_model_error(detail):
            raise SettingsValidationError(
                f"Anthropic rejected the selected model: {detail}",
                "model",
            ) from exc
        raise SettingsValidationError(
            f"Could not verify the {_provider_label(family)} API key. Provider returned HTTP {exc.code}: {detail}",
            f"api_keys.{family}",
        ) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise SettingsValidationError(
            f"Could not verify the {_provider_label(family)} API key. Check your connection and try again.",
            f"api_keys.{family}",
        ) from exc


def _provider_verification_request(
    family: str,
    value: str,
    model: str | None = None,
) -> urllib.request.Request | None:
    if family == "openai":
        return urllib.request.Request(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {value}"},
            method="GET",
        )
    if family == "anthropic":
        body = json.dumps(
            {
                "model": _anthropic_verification_model(model),
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
        ).encode("utf-8")
        return urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "x-api-key": value,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
    if family == "google":
        return urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={value}",
            method="GET",
        )
    return None


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _anthropic_verification_model(model: str | None) -> str:
    if model and _model_family(model) == "anthropic":
        return model
    return "claude-sonnet-4-6"


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    raw = exc.read().decode("utf-8", errors="replace").strip()
    if not raw:
        return exc.reason or "no response body"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:500]
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        error_type = error.get("type")
        if message and error_type:
            return f"{error_type}: {message}"
        if message:
            return str(message)
    return raw[:500]


def _provider_auth_error_message(family: str, detail: str) -> str:
    if detail and detail != "no response body":
        return f"The {_provider_label(family)} API key was rejected by the provider: {detail}"
    return f"The {_provider_label(family)} API key was rejected by the provider."


def _looks_like_model_error(detail: str) -> bool:
    normalized = detail.lower()
    return "model" in normalized and (
        "not found" in normalized
        or "does not exist" in normalized
        or "invalid" in normalized
        or "not supported" in normalized
    )


def _provider_label(family: str) -> str:
    return PROVIDER_LABELS.get(family, family.title())


def _write_toml_updates(path: Path, updates: dict[str, Any]) -> None:
    if not updates:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    updated_keys: set[str] = set()
    key_pattern = re.compile(r"^(\s*)([A-Za-z0-9_]+)(\s*=\s*)(.*)$")
    table_pattern = re.compile(r"^\s*\[[^\]]+\]\s*(#.*)?$")
    next_lines: list[str] = []
    in_root = True
    first_table_index: int | None = None

    for line in lines:
        if table_pattern.match(line):
            in_root = False
            if first_table_index is None:
                first_table_index = len(next_lines)
        match = key_pattern.match(line)
        if in_root and match and match.group(2) in updates:
            key = match.group(2)
            updated_keys.add(key)
            value = updates[key]
            if value is None:
                continue
            next_lines.append(f"{match.group(1)}{key}{match.group(3)}{_toml_scalar(value)}")
        else:
            next_lines.append(line)

    missing_lines = [
        f"{key} = {_toml_scalar(updates[key])}"
        for key in updates
        if key not in updated_keys and updates[key] is not None
    ]
    if missing_lines:
        if first_table_index is None:
            if next_lines and next_lines[-1].strip():
                next_lines.append("")
            next_lines.extend(missing_lines)
        else:
            insertion = list(missing_lines)
            if first_table_index > 0 and next_lines[first_table_index - 1].strip():
                insertion.insert(0, "")
            if first_table_index < len(next_lines) and next_lines[first_table_index].strip():
                insertion.append("")
            next_lines[first_table_index:first_table_index] = insertion

    # Atomic, owner-only: this file is the only copy of every provider key and the
    # GitHub token (S6).
    write_private_text(path, "\n".join(next_lines).rstrip() + "\n")


# TOML basic strings cannot contain a raw control character, and a literal newline
# ends the string outright. Escaping only `\` and `"` meant a value carrying either —
# a paste accident, or any client posting straight to `PUT /api/settings`, where only
# the three first-class providers are format-validated and everything matching
# `*_API_KEY` is stored verbatim — produced a file `tomllib` then refused to parse on
# EVERY subsequent read (AUDIT-2026-07-24 S6).
_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def _escape_toml_basic(text: str) -> str:
    out: list[str] = []
    for char in text:
        if char in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return "".join(out)


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    return '"' + _escape_toml_basic(str(value)) + '"'
