"""v2.5 E0 tests: MCP server CRUD, scope, resolution and secret refusal.

Route functions are called directly (as the other route tests do); only the DB is
patched. The `/test` endpoint is covered separately in `test_mcp_server_test_route`
because it spawns a real subprocess.
"""

import pytest
from fastapi import HTTPException

from app import db, store
from app.routes import mcp_servers as route
from app.routes.mcp_servers import ServerAssignment, ServerPayload, ServerTest, ServerUpdate


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def test_create_list_get(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    created = route.create_server(ServerPayload(
        name="Lean LSP", command="uvx", args=["lean-lsp-mcp"],
        env={"LEAN_PROJECT_PATH": "/tmp/ws"}))
    assert created["slug"] == "lean-lsp"
    assert created["args"] == ["lean-lsp-mcp"]
    assert created["env"] == {"LEAN_PROJECT_PATH": "/tmp/ws"}
    assert created["enabled"] is True
    assert created["is_global"] is False

    assert [s["id"] for s in route.list_servers()["servers"]] == [created["id"]]
    assert route.get_server(created["id"])["name"] == "Lean LSP"


def test_command_with_spaces_is_refused(tmp_path, monkeypatch):
    """The most likely user error: pasting the whole command line into Command.
    It must fail at SAVE with an instruction, not silently at the next run."""
    _setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        route.create_server(ServerPayload(name="Bad", command="uvx lean-lsp-mcp"))
    assert exc.value.status_code == 400
    assert "single executable" in exc.value.detail


def test_secret_shaped_env_is_refused_and_steered(tmp_path, monkeypatch):
    """A7: a credential must never be stored in `env` — it belongs in `env_from`,
    where only the NAME is persisted and the value is read at spawn."""
    _setup(tmp_path, monkeypatch)
    for name in ("WOLFRAM_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD", "SOME_SECRET"):
        with pytest.raises(HTTPException) as exc:
            route.create_server(ServerPayload(name="X", command="uvx", env={name: "sk-live-123"}))
        assert exc.value.status_code == 400
        assert "Settings → API keys" in exc.value.detail
        assert name in exc.value.detail

    # The same value is fine as a NAME in env_from.
    ok = route.create_server(ServerPayload(
        name="Fine", command="uvx", env_from=["WOLFRAM_API_KEY"]))
    assert ok["env_from"] == ["WOLFRAM_API_KEY"]
    assert ok["env"] == {}


def test_missing_endpoint_for_each_transport(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException):
        route.create_server(ServerPayload(name="NoCmd", transport="stdio"))
    with pytest.raises(HTTPException):
        route.create_server(ServerPayload(name="NoUrl", transport="http"))
    with pytest.raises(HTTPException):
        route.create_server(ServerPayload(name="Bogus", transport="carrier-pigeon", command="x"))


def test_scope_and_resolution(tmp_path, monkeypatch):
    """Resolution mirrors skills (global ∪ assigned) with one deliberate difference:
    a loose session still gets the GLOBAL servers."""
    _setup(tmp_path, monkeypatch)
    proj_a = store.create_project("proj-a", title="A")["id"]
    proj_b = store.create_project("proj-b", title="B")["id"]

    glob = route.create_server(ServerPayload(name="Global", command="g", is_global=True))
    scoped = route.create_server(ServerPayload(name="Scoped", command="s", project_ids=[proj_a]))
    assert glob["is_global"] is True
    assert scoped["project_ids"] == [proj_a]

    assert set(store.mcp_server_specs(None)) == {"global"}
    assert set(store.mcp_server_specs(proj_a)) == {"global", "scoped"}
    assert set(store.mcp_server_specs(proj_b)) == {"global"}


def test_disabled_server_does_not_resolve(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    s = route.create_server(ServerPayload(name="Off", command="x", is_global=True))
    assert set(store.mcp_server_specs(None)) == {"off"}
    route.update_server(s["id"], ServerUpdate(enabled=False))
    assert store.mcp_server_specs(None) == {}


def test_update_is_partial_and_slug_is_stable(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    s = route.create_server(ServerPayload(
        name="Lean LSP", command="uvx", args=["lean-lsp-mcp"], env_from=["K_API_KEY"]))
    updated = route.update_server(s["id"], ServerUpdate(args=["lean-lsp-mcp", "--verbose"]))
    assert updated["args"] == ["lean-lsp-mcp", "--verbose"]
    assert updated["command"] == "uvx"            # untouched
    assert updated["env_from"] == ["K_API_KEY"]   # untouched
    assert updated["slug"] == "lean-lsp"          # stable identity


def test_resolved_spec_shape_matches_the_prover(tmp_path, monkeypatch):
    """`mcp_server_specs` feeds `cfg.mcp_servers` verbatim, so its shape is a contract
    with `lea.mcp._connect_one`. A secret value must never appear in it."""
    _setup(tmp_path, monkeypatch)
    route.create_server(ServerPayload(
        name="Lean LSP", command="uvx", args=["lean-lsp-mcp"],
        env={"LEAN_PROJECT_PATH": "/ws"}, env_from=["WOLFRAM_API_KEY"], is_global=True))
    route.create_server(ServerPayload(
        name="Remote", transport="sse", url="https://x.example/mcp",
        api_key_name="X_API_KEY", is_global=True))

    specs = store.mcp_server_specs(None)
    assert specs["lean-lsp"] == {
        "command": "uvx", "args": ["lean-lsp-mcp"],
        "env": {"LEAN_PROJECT_PATH": "/ws"}, "env_from": ["WOLFRAM_API_KEY"]}
    assert specs["remote"] == {
        "url": "https://x.example/mcp", "transport": "sse", "api_key_name": "X_API_KEY"}


def test_assignment_rejects_unknown_project(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    s = route.create_server(ServerPayload(name="S", command="x"))
    with pytest.raises(HTTPException) as exc:
        route.set_assignment(s["id"], ServerAssignment(project_ids=["nope"]))
    assert exc.value.status_code == 400


def test_delete_and_404s(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    s = route.create_server(ServerPayload(name="S", command="x", project_ids=[]))
    assert route.delete_server(s["id"])["deleted"] is True
    for call in (lambda: route.get_server(s["id"]),
                 lambda: route.delete_server(s["id"]),
                 lambda: route.update_server(s["id"], ServerUpdate(name="Z")),
                 lambda: route.set_assignment(s["id"], ServerAssignment())):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404


def test_test_endpoint_reports_the_real_failure(tmp_path, monkeypatch):
    """E0b: a bad command must come back as an actionable answer, not a 500 — and it
    must carry the child's real reason (A3), not the SDK's generic wording."""
    _setup(tmp_path, monkeypatch)
    result = route.test_server(ServerTest(command="definitely-not-a-real-binary-xyz"))
    assert result["ok"] is False
    assert result["tool_count"] == 0
    # `reason` is the headline the form shows; it must name the thing that is wrong.
    assert "definitely-not-a-real-binary-xyz" in result["reason"]
    # ...and read as a sentence, not a Python exception class.
    assert not result["reason"].startswith("FileNotFoundError")
    # ...and it must be ONE line, never a traceback dumped at the user.
    assert "\n" not in result["reason"]


def test_test_endpoint_validates_before_spawning(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException):
        route.test_server(ServerTest(transport="stdio", command=""))
    with pytest.raises(HTTPException):
        route.test_server(ServerTest(transport="http", url=""))


def test_preflight_answers_before_spawning(tmp_path, monkeypatch):
    """A2: both mistakes are knowable without starting anything, so the answer should be
    instant and in plain language — not a traceback five seconds later."""
    _setup(tmp_path, monkeypatch)

    missing = route.test_server(ServerTest(command="definitely-not-a-real-binary-xyz"))
    assert missing["ok"] is False
    assert "can't find" in missing["reason"]
    assert missing["detail"] == ""          # nothing was spawned, so there is no stderr

    not_a_project = route.test_server(ServerTest(
        command="sh", env={"LEAN_PROJECT_PATH": str(tmp_path)}))
    assert not_a_project["ok"] is False
    assert "lean-toolchain" in not_a_project["reason"]

    nowhere = route.test_server(ServerTest(
        command="sh", env={"LEAN_PROJECT_PATH": "/no/such/place"}))
    assert nowhere["ok"] is False
    assert "no folder" in nowhere["reason"]


def test_defaults_offers_the_lean_workspace(tmp_path, monkeypatch):
    """A4: the one field a mathematician cannot supply — the adapter has always known it."""
    _setup(tmp_path, monkeypatch)
    path = route.server_defaults()["lean_project_path"]
    assert path is None or path.endswith("workspace")


def test_key_requirements_reports_what_is_missing(tmp_path, monkeypatch):
    """D1: `env_from` / `api_key_name` already NAME the credentials a server needs — this
    reads that declaration back so the gap can be raised before the server 401s."""
    _setup(tmp_path, monkeypatch)
    route.create_server(ServerPayload(name="Wolfram", command="uvx",
                                      env_from=["WOLFRAM_API_KEY"]))
    route.create_server(ServerPayload(name="Remote", transport="http",
                                      url="https://x.example", api_key_name="X_API_KEY"))
    reqs = {r["env"]: r for r in route.key_requirements()["requirements"]}
    assert reqs["WOLFRAM_API_KEY"]["servers"] == ["wolfram"]
    assert reqs["X_API_KEY"]["servers"] == ["remote"]
    # Nothing is saved in a scratch config, so both read as missing — which is the
    # signal the enable-time warning is built on.
    assert reqs["WOLFRAM_API_KEY"]["configured"] is False


def test_clearing_a_key_in_use_is_refused_then_forceable(tmp_path, monkeypatch):
    """D3: the break would otherwise surface as a 401 on first use, far from the cause."""
    import pytest as _pytest

    from app import settings as settings_service
    _setup(tmp_path, monkeypatch)
    config = tmp_path / "lea.local.toml"
    # A provider key for the configured model too, or model validation fires first.
    config.write_text(
        'WOLFRAM_API_KEY = "sk-live-123"\n'
        'google_api_key = "AIzaSyTestKeyForValidationOnly"\n'
        'model = "gemini/gemini-2.0-flash"\n')
    route.create_server(ServerPayload(name="Wolfram", command="uvx",
                                      env_from=["WOLFRAM_API_KEY"]))

    with _pytest.raises(settings_service.SettingsValidationError) as exc:
        settings_service.update_settings(
            {"api_keys": {"WOLFRAM_API_KEY": {"clear": True}}}, path=config)
    assert "still used by wolfram" in str(exc.value)

    # ...and the user can insist.
    settings_service.update_settings(
        {"api_keys": {"WOLFRAM_API_KEY": {"clear": True}}, "force_clear_keys": True},
        path=config)
    assert "WOLFRAM_API_KEY" not in settings_service.configured_provider_keys(config)


def test_static_routes_are_reachable_over_http(tmp_path, monkeypatch):
    """Regression: `/defaults` and `/key-requirements` shipped BROKEN because
    `/api/mcp-servers/{server_id}` was declared first and swallowed them — FastAPI matches
    in declaration order. Every test above calls the route functions directly, so none of
    them could see it. This one goes through the real router.

    Any new static `/api/mcp-servers/<name>` route must be declared above `{server_id}`.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    _setup(tmp_path, monkeypatch)
    # `base_url` matters — netguard rejects TestClient's default `testserver` host
    # as a DNS-rebinding guard (see test_netguard.py).
    client = TestClient(app, base_url="http://localhost:8001")
    for path in ("/api/mcp-servers/defaults", "/api/mcp-servers/key-requirements"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        assert "not found" not in response.text.lower()


def test_catalog_entry_is_pinned_and_installs(tmp_path, monkeypatch):
    """E2/A6. `uvx <pkg>` unpinned executes whatever PyPI serves that day — that, not
    privilege escalation, is the real exposure. A curated entry must be a fixed thing."""
    _setup(tmp_path, monkeypatch)
    entries = route.list_catalog()["entries"]
    assert entries and entries[0]["id"] == "lean-lsp"
    assert entries[0]["installed"] is False
    args = entries[0]["server"]["args"]
    assert any("==" in a for a in args), f"catalog entry is not pinned: {args}"

    installed = route.install_catalog_entry("lean-lsp")
    assert installed["enabled"] is True
    assert any("==" in a for a in installed["args"])
    # The recommended SUBSET is what stops the overlap problem: `lean_goal` adds
    # something Lea lacks; `lean_leansearch` duplicates `search_mathlib`.
    assert "lean_goal" in installed["recommended_tools"]
    assert "lean_leansearch" not in installed["recommended_tools"]
    assert route.list_catalog()["entries"][0]["installed"] is True

    with pytest.raises(HTTPException) as exc:
        route.install_catalog_entry("nope")
    assert exc.value.status_code == 404


def test_catalog_route_is_reachable_over_http(tmp_path, monkeypatch):
    """Same shadowing trap that broke `/defaults` — a static route under a parameterized
    prefix. Walk the real router."""
    from fastapi.testclient import TestClient

    from app.main import app

    _setup(tmp_path, monkeypatch)
    client = TestClient(app, base_url="http://localhost:8001")
    assert client.get("/api/mcp-servers/catalog").status_code == 200
