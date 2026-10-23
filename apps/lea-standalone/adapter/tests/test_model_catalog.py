import json
import threading
from pathlib import Path

import httpx
import pytest

from app import catalog_service as catalog


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog.models_catalog, "is_available", lambda: False)
    now = [1_800_000_000.0]
    sources = {"openai": {"url": "https://provider.test/models", "headers": {"Authorization": "Bearer secret-one"}}}
    pages = {"https://provider.test/models": {"data": [{"id": "gpt-future", "created": now[0]}]}}
    calls = []

    def fetch(url, headers):
        calls.append((url, headers))
        value = pages[url]
        if isinstance(value, Exception):
            raise value
        return value

    service = catalog.CatalogService(tmp_path / "catalog.json", sources=lambda: sources, fetch=fetch, clock=lambda: now[0])
    return service, sources, pages, calls, now


def test_new_model_appears_after_one_week_without_restart_and_survives_reload(setup):
    service, sources, pages, calls, now = setup
    service.refresh()
    old = service.payload()
    assert next(m for m in old["models"] if m["value"] == "gpt-future")["availability"] == "available"
    assert "gpt-future" in [m["value"] for m in old["featured"]]
    pages[sources["openai"]["url"]]["data"].append({"id": "gpt-new", "created": now[0] + 1})
    service.refresh()
    assert len(calls) == 1
    assert catalog.INTERVAL == 7 * 24 * 60 * 60
    now[0] += 6 * 60 * 60
    service.refresh()
    assert len(calls) == 1
    now[0] += catalog.INTERVAL - 6 * 60 * 60 - 1
    service.refresh()
    assert len(calls) == 1
    now[0] += 1
    service.refresh()
    assert len(calls) == 2
    current = service.payload("saved/custom-model")
    assert current["revision"] != old["revision"]
    assert "gpt-new" in [m["value"] for m in current["models"]]
    assert current["featured"][0]["value"] == "saved/custom-model"
    restored = catalog.CatalogService(service.path, sources=lambda: sources, clock=lambda: now[0])
    assert restored.payload("saved/custom-model") == current
    assert "secret-one" not in service.path.read_text()
    assert service.path.stat().st_mode & 0o077 == 0


def test_startup_respects_weekly_schedule_for_existing_cache(setup, monkeypatch):
    service, sources, pages, calls, now = setup
    service.refresh()
    # Simulate a cache written with the previous six-hour cadence.
    service.records["openai"]["next_attempt"] = now[0] + 6 * 60 * 60
    service._save()
    now[0] += 24 * 60 * 60
    restored = catalog.CatalogService(service.path, sources=lambda: sources, fetch=service.fetch, clock=lambda: now[0])
    completed = threading.Event()
    refresh = restored.refresh

    def checked_refresh(force=False):
        try:
            refresh(force=force)
        finally:
            completed.set()

    monkeypatch.delenv("LEA_MODEL_CATALOG_OFFLINE", raising=False)
    monkeypatch.setattr(restored, "refresh", checked_refresh)
    restored.start()
    try:
        assert completed.wait(2)
        assert len(calls) == 1
        assert not restored.payload()["sources"]["openai"]["stale"]
    finally:
        restored.close()


def test_outage_keeps_data_reports_stale_and_backs_off(setup):
    service, sources, pages, calls, now = setup
    service.refresh()
    now[0] += catalog.INTERVAL
    pages[sources["openai"]["url"]] = RuntimeError("secret-one must never leak")
    service.refresh()
    payload = service.payload()
    assert payload["degraded"]
    assert payload["sources"]["openai"]["error"] == "RuntimeError"
    assert next(m for m in payload["models"] if m["value"] == "gpt-future")["availability"] == "unknown"
    assert "secret-one" not in json.dumps(payload)
    service.refresh()
    assert len(calls) == 2
    pages[sources["openai"]["url"]] = {"data": []}
    now[0] += 120
    service.refresh()
    assert not service.payload()["degraded"]
    assert "gpt-future" not in [m["value"] for m in service.payload()["models"]]


def test_changed_credentials_and_endpoint_never_reuse_old_account_list(setup):
    service, sources, pages, calls, now = setup
    service.refresh()
    sources["openai"]["headers"]["Authorization"] = "Bearer secret-two"
    assert "gpt-future" not in [m["value"] for m in service.payload()["models"]]
    sources["openai"]["url"] = "https://other.test/models"
    pages[sources["openai"]["url"]] = {"data": [{"id": "gpt-other"}]}
    service.refresh()
    assert "gpt-other" in [m["value"] for m in service.payload()["models"]]
    del sources["openai"]
    assert "gpt-other" not in [m["value"] for m in service.payload()["models"]]


