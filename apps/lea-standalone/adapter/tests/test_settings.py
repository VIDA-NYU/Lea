import json
from io import BytesIO
import urllib.error

import pytest

from app import db
from app import settings as settings_service


def test_settings_payload_masks_api_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(
        """
        model = "gpt-4o"
        max_turns = 12
        max_spend_usd = 20.0
        openai_api_key = "sk-secret-1234"
        """
    )

    payload = settings_service.settings_payload(config_path)

    assert payload["model"] == "gpt-4o"
    assert payload["max_turns"] == 12
    assert payload["max_spend_usd"] == 20.0
    assert payload["api_keys"]["OPENAI_API_KEY"] == {"configured": True, "last4": "1234", "label": "OpenAI"}
    assert payload["api_keys"]["ANTHROPIC_API_KEY"] == {"configured": False, "last4": None, "label": "Anthropic"}
    assert {option["value"] for option in payload["model_options"]} >= {
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    }


def test_update_settings_preserves_unrelated_config_and_updates_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(settings_service, "_verify_api_key_credentials", lambda family, value, model=None: None)
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(
        """
        lea_root = "prover"
        model = "gpt-4o"
        max_turns = 12
        openai_api_key = "old-secret"
        """
    )

    payload = settings_service.update_settings(
        {
            "model": "claude-sonnet-4-6",
            "max_turns": 30,
            "max_spend_usd": 9.5,
            "api_keys": {
                "OPENAI_API_KEY": {"clear": True},
                "ANTHROPIC_API_KEY": {"value": "sk-ant-secret123456"},
            },
        },
        config_path,
    )
    text = config_path.read_text()

    assert payload["model"] == "claude-sonnet-4-6"
    assert payload["max_turns"] == 30
    assert 'lea_root = "prover"' in text
    assert 'model = "claude-sonnet-4-6"' in text
    assert "max_turns = 30" in text
    assert "max_spend_usd = 9.5" in text
    assert "openai_api_key" not in text
    assert 'anthropic_api_key = "sk-ant-secret123456"' in text


def test_update_settings_rejects_selected_model_without_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    # The required-key name comes from LiteLLM (env-dependent); make the test
    # deterministic regardless of the developer's shell.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(
        """
        model = "gpt-4o"
        max_turns = 12
        """
    )

    try:
        settings_service.update_settings({"model": "claude-sonnet-4-6"}, config_path)
    except settings_service.SettingsValidationError as exc:
        assert str(exc) == "An API key (Anthropic) is required before saving this model."
        assert exc.field == "api_keys.ANTHROPIC_API_KEY"
    else:
        raise AssertionError("Expected SettingsValidationError")


def test_validate_configured_model_normalizes_explicit_run_model(tmp_path, monkeypatch):
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text("")
    monkeypatch.setattr(settings_service, "_required_env_keys", lambda model: [])

    model = settings_service.validate_configured_model(
        "  custom/provider-model  ",
        config_path,
    )

    assert model == "custom/provider-model"


def test_validate_configured_model_rejects_missing_provider_key(tmp_path, monkeypatch):
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text("")
    monkeypatch.setattr(
        settings_service,
        "_required_env_keys",
        lambda model: ["EXAMPLE_API_KEY"],
    )

    try:
        settings_service.validate_configured_model("example/model", config_path)
    except settings_service.SettingsValidationError as exc:
        assert str(exc) == "An API key (Example) is required before starting a run with this model."
        assert exc.field == "api_keys.EXAMPLE_API_KEY"
    else:
        raise AssertionError("Expected SettingsValidationError")


def test_update_settings_rejects_malformed_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(
        """
        model = "gpt-4o"
        max_turns = 12
        """
    )

    try:
        settings_service.update_settings(
            {
                "model": "gpt-4o",
                "api_keys": {"OPENAI_API_KEY": {"value": "not-a-real-key"}},
            },
            config_path,
        )
    except settings_service.SettingsValidationError as exc:
        assert str(exc) == "The OpenAI API key does not look valid. Check the key and try again."
        assert exc.field == "api_keys.openai"
    else:
        raise AssertionError("Expected SettingsValidationError")


