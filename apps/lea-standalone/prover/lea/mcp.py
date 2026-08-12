"""MCP integration — connect to configured MCP servers and register their tools.

External MCP servers (filesystem, git, web search, a Lean server, ...) expose
tools we don't have to write. This manager connects to each server named in
`mcp.servers`, lists its tools, and registers each into the shared tool registry
as `<server>__<tool>` — after which they are ordinary tools to the loop.

Bridging sync ↔ async: the MCP SDK is asyncio-based and its client contexts use
anyio cancel scopes that must be entered and exited in the *same* task. So we run
one long-lived `_serve` coroutine on a private event loop in a background thread:
it opens every session, signals ready, then parks on a stop event (holding the
contexts open). Tool calls are dispatched onto that loop with
`run_coroutine_threadsafe`, giving the loop the plain `dict -> str` handlers it
expects. Shutdown sets the stop event so `_serve` unwinds the contexts itself.

A server that fails to start is warned-and-skipped (the run continues with the
other tools); it does not abort the agent.
"""

import asyncio
import atexit
import os
import re
import sys
import tempfile
import threading
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from .errors import McpError
from .registry import Tool, is_registered, register, unregister


def _warn(msg: str) -> None:
    print(f"[mcp] {msg}", file=sys.stderr)


# How much of a failed server's stderr to keep. A crashing server's *last* words carry
# the cause (lean-lsp-mcp ends with a one-line ValueError naming the missing lakefile),
# so we keep a bounded tail rather than the whole log — a healthy server chatters INFO
# lines for the life of the run and would otherwise grow without limit.
_STDERR_TAIL_BYTES = 4000


def _new_stderr_capture():
    """A real file to hand a stdio child as its stderr.

    The MCP SDK's `stdio_client(server, errlog=...)` defaults to `sys.stderr`, which is
    why a failed server's actual cause went straight to the terminal and was lost — the
    caller saw only the SDK's generic "Connection closed". Capturing it is what lets
    `startup_errors` carry the real reason to the UI (v2.5 A3).

    It must be an **OS-level file**, not a file-like object: `errlog` is passed through
    to the subprocess as `stderr=`, so it is duped to a real descriptor. A `TextIO`
    duck-type fails with `AttributeError: no attribute 'fileno'` — and, because the
    connection then never opens, it takes the healthy path down with it.
    """
    return tempfile.TemporaryFile(mode="w+b")


def _tail_of(f) -> str:
    """The last `_STDERR_TAIL_BYTES` of a capture file. A crashing server's *last* words
    carry the cause (lean-lsp-mcp ends with a one-line ValueError naming the missing
    lakefile), and seeking to the end bounds the read regardless of how chatty a
    long-lived healthy server has been."""
    if f is None:
        return ""
    try:
        f.flush()
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - _STDERR_TAIL_BYTES))
        return f.read().decode("utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return ""


# A crashing stdio server's stderr is usually a full Python traceback, and for an
# exception group it is wrapped in box-drawing characters. The ONE line that says what
# to do is buried in the middle, so the raw tail ends on `+------` — useless to the
# person who has to fix it.
_ERROR_LINE_RE = re.compile(r"^\w*(?:Error|Exception)\b\s*:\s*\S")
_DECORATION = set("+-|_= ")


def summarize_stderr(tail: str) -> str:
    """The single most informative line of a failed server's stderr.

    Prefers the LAST `SomeError: message` line — in an exception group the outer
    `ExceptionGroup:` comes first and the specific cause last, so "last wins" picks
    the actionable one (lean-lsp-mcp's "must contain lean-toolchain and either
    lakefile.lean or lakefile.toml"). Falls back to the last line of real prose, and
    to "" when there is nothing usable. The full tail is kept separately — this is
    the headline, not a replacement.
    """
    lines = [ln.strip().lstrip("|+").strip() for ln in (tail or "").splitlines()]
    lines = [ln for ln in lines if ln and not set(ln) <= _DECORATION]
    for line in reversed(lines):
        if _ERROR_LINE_RE.match(line):
            # Drop the exception CLASS ("ValueError: ") — it is noise to the person
            # reading this, and the raw tail keeps it for anyone debugging. What is
            # left is the sentence the server author actually wrote.
            return line.split(":", 1)[1].strip()
    return lines[-1] if lines else ""


def _child_env(spec: dict) -> dict[str, str] | None:
    """The environment a stdio MCP child is given (v2.5 A7).

    Base is the SDK's `get_default_environment()` — a deliberate allowlist (PATH, HOME,
    SHELL, ...) — plus the spec's literal `env` values, plus the *host* values of any
    variable names listed in `env_from`.

    This used to be `{**os.environ, **cfg_env}` whenever a spec set any env at all, to
    stop a bare `env` from stripping PATH. It fixed that by handing the child EVERY
    variable this process holds. Measured: a spec setting only `LEAN_PROJECT_PATH` gave
    the child 54 variables including `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`,
    `OPENAI_API_KEY` and `GEMINI_API_KEY` — every provider credential the adapter had
    exported. An MCP server is third-party code; it gets what it declares, not the
    keyring. `get_default_environment()` keeps PATH, so the original bug stays fixed.

    `env_from` is how a server asks for a credential WITHOUT the secret living in its
    spec: the NAME travels in config, the VALUE is read here at spawn. That is what
    keeps a stored/serialized spec free of secrets — `LeaConfig` promises it is "safe
    to log or serialize", and `mcp_servers` is a field on it.

    Returns None when nothing is configured, so the SDK applies its own default (the
    same allowlist this would build).
    """
    literal = spec.get("env") or {}
    passthrough = spec.get("env_from") or []
    if not literal and not passthrough:
        return None
    env = get_default_environment()
    env.update({str(k): str(v) for k, v in literal.items()})
    for name in passthrough:
        value = os.environ.get(name)
        if value is None:
            # Not fatal: the server starts and fails its own way (usually a 401 on first
            # use). Surfacing this as a proper `degraded` diagnostic is G1's job.
            _warn(f"env_from names {name!r}, which is not set; the server will start without it")
            continue
        env[name] = value
    return env


def _stringify(result) -> str:
    """Flatten an MCP CallToolResult into the string the loop expects."""
    parts = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else f"[{getattr(block, 'type', 'content')}]")
    out = "\n".join(parts) if parts else "(no content)"
    if getattr(result, "isError", False):
        out = "Error: " + out
    return out