@pytest.mark.parametrize("provider,cursor", [("anthropic", "&after_id=claude-new"), ("gemini", "&pageToken=second")])
def test_pagination_is_complete_and_partial_failure_does_not_replace_cache(setup, provider, cursor):
    service, sources, pages, calls, now = setup
    sources.clear()
    sources[provider] = {"url": "https://provider.test/models", "headers": {}}
    first = "https://provider.test/models?" + ("limit=100" if provider == "anthropic" else "pageSize=100")
    page = {"data": [{"id": "claude-new"}], "has_more": True, "last_id": "claude-new"} if provider == "anthropic" else {
        "models": [{"name": "models/gemini-new", "supportedGenerationMethods": ["generateContent"]}], "nextPageToken": "second"}
    pages[first] = page
    pages[first + cursor] = {"data": [{"id": "claude-next"}]} if provider == "anthropic" else {
        "models": [{"name": "models/gemini-next", "supportedGenerationMethods": ["generateContent"]}]}
    service.refresh()
    record = service.records[provider]
    assert len(record["models"]) == 2
    pages[first + cursor] = ValueError("partial response")
    now[0] += catalog.INTERVAL
    service.refresh()
    assert service.records[provider]["models"] == record["models"]
    assert service.payload()["degraded"]


def test_repeated_cursor_is_rejected_and_gateway_id_is_opaque(setup):
    service, sources, pages, calls, now = setup
    config = {"url": "https://provider.test/models", "headers": {}}
    pages[config["url"]] = {"data": [{"id": "@vertexai-user/anthropic.claude-new"}]}
    rows = service._read_source("portkey", config)
    assert rows[0]["value"] == "portkey/@vertexai-user/anthropic.claude-new"
    pages[config["url"] + "?limit=100"] = {"data": [{"id": "claude"}], "has_more": True, "last_id": "claude"}
    pages[config["url"] + "?limit=100&after_id=claude"] = pages[config["url"] + "?limit=100"]
    with pytest.raises(ValueError, match="Repeated"):
        service._read_source("anthropic", config)


def test_sources_fail_independently_and_status_codes_are_safe(setup):
    service, sources, pages, calls, now = setup
    sources["portkey"] = {"url": "https://gateway.test/models", "headers": {}}
    pages["https://gateway.test/models"] = httpx.HTTPStatusError("sensitive body", request=httpx.Request("GET", "https://gateway.test"), response=httpx.Response(429))
    service.refresh()
    payload = service.payload()
    assert payload["sources"]["portkey"]["error"] == "HTTP 429"
    assert not payload["sources"]["openai"]["stale"]
    assert "sensitive" not in json.dumps(payload)


def test_corrupt_cache_and_stale_bundle_are_usable(setup):
    service, sources, pages, calls, now = setup
    service.path.write_text('{"sources": "broken"}')
    restored = catalog.CatalogService(service.path, sources=lambda: sources)
    payload = restored.payload("custom/my-model")
    assert payload["degraded"]
    assert payload["featured"][0]["value"] == "custom/my-model"
    assert payload["models"]


def test_metadata_accepts_responses_aliases_and_excludes_other_modalities():
    rows = catalog.metadata_entries({
        "gpt-new": {"mode": "responses", "litellm_provider": "openai", "supports_function_calling": True, "aliases": ["gpt-new-alias"]},
        "image": {"mode": "image_generation", "litellm_provider": "openai"},
        "no-tools": {"mode": "chat", "litellm_provider": "openai", "supports_function_calling": False},
        "unknown": {"mode": "chat", "litellm_provider": "other"},
    })
    assert [r["value"] for r in rows] == ["gpt-new", "gpt-new-alias", "unknown"]
    assert rows[0]["compatibility"] == "supported"
    assert rows[-1]["compatibility"] == "unverified"


def test_manual_refresh_is_coalesced_and_worker_stops(setup, monkeypatch):
    service, sources, pages, calls, now = setup
    monkeypatch.delenv("LEA_MODEL_CATALOG_OFFLINE", raising=False)
    completed = threading.Event()
    def refresh(force=False):
        completed.set()
    monkeypatch.setattr(service, "refresh", refresh)
    service.request_refresh()
    service.start()
    service.start()
    assert completed.wait(2)
    service.close()
    assert not service.worker.is_alive()


