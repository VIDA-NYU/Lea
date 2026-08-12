"""LSP-over-WebSocket proxy for the live Canvas editor (v2.2 · D60/D61/D64).

The browser runs a real Monaco editor (`lean4monaco`) that speaks the Lean
language server over a WebSocket. This module is the seam that lean4web puts in
its Node `server/index.mjs`, re-implemented here so the browser talks only to the
one FastAPI backend on :8001 (no sidecar — D59):

  browser (lean4monaco)  --bare JSON per WS frame-->  [ this proxy ]  --Content-Length framed--> `lake serve`

Two format boundaries:
  * On the **WebSocket**, `vscode-ws-jsonrpc` sends one JSON-RPC object per text
    frame — *no* Content-Length framing.
  * On the **process stdio**, the Lean server uses LSP's `Content-Length` framing.
So the pump reframes in both directions and, in between, rewrites `file://` URIs
between the browser's virtual document URI and the real on-disk path (mirrors
lean4web's `urisToFilenames` / `FilenamesToUri`).

This is a **pass-through** — unlike `prover/lea/lsp_daemon.py` (the agent's batch
compiler behind the `lean_check` tool, which cooks diagnostics into a string), the
live editor needs the raw bidirectional stream (hover, completion, goals,
incremental diagnostics). The two never share a Lean process (D61): this spawns its
own `lake serve` per connection and kills it on disconnect; the daemon is untouched.

No sandbox (D63): local, single-user, and the agent already runs arbitrary Lean —
so the interactive server runs on the host like the daemon does. ⚠️ If the adapter
is ever exposed to untrusted remote users, reintroduce a container per session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path

from starlette.websockets import WebSocketDisconnect

logger = logging.getLogger("lea-interface.lsp_proxy")

# The command that launches the Lean language server for a Lake project. `lake
# serve` starts the standard Lean *watchdog* (the same one the VSCode extension
# drives), which lean4monaco expects. Overridable for odd toolchains.
_SERVE_CMD = os.environ.get("LEA_LSP_SERVE_CMD", "lake serve --").split()

# How long we wait for the Lean process to drain after the socket closes before
# we hard-kill it.
_TERM_GRACE = float(os.environ.get("LEA_LSP_TERM_GRACE", "3"))

# Concurrent `lake serve` processes allowed (AUDIT-2026-07-24 X3). Nothing bounded
# this: every WebSocket connection spawned one, each an 8 MiB-buffered process that
# loads Mathlib and is multi-GB resident, so a client opening sockets in a loop could
# swap the machine — and until S1 landed, any page the user had open could do it. Four
# matches `LEA_MAX_CONCURRENT_RUNS`: one live editor per concurrent run is the most a
# single human plausibly drives.
MAX_SESSIONS = max(1, int(os.environ.get("LEA_MAX_LSP_SESSIONS", "4")))
_session_slots = threading.BoundedSemaphore(MAX_SESSIONS)


def acquire_session_slot() -> bool:
    """Claim a slot for one `lake serve`, or False when the cap is reached."""
    return _session_slots.acquire(blocking=False)


def release_session_slot() -> None:
    """Release a slot claimed by :func:`acquire_session_slot`. Tolerates an extra
    release so a caller's `finally` can run unconditionally."""
    try:
        _session_slots.release()
    except ValueError:
        pass


# ── URI rewriting (port of lean4web's urisToFilenames / FilenamesToUri) ────────
#
# `prefix` is the absolute Lake root (no trailing slash, no scheme), e.g.
# "/abs/workspace". The browser opens a document whose URI is `file://` + a path
# relative to that root (e.g. "file:///proofs/<sid>/Main.lean"); prefixing turns
# it into the real file "file:///abs/workspace/proofs/<sid>/Main.lean", and
# stripping reverses it. This is exactly lean4web's *development-mode* rewrite
# (we, like their dev mode, run the server on the host with a real cwd — there is
# no bubblewrap mount remapping the paths for us).

def _escapes_root(path: str, prefix: str) -> bool:
    """Whether a rewritten filesystem path lands outside the Lake root.

    The rewrite is a blind string prefix, so a client-supplied URI carrying `..`
    (`file:///../../../../etc/passwd`) resolved to a real file anywhere on the host,
    and the Lean server would then read it and report its contents back through
    diagnostics and hover (AUDIT-2026-07-24 S1). Normalizing here — rather than
    trusting the client to send a well-formed relative path — is what makes the
    prefix rewrite safe.
    """
    root = os.path.realpath(prefix)
    return os.path.commonpath([os.path.realpath(path), root]) != root


