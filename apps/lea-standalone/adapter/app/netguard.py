"""Network-boundary guards for a localhost-only backend (AUDIT-2026-07-24 S1).

The adapter binds 127.0.0.1 and its only access control was `CORSMiddleware` with a
two-entry allowlist. That is thin cover for an API that starts runs which execute
arbitrary shell commands, deletes projects, pushes to GitHub, and reads and writes
files, because CORS does not do what it looks like it does here:

* **A cross-site request still executes.** CORS decides whether the *response* is
  readable, not whether the request runs. A "simple" cross-origin request reaches the
  handler and its side effects happen; the attacker simply doesn't get to read the
  reply. The Overleaf companion already draws this distinction and rejects
  unrecognized origins outright (`companion/server.mjs`); the adapter did not.
* **CORS cannot see a DNS-rebinding attack at all.** An attacker domain that
  re-resolves to 127.0.0.1 makes the page *same-origin* with this server from the
  browser's point of view, so no CORS check fires. The `Host` header is the only place
  the attacker's name still shows up, which is why it is checked here.
* **WebSockets are not subject to CORS.** The LSP endpoint (`routes/sessions.lsp_socket`)
  has to check `Origin` itself; the HTTP middleware never sees the handshake.

The rule for both headers is *loopback identity*, not an exact string match. A
localhost service is legitimately addressed as `localhost`, `127.0.0.1`, or `[::1]`,
on whatever port the deployment uses — the Vite dev server proxies `/api` without
`changeOrigin`, so it forwards `Host: localhost:5173`, while the single-container
build is reached at `localhost:8001`. Matching on hostname and ignoring the port
covers every one of those without an allowlist that has to be maintained. It gives up
nothing: a remote page cannot present a loopback hostname in `Origin`, and a
rebinding attack cannot present one in `Host` — that is the whole mechanism of the
attack. Anything already able to send a loopback `Origin` is a process on the user's
own machine, which this design has always trusted.

`LEA_ALLOWED_ORIGINS` / `LEA_ALLOWED_HOSTS` (comma-separated) extend both sets for
deployments behind a reverse proxy or on a LAN name.
"""

from __future__ import annotations

import os

# Hostnames that mean "this machine". `0.0.0.0` is included because a bind-all
# deployment is reached that way; it is a name for the local host, not a route to a
# remote one.
LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

ALLOWED_ORIGINS_ENV = "LEA_ALLOWED_ORIGINS"
ALLOWED_HOSTS_ENV = "LEA_ALLOWED_HOSTS"


def _configured(env_var: str) -> set[str]:
    """Extra allowed values from a comma-separated env var, lowercased."""
    raw = os.environ.get(env_var, "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _hostname(authority: str) -> str:
    """The hostname from a `host[:port]` authority, minus brackets and port.

    Written by hand rather than via `urlsplit`, because a bare authority is not a URL:
    `urlsplit("localhost:5173")` reads `localhost` as a *scheme*. IPv6 literals arrive
    bracketed (`[::1]:8001`), so the bracket form is unwrapped before the port split.
    """
    value = authority.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value[1:]
    return value.split(":", 1)[0]


def is_allowed_host(host_header: str | None) -> bool:
    """Whether a request's `Host` names this machine (DNS-rebinding guard).

    A missing `Host` is allowed: HTTP/1.1 browsers always send one, so its absence
    means non-browser tooling (a health check, curl, the Node companion), which is not
    the threat this defends against.
    """
    if not host_header:
        return True
    value = host_header.strip().lower()
    if value in _configured(ALLOWED_HOSTS_ENV):
        return True
    return _hostname(value) in LOOPBACK_HOSTNAMES | _configured(ALLOWED_HOSTS_ENV)


def is_allowed_origin(origin: str | None) -> bool:
    """Whether a browser `Origin` belongs to a page served from this machine.

    A missing/`null` `Origin` is allowed — no browser cross-origin request omits it,
    so its absence means a non-browser client. Everything else must be an http(s)
    origin on a loopback hostname.
    """
    if not origin:
        return True
    value = origin.strip().lower()
    if value in _configured(ALLOWED_ORIGINS_ENV):
        return True
    # `null` is what a sandboxed iframe or a `file://` page sends. It is not a name we
    # can attribute to anything, so it is not trusted.
    if value == "null":
        return False
    scheme, separator, authority = value.partition("://")
    if not separator or scheme not in ("http", "https"):
        return False
    return _hostname(authority) in LOOPBACK_HOSTNAMES | _configured(ALLOWED_ORIGINS_ENV)