class MCPManager:
    """Lifecycle owner for the configured MCP servers and their registered tools."""

    def __init__(self, servers: dict[str, dict]):
        self.servers = servers or {}
        self.tool_names: list[str] = []           # registry names we added (server__tool)
        # Tools discovered on the MCP loop thread, registered later on the caller thread
        # (item 27): {server, tool, description, input_schema}. Discovery and registration
        # are split because registration must run where the per-activation registry overlay
        # is visible — the agent thread — not the MCP background thread.
        self._discovered: list[dict] = []
        # A3: servers that failed to start, as {server, transport, message, stderr_tail}.
        # A failure used to be a stderr warning only, so a UI run silently continued with
        # zero MCP tools and no way to say why. The agent loop turns each of these into a
        # `degraded` Diagnostic the human actually sees.
        self.startup_errors: list[dict] = []
        self._stderr_captures: dict[str, object] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._sessions: dict[str, ClientSession] = {}
        self._stop_event: asyncio.Event | None = None
        self._serve_future = None
        self._loop_ready = threading.Event()
        self._started = threading.Event()         # set when _serve finished setup (or failed)

    # ---- lifecycle ----------------------------------------------------------

    def start(self, *, register: bool = True) -> None:
        """Start the background loop and connect every configured server (blocking
        until startup finishes). No-op when no servers are configured.

        `register=False` connects and discovers but does NOT register into the tool
        registry — the pooled path (A8) registers once per *run* instead, so one live
        connection can serve many activations. Direct callers keep the default.
        """
        if not self.servers:
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="lea-mcp")
        self._thread.start()
        self._loop_ready.wait()
        self._serve_future = asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
        self._started.wait()
        # Discovery (on the loop thread) is done; register on THIS (caller) thread so the
        # tools land in the caller's per-activation registry overlay (item 27), not the
        # loop thread's context where the overlay isn't visible.
        if register:
            self.register_discovered()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()
        self._loop.close()

    async def _serve(self) -> None:
        """Open all sessions in THIS task, signal ready, park until stop, then the
        `async with` unwinds every context in this same task (anyio-safe)."""
        self._stop_event = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                for name, spec in self.servers.items():
                    try:
                        await self._connect_one(stack, name, spec)
                    except Exception as e:
                        _warn(f"server {name!r} failed to start: {e}; continuing without its tools")
                        # A3: record it structurally too. `e` is often the SDK's generic
                        # "Connection closed"; the child's stderr tail is where the real
                        # cause lives, so both travel together.
                        capture = self._stderr_captures.get(name)
                        self.startup_errors.append({
                            "server": name,
                            "transport": "stdio" if "command" in spec else spec.get("transport", "http"),
                            "message": f"{type(e).__name__}: {e}",
                            "stderr_tail": _tail_of(capture),
                        })
                self._started.set()
                await self._stop_event.wait()
        finally:
            self._started.set()  # ensure start() unblocks even if setup raised

    async def _connect_one(self, stack: AsyncExitStack, name: str, spec: dict) -> None:
        if "command" in spec:
            # A7: the child gets a safe base + what the spec declares — never this
            # process's whole environment. See `_child_env`.
            env = _child_env(spec)
            params = StdioServerParameters(
                command=spec["command"],
                args=spec.get("args", []),
                env=env,
                cwd=spec.get("cwd"),
            )
            # A3: capture the child's stderr instead of letting the SDK's default
            # (`sys.stderr`) swallow it. This is the only channel carrying the real
            # failure — the exception the caller sees is usually "Connection closed".
            capture = _new_stderr_capture()
            self._stderr_captures[name] = capture
            read, write = await stack.enter_async_context(stdio_client(params, errlog=capture))
        else:  # url-based remote server
            url = spec["url"]
            headers = spec.get("headers")
            if spec.get("transport") == "sse":
                read, write = await stack.enter_async_context(sse_client(url, headers=headers))
            else:  # streamable HTTP (current standard); yields a 3-tuple
                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(url, headers=headers)
                )
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listed = await session.list_tools()
        self._sessions[name] = session
        # DISCOVER only here (on the loop thread) — registration into the tool registry
        # happens later on the caller thread (`_register_discovered`), where the
        # per-activation overlay is visible.
        for t in listed.tools:
            self._discovered.append({
                "server": name,
                "tool": t.name,
                "description": t.description or "",
                "input_schema": t.inputSchema or {"type": "object", "properties": {}},
            })

    def register_discovered(self) -> list[str]:
        """Register every discovered MCP tool — on the CALLING (agent) thread, so each
        lands in this run's registry overlay (item 27). Runs after `_serve` finished
        discovery, so `self._discovered` is complete.

        Callable once per run against a pooled manager (A8): each activation opens a
        fresh overlay, so the same names register cleanly again and are dropped by that
        run's `pop_scope`. `tool_names` is REPLACED rather than appended for exactly
        that reason — otherwise a pooled manager would accumulate a duplicate entry per
        run for the life of the process."""
        self.tool_names = []
        for spec in self._discovered:
            server, real = spec["server"], spec["tool"]
            # Expose the tool by its bare name (how models expect MCP tools, à la
            # Claude Desktop/Cursor). Only on a name clash do we prefix with
            # `<server>__` to disambiguate. The handler always calls the real MCP
            # tool name on the server, regardless of the registry display name.
            tname = real
            if is_registered(tname):
                prefixed = f"{server}__{real}"
                if is_registered(prefixed):
                    _warn(f"tool {real!r} from server {server!r} clashes even after prefixing; skipping")
                    continue
                _warn(f"tool {real!r} from server {server!r} clashes; exposing it as {prefixed!r}")
                tname = prefixed
            schema = {
                "name": tname,
                "description": spec["description"],
                "input_schema": spec["input_schema"],
            }
            # scoped=True → this run's overlay when one is open, else the global (the
            # standalone/unit-test path), so pre-item-27 direct MCPManager use is unchanged.
            register(Tool(name=tname, schema=schema, handler=self._make_handler(server, real)), scoped=True)
            self.tool_names.append(tname)

    def _close_captures(self) -> None:
        """Release the stderr capture files (A3). Each server's failure tail was already
        materialized into `startup_errors` at failure time, so closing here loses nothing
        — and a child still shutting down holds its own dup'd descriptor, so it can keep
        writing safely. Without this, a pooled/long-lived manager would leak one open
        temp file per server per run."""
        for f in self._stderr_captures.values():
            try:
                f.close()
            except (OSError, ValueError):
                pass
        self._stderr_captures.clear()

    def stop(self) -> None:
        """Unregister our tools and unwind all sessions/loop. Safe to call once."""
        for n in self.tool_names:
            unregister(n)
        self.tool_names.clear()
        try:
            if self._loop is None:
                return
            if self._stop_event is not None:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            if self._serve_future is not None:
                try:
                    self._serve_future.result(timeout=10)
                except Exception as e:
                    _warn(f"error during shutdown: {e}")
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._loop = None
        finally:
            self._close_captures()

    # ---- tool dispatch ------------------------------------------------------

    def _make_handler(self, server: str, tool: str):
        def handler(args: dict) -> str:
            if self._loop is None:
                raise McpError(f"MCP loop not running for {server}__{tool}")
            fut = asyncio.run_coroutine_threadsafe(self._acall(server, tool, args), self._loop)
            return fut.result()

        return handler

    async def _acall(self, server: str, tool: str, args: dict) -> str:
        session = self._sessions.get(server)
        if session is None:
            raise McpError(f"no live MCP session for server {server!r}")
        result = await session.call_tool(tool, args or {})
        return _stringify(result)

    def is_alive(self) -> bool:
        """True when the loop thread is running and at least one session is open."""
        return bool(self._sessions) and self._loop is not None and self._thread is not None \
            and self._thread.is_alive()


