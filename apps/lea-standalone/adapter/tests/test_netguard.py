"""AUDIT-2026-07-24 S1: the localhost boundary.

The adapter binds 127.0.0.1 and had only `CORSMiddleware` guarding it. CORS decides
whether a *response* is readable, not whether a request runs — so a cross-site simple
request still reached handlers that start shell-executing runs, delete projects, and
push to GitHub. And CORS cannot see DNS rebinding at all, because rebinding makes the
attacker's page same-origin with this server; the `Host` header is where the
attacker's name still appears.

These tests pin both directions: what is now refused, and what must keep working —
the Vite dev proxy (which forwards `Host: localhost:5173`), the single-container build
on :8001, and the Node companion (no `Origin` at all).
"""

import pytest
from fastapi.testclient import TestClient

from app import db, netguard


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client addressing the adapter the way a browser does.

    `base_url` matters: TestClient defaults to `http://testserver`, which is exactly
    the kind of non-loopback name the Host guard is built to refuse — so without this
    every request would 403 for the *wrong* reason and the origin tests below would
    never reach the check they are about."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    from app.main import app
    return TestClient(app, base_url="http://localhost:8001")


# ── the unit rule ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("origin", [
    "https://evil.example.com",
    "http://evil.example.com",
    "https://localhost.evil.example.com",   # suffix lookalike
    "https://www.overleaf.com",             # a real site, still not this machine
    "null",                                 # sandboxed iframe / file:// page
    "chrome-extension://abcdef",            # not http(s)
])
def test_remote_origins_are_refused(origin):
    assert netguard.is_allowed_origin(origin) is False


@pytest.mark.parametrize("origin", [
    "http://localhost:5173",     # Vite dev server
    "http://127.0.0.1:5173",
    "http://localhost:8001",     # single-container build serving the bundled UI
    "http://127.0.0.1:8001",
    "http://[::1]:8001",
    "https://localhost:8443",    # a TLS-terminating local setup
])
def test_loopback_origins_are_allowed(origin):
    assert netguard.is_allowed_origin(origin) is True


def test_a_missing_origin_is_allowed():
    """No browser cross-origin request omits Origin, so its absence means a
    non-browser client — the Node companion, curl, the Docker health check."""
    assert netguard.is_allowed_origin(None) is True
    assert netguard.is_allowed_origin("") is True


@pytest.mark.parametrize("host", [
    "evil.example.com",
    "evil.example.com:8001",
    "localhost.evil.example.com:8001",
    "rebind.attacker.test:8001",
])
def test_rebinding_hosts_are_refused(host):
    """A DNS-rebinding page resolves its own name to 127.0.0.1, so the request is
    same-origin and CORS never fires — but the Host header still carries the
    attacker's name."""
    assert netguard.is_allowed_host(host) is False


@pytest.mark.parametrize("host", [
    "localhost:5173",   # the Vite proxy forwards this (no changeOrigin)
    "localhost:8001",
    "127.0.0.1:8001",
    "[::1]:8001",
    "localhost",
    "0.0.0.0:8001",
])
def test_loopback_hosts_are_allowed(host):
    assert netguard.is_allowed_host(host) is True


def test_env_vars_extend_both_allowlists(monkeypatch):
    """The escape hatch for a reverse proxy or a LAN name."""
    assert netguard.is_allowed_host("lea.internal:8001") is False
    assert netguard.is_allowed_origin("https://lea.internal") is False

    monkeypatch.setenv(netguard.ALLOWED_HOSTS_ENV, "lea.internal, other.host")
    monkeypatch.setenv(netguard.ALLOWED_ORIGINS_ENV, "https://lea.internal")

    assert netguard.is_allowed_host("lea.internal:8001") is True
    assert netguard.is_allowed_origin("https://lea.internal") is True


# ── the middleware, end to end ───────────────────────────────────────────────

def test_a_cross_site_request_is_stopped_not_merely_unreadable(client):
    """The whole point: the handler must not run. A 403 here (rather than a 200 the
    attacker can't read) is the difference between refusing the side effect and
    performing it."""
    response = client.get("/api/health", headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 403
    assert "evil.example.com" in response.json()["detail"]


def test_a_cross_site_post_cannot_start_a_run(client):
    """POST /api/runs starts a run that executes shell commands. It is the endpoint
    this boundary exists for."""
    response = client.post(
        "/api/runs",
        json={"message": "exfiltrate everything"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert response.status_code == 403


def test_a_rebound_host_is_refused(client):
    response = client.get("/api/health", headers={"Host": "rebind.attacker.test:8001"})
    assert response.status_code == 403
    assert "host" in response.json()["detail"].lower()


def test_the_default_test_client_host_is_itself_refused(tmp_path, monkeypatch):
    """`testserver` is a non-loopback name, so it is refused like any other. Asserted
    so the guard's reach is explicit rather than a surprise for the next test author."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    from app.main import app

    assert TestClient(app).get("/api/health").status_code == 403


def test_the_dev_proxy_and_the_bundled_ui_still_work(client):
    """Vite forwards Host: localhost:5173 with the page's own Origin; the container
    build is same-origin on :8001. Both must pass."""
    dev = client.get(
        "/api/health",
        headers={"Host": "localhost:5173", "Origin": "http://localhost:5173"},
    )
    bundled = client.get(
        "/api/health",
        headers={"Host": "localhost:8001", "Origin": "http://localhost:8001"},
    )
    assert dev.status_code == 200 and dev.json() == {"ok": True}
    assert bundled.status_code == 200


def test_a_non_browser_client_is_unaffected(client):
    """The Node companion drives this API with no Origin header at all."""
    response = client.get("/api/health", headers={"Host": "127.0.0.1:8001"})
    assert response.status_code == 200


# ── the WebSocket, which CORS never covered ──────────────────────────────────

def test_the_lsp_socket_refuses_a_remote_origin(client):
    """WebSockets are exempt from the same-origin policy, so this endpoint was
    reachable from any page the user had open — and it spawns a `lake serve` per
    connection."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
            "/api/sessions/does-not-matter/lsp",
            headers={"Origin": "https://evil.example.com"},
        ):
            pass
    assert ei.value.code == 1008