def test_update_settings_allows_anthropic_prefix_then_defers_to_provider_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    seen = {}

    def verify_key(family, value, model=None):
        seen["family"] = family
        seen["value"] = value
        seen["model"] = model

    monkeypatch.setattr(settings_service, "_verify_api_key_credentials", verify_key)
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(
        """
        model = "gpt-4o"
        max_turns = 12
        """
    )

    settings_service.update_settings(
        {
            "model": "claude-sonnet-4-6",
            "api_keys": {"ANTHROPIC_API_KEY": {"value": "sk-ant-api03-test.value/with+chars"}},
        },
        config_path,
    )

    assert seen == {
        "family": "anthropic",
        "value": "sk-ant-api03-test.value/with+chars",
        "model": "claude-sonnet-4-6",
    }
    assert 'anthropic_api_key = "sk-ant-api03-test.value/with+chars"' in config_path.read_text()


def test_anthropic_verification_uses_messages_endpoint_with_selected_model(tmp_path):
    request = settings_service._provider_verification_request(
        "anthropic",
        "sk-ant-api03-test",
        "claude-sonnet-4-6",
    )

    assert request is not None
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.get_method() == "POST"
    assert request.headers["X-api-key"] == "sk-ant-api03-test"
    assert request.headers["Anthropic-version"] == "2023-06-01"
    assert request.headers["Content-type"] == "application/json"
    body = json.loads(request.data.decode("utf-8"))
    assert body == {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }


def test_anthropic_model_error_is_reported_as_model_error(monkeypatch):
    body = json.dumps(
        {
            "error": {
                "type": "invalid_request_error",
                "message": "model: claude-missing does not exist",
            }
        }
    ).encode("utf-8")

    def fail_with_model_error(request, timeout=None, context=None):
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            BytesIO(body),
        )

    monkeypatch.setattr(settings_service.urllib.request, "urlopen", fail_with_model_error)

    try:
        settings_service._verify_api_key_credentials(
            "anthropic",
            "sk-ant-api03-test",
            "claude-missing",
        )
    except settings_service.SettingsValidationError as exc:
        assert exc.field == "model"
        assert "Anthropic rejected the selected model" in str(exc)
        assert "claude-missing" in str(exc)
    else:
        raise AssertionError("Expected SettingsValidationError")


def test_update_settings_rejects_provider_rejected_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(
        """
        model = "gpt-4o"
        max_turns = 12
        """
    )

    def reject_key(family, value, model=None):
        raise settings_service.SettingsValidationError(
            "The OpenAI API key was rejected by the provider.",
            "api_keys.openai",
        )

    monkeypatch.setattr(settings_service, "_verify_api_key_credentials", reject_key)

    try:
        settings_service.update_settings(
            {
                "model": "gpt-4o",
                "api_keys": {"OPENAI_API_KEY": {"value": "sk-validlooking123456"}},
            },
            config_path,
        )
    except settings_service.SettingsValidationError as exc:
        assert str(exc) == "The OpenAI API key was rejected by the provider."
        assert exc.field == "api_keys.openai"
    else:
        raise AssertionError("Expected SettingsValidationError")


# ── GitHub token (D34) — redacted like a provider key ──────────────────────────

def _gh(n):
    return "ghp_" + ("a" * 36) if n == 1 else "ghp_" + ("b" * 36)


