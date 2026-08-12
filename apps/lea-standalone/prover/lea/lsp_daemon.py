"""Persistent `lake env lean --server` daemon per Lake project root.

Keeps Mathlib oleans mmapped in one long-running Lean LSP server process
instead of cold-spawning `lake env lean <file>` for every `lean_check` call.

Headline win measured on FQB: ~0.21 s per in-place edit vs. ~88 s for cold
subprocess. See `tests/lsp/README.md` for the benchmark.

This is a *real* JSON-RPC/LSP client, not a synchronous shim: one reader thread
demultiplexes the server's stream, routing responses by request `id` and
notifications by document `uri`, so one warm server can serve many concurrent
checks (Lean spawns one file-worker per open document and is built for
concurrent requests). Readiness is the server's own signal — the Lean-specific
`textDocument/waitForDiagnostics` request, which resolves only once a version's
diagnostics are final — not a timing heuristic. See `tests/lsp/` for the
dispatch unit test and the concurrent real-Lean guard.

Falls back transparently to the caller (which should re-run via subprocess)
on any LSP error or server crash. Set `LEA_DISABLE_LSP=1` to skip entirely.
"""
from __future__ import annotations
import atexit
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from queue import Queue, Empty, Full


# Restart a daemon after this many checks. Bounds memory growth from Lean's
# per-document elaboration cache. Tunable via env var.
_RESTART_AFTER = int(os.environ.get("LEA_LSP_RESTART_AFTER", "500"))

# Hard timeout for one check (incl. cold first-open). Subsumed by the
# caller's LEAN_CHECK_TIMEOUT if larger. This is the ONLY time-based bound in
# a check now — the readiness signal itself is `waitForDiagnostics`, not a
# timer (see `LeanDaemon.check`).
_CHECK_TIMEOUT = int(os.environ.get("LEAN_CHECK_TIMEOUT", "900"))

# Handshake timeout (initialize response). The server answers `initialize`
# before it touches Mathlib, so this is fast even cold.
_INIT_TIMEOUT = 120

# Mapping from LSP severity int → string matching `lake env lean` output.
_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}

# Server→client requests we acknowledge with a null result. The Lean server
# genuinely sends these (measured: `client/registerCapability` with a *string*
# id, plus `workspace/*/refresh` on a ~2s cadence) and RETRIES a refresh until
# acked — so acking is not just protocol-correct, it stops a retry storm. They
# carry both a `method` and an `id`; the dispatcher must never mistake one for
# a response to our own request (their ids collide with ours), which is exactly
# why `_dispatch` checks `method` first.
_ACK_METHODS = {
    "client/registerCapability",
    "client/unregisterCapability",
    "workspace/semanticTokens/refresh",
    "workspace/inlayHint/refresh",
    "workspace/codeLens/refresh",
    "workspace/diagnostic/refresh",
}
_METHOD_NOT_FOUND = -32601


def _encode(msg: dict) -> bytes:
    body = json.dumps(msg).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