def test_remote_protocol_hints_reach_execution_and_key_requirements(setup, monkeypatch):
    from lea import providers
    service, sources, pages, calls, now = setup
    monkeypatch.setattr(providers, "_CATALOG_RESPONSES_MODELS", frozenset())
    monkeypatch.setattr(catalog.models_catalog, "_CATALOG_PROVIDERS", {})
    sources["metadata"] = {"url": "https://metadata.test/models", "headers": {}}
    pages[sources["openai"]["url"]] = {"data": [{"id": "future-reasoner"}]}
    pages["https://metadata.test/models"] = {f"future-reasoner{'-' + str(i) if i else ''}": {
        "mode": "responses", "litellm_provider": "openai", "supports_function_calling": True} for i in range(100)}
    service.refresh()
    payload = service.payload()
    assert next(m for m in payload["models"] if m["value"] == "future-reasoner")["compatibility"] == "supported"
    assert providers._litellm_model("future-reasoner") == "openai/responses/future-reasoner"
    assert catalog.models_catalog.requirements_for("future-reasoner")["required_keys"] == ["OPENAI_API_KEY"]


def test_refresh_endpoint_preserves_envelope_and_returns_immediately(setup, monkeypatch):
    from app.routes import settings as routes
    service, *_ = setup
    monkeypatch.setattr(catalog, "_service", service)
    payload = routes.models.worker()
    assert {"models", "featured", "revision", "sources", "warnings"} <= payload.keys()
    monkeypatch.delenv("LEA_MODEL_CATALOG_OFFLINE", raising=False)
    result = routes.refresh_models.worker()
    assert result["refreshing"]
    assert service.wake_event.is_set()


def test_retirement_keeps_selected_model_but_removes_it_from_suggestions(setup):
    service, sources, pages, calls, now = setup
    pages[sources["openai"]["url"]] = {"data": [
        {"id": "gpt-retired", "shutdown_date": "2020-01-01"}, {"id": "gpt-active"}]}
    service.refresh()
    payload = service.payload()
    assert "gpt-retired" not in [m["value"] for m in payload["featured"]]
    selected = service.payload("gpt-retired")
    assert selected["featured"][0]["value"] == "gpt-retired"
    assert selected["featured"][0]["availability"] == "retired"


def test_known_non_chat_provider_models_are_not_reintroduced_by_discovery(setup, monkeypatch):
    service, sources, pages, calls, now = setup
    monkeypatch.setattr(catalog.models_catalog, "is_available", lambda: True)
    monkeypatch.setattr(catalog.models_catalog.litellm, "model_cost", {
        "image-model": {"mode": "image_generation", "litellm_provider": "openai"}})
    pages[sources["openai"]["url"]] = {"data": [{"id": "image-model"}, {"id": "gpt-future"}]}
    service.refresh()
    assert "image-model" not in [m["value"] for m in service.payload()["models"]]


def test_config_changes_request_refresh_after_saving(tmp_path, monkeypatch):
    from app import settings
    from app import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "settings.sqlite3")
    db.init_db()
    config = tmp_path / "lea.local.toml"
    config.write_text('model = "gpt-4o"\nopenai_api_key = "sk-existing123456"\n')
    requests = []
    monkeypatch.setattr(catalog.service(), "request_refresh", lambda: requests.append(True))
    monkeypatch.setattr(settings, "_verify_api_key_credentials", lambda *a, **kw: None)
    settings.update_settings({"api_keys": {"OPENAI_API_KEY": {"value": "sk-replacement123456"}}}, config)
    assert requests == [True]


def test_recent_selections_are_shared_persisted_and_do_not_change_current_choice(setup):
    service, sources, pages, calls, now = setup
    service.remember_model("ollama/my-model")
    service.remember_model("custom/previous")
    restored = catalog.CatalogService(service.path, sources=lambda: sources, clock=lambda: now[0])
    assert restored.payload("custom/current")["featured"][0]["value"] == "custom/current"
    assert "custom/previous" in [m["value"] for m in restored.payload()["featured"]]


def test_new_aliases_do_not_crowd_out_models_with_known_release_dates(setup):
    service, sources, pages, calls, now = setup
    pages[sources["openai"]["url"]] = {"data": [
        *[{"id": f"alias-{i}"} for i in range(20)],
        {"id": "gpt-dated", "created": now[0] - 10000}]}
    service.refresh()
    suggestions = service.payload()["featured"]
    assert "gpt-dated" in [m["value"] for m in suggestions]
    assert len([m for m in suggestions if m["value"].startswith("alias-")]) == 2
    assert [m["value"] for m in suggestions].index("gpt-dated") < next(i for i, m in enumerate(suggestions) if m["value"].startswith("alias-"))
