"""D1·cfg: the adapter loads the *one* `LeaConfig` (owned by the prover).

There is a single config dataclass for the whole system — `lea.config.LeaConfig`
— and the adapter's `load_config()` is the single loader for it. These tests pin:
the loader builds that exact class (single source of truth), the UI knobs parse,
agent-internal fields keep their defaults, provider keys land in `os.environ` and
are absent from the (loggable, secret-free) config object, and the dead
HTTP/tier fields are gone.
"""

import os
from pathlib import Path

import pytest

import lea.config
import os
from app.config import configured_provider_keys, load_config


def test_load_config_returns_the_one_prover_leaconfig(tmp_path):
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text("")

    config = load_config(config_path)

    # Single source of truth: this IS the prover's dataclass, not an adapter copy.
    assert type(config) is lea.config.LeaConfig


def test_load_config_defaults(tmp_path):
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text("")

    config = load_config(config_path)

    # UI knobs
    assert config.model == "gemini/gemini-3.1-pro-preview"
    assert config.max_turns is None
    assert config.max_spend_usd is None
    assert config.narrate_tool_steps is False
    assert config.lea_root == Path(__file__).resolve().parents[2] / "prover"
    # agent-internal fields keep their LeaConfig defaults (the UI never sets them)
    assert config.prompt_variant == "interactive"
    assert config.stream is True
    assert config.tools is None
    assert config.model_kwargs == {} and config.tool_modules == [] and config.skills == []
    assert config.mcp_servers == {}


def test_load_config_honors_ui_settings(tmp_path):
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(
        """
        model = "o4-mini"
        max_turns = 7
        max_spend_usd = 12.5
        lea_root = "prover"
        narrate_tool_steps = true
        """
    )

    config = load_config(config_path)

    assert config.model == "o4-mini"
    assert config.max_turns == 7
    assert config.max_spend_usd == 12.5
    assert isinstance(config.lea_root, Path) and config.lea_root.name == "prover"
    assert config.narrate_tool_steps is True


def test_ui_model_is_not_overridden_by_process_environment(tmp_path, monkeypatch):
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text('model = "ui-selected-model"\n')
    monkeypatch.setenv("LEA_MODEL", "environment-model")

    config = load_config(config_path)

    assert config.model == "ui-selected-model"


def test_provider_keys_go_to_env_not_onto_the_config_object(tmp_path, monkeypatch):
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(
        """
        google_api_key = "google-secret"
        anthropic_api_key = "anthropic-secret"
        openai_api_key = "openai-secret"
        MISTRAL_API_KEY = "mistral-secret"
        """
    )
    for env in ("GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(env, raising=False)

    config = load_config(config_path)

    # keys land in the process environment (where litellm reads them)
    assert os.environ["GOOGLE_API_KEY"] == "google-secret"
    assert os.environ["ANTHROPIC_API_KEY"] == "anthropic-secret"
    assert os.environ["OPENAI_API_KEY"] == "openai-secret"
    assert os.environ["MISTRAL_API_KEY"] == "mistral-secret"  # generic *_API_KEY too
    # and NOT on the config object — it holds no secrets and is safe to log
    assert not hasattr(config, "api_keys")
    assert not hasattr(config, "google_api_key")


def test_configured_provider_keys_reads_without_exporting(tmp_path, monkeypatch):
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text('google_api_key = "g"\nMISTRAL_API_KEY = "m"\n')
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)

    keys = configured_provider_keys(config_path)

    assert keys == {"GOOGLE_API_KEY": "g", "MISTRAL_API_KEY": "m"}
    # read-only: this helper must not touch the environment
    assert "MISTRAL_API_KEY" not in os.environ


def test_dead_http_and_tier_fields_are_gone(tmp_path):
    config_path = tmp_path / "lea.local.toml"
    # a TOML carrying obsolete keys must load cleanly and simply ignore them
    config_path.write_text(
        """
        model = "o4-mini"
        lea_api_base_url = "http://localhost:9999"
        permission_tier = "stepwise"
        theorem_translation_max_retries = 5
        lea_job_timeout_seconds = 45
        """
    )

    config = load_config(config_path)

    assert config.model == "o4-mini"
    for dead in (
        "lea_api_base_url",
        "lea_api_key",
        "lea_job_timeout_seconds",
        "permission_tier",
        "theorem_translation_max_retries",
        "openai_base_url",
    ):
        assert not hasattr(config, dead), f"{dead} should be gone from LeaConfig"


def test_load_config_rejects_non_boolean_narration_flag(tmp_path):
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text('narrate_tool_steps = "false"\n')

    with pytest.raises(ValueError, match="narrate_tool_steps"):
        load_config(config_path)


def test_load_config_rejects_negative_max_spend(tmp_path):
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text("max_spend_usd = -1\n")

    with pytest.raises(ValueError, match="max_spend_usd"):
        load_config(config_path)


# --- AUDIT-2026-07-24 C8: clearing a key must take it out of the environment ----

def test_clearing_a_key_retracts_it_from_the_environment(tmp_path, monkeypatch):
    """Exporting was one-way: "Clear" removed the key from the file while the value
    stayed live in os.environ for the rest of the process, so LiteLLM kept
    authenticating with a credential the user believed they had revoked — and the
    Settings UI, which reads the file, reported it as gone. The two disagreed, and the
    environment was the one that mattered."""
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    config_path = tmp_path / "lea.local.toml"

    config_path.write_text('MISTRAL_API_KEY = "sk-live"\n')
    load_config(config_path)
    assert os.environ["MISTRAL_API_KEY"] == "sk-live"

    config_path.write_text("")  # the key was cleared in Settings
    load_config(config_path)
    assert "MISTRAL_API_KEY" not in os.environ


def test_a_key_this_loader_never_set_is_left_alone(tmp_path, monkeypatch):
    """Only keys WE exported are retracted. One the user exported in their own shell is
    theirs, and unsetting it would break a working setup for no reason."""
    monkeypatch.setenv("COHERE_API_KEY", "sk-from-the-users-shell")
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text("")

    load_config(config_path)

    assert os.environ["COHERE_API_KEY"] == "sk-from-the-users-shell"


def test_a_rotated_key_replaces_the_old_value(tmp_path, monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    config_path = tmp_path / "lea.local.toml"

    config_path.write_text('MISTRAL_API_KEY = "sk-old"\n')
    load_config(config_path)
    config_path.write_text('MISTRAL_API_KEY = "sk-new"\n')
    load_config(config_path)

    assert os.environ["MISTRAL_API_KEY"] == "sk-new"