class _Transport:
    """Owns the `lake env lean --server` subprocess and JSON-RPC framing.

    The only Lean-agnostic layer: it spawns the process, frames/parses
    messages, serializes writes so two threads can't interleave a
    `Content-Length` header with another body on the unbuffered pipe, and hands
    each parsed message to a callback. On stream close it delivers a single
    `None` so the owner can fail every waiter. Injected as a seam so the
    dispatcher is testable with a fake server and no Lean toolchain.
    """

    def __init__(self, command: list[str], cwd: str):
        self._command = command
        self._cwd = cwd
        self._proc: subprocess.Popen | None = None
        self._io_lock = threading.Lock()
        self._on_message = None

    def start(self, on_message) -> bool:
        self._on_message = on_message
        try:
            self._proc = subprocess.Popen(
                self._command, cwd=self._cwd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError:
            return False
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        return True

    def send(self, msg: dict) -> None:
        """Serialize a message onto the pipe. Raises on a dead pipe."""
        data = _encode(msg)
        with self._io_lock:
            if self._proc is None or self._proc.stdin is None:
                raise RuntimeError("transport not started")
            self._proc.stdin.write(data)
            self._proc.stdin.flush()

    def poll(self):
        return None if self._proc is None else self._proc.poll()

    def close(self) -> None:
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _reader(self) -> None:
        stream = self._proc.stdout
        while True:
            headers = {}
            while True:
                line = stream.readline()
                if not line:
                    self._on_message(None)  # EOF → owner fails all waiters
                    return
                s = line.decode("utf-8", errors="replace").strip()
                if s == "":
                    break
                k, _, v = s.partition(":")
                headers[k.strip().lower()] = v.strip()
            n = int(headers.get("content-length", 0))
            if n == 0:
                continue
            body = stream.read(n).decode("utf-8", errors="replace")
            try:
                msg = json.loads(body)
            except json.JSONDecodeError:
                continue
            try:
                self._on_message(msg)
            except Exception:
                pass  # a routing bug must never kill the reader

    def _drain_stderr(self) -> None:
        for _ in iter(self._proc.stderr.readline, b""):
            pass  # discard; LSP server stderr is mostly info noise


class LeanDaemon:
    """One `lake env lean --server` instance scoped to a Lake project root.

    Serves many concurrent `check()` calls over one warm server: responses are
    routed to per-request waiters by `id`, notifications to per-`uri` queues.
    Distinct documents check in parallel (Lean gives each its own file worker);
    two checks of the *same* document serialize on a per-uri lock, because a
    Lean file worker holds one state per uri and a version bump can't make two
    contents coexist.
    """

    def __init__(self, lake_root: str, transport_factory=None):
        self.lake_root = lake_root
        self.broken = False
        # Set by mark_stale() when another process (rebuild_module's `lake
        # build`) changed a module this daemon may have imported. Lean's import
        # cache is process-lifetime, so the whole server is restarted on the
        # next check. (A finer fix Lean itself supports — reopen the affected
        # uri with `dependencyBuildMode: "once"` on an "Imports are out of
        # date" diagnostic — is left for the deferred staleness work.)
        self.stale = False

        # Transport seam.
        factory = transport_factory or (
            lambda: _Transport(["lake", "env", "lean", "--server"], lake_root)
        )
        self._transport = factory()

        # Dispatch state.
        self._state_lock = threading.Lock()
        self.opened: set[str] = set()          # uris seen via didOpen
        self._version = 0                       # monotonic LSP document version
        self._check_count = 0                   # for _RESTART_AFTER
        self._uri_queues: dict[str, Queue] = {}  # uri → notifications for the active check
        self._uri_locks: dict[str, threading.Lock] = {}

        self._id_lock = threading.Lock()
        self._next_id = 0
        self._pending_lock = threading.Lock()
        self._pending: dict[int, Queue] = {}    # our-request id → response slot

        # Lease / retire state (see check_via_lsp): a retired daemon is removed
        # from the registry so no new leases attach, and shut down only once its
        # last in-flight check drains.
        self._lease_lock = threading.Lock()
        self._leases = 0
        self._retiring = False
        self._shut = False

    # ---- lifecycle ----
    def start(self) -> bool:
        """Spawn the server and run the LSP handshake. False on failure."""
        if not self._transport.start(self._dispatch):
            return False
        try:
            self._request("initialize", {
                "processId": os.getpid(),
                "rootUri": Path(self.lake_root).as_uri(),
                "capabilities": {"textDocument": {"publishDiagnostics": {}}},
            }, timeout=_INIT_TIMEOUT)
            self._transport.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            return True
        except Exception:
            self.broken = True
            return False

    def shutdown(self) -> None:
        if self.broken and self._transport.poll() is not None:
            return
        try:
            self._transport.send({"jsonrpc": "2.0", "id": self._nxt(), "method": "shutdown"})
            self._transport.send({"jsonrpc": "2.0", "method": "exit"})
        except Exception:
            pass
        self._transport.close()
        self.broken = True
        self._fail_all_waiters()

    def close_documents_under(self, dir_path: str) -> int:
        """`didClose` every open document whose file lives under ``dir_path``,
        drop its per-uri state, and return how many were closed.

        Lean spawns one ``lean --worker`` per *open* document and holds it until
        the document is closed. This daemon otherwise never closes a document, so
        every unique file it checks leaks a worker for the server's lifetime.
        Sub-agents make that acute: each ``proof-candidate`` child checks a
        candidate at a unique scratch path, leaking one worker per child (B1 —
        ~118 workers, GBs resident, in a single session). Called when the run that
        owns ``dir_path`` finishes (a sub-agent returning), this reaps those
        workers at the one moment nothing will re-check those files: the parent
        collates via the filesystem into its own canonical file, never the scratch
        path.

        Uris are stored resolved (``lean_check`` does ``Path(path).resolve()``
        before handing us the path), so the prefix is resolved to match — no
        symlink (`/var`→`/private/var`) misses. A uri mid-check (still in
        ``_uri_queues``) is left for its own check to close; a finished child has
        none. Best-effort: a dead pipe just means the server is already gone.
        """
        if self.broken:
            return 0
        try:
            prefix = Path(dir_path).resolve().as_uri().rstrip("/") + "/"
        except Exception:
            return 0
        with self._state_lock:
            victims = [u for u in self.opened
                       if u.startswith(prefix) and u not in self._uri_queues]
        closed = 0
        for uri in victims:
            try:
                self._transport.send({
                    "jsonrpc": "2.0", "method": "textDocument/didClose",
                    "params": {"textDocument": {"uri": uri}},
                })
                closed += 1
            except Exception:
                self.broken = True
                break
        if victims:
            with self._state_lock:
                for uri in victims:
                    self.opened.discard(uri)
                    self._uri_locks.pop(uri, None)
        return closed

    # ---- the check ----
    def check(self, file_path: str, content: str) -> str:
        """Open or update `file_path` with `content`; return its diagnostics.

        Blocks until `textDocument/waitForDiagnostics` confirms the version is
        fully elaborated, then returns the final diagnostics for that version.
        Empty → clean; otherwise the formatted errors/warnings.
        """
        if self.broken:
            raise RuntimeError("daemon not alive")
        uri = Path(file_path).as_uri()

        # Serialize same-document checks: one file worker, one doc state per uri.
        with self._uri_lock(uri):
            with self._state_lock:
                self._version += 1
                version = self._version
                self._check_count += 1
                is_open = uri in self.opened
                q: Queue = Queue()
                self._uri_queues[uri] = q
            try:
                self._send_document(uri, content, version, is_open)
                with self._state_lock:
                    self.opened.add(uri)
                # waitForDiagnostics resolves only once this version's
                # diagnostics are final (measured on v4.29.0: its response
                # trails the last publishDiagnostics on the wire). The single
                # reader thread processes that publishDiagnostics — putting it
                # on `q` — strictly before it processes this response, so once
                # we're here every diagnostic for `version` is already queued.
                self._request(
                    "textDocument/waitForDiagnostics",
                    {"uri": uri, "version": version},
                    timeout=_CHECK_TIMEOUT,
                )
                return self._format(file_path, self._collect(q, version))
            finally:
                with self._state_lock:
                    self._uri_queues.pop(uri, None)

    def _send_document(self, uri: str, content: str, version: int, is_open: bool) -> None:
        try:
            if is_open:
                self._transport.send({
                    "jsonrpc": "2.0", "method": "textDocument/didChange",
                    "params": {
                        "textDocument": {"uri": uri, "version": version},
                        "contentChanges": [{"text": content}],
                    },
                })
            else:
                self._transport.send({
                    "jsonrpc": "2.0", "method": "textDocument/didOpen",
                    "params": {"textDocument": {
                        "uri": uri, "languageId": "lean4",
                        "version": version, "text": content,
                    }},
                })
        except (BrokenPipeError, OSError, RuntimeError):
            self.broken = True
            raise

    def _collect(self, q: Queue, version: int) -> list:
        """Drain queued notifications; return the last publishDiagnostics whose
        version is current (or unversioned). The waitForDiagnostics response has
        already fired, so all of this version's diagnostics are present."""
        diags: list = []
        while True:
            try:
                m = q.get_nowait()
            except Empty:
                break
            if m is None:
                raise RuntimeError("server stream closed")
            if m.get("method") != "textDocument/publishDiagnostics":
                continue
            p = m.get("params", {})
            v = p.get("version")
            if v is None or v >= version:
                diags = p.get("diagnostics", [])
        return diags

    # ---- dispatch (reader thread) ----
    def _dispatch(self, msg) -> None:
        if msg is None:
            self.broken = True
            self._fail_all_waiters()
            return
        method = msg.get("method")
        msg_id = msg.get("id")
        # method AND id → a server→client REQUEST, never a response. Its id
        # lives in the server's namespace and collides with ours; checking
        # `method` first is what keeps us from reading it as our answer.
        if method is not None and msg_id is not None:
            self._ack_server_request(msg_id, method)
            return
        if msg_id is not None:
            with self._pending_lock:
                slot = self._pending.get(msg_id)
            if slot is not None:
                try:
                    slot.put_nowait(msg)
                except Full:
                    pass
            return
        if method is not None:
            uri = _uri_of(method, msg.get("params") or {})
            if uri is None:
                return  # window/logMessage, global $/lean/* — nothing waits on it
            with self._state_lock:
                q = self._uri_queues.get(uri)
            if q is not None:
                q.put(msg)

    def _ack_server_request(self, msg_id, method: str) -> None:
        if method in _ACK_METHODS:
            payload = {"jsonrpc": "2.0", "id": msg_id, "result": None}
        else:
            payload = {"jsonrpc": "2.0", "id": msg_id,
                       "error": {"code": _METHOD_NOT_FOUND, "message": f"unsupported: {method}"}}
        try:
            self._transport.send(payload)
        except Exception:
            pass

    def _request(self, method: str, params: dict, timeout: float):
        """Send a request and block for its response, routed by id."""
        req_id = self._nxt()
        slot: Queue = Queue(maxsize=1)
        with self._pending_lock:
            self._pending[req_id] = slot
        try:
            self._transport.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            try:
                msg = slot.get(timeout=timeout)
            except Empty:
                raise RuntimeError(f"{method} timed out after {timeout}s")
        except (BrokenPipeError, OSError):
            self.broken = True
            raise
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)
        if msg is None:
            self.broken = True
            raise RuntimeError("server stream closed")
        if "error" in msg:
            raise RuntimeError(f"LSP error: {msg['error']}")
        return msg.get("result")

    def _fail_all_waiters(self) -> None:
        """Unblock every pending request and active check with a sentinel."""
        with self._pending_lock:
            slots = list(self._pending.values())
        for s in slots:
            try:
                s.put_nowait(None)
            except Full:
                pass
        with self._state_lock:
            queues = list(self._uri_queues.values())
        for qq in queues:
            qq.put(None)

    # ---- leases (called from check_via_lsp) ----
    def _acquire_lease(self) -> None:
        with self._lease_lock:
            self._leases += 1

    def _release_lease(self) -> None:
        with self._lease_lock:
            self._leases -= 1
            should = self._retiring and self._leases == 0 and not self._shut
            if should:
                self._shut = True
        if should:
            self.shutdown()

    def _retire(self) -> None:
        """Removed from the registry: shut down now if idle, else when the last
        in-flight check releases its lease."""
        with self._lease_lock:
            self._retiring = True
            should = self._leases == 0 and not self._shut
            if should:
                self._shut = True
        if should:
            self.shutdown()

    # ---- internal ----
    def _nxt(self) -> int:
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _uri_lock(self, uri: str) -> threading.Lock:
        with self._state_lock:
            lk = self._uri_locks.get(uri)
            if lk is None:
                lk = threading.Lock()
                self._uri_locks[uri] = lk
            return lk

    def _format(self, file_path: str, diags: list) -> str:
        # Only surface errors (severity 1) and warnings (severity 2) to match
        # subprocess `lake env lean` behavior. LSP info/hint diagnostics (3/4)
        # are hover-style metadata that confuse the agent (it interpreted an
        # `info:` path-resolution note as a real problem and went off-rails).
        diags = [d for d in diags if d.get("severity", 1) in (1, 2)]
        if not diags:
            return "OK — no errors, no warnings."
        lines = []
        for d in diags:
            sev = _SEVERITY.get(d.get("severity", 1), "error")
            r = d.get("range", {}).get("start", {})
            ln = r.get("line", 0) + 1
            col = r.get("character", 0) + 1
            msg = d.get("message", "").rstrip()
            lines.append(f"{file_path}:{ln}:{col}: {sev}: {msg}")
        return "\n".join(lines)


