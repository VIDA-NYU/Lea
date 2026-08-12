"""Declarative HTTP tools — a REST endpoint as a tool, with no code (v2.5 F1/F2/F3).

MCP is the right answer when a service already speaks it. For a plain JSON endpoint —
Wolfram Alpha, Semantic Scholar, a private inference service — standing up an MCP server
is disproportionate: the whole integration is a URL, a couple of parameters, and a key.
So a tool can be *declared* instead: `{name, description, method, url, params, auth}` in,
a registered tool out.

Three things this deliberately is NOT:

  * **Not `tool_modules`.** That imports arbitrary Python, which is fine for a developer
    and unacceptable as a UI feature. A spec has no code in it.
  * **Not a secret store.** `auth_key_name` NAMES an environment variable; the value is
    read at call time. A stored spec is safe to log, exactly as an MCP spec is (A7).
  * **Not free-range.** Every request goes through `check_url`, because this is the first
    thing in Lea that makes an OUTBOUND request to a user-supplied address —
    `netguard.py` guards inbound only. See `check_url` for the reasoning.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

from . import diagnostics
from .errors import ToolError
from .registry import Tool, is_registered, register

DEFAULT_TIMEOUT = 20
MAX_RESPONSE_CHARS = 20_000


class UrlRefused(ToolError):
    """The target address is not one we are willing to fetch."""


def _is_public(host: str) -> bool:
    """True when every address `host` resolves to is public.

    Resolution happens HERE, and the decision is made on the resolved addresses rather
    than on the hostname, because a name is not a promise: `evil.example` can resolve to
    127.0.0.1 or 169.254.169.254 (the cloud metadata endpoint) and a string check would
    wave it through. Multi-homed names are refused unless *all* addresses are public, so
    a single private answer cannot be the one that gets used.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def check_url(url: str) -> str:
    """Validate an outbound target, or raise `UrlRefused` (v2.5 F3).

    `netguard.py` protects the adapter's INBOUND surface; this is the first outbound one.
    Without it a tool spec — which a user can author, and which an imported pack could
    carry — turns Lea into a request proxy for anything reachable from this machine:
    `http://localhost:8001` (its own API), a home router, or a cloud metadata endpoint.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise UrlRefused(f"Refusing {url!r}: only https URLs are allowed.")
    if not parsed.hostname:
        raise UrlRefused(f"Refusing {url!r}: no host.")
    if not _is_public(parsed.hostname):
        raise UrlRefused(
            f"Refusing {url!r}: it resolves to a private or local address. Tools may only "
            f"reach public internet endpoints."
        )
    return url


def _render(template: str, args: dict) -> str:
    """Substitute `{name}` placeholders, URL-encoding each value.

    Encoding is not cosmetic: an un-encoded value containing `?` or `#` rewrites the rest
    of the URL, so a parameter could redirect the request somewhere the author never
    wrote.
    """
    out = template
    for key, value in (args or {}).items():
        token = "{" + str(key) + "}"
        if token in out:
            out = out.replace(token, urllib.parse.quote(str(value), safe=""))
    return out


def _truncate(text: str) -> str:
    if len(text) <= MAX_RESPONSE_CHARS:
        return text
    return text[:MAX_RESPONSE_CHARS] + f"\n… [truncated at {MAX_RESPONSE_CHARS} characters]"


def make_handler(spec: dict):
    """The `dict -> str` handler for one declared tool."""

    def handler(args: dict) -> str:
        method = str(spec.get("method") or "GET").upper()
        url = _render(str(spec.get("url") or ""), args)
        # Re-checked at CALL time, not only at save: the spec may interpolate arguments
        # the model chose, and DNS can change between the two.
        try:
            check_url(url)
        except UrlRefused as exc:
            return f"Error: {exc}"

        headers = {"Accept": "application/json, text/plain;q=0.9, */*;q=0.8"}
        for key, value in (spec.get("headers") or {}).items():
            headers[str(key)] = str(value)
        key_name = spec.get("auth_key_name")
        if key_name:
            secret = os.environ.get(str(key_name))
            if not secret:
                # The tool is unusable, and saying which key is missing is the whole
                # difference between a fixable problem and a mysterious 401.
                diagnostics.report(
                    "degraded", "tool.auth_missing",
                    f"The tool {spec.get('name')!r} needs {key_name}, which is not saved.",
                    source="tool", remedy=f"Add {key_name} under Settings → API keys.",
                    once=True, tool=spec.get("name"),
                )
                return (f"Error: this tool needs the API key {key_name}, which is not "
                        f"configured. Tell the user to add it in Settings.")
            template = str(spec.get("auth_header") or "Authorization: Bearer {key}")
            name, _, value = template.partition(":")
            headers[name.strip()] = value.strip().replace("{key}", secret)

        body = None
        if method in ("POST", "PUT", "PATCH"):
            body = json.dumps(args or {}).encode()
            headers.setdefault("Content-Type", "application/json")

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        timeout = int(spec.get("timeout") or DEFAULT_TIMEOUT)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return _truncate(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:600]
            return f"Error: the service returned {exc.code} {exc.reason}. {detail}"
        except Exception as exc:  # noqa: BLE001 — a failed call is a tool result, not a crash
            return f"Error: could not reach the service ({type(exc).__name__}: {exc})."

    return handler


def register_http_tools(specs: list[dict]) -> list[str]:
    """Register each declared tool into THIS activation's registry overlay (F2).

    Scoped exactly like MCP tools: they exist for the run and vanish with `pop_scope`, so
    two concurrent runs with different tools cannot corrupt each other. A malformed or
    clashing spec is skipped with a diagnostic rather than aborting the run — one bad tool
    must not cost the user their proof.
    """
    registered: list[str] = []
    for spec in specs or []:
        name = str(spec.get("name") or "").strip()
        if not name or not spec.get("url"):
            continue
        if is_registered(name):
            diagnostics.report(
                "degraded", "tool.name_clash",
                f"The custom tool {name!r} shares its name with an existing tool, so it "
                f"was not loaded.",
                source="tool", remedy="Rename it under Library → Tools.", once=True, tool=name,
            )
            continue
        schema = {
            "name": name,
            "description": str(spec.get("description") or ""),
            "input_schema": spec.get("input_schema") or {"type": "object", "properties": {}},
        }
        register(Tool(name=name, schema=schema, handler=make_handler(spec)), scoped=True)
        registered.append(name)
    return registered
