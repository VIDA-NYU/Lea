"""The adapter's config loader — reads `config/lea.local.toml` into the *one*
`LeaConfig` (architecture D1·cfg).

There is a single config dataclass for the whole system, and it lives in the
prover (`lea.config.LeaConfig`) because `run_events(config, …)` is typed to it
and the prover must stay an independently-importable library. This module is the
single loader: it parses the TOML the Settings UI edits and builds one of those
objects. We re-export `LeaConfig` so existing `from .config import LeaConfig`
imports keep working.

Provider API keys are **not** stored on the config object — `load_config()`
exports them into `os.environ` (litellm reads them there), so the returned config
holds no secrets and is safe to log or serialize. Previously `scripts/dev.mjs`
injected these into the bundled prover *subprocess*; now the prover runs
in-process, so the adapter's own environment is where they belong.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import tomllib
from pathlib import Path



__all__ = [
    "LeaConfig", "load_config", "configured_provider_keys", "ROOT", "LEGACY_KEY_ENV",
    "PROVIDER_ENDPOINT_ENV", "configured_provider_endpoints", "provider_endpoint",
    "ENDPOINT_ALIASES", "ENVIRONMENT_TABLE", "configured_environment", "environment_name_problem",
    "permission_tier", "PERMISSION_TIERS", "DEFAULT_PERMISSION_TIER", "github_token",
    "write_private_text", "read_config_data",
]

logger = logging.getLogger("lea-interface.config")

ROOT = Path(__file__).resolve().parents[2]


def write_private_text(path: Path, text: str) -> None:
    """Write a config file that may hold secrets: atomically, and owner-only.

    Three problems this closes (AUDIT-2026-07-24 S6):

    * **`write_text` truncates first.** A crash, a full disk, or a killed process
      between the truncate and the write left `lea.local.toml` *empty* — every
      provider key and the GitHub token gone, with no copy anywhere. Writing a temp
      file in the same directory and `os.replace`-ing it is atomic on POSIX: a reader
      sees either the whole old file or the whole new one, never a stump.
    * **Default permissions.** The file was created under the process umask, typically
      `0644` — world-readable on a shared machine, for a file whose entire content is
      credentials. The temp file is chmod'ed before anything is written to it, so the
      secret is never on disk at a wider mode, and `os.replace` carries `0600` onto the
      final path (which also repairs an existing file that predates this).
    * **A half-written file is unparseable**, and every request re-reads this file.

    Same directory, deliberately: `os.replace` is only atomic within a filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())  # the rename is atomic; the CONTENT must be there first
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_config_data(path: Path) -> dict:
    """Parse a config TOML, degrading to `{}` on a missing or malformed file.

    Every reader below re-parses this file on every call, so an unparseable one used to
    turn into a 500 on essentially every endpoint — including the Settings page the
    user would need to repair it (S6). Defaults plus a loud log leave the app usable
    and the problem visible."""
    try:
        return tomllib.loads(path.read_text()) if path.exists() else {}
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        logger.exception(
            "Could not read %s; falling back to defaults. Fix or delete the file to "
            "restore your saved settings.", path,
        )
        return {}


# Legacy flat TOML key -> the LiteLLM env var it maps to. Kept for back-compat;
# any other provider key is stored under its uppercase env var name directly.
LEGACY_KEY_ENV = {
    "google_api_key": "GOOGLE_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
}
_ENV_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*_API_KEY")