def test_settings_payload_masks_github_token(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(f'model = "gpt-4o"\nopenai_api_key = "sk-x"\ngithub_token = "{_gh(1)}"\n')

    payload = settings_service.settings_payload(config_path)
    assert payload["github_token"] == {"configured": True, "last4": "aaaa"}


def test_settings_payload_github_token_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text('model = "gpt-4o"\nopenai_api_key = "sk-x"\n')
    assert settings_service.settings_payload(config_path)["github_token"] == {"configured": False, "last4": None}


def test_update_settings_sets_then_clears_github_token(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text('model = "gpt-4o"\nopenai_api_key = "sk-x"\n')

    payload = settings_service.update_settings({"github_token": {"value": _gh(2)}}, config_path)
    assert payload["github_token"]["configured"] is True
    assert _gh(2) in config_path.read_text()  # persisted to the TOML

    payload = settings_service.update_settings({"github_token": {"clear": True}}, config_path)
    assert payload["github_token"] == {"configured": False, "last4": None}
    assert "github_token" not in config_path.read_text()


def test_update_settings_rejects_malformed_github_token(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text('model = "gpt-4o"\nopenai_api_key = "sk-x"\n')
    try:
        settings_service.update_settings({"github_token": {"value": "not-a-token"}}, config_path)
    except settings_service.SettingsValidationError as exc:
        assert exc.field == "github_token"
    else:
        raise AssertionError("Expected SettingsValidationError")


# --- AUDIT-2026-07-24 C11: requirements are a property of the model -----------
# `required_keys` came from litellm.validate_environment()["missing_keys"], which is
# computed against os.environ. A key exported in the shell made the list come back
# EMPTY, so the caller's "is it configured?" check had nothing to check and
# `satisfied` was unconditionally True — a guard that passed every model, with an
# answer that depended on the process it ran in.

def _blank_config(tmp_path):
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text("")
    return config_path


@pytest.mark.parametrize(
    "model,expected",
    [
        ("gpt-5.6-sol", "OPENAI_API_KEY"),
        ("gpt-5.5", "OPENAI_API_KEY"),
        ("claude-opus-4-8", "ANTHROPIC_API_KEY"),
        ("gemini/gemini-3.1-pro-preview", "GEMINI_API_KEY"),
        ("mistral/mistral-large-latest", "MISTRAL_API_KEY"),
    ],
)
def test_required_keys_do_not_change_when_the_key_is_in_the_environment(
    tmp_path, monkeypatch, model, expected
):
    config_path = _blank_config(tmp_path)

    monkeypatch.delenv(expected, raising=False)
    without = settings_service.model_requirements(model, config_path)
    monkeypatch.setenv(expected, "sk-exported-in-the-shell")
    with_env = settings_service.model_requirements(model, config_path)

    envs = [key["env"] for key in without["required_keys"]]
    assert expected in envs
    assert [key["env"] for key in with_env["required_keys"]] == envs


def test_a_model_with_no_key_anywhere_is_not_satisfied(tmp_path, monkeypatch):
    """The check has to actually refuse something — `satisfied` was unconditionally
    True whenever the key happened to be exported, which is every developer machine."""
    config_path = _blank_config(tmp_path)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)

    requirements = settings_service.model_requirements("mistral/mistral-large-latest", config_path)

    assert requirements["provider"] == "mistral"
    assert requirements["satisfied"] is False
    assert requirements["required_keys"][0]["configured"] is False


def test_an_exported_key_satisfies_without_erasing_the_requirement(tmp_path, monkeypatch):
    """An exported key must MEET the requirement, not delete it — the run works, so
    refusing it would be a false negative, but the key must still be reported."""
    config_path = _blank_config(tmp_path)
    monkeypatch.setenv("MISTRAL_API_KEY", "sk-exported")

    requirements = settings_service.model_requirements("mistral/mistral-large-latest", config_path)

    assert [key["env"] for key in requirements["required_keys"]] == ["MISTRAL_API_KEY"]
    assert requirements["required_keys"][0]["configured"] is True
    assert requirements["satisfied"] is True


def test_a_saved_key_satisfies_with_a_clean_environment(tmp_path, monkeypatch):
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text('MISTRAL_API_KEY = "sk-saved-in-settings"\n')
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)

    requirements = settings_service.model_requirements("mistral/mistral-large-latest", config_path)

    assert requirements["required_keys"][0]["configured"] is True
    assert requirements["satisfied"] is True


def test_either_acceptable_key_satisfies_a_multi_key_provider(tmp_path, monkeypatch):
    """Gemini takes GEMINI_API_KEY or GOOGLE_API_KEY; both must be reported, and
    either alone must satisfy."""
    config_path = _blank_config(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-exported")

    requirements = settings_service.model_requirements(
        "gemini/gemini-3.1-pro-preview", config_path
    )

    assert [key["env"] for key in requirements["required_keys"]] == [
        "GEMINI_API_KEY", "GOOGLE_API_KEY",
    ]
    assert requirements["satisfied"] is True


def test_credential_chain_providers_require_no_env_key(tmp_path, monkeypatch):
    """Vertex/Bedrock/Ollama authenticate through a credential chain or not at all.
    Demanding a `<PROVIDER>_API_KEY` for them would be a false rejection."""
    config_path = _blank_config(tmp_path)

    for model, provider in (
        ("vertex_ai/gemini-2.0-flash", "vertex_ai"),
        ("bedrock/anthropic.claude-v2", "bedrock"),
        ("ollama/llama3", "ollama"),
    ):
        requirements = settings_service.model_requirements(model, config_path)
        assert requirements["provider"] == provider, model
        assert requirements["required_keys"] == [], model
        assert requirements["satisfied"] is True, model


# --- AUDIT-2026-07-24 S6: the file that holds every credential -----------------

def test_settings_are_written_atomically_and_owner_only(tmp_path, monkeypatch):
    """`write_text` truncates first, so an interrupted save left this file EMPTY —
    every provider key and the GitHub token gone, with no copy anywhere. And it was
    created under the process umask, typically world-readable, for a file that is
    entirely secrets."""
    import os
    import stat

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(settings_service, "_verify_api_key_credentials", lambda *a, **k: None)
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text('model = "gpt-4o"\n')
    config_path.chmod(0o644)  # a file predating this fix

    settings_service.update_settings(
        {"api_keys": {"OPENAI_API_KEY": {"value": "sk-secret-abcd"}}}, config_path
    )

    mode = stat.S_IMODE(config_path.stat().st_mode)
    assert mode == 0o600, f"secrets file is mode {mode:o}"
    assert "sk-secret-abcd" in config_path.read_text()
    # No temp file left beside it.
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".lea.local.toml")] == []