# --- cross-run connection pool (v2.5 A8) ---------------------------------------
#
# `run_events` used to build and tear down an MCPManager per activation, so every run
# spawned a fresh server. Measured against lean-lsp-mcp: the first Lean call cost 51.8s
# on run 1 and 34.2s on run 2 — the language server inside is cold again each time,
# while a second call in the SAME run is ~0.0s. In the UI every user message is a run,
# so an MCP-enabled session paid ~35s per message.
#
# This is precisely the problem `lsp_daemon.py` already solves for `lean_check` (a
# persistent `lake env lean --server` per Lake root, ~0.2s vs ~88s cold), so the fix is
# the same shape: keep the CONNECTION alive across runs, while each run still registers
# its own tools into its own registry overlay (item 27) and drops them at `pop_scope`.
# Isolation of tool *registration* was never the expensive part.
_pool: dict[str, tuple["MCPManager", list]] = {}   # key -> (manager, [last_used_monotonic])
_pool_lock = threading.Lock()

# A pooled Lean server holds Mathlib in RAM, so an abandoned one is expensive. Reaped
# lazily on the next `acquire`; a background reaper is a follow-up, not this slice.
_IDLE_TIMEOUT = float(os.environ.get("LEA_MCP_IDLE_TIMEOUT", "1800"))