# Provider ENDPOINT settings — not secrets, but read from the environment by the
# prover exactly like the keys are (`lea.providers` reads `PORTKEY_BASE_URL`). They
# are stored at the TOML root under their env-var name and exported alongside the
# keys, but reported separately: Settings shows a URL as a URL, never as a masked
# key. Value = the default the prover applies when nothing is configured.
PROVIDER_ENDPOINT_ENV: dict[str, str] = {
    "PORTKEY_BASE_URL": "https://api.portkey.ai/v1",
    # Self-hosted / OpenAI-compatible servers. `hosted_vllm/<model>` is LiteLLM's
    # route for any OpenAI-compatible endpoint (vLLM, SGLang, TGI, LM Studio, a
    # LiteLLM proxy) and needs no key; `ollama/<model>` talks to an Ollama server;
    # `openai/<model>` can be pointed at a compatible server instead of
    # api.openai.com. An empty default means "not configured".
    "HOSTED_VLLM_API_BASE": "",
    "OLLAMA_API_BASE": "http://localhost:11434",
    "OPENAI_BASE_URL": "https://api.openai.com/v1",
}
# A second name some libraries read for the same setting; exported together and
# retracted together.
ENDPOINT_ALIASES: dict[str, tuple[str, ...]] = {
    "OPENAI_BASE_URL": ("OPENAI_API_BASE",),
}

# --- user-set environment variables (`[environment]` table) ---------------------
# Anything else the backend or LiteLLM reads from the environment (LEA_* tunables,
# proxy settings, a provider's extra variables) can be saved here instead of
# exported in a shell. Exported like the keys: the file wins over the shell, and
# clearing retracts. The loader never lets the file set process identity, paths
# the supervisor controls, or the interpreter's own switches.
ENVIRONMENT_TABLE = "environment"
ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")
RESERVED_ENV_NAMES = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "PWD", "LANG", "LC_ALL",
    "VIRTUAL_ENV", "ELAN_HOME",
    "LEA_PROCESS_ROLE", "LEA_SHARED_DATA_DIR", "LEA_CONFIG_FILE", "LEA_DATABASE_FILE",
    "LEA_WEB_DIST", "LEA_ROOT", "LEA_NODE", "LEA_API_BASE_URL", "LEA_API_PORT",
    "LEA_API_DENY_PATHS", "LEA_WORKSPACE_OWNERSHIP_DIR", "LEA_OWNED_PROCESS_DIR",
    "LEA_REQUIRE_SHARED_OWNERSHIP", "LEA_COMPANION_IMPORT_DIR", "LEA_ITEST_PORT",
})
RESERVED_ENV_PREFIXES = ("LD_", "DYLD_", "PYTHON", "UV_", "ELECTRON_")


def environment_name_problem(name: str) -> str | None:
    """Why `name` cannot be a user-set environment variable, or None if it can."""
    if not ENV_NAME_RE.fullmatch(name):
        return "Names are uppercase letters, digits and underscores, like LEA_JOB_TIMEOUT_SECONDS."
    if name in RESERVED_ENV_NAMES or name.startswith(RESERVED_ENV_PREFIXES):
        return f"{name} is managed by the app and cannot be set here."
    if _ENV_KEY_RE.fullmatch(name):
        return f"{name} is an API key; add it under Providers instead."
    if name in PROVIDER_ENDPOINT_ENV or any(name in aliases for aliases in ENDPOINT_ALIASES.values()):
        return f"{name} is a provider server URL; set it under Providers instead."
    return None


def _environment_entries(data: dict) -> dict[str, str]:
    """The user-set variables in the TOML `[environment]` table, reserved names dropped."""
    table = data.get(ENVIRONMENT_TABLE)
    if not isinstance(table, dict):
        return {}
    return {
        name: str(value)
        for name, value in table.items()
        if isinstance(name, str) and environment_name_problem(name) is None
        and value is not None and str(value) != ""
    }


def configured_environment(path: Path | None = None) -> dict[str, str]:
    """User-set environment variables saved in the config file (read-only; no export)."""
    config_path = path or Path(os.environ.get("LEA_CONFIG_FILE",ROOT / "config" / "lea.local.toml"))
    return _environment_entries(read_config_data(config_path))


def _provider_endpoints(data: dict) -> dict[str, str]:
    """The provider endpoint overrides present in the TOML, env-var-keyed."""
    return {
        env_name: str(data[env_name]).strip()
        for env_name in PROVIDER_ENDPOINT_ENV
        if data.get(env_name) and str(data[env_name]).strip()
    }