def test_a_failed_write_leaves_the_previous_settings_intact(tmp_path, monkeypatch):
    """The point of writing to a temp file and renaming: a crash mid-write must not
    take the keys with it."""
    import app.config as config_module

    original = 'model = "gpt-4o"\nopenai_api_key = "sk-keep-me"\n'
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(original)

    def die(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(config_module.os, "replace", die)

    with pytest.raises(OSError):
        config_module.write_private_text(config_path, "brand new content\n")

    assert config_path.read_text() == original, "the previous settings must survive"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".lea.local.toml")] == []


@pytest.mark.parametrize("hostile", [
    "sk-with-a-\nnewline",
    "sk-with-a-\ttab",
    'sk-with-a-"quote',
    "sk-with-a-\\backslash",
    "sk-with-a-\x1bescape",
])
def test_a_value_with_control_characters_stays_parseable(tmp_path, monkeypatch, hostile):
    """Only `\\` and `"` were escaped. A value carrying a newline produced a file
    tomllib refused to parse on EVERY subsequent read — and since only the three
    first-class providers are format-validated, anything matching `*_API_KEY` reached
    the writer verbatim."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    # The selected model is the one whose key this test saves, so the save is
    # self-consistent. (An earlier draft used `gpt-4o` and passed only because some
    # other test had leaked OPENAI_API_KEY into os.environ — the exact leak C8 fixes.)
    config_path.write_text('model = "mistral/mistral-large-latest"\n')

    settings_service.update_settings(
        {"api_keys": {"MISTRAL_API_KEY": {"value": hostile}}}, config_path
    )

    # The file still parses, and the value round-trips exactly.
    assert settings_service.configured_provider_keys(config_path)["MISTRAL_API_KEY"] == hostile
    assert settings_service.settings_payload(config_path)["model"] == "mistral/mistral-large-latest"


def test_a_corrupt_config_degrades_to_defaults_instead_of_500ing(tmp_path):
    """Every reader re-parses this file on every call, so an unparseable one turned
    into a 500 on essentially every endpoint — including the Settings page the user
    would need in order to repair it."""
    from app.config import github_token, load_config, permission_tier

    config_path = tmp_path / "lea.local.toml"
    config_path.write_text('model = "unterminated\n')

    assert load_config(config_path).model  # a default, not an exception
    assert permission_tier(config_path) == "stepwise"
    assert github_token(config_path) is None
    assert settings_service.configured_provider_keys(config_path) == {}
    assert settings_service.settings_payload(config_path)["api_keys"]["OPENAI_API_KEY"]["configured"] is False


def test_a_null_byte_in_a_key_is_refused_rather_than_saved(tmp_path, monkeypatch):
    """A NUL escapes into the file cleanly but cannot live in a POSIX environment
    variable — and `load_config` exports every saved key into `os.environ`. Saving one
    would have broken every subsequent request with `ValueError: embedded null byte`,
    so it is refused at the boundary: a credential with a NUL in it is a mangled paste,
    not a credential."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text('model = "gpt-4o"\n')

    try:
        settings_service.update_settings(
            {"api_keys": {"MISTRAL_API_KEY": {"value": "sk-with-a-\x00null"}}}, config_path
        )
    except settings_service.SettingsValidationError as exc:
        assert exc.field == "api_keys.MISTRAL_API_KEY"
        assert "null byte" in str(exc)
    else:
        raise AssertionError("Expected SettingsValidationError")

    # Nothing was written, and the file still loads.
    assert "MISTRAL_API_KEY" not in settings_service.configured_provider_keys(config_path)
    assert settings_service.load_config(config_path).model == "gpt-4o"