def rewrite_client_to_server(obj, prefix: str):
    """Client → server: prefix every `file://` URI / rootUri and `rootPath` with
    the Lake root. Mutates and returns `obj`.

    A URI that escapes the Lake root after rewriting is left **unprefixed**, so it
    names nothing the server will open, rather than being silently redirected at a
    real file outside the project."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key in ("uri", "rootUri") and isinstance(val, str):
                rewritten = val.replace("file://", f"file://{prefix}", 1)
                path = rewritten[len("file://"):] if rewritten.startswith("file://") else None
                if path is not None and _escapes_root(path, prefix):
                    logger.warning("dropping LSP uri that escapes the lake root: %r", val)
                    continue
                obj[key] = rewritten
            elif key == "rootPath" and isinstance(val, str):
                candidate = os.path.join(prefix, val.lstrip("/"))
                if _escapes_root(candidate, prefix):
                    logger.warning("dropping LSP rootPath that escapes the lake root: %r", val)
                    continue
                obj[key] = candidate
            else:
                rewrite_client_to_server(val, prefix)
    elif isinstance(obj, list):
        for item in obj:
            rewrite_client_to_server(item, prefix)
    return obj


def rewrite_server_to_client(obj, prefix: str):
    """Server → client: strip the Lake root back out of every `uri`. Mutates and
    returns `obj`."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key == "uri" and isinstance(val, str):
                obj[key] = val.replace(prefix, "", 1)
            else:
                rewrite_server_to_client(val, prefix)
    elif isinstance(obj, list):
        for item in obj:
            rewrite_server_to_client(item, prefix)
    return obj


def _apply_client_to_server(message: dict, prefix: str) -> dict:
    """Rewrite one inbound message. lean4web leaves `textDocument/definition`
    request URIs untouched (the response is remapped on the way back), so we do
    the same to keep go-to-definition working."""
    if message.get("method") == "textDocument/definition":
        return message
    return rewrite_client_to_server(message, prefix)


def _encode(message: dict) -> bytes:
    body = json.dumps(message).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


async def _read_message(stdout: asyncio.StreamReader) -> dict | None:
    """Read one Content-Length-framed LSP message from the process. Returns None
    at EOF / on a malformed frame (caller then tears down)."""
    try:
        header = await stdout.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return None
    length = 0
    for line in header.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            try:
                length = int(line.split(b":", 1)[1].strip())
            except ValueError:
                return None
    if length <= 0:
        return None
    try:
        body = await stdout.readexactly(length)
    except asyncio.IncompleteReadError:
        return None
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        logger.debug("dropping non-JSON LSP frame from server")
        return None


class LspProxy:
    """One `lake serve` process bridged to one WebSocket, for the lifetime of the
    socket. Spawned lazily on connect (D61); killed on disconnect (idle-reap)."""

    def __init__(self, lake_root: Path, prefix: str):
        self.lake_root = lake_root
        self.prefix = prefix
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *_SERVE_CMD,
            cwd=str(self.lake_root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,  # server stderr is info noise
            # Mathlib LSP frames (e.g. full-file goal dumps) can be large; lift the
            # default 64 KiB reader limit so readuntil/readexactly don't overflow.
            limit=8 * 1024 * 1024,
        )
        logger.info("lake serve started (pid=%s) in %s", self.proc.pid, self.lake_root)

    async def _client_to_server(self, websocket) -> None:
        """Pump: browser WS frame → framed stdin."""
        assert self.proc and self.proc.stdin
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self.proc.stdin.write(_encode(_apply_client_to_server(message, self.prefix)))
            await self.proc.stdin.drain()

    async def _server_to_client(self, websocket) -> None:
        """Pump: framed stdout → browser WS frame."""
        assert self.proc and self.proc.stdout
        while True:
            message = await _read_message(self.proc.stdout)
            if message is None:
                return  # server EOF/closed
            rewrite_server_to_client(message, self.prefix)
            await websocket.send_text(json.dumps(message))

    async def pump(self, websocket) -> None:
        """Run both directions until either side closes, then tear down."""
        up = asyncio.create_task(self._client_to_server(websocket))
        down = asyncio.create_task(self._server_to_client(websocket))
        try:
            done, pending = await asyncio.wait(
                {up, down}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            # Retrieve the finished task's exception so asyncio doesn't warn about
            # an unretrieved one. A normal disconnect / server EOF ends the pump —
            # that's expected teardown, not an error.
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, WebSocketDisconnect):
                    logger.debug("lsp pump ended: %r", exc)
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self.proc is None:
            return
        proc, self.proc = self.proc, None
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
        logger.info("lake serve stopped (pid=%s)", proc.pid)


def resolve_target(abs_file: str) -> tuple[Path, str]:
    """(lake_root, editor_file_name) for a session's proof file.

    `editor_file_name` is the path **relative to the Lake root** — this is what the
    browser opens the Monaco model as, so the proxy's blind `file://`-prefix
    rewrite (D60/D64) lands it back on `abs_file`. Raises if no Lake project
    encloses the file."""
    p = Path(abs_file).resolve()
    for parent in [p.parent, *p.parent.parents]:
        if (parent / "lakefile.lean").exists() or (parent / "lakefile.toml").exists():
            return parent, p.relative_to(parent).as_posix()
    raise FileNotFoundError(f"no Lake project (lakefile) found above {abs_file}")