def configured_provider_endpoints(path: Path | None = None) -> dict[str, str]:
    """Endpoint overrides saved in the config file (read-only; no export)."""
    from lea.config import LeaConfig
    config_path = path or Path(os.environ.get("LEA_CONFIG_FILE",ROOT / "config" / "lea.local.toml"))
    return _provider_endpoints(read_config_data(config_path))


def provider_endpoint(env_name: str, path: Path | None = None) -> dict[str, str | None]:
    """The EFFECTIVE value of a provider endpoint and where it comes from.

    Same precedence the prover sees: the saved file (which `load_config` exports)
    beats a shell export, which beats the built-in default. `source` lets Settings
    show an inherited value as a placeholder rather than as something the user
    typed and can clear.
    """
    default = PROVIDER_ENDPOINT_ENV[env_name]
    saved = configured_provider_endpoints(path).get(env_name)
    if saved:
        return {"value": saved, "source": "config", "default": default}
    exported = (os.environ.get(env_name) or "").strip()
    if exported:
        return {"value": exported, "source": "env", "default": default}
    return {"value": default, "source": "default", "default": default}


def _provider_keys(data: dict) -> dict[str, str]:
    """Collect provider API keys from the TOML, keyed by LiteLLM env-var name.

    Legacy flat keys (`google_api_key`, …) first, then any uppercase
    `*_API_KEY` root entries (additional providers).
    """
    api_keys: dict[str, str] = {}
    for flat_key, env_name in LEGACY_KEY_ENV.items():
        if data.get(flat_key):
            api_keys[env_name] = str(data[flat_key])
    for key, value in data.items():
        if isinstance(key, str) and _ENV_KEY_RE.fullmatch(key) and value:
            api_keys[key] = str(value)
    return api_keys


# The provider keys THIS loader put into `os.environ`, so a later load can take back
# the ones the user removed (AUDIT-2026-07-24 C8). Only keys we exported are ever
# popped: a key the user exported in their own shell is theirs, not ours to unset.
_exported_lock = threading.Lock()
_exported_keys: set[str] = set()


def _export_provider_keys(keys: dict[str, str]) -> None:
    """Publish the saved keys to `os.environ`, retracting any we previously published
    that are no longer saved.

    Exporting was one-way, so "Clear" in Settings removed the key from the file while
    the value stayed live in the environment for the rest of the process — LiteLLM
    kept authenticating with a credential the user believed they had revoked, and the
    Settings UI (which reads the file) reported it as gone. The two disagreed, and the
    environment was the one that mattered."""
    global _exported_keys
    with _exported_lock:
        for stale in _exported_keys - keys.keys():
            os.environ.pop(stale, None)
            logger.info("Provider key %s was cleared; removed it from the environment", stale)
        for env_name, value in keys.items():
            os.environ[env_name] = value
        _exported_keys = set(keys)


def configured_provider_keys(path: Path | None = None) -> dict[str, str]:
    """The provider API keys present in the config file, env-var-keyed.

    Read-only — does **not** export to `os.environ`. The Settings UI uses this to
    show which keys are set (and validate the selected model has one) without
    going through `load_config`. Key-parsing lives only here, so the loader and
    the Settings layer can never disagree about what counts as a configured key.
    """
    from lea.config import LeaConfig
    config_path = path or Path(os.environ.get("LEA_CONFIG_FILE",ROOT / "config" / "lea.local.toml"))
    return _provider_keys(read_config_data(config_path))


# --- approval / permission tier (adapter-only; controls the run's gate) --------
# `permission_tier` is the persisted default for ordinary UI runs:
#   "stepwise" -> gate the mutating tools (bash/write_file/edit_file) — the default
#   "none"     -> fully autonomous (no gate + the non-interactive prompt)
# Extension runs request autonomous execution (D19). Overleaf-linked projects and
# Overleaf-origin chats also skip the gate in LeaChat, independently of execution
# mode: they retain the conversational prompt and subagents under `stepwise`.
# Both policies are adapter concepts, not fields on the prover's LeaConfig.
PERMISSION_TIERS = ("stepwise", "none")
DEFAULT_PERMISSION_TIER = "stepwise"