def _pool_key(servers: dict) -> str:
    import json
    return json.dumps(servers, sort_keys=True, default=str)


def _reap_idle(now: float) -> None:
    """Drop pooled managers unused for longer than `_IDLE_TIMEOUT`. Caller holds the lock."""
    for key in [k for k, (_, used) in _pool.items() if now - used[0] > _IDLE_TIMEOUT]:
        mgr, _ = _pool.pop(key)
        try:
            mgr.stop()
        except Exception as exc:  # noqa: BLE001 — reaping must never break the acquiring run
            _warn(f"error reaping idle pooled servers: {exc}")


def acquire(servers: dict) -> "MCPManager | None":
    """A started manager for `servers`, reusing a pooled connection when one is live.

    The caller registers the tools into ITS OWN activation overlay
    (`manager.register_discovered()`) and must NOT call `stop()` — the pool owns the
    lifetime. Returns None when nothing is configured.

    A manager that came up with **no live session** is deliberately not pooled, so the
    next run retries instead of caching a failure the user may have already fixed (a
    corrected path, a newly-installed binary). Its `startup_errors` are therefore
    re-reported each run — which is honest: the capability is still missing.
    """
    if not servers:
        return None
    import time
    key, now = _pool_key(servers), time.monotonic()
    with _pool_lock:
        _reap_idle(now)
        entry = _pool.get(key)
        if entry is not None and entry[0].is_alive():
            entry[1][0] = now
            return entry[0]
        if entry is not None:            # died since last use — drop and rebuild
            _pool.pop(key, None)
    # Connect OUTSIDE the lock: startup blocks (a cold server takes seconds) and holding
    # the lock would serialize unrelated runs behind it.
    mgr = MCPManager(servers)
    mgr.start(register=False)
    with _pool_lock:
        if not mgr.is_alive():
            return mgr                    # usable for its startup_errors; not cached
        existing = _pool.get(key)
        if existing is not None and existing[0].is_alive():
            # Another run built one while we were connecting; keep theirs, drop ours.
            mgr.stop()
            existing[1][0] = now
            return existing[0]
        _pool[key] = (mgr, [now])
        return mgr


def shutdown_pool() -> None:
    """Stop and clear every pooled manager (process exit, and test isolation)."""
    with _pool_lock:
        for mgr, _ in _pool.values():
            try:
                mgr.stop()
            except Exception as exc:  # noqa: BLE001
                _warn(f"error during pool shutdown: {exc}")
        _pool.clear()


# Pooling is what makes this necessary: a per-run manager tore its servers down in the
# run's `finally`, whereas a pooled one deliberately outlives every run. Without this an
# interrupted or exiting process could orphan a `lean --worker` holding Mathlib in RAM.
# Registered here, in the module that owns the pool, so no embedder has to remember.
atexit.register(shutdown_pool)