def _uri_of(method: str, params: dict) -> str | None:
    if method == "textDocument/publishDiagnostics":
        return params.get("uri")
    if method == "$/lean/fileProgress":
        return params.get("textDocument", {}).get("uri")
    return None


# ---- module-level cache ----
_daemons: dict[str, LeanDaemon] = {}
_lock = threading.Lock()


def check_via_lsp(file_path: str, content: str, lake_root: str) -> str:
    """Run `lean_check` via the persistent LSP daemon for `lake_root`.

    Raises on any failure so the caller can fall back to subprocess.
    """
    retiring = None
    with _lock:
        d = _daemons.get(lake_root)
        if d is not None and (d.broken or d.stale or d._check_count >= _RESTART_AFTER):
            # Retire it: out of the registry so no new lease attaches. Shutdown
            # is deferred (below, outside _lock) to the retire path, which fires
            # it now if idle or on the last in-flight check otherwise — never
            # under another check the way the old code tore the server down.
            del _daemons[lake_root]
            retiring, d = d, None
        if d is None:
            d = LeanDaemon(lake_root)
            if not d.start():
                raise RuntimeError(f"failed to start lean --server in {lake_root}")
            _daemons[lake_root] = d
        d._acquire_lease()
    if retiring is not None:
        retiring._retire()
    try:
        return d.check(file_path, content)
    finally:
        d._release_lease()


