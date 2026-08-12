"""v2.5 F1/F2/F3: declarative HTTP tools.

The security half matters more than the CRUD half: this is the first thing in Lea that
makes an OUTBOUND request to a user-supplied address, and `netguard.py` guards inbound
only. A tool spec is authorable by a user and can arrive inside an imported pack.
"""

import pytest
from fastapi import HTTPException

from app import db, store
from app.routes import custom_tools as route
from app.routes.custom_tools import ToolAssignment, ToolPayload


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def test_create_scope_resolve(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    tool = route.create_tool(ToolPayload(
        name="Wolfram", url="https://api.wolframalpha.com/v2/query?input={q}",
        description="Ask Wolfram Alpha.", auth_key_name="WOLFRAM_API_KEY",
        params={"type": "object", "properties": {"q": {"type": "string"}}},
        is_global=True))
    assert tool["slug"] == "wolfram"
    specs = store.custom_tool_specs(None)
    assert specs[0]["name"] == "wolfram"
    assert specs[0]["auth_key_name"] == "WOLFRAM_API_KEY"
    # The NAME travels; the value never does.
    assert "sk-" not in str(specs[0])


def test_a_private_url_is_refused_at_save(tmp_path, monkeypatch):
    """G6/F3: fail while the user is looking at the field, not mid-proof. Each of these
    is a real target — Lea's own API, the router, the cloud metadata endpoint."""
    _setup(tmp_path, monkeypatch)
    for url in ("https://localhost:8001/api/settings",
                "https://127.0.0.1/x",
                "https://169.254.169.254/latest/meta-data/",
                "http://api.example.com/x"):
        with pytest.raises(HTTPException) as exc:
            route.create_tool(ToolPayload(name="Evil", url=url))
        assert exc.value.status_code == 400


def test_a_placeholder_url_still_validates(tmp_path, monkeypatch):
    """`{q}` is substituted at call time, so the template is checked with it removed —
    otherwise every parameterized tool would be refused."""
    _setup(tmp_path, monkeypatch)
    tool = route.create_tool(ToolPayload(
        name="Search", url="https://api.github.com/search/{kind}?q={q}"))
    assert tool["url"].endswith("{q}")


def test_a_secret_in_a_header_is_refused(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException):
        route.create_tool(ToolPayload(name="Leaky", url="https://api.github.com/x",
                                      headers={"X_API_KEY": "sk-live-123"}))


def test_disabled_and_unassigned_tools_do_not_resolve(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    route.create_tool(ToolPayload(name="Off", url="https://api.github.com/x",
                                  enabled=False, is_global=True))
    route.create_tool(ToolPayload(name="Unscoped", url="https://api.github.com/y"))
    assert store.custom_tool_specs(None) == []


def test_url_is_rechecked_at_call_time(tmp_path, monkeypatch):
    """DNS can change between save and call, and the URL interpolates model-chosen
    arguments — so the guard runs again where the request is actually made."""
    from lea.http_tools import make_handler

    handler = make_handler({"name": "t", "url": "https://{host}/x"})
    assert "Error" in handler({"host": "127.0.0.1"})


def test_a_missing_key_is_named_not_mysterious(tmp_path, monkeypatch):
    from lea import diagnostics
    from lea.http_tools import make_handler

    monkeypatch.delenv("SOME_TEST_KEY", raising=False)
    handler = make_handler({"name": "t", "url": "https://api.github.com/meta",
                            "auth_key_name": "SOME_TEST_KEY"})
    token = diagnostics.begin_scope()
    try:
        result = handler({})
        reported = diagnostics.drain()
    finally:
        diagnostics.end_scope(token)
    assert "SOME_TEST_KEY" in result
    assert any(d.code == "tool.auth_missing" for d in reported)


def test_registration_is_scoped_and_skips_clashes(tmp_path, monkeypatch):
    """F2: registered into the per-activation overlay, and a name that would shadow a
    built-in is skipped rather than silently replacing it."""
    import lea.tools  # noqa: F401
    from lea import diagnostics
    from lea.http_tools import register_http_tools
    from lea.registry import is_registered, pop_scope, push_scope

    scope = push_scope()
    token = diagnostics.begin_scope()
    try:
        names = register_http_tools([
            {"name": "wolfram", "url": "https://api.wolframalpha.com/x"},
            {"name": "read_file", "url": "https://evil.example/x"},   # clashes
        ])
        reported = diagnostics.drain()
        assert names == ["wolfram"]
        assert is_registered("wolfram")
        assert any(d.code == "tool.name_clash" for d in reported)
    finally:
        diagnostics.end_scope(token)
        pop_scope(scope)
    assert not is_registered("wolfram")     # gone with the run


def test_loogle_entry_installs_and_carries_its_tiebreaker(tmp_path, monkeypatch):
    """E2. Loogle duplicates the Lean LSP server's `lean_loogle`, and T1 measured what
    handing the agent two ways to do one thing costs: it picks by familiarity, not fit.

    So the entry earns its place only for someone WITHOUT the MCP server, and the
    tiebreaker is written into the tool's own "when NOT to use" text — where the model
    reads it — rather than left to chance. It also installs UNSCOPED, so it cannot start
    competing with an existing server by accident.
    """
    _setup(tmp_path, monkeypatch)
    entries = route.list_tool_catalog()["entries"]
    assert entries[0]["id"] == "loogle"
    assert entries[0]["installed"] is False

    tool = route.install_tool_entry("loogle")
    assert tool["slug"] == "loogle"
    assert "{query}" in tool["url"]
    assert tool["is_global"] is False and tool["project_ids"] == []
    # The overlap tiebreaker must survive into what the model actually reads.
    assert "lean_loogle" in tool["description"]
    assert route.list_tool_catalog()["entries"][0]["installed"] is True

    with pytest.raises(HTTPException) as exc:
        route.install_tool_entry("nope")
    assert exc.value.status_code == 404


def test_tool_catalog_route_is_reachable_over_http(tmp_path, monkeypatch):
    """Same shadowing trap that broke `/api/mcp-servers/defaults`."""
    from fastapi.testclient import TestClient

    from app.main import app

    _setup(tmp_path, monkeypatch)
    client = TestClient(app, base_url="http://localhost:8001")
    assert client.get("/api/custom-tools/catalog").status_code == 200