def permission_tier(path: Path | None = None) -> str:
    """The configured default approval tier for UI runs (read-only).

    Falls back to the default for a missing/unknown value, so a hand-edited or
    stale config can never select a tier the run code doesn't understand.
    """
    from lea.config import LeaConfig
    config_path = path or Path(os.environ.get("LEA_CONFIG_FILE",ROOT / "config" / "lea.local.toml"))
    tier = read_config_data(config_path).get("permission_tier", DEFAULT_PERMISSION_TIER)
    if path is None:
        from .settings_policy import read
        tier=(read() or {}).get('permission_tier',tier)
    return tier if tier in PERMISSION_TIERS else DEFAULT_PERMISSION_TIER


def github_token(path: Path | None = None) -> str | None:
    """The GitHub token for `git push` (D34), or None. Read-only; never exported to
    the environment or returned to the client (Settings reports presence only). The
    git layer injects it into the push URL for a single invocation — it is never
    written to `.git/config`."""
    from lea.config import LeaConfig
    config_path = path or Path(os.environ.get("LEA_CONFIG_FILE",ROOT / "config" / "lea.local.toml"))
    token = read_config_data(config_path).get("github_token")
    return str(token) if token else None


def load_config(path: Path | None = None) -> LeaConfig:
    """Parse `lea.local.toml` into the one `LeaConfig`, exporting keys to env.

    Only the UI-facing knobs come from the file (`model`, `max_turns`,
    `narrate_tool_steps`, `lea_root`, `max_spend_usd`); the agent-internal fields
    (`prompt_variant`, `stream`, `tools`, …) keep their `LeaConfig` defaults.
    Provider keys are exported to `os.environ`, never stored on the object.
    """
    from lea.config import LeaConfig
    config_path = path or Path(os.environ.get("LEA_CONFIG_FILE",ROOT / "config" / "lea.local.toml"))
    data: dict = read_config_data(config_path)
    if path is None:
        from .settings_policy import read
        data.update(read() or {})

    lea_root = data.get("lea_root", "prover")
    resolved_lea_root: Path | None = None
    if lea_root:
        resolved_lea_root = Path(lea_root)
        if not resolved_lea_root.is_absolute():
            resolved_lea_root = ROOT / resolved_lea_root

    max_turns = data.get("max_turns")

    narrate_tool_steps = data.get("narrate_tool_steps", False)
    if not isinstance(narrate_tool_steps, bool):
        raise ValueError("narrate_tool_steps must be a boolean")

    max_spend_usd = data.get("max_spend_usd")
    if max_spend_usd is not None:
        max_spend_usd = float(max_spend_usd)
        if max_spend_usd < 0:
            raise ValueError("max_spend_usd must be greater than or equal to 0")

    # Secrets go to the process environment (litellm reads them there), not onto
    # the config object — so the config is loggable and the prover, running
    # in-process, sees the keys the same way the old subprocess did. Endpoint
    # overrides (a Portkey gateway URL) ride along: the prover reads them from the
    # same place, and clearing one must retract it from the environment too.
    exported = {**_provider_keys(data), **_provider_endpoints(data), **_environment_entries(data)}
    for env_name, aliases in ENDPOINT_ALIASES.items():
        if env_name in exported:
            for alias in aliases:
                exported.setdefault(alias, exported[env_name])
    _export_provider_keys(exported)

    return LeaConfig(
        model=data.get("model", "gemini/gemini-3.1-pro-preview"),
        max_turns=int(max_turns) if max_turns is not None else None,
        narrate_tool_steps=narrate_tool_steps,
        lea_root=resolved_lea_root,
        max_spend_usd=max_spend_usd,
    )


def __getattr__(name):
    if name=='LeaConfig':
        from lea.config import LeaConfig
        return LeaConfig
    raise AttributeError(name)