def mark_stale(lake_root: str) -> None:
    """Flag the persistent daemon for `lake_root`, if one is running, so its
    *next* check restarts the underlying `lean --server` process first.

    Call this after any real `lake build` (`tools.rebuild_module`) that
    changes a module's `.olean` on disk. Lean's server caches every module it
    has ever imported for the life of its process (see this module's
    docstring) -- a build in a different process can't reach into that cache
    and fix just the one module, so a full restart is the only guaranteed-
    correct remedy. This is deliberately lazy (flag now, restart on next use)
    rather than an eager synchronous restart: an eager restart would pay the
    cold Mathlib-reload cost inside whatever request triggered the rebuild,
    and would tear the shared daemon down out from under any unrelated
    request that happens to be mid-check. Callers that cannot wait for the
    next check to trigger this (the Overleaf lean pane's cascade re-check,
    which runs immediately after its own rebuild) should bypass the daemon
    entirely instead -- see `tools.lean_check_cold`.

    A no-op if no daemon has been started for `lake_root` yet (nothing to
    invalidate) or if `lake_root` doesn't match any tracked daemon.
    """
    with _lock:
        d = _daemons.get(lake_root)
        if d is not None:
            d.stale = True


def close_documents_under(dir_path: str) -> int:
    """Close (didClose) every open LSP document under ``dir_path`` across all live
    daemons, reaping the per-document ``lean --worker`` Lean keeps per open file.

    Call when a run that owns a scratch tree finishes — notably a sub-agent
    returning (B1); its candidate files are never checked again. Best-effort and
    safe: a no-op when the LSP path is disabled (no daemons started) or nothing
    under ``dir_path`` is open. Returns the number of documents closed.
    """
    with _lock:
        daemons = list(_daemons.values())
    total = 0
    for d in daemons:
        try:
            total += d.close_documents_under(dir_path)
        except Exception:
            pass
    return total


@atexit.register
def _shutdown_all():
    for d in list(_daemons.values()):
        d.shutdown()
    _daemons.clear()
