"""Persistent Lean servers, with bounded recovery and per-Lake-root ownership.

The editor WebSocket server is independent. This module owns only subprocesses it
starts, and a check is complete only after exact-version diagnostics and Lean's
waitForDiagnostics barrier have both arrived.
"""
from __future__ import annotations

import atexit
from collections import OrderedDict
import json
import logging
import os
from pathlib import Path
from queue import Empty, Full, Queue
import select
import subprocess
import threading
import time
from uuid import uuid4

from .check_runtime import CheckBudget, CheckFailure, lsp_disabled, stop_process

_RESTART_AFTER = int(os.environ.get("LEA_LSP_RESTART_AFTER", "500"))
_CHECK_TIMEOUT = float(os.environ.get("LEAN_CHECK_TIMEOUT", "900"))
_INIT_TIMEOUT = 120
_MAX_IDLE_DOCUMENTS = 8
_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}
_ACK_METHODS = {
    "client/registerCapability", "client/unregisterCapability",
    "workspace/semanticTokens/refresh", "workspace/inlayHint/refresh",
    "workspace/codeLens/refresh", "workspace/diagnostic/refresh",
}
_METHOD_NOT_FOUND = -32601
_log = logging.getLogger("lea.lean_checks")
_RUNTIME_ID = uuid4().hex


def _encode(msg: dict) -> bytes:
    body = json.dumps(msg).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


class _Transport:
    def __init__(self, command: list[str], cwd: str):
        self._command, self._cwd = command, cwd
        self._proc = None
        self._io_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._failure_lock = threading.Lock()
        self._on_message = None
        self._threads = []
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        self.failure: CheckFailure | None = None
        self._ended = False
        self._closed = False

    def start(self, on_message) -> bool:
        self._on_message = on_message
        with self._close_lock:
            if self._closed:
                self.failure = CheckFailure("cancelled", "Lean transport was shut down", retryable=False)
                return False
            try:
                proc = subprocess.Popen(
                    self._command, cwd=self._cwd, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                    start_new_session=True,
                )
                self._proc = proc
                os.set_blocking(proc.stdin.fileno(), False)
            except OSError as exc:
                self.failure = CheckFailure("executable_missing" if isinstance(exc, FileNotFoundError)
                                            else "startup", str(exc), phase="initialization",
                                            retryable=not isinstance(exc, FileNotFoundError))
            if not self.failure:
                for fn in (self._reader, self._drain_stderr):
                    thread = threading.Thread(target=fn, args=(proc,), daemon=True)
                    self._threads.append(thread)
                    thread.start()
        if self.failure:
            self.close()
            return False
        return True

    def evidence(self):
        with self._stderr_lock:
            tail = bytes(self._stderr).decode("utf-8", errors="replace")
        return {"stderr_tail": tail, "exit_code": self.poll()}

    def _fail(self, exc: Exception):
        with self._failure_lock:
            if self._ended:
                return
            self._ended = True
            self.failure = exc if isinstance(exc, CheckFailure) else CheckFailure(
                "transport", f"{type(exc).__name__}: {exc}", **self.evidence())
        if self._on_message:
            self._on_message(None)

    def send(self, msg: dict, *, budget: CheckBudget | None = None):
        budget = budget or CheckBudget(5)
        data = memoryview(_encode(msg))
        frame_size = len(data)
        with budget.acquire(self._io_lock):
            proc = self._proc
            if proc is None or proc.stdin is None:
                raise CheckFailure("transport", "Lean transport is not running")
            if self.failure:
                raise _failure(self.failure)
            try:
                while data:
                    budget.remaining("lsp")
                    count = proc.stdin.write(data)
                    if count:
                        data = data[count:]
                    else:
                        # A nonblocking raw pipe returns None when full. Never hold
                        # an unbounded write or silently discard a short write.
                        select.select([], [proc.stdin], [], min(0.1, budget.remaining("lsp")))
            except CheckFailure as exc:
                if len(data) != frame_size:
                    # A cancellation/deadline must retain its caller-facing
                    # meaning, but nobody can reuse a partially written frame.
                    self._fail(CheckFailure("transport", "Lean write interrupted mid-frame",
                                            cause=exc.as_dict(), **self.evidence()))
                raise
            except (OSError, ValueError) as exc:
                self._fail(exc)
                raise _failure(self.failure) from exc

    def poll(self):
        return self._proc.poll() if self._proc is not None else None

    def close(self):
        deadline = time.monotonic() + 5
        if not self._close_lock.acquire(timeout=5):
            return
        try:
            self._closed = True
            proc = self._proc
            if proc is None:
                return
            stop_process(proc, deadline=deadline)
            self._fail(CheckFailure("transport", "Lean transport closed", **self.evidence()))
            if self.failure:
                self.failure.evidence.update(self.evidence())
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass
            for thread in self._threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=max(0, deadline - time.monotonic()))
            self._proc = None
        finally:
            self._close_lock.release()

    def _reader(self, proc=None):
        stream = (proc or self._proc).stdout
        try:
            while True:
                headers = {}
                header_bytes = 0
                while True:
                    line = stream.readline(8193)
                    if not line:
                        raise CheckFailure("transport", "EOF in Lean server stream", **self.evidence())
                    header_bytes += len(line)
                    if header_bytes > 8192:
                        raise ValueError("JSON-RPC headers exceed 8 KiB")
                    if line == b"\r\n":
                        break
                    key, sep, value = line.decode("ascii").strip().partition(":")
                    if not sep or key.lower() in headers:
                        raise ValueError("Malformed or duplicate JSON-RPC header")
                    headers[key.lower()] = value.strip()
                size = int(headers["content-length"])
                if not 0 < size <= 64 * 1024 * 1024:
                    raise ValueError("Invalid JSON-RPC Content-Length")
                body = bytearray()
                while len(body) < size:
                    chunk = stream.read(size - len(body))
                    if not chunk:
                        raise ValueError("Truncated JSON-RPC body")
                    body.extend(chunk)
                msg = json.loads(body.decode("utf-8"))
                if not isinstance(msg, dict):
                    raise ValueError("JSON-RPC message must be an object")
                self._on_message(msg)
        except Exception as exc:
            failure = exc if isinstance(exc, CheckFailure) else CheckFailure(
                "protocol", f"{type(exc).__name__}: {exc}", **self.evidence())
            self._fail(failure)

    def _drain_stderr(self, proc=None):
        stream = (proc or self._proc).stderr
        try:
            while chunk := stream.read(4096):
                with self._stderr_lock:
                    self._stderr.extend(chunk)
                    del self._stderr[:-32768]
        except (OSError, ValueError):
            pass


class LeanDaemon:
    def __init__(self, lake_root: str, transport_factory=None):
        self.lake_root = lake_root
        self.generation = uuid4().hex
        self.broken = False
        self.stale = False
        self.failure = None
        self.closed = threading.Event()
        self._transport = (transport_factory or (
            lambda: _Transport(["lake", "env", "lean", "--server"], lake_root)))()
        self._state_lock = threading.RLock()
        self.opened = set()
        self._idle = OrderedDict()
        self._version = self._check_count = 0
        self._uri_queues = {}
        self._uri_locks = {}
        self._uri_users = {}
        self._id_lock = threading.Lock()
        self._next_id = 0
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._lease_lock = threading.Lock()
        self._leases = 0
        self._retiring = self._shut = False
        self._shutdown_lock = threading.Lock()

    def _send(self, msg, budget=None):
        # Fake transports implement the same routing seam without OS I/O.
        if isinstance(self._transport, _Transport):
            self._transport.send(msg, budget=budget)
        else:
            self._transport.send(msg)

    def start(self, budget=None) -> bool:
        budget = budget or CheckBudget(_CHECK_TIMEOUT)
        try:
            with budget.measure("initialization"):
                if not self._transport.start(self._dispatch):
                    raise getattr(self._transport, "failure", None) or CheckFailure(
                        "startup", "Could not start Lean server", phase="initialization")
                self._request("initialize", {
                    "processId": os.getpid(), "rootUri": Path(self.lake_root).as_uri(),
                    "capabilities": {"textDocument": {"publishDiagnostics": {"versionSupport": True}}},
                }, timeout=_INIT_TIMEOUT, budget=budget)
                self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}}, budget)
            return True
        except Exception as exc:
            self.failure = _failure(exc, phase="initialization")
            self.broken = True
            self.shutdown()
            return False

    def shutdown(self):
        if not self._shutdown_lock.acquire(blocking=False):
            return
        try:
            if self.closed.is_set():
                return
            self.broken = True
            self._fail_all_waiters()
            # Closing the owned process group also reaps workers when the
            # protocol itself is damaged; no request can extend cleanup time.
            self._transport.close()
            self.closed.set()
        finally:
            self._shutdown_lock.release()

    def _close_uri_locked(self, uri, budget=None):
        if uri not in self.opened:
            return
        self._send({"jsonrpc": "2.0", "method": "textDocument/didClose",
                    "params": {"textDocument": {"uri": uri}}}, budget)
        self.opened.discard(uri)
        self._idle.pop(uri, None)
        if not self._uri_users.get(uri):
            self._uri_locks.pop(uri, None)

    def close_documents_under(self, dir_path):
        prefix = Path(dir_path).resolve().as_uri().rstrip("/") + "/"
        count = 0
        with self._state_lock:
            for uri in list(self.opened):
                if (uri.startswith(prefix) and not self._uri_users.get(uri)
                        and uri not in self._uri_queues):
                    self._close_uri_locked(uri)
                    count += 1
        return count

    def check(self, file_path: str, content: str, *, budget=None) -> str:
        budget = budget or CheckBudget(_CHECK_TIMEOUT)
        uri = Path(file_path).resolve().as_uri()
        # Count queued users too: close/evict must not replace a lock held by
        # someone who is waiting for the same document.
        with self._state_lock:
            lock = self._uri_locks.setdefault(uri, threading.Lock())
            self._uri_users[uri] = self._uri_users.get(uri, 0) + 1
            self._idle.pop(uri, None)
        try:
            with budget.acquire(lock):
                if self.broken:
                    raise self.failure or CheckFailure("transport", "Lean daemon is not alive")
                with self._state_lock:
                    self._version += 1
                    version = self._version
                    self._check_count += 1
                    q = Queue()
                    self._uri_queues[uri] = q
                    is_open = uri in self.opened
                try:
                    with budget.measure("lsp"):
                        self._send_document(uri, content, version, is_open, budget)
                        with self._state_lock:
                            self.opened.add(uri)
                        self._request("textDocument/waitForDiagnostics", {"uri": uri, "version": version},
                                      timeout=budget.remaining(), budget=budget)
                        return self._format(file_path, self._collect(q, version))
                except CheckFailure as exc:
                    exc = _failure(exc)
                    exc.evidence.update(uri=uri, document_version=version,
                                        daemon_generation=self.generation)
                    if exc.kind == "cancelled":
                        with self._state_lock:
                            self._close_uri_locked(uri)
                    raise exc
                finally:
                    with self._state_lock:
                        self._uri_queues.pop(uri, None)
        finally:
            with self._state_lock:
                self._uri_users[uri] -= 1
                if not self._uri_users[uri]:
                    del self._uri_users[uri]
                    if uri in self.opened:
                        self._idle[uri] = time.monotonic()
                    else:
                        self._uri_locks.pop(uri, None)
                while len(self._idle) > _MAX_IDLE_DOCUMENTS:
                    victim = next(iter(self._idle))
                    try:
                        self._close_uri_locked(victim)
                    except Exception as exc:
                        self.broken = True
                        self.failure = _failure(exc)
                        break

    def _send_document(self, uri, content, version, is_open, budget=None):
        doc = {"uri": uri, "version": version}
        method = "textDocument/didChange" if is_open else "textDocument/didOpen"
        params = {"textDocument": doc, "contentChanges": [{"text": content}]} if is_open else {
            "textDocument": {**doc, "languageId": "lean4", "text": content}}
        self._send({"jsonrpc": "2.0", "method": method, "params": params}, budget)

    def _collect(self, q, version):
        diags = None
        while True:
            try:
                msg = q.get_nowait()
            except Empty:
                break
            if msg is None:
                raise self.failure or CheckFailure("transport", "Lean server stream closed")
            if msg.get("method") == "textDocument/publishDiagnostics":
                params = msg.get("params", {})
                if params.get("version") == version:
                    value = params.get("diagnostics")
                    if not isinstance(value, list):
                        raise CheckFailure("protocol", "Invalid Lean diagnostics publication")
                    diags = value
        if diags is None:
            raise CheckFailure("missing_diagnostics", "Lean returned no diagnostics for the checked version")
        return diags

    def _dispatch(self, msg):
        if msg is None:
            self.broken = True
            self.failure = self.failure or getattr(self._transport, "failure", None) or CheckFailure(
                "transport", "Lean server stream closed")
            self._fail_all_waiters()
            return
        method, msg_id = msg.get("method"), msg.get("id")
        if method is not None and msg_id is not None:
            self._ack_server_request(msg_id, method)
        elif msg_id is not None:
            with self._pending_lock:
                slot = self._pending.get(msg_id)
            if slot is not None:
                try:
                    slot.put_nowait(msg)
                except Full:
                    pass
        elif method is not None:
            uri = _uri_of(method, msg.get("params") or {})
            with self._state_lock:
                q = self._uri_queues.get(uri)
            if q is not None:
                q.put(msg)

    def _ack_server_request(self, msg_id, method):
        payload = {"jsonrpc": "2.0", "id": msg_id}
        payload.update({"result": None} if method in _ACK_METHODS else {
            "error": {"code": _METHOD_NOT_FOUND, "message": f"unsupported: {method}"}})
        self._send(payload)

    def _request(self, method, params, timeout, budget=None):
        budget = budget or CheckBudget(timeout)
        until = min(budget.deadline, time.monotonic() + timeout)
        req_id = self._nxt()
        slot = Queue(maxsize=1)
        with self._pending_lock:
            self._pending[req_id] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}, budget)
            while True:
                left = min(budget.remaining("initialization" if method == "initialize" else "lsp"),
                           until - time.monotonic())
                if left <= 0:
                    raise CheckFailure("initialization_timeout", f"{method} timed out after {timeout}s",
                                       phase="initialization")
                try:
                    msg = slot.get(timeout=min(0.1, left))
                    break
                except Empty:
                    continue
            if msg is None:
                raise self.failure or CheckFailure("transport", "Lean server stream closed")
            if "error" in msg:
                error = msg["error"]
                unsupported = isinstance(error, dict) and error.get("code") == _METHOD_NOT_FOUND
                raise CheckFailure("unsupported_method" if unsupported else "server_error",
                                   f"LSP error: {error}", retryable=not unsupported)
            if "result" not in msg:
                raise CheckFailure("protocol", "Lean response has neither result nor error")
            return msg["result"]
        except CheckFailure as exc:
            exc = _failure(exc)
            exc.evidence.update(method=method, request_id=req_id)
            if exc.kind in {"cancelled", "timeout"}:
                try:
                    self._send({"jsonrpc": "2.0", "method": "$/cancelRequest",
                                "params": {"id": req_id}}, CheckBudget(0.2))
                except Exception:
                    pass
            raise exc
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)

    def _fail_all_waiters(self):
        with self._pending_lock:
            slots = list(self._pending.values())
        with self._state_lock:
            slots += list(self._uri_queues.values())
        for slot in slots:
            try:
                slot.put_nowait(None)
            except Full:
                pass

    def _acquire_lease(self):
        with self._lease_lock:
            self._leases += 1

    def _release_lease(self):
        with self._lease_lock:
            self._leases -= 1
            close = self._retiring and self._leases == 0
        if close:
            self.shutdown()

    def _retire(self):
        with self._lease_lock:
            self._retiring = True
            close = self._leases == 0
        if close:
            self.shutdown()

    def _nxt(self):
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _format(self, file_path, diags):
        lines = []
        for diag in diags:
            severity = diag.get("severity", 1)
            if severity not in (1, 2):
                continue
            pos = diag.get("range", {}).get("start", {})
            lines.append(f"{file_path}:{pos.get('line', 0) + 1}:{pos.get('character', 0) + 1}: "
                         f"{_SEVERITY[severity]}: {diag.get('message', '').rstrip()}")
        return "\n".join(lines) or "OK — no errors, no warnings."


def _uri_of(method, params):
    if method == "textDocument/publishDiagnostics":
        return params.get("uri")
    if method == "$/lean/fileProgress":
        return params.get("textDocument", {}).get("uri")
    return None


def _failure(exc, **context):
    if isinstance(exc, CheckFailure):
        return CheckFailure(exc.kind, str(exc), phase=exc.phase, retryable=exc.retryable,
                            **exc.evidence)
    return CheckFailure("transport", f"{type(exc).__name__}: {exc}", **context)


class _Root:
    def __init__(self):
        self.condition = threading.Condition(threading.RLock())
        self.retired = []
        self.starting = False
        self.probing = False
        self.failures = 0
        self.retry_at = 0.0
        self.last_failure = None
        self.episode = None
        self.active_cold = 0
        self.state = "idle"
        self.revision = 0
        self.closing = False
        self.starting_daemon = None
        self.restarts = 0


_daemons = {}
_roots = {}
_lock = threading.Lock()


def _root(root):
    root = str(Path(root).resolve())
    with _lock:
        state = _roots.get(root)
        if state is None:
            state = _roots[root] = _Root()
    return root, state


def runtime_is_closing(lake_root):
    with _lock:
        state = _roots.get(str(Path(lake_root).resolve()))
    return bool(state and state.closing)


def runtime_snapshot(lake_root=None):
    base = {"runtime_id": _RUNTIME_ID, "generation": None, "state": "disabled" if lsp_disabled() else "idle",
            "active_cold_checks": 0, "retry_after_ms": 0, "last_failure": None, "episode_id": None}
    if lake_root is None:
        return base
    key = str(Path(lake_root).resolve())
    with _lock:
        state = _roots.get(key)
    if state is None:
        return base
    with state.condition:
        daemon = _daemons.get(key)
        return {**base, "generation": getattr(daemon, "generation", None),
                "state": "disabled" if lsp_disabled() else state.state,
                "active_cold_checks": state.active_cold, "episode_id": state.episode,
                "retry_after_ms": max(0, round((state.retry_at - time.monotonic()) * 1000)),
                "last_failure": state.last_failure}


def _report(state, code, message, budget, path, *, severity="notice"):
    from . import diagnostics
    diagnostics.report(severity, code, message, source="lean_check", once=True,
                       episode_id=state.episode, check_id=budget.execution["check_id"], path=path,
                       failure=state.last_failure)


def note_cold(lake_root, delta):
    _, state = _root(lake_root)
    with state.condition:
        state.active_cold = max(0, state.active_cold + delta)


def _retire_locked(key, state, daemon):
    if _daemons.get(key) is daemon:
        daemon.stale = True
        state.retired = [d for d in state.retired if not d.closed.is_set()]
        if state.retired:
            # Keep the invalid current generation registered (unavailable for
            # new leases) until its predecessor has drained. Never have two
            # retiring generations plus a serving replacement.
            return
        _daemons.pop(key, None)
        state.retired.append(daemon)
        # Cleanup outside the root lock: it must not block other check leases.
        threading.Thread(target=daemon._retire, daemon=True).start()


def _acquire_daemon(key, state, budget, file_path=None):
    while True:
        with state.condition:
            if state.closing:
                raise CheckFailure("cancelled", "Lean runtime is shutting down", retryable=False)
            while state.probing and state.probing != budget.execution["check_id"]:
                with budget.measure("queue"):
                    state.condition.wait(timeout=min(0.1, budget.remaining("queue")))
            state.retired = [d for d in state.retired if not d.closed.is_set()]
            daemon = _daemons.get(key)
            if daemon and (daemon.broken or daemon.stale or daemon._check_count >= _RESTART_AFTER):
                if daemon.broken:
                    state.probing = budget.execution["check_id"]
                    if state.episode is None:
                        state.episode = uuid4().hex
                        state.restarts = 0
                        state.last_failure = (daemon.failure or CheckFailure("transport", "Lean server exited")).as_dict()
                if not state.retired:
                    _retire_locked(key, state, daemon)
                    daemon = None
                else:
                    daemon = None  # wait for a free generation slot
            if daemon is not None:
                daemon._acquire_lease()
                return daemon
            if time.monotonic() < state.retry_at:
                raise CheckFailure("backoff", "Lean server recovery is waiting before its next attempt",
                                   retryable=False, cause=state.last_failure)
            if not state.starting and key not in _daemons:
                state.starting = True
                state.state = "recovering" if state.episode else "starting"
                revision = state.revision
                if state.episode:
                    state.restarts += 1
                daemon = LeanDaemon(key)
                state.starting_daemon = daemon
                break
            with budget.measure("queue"):
                state.condition.wait(timeout=min(0.1, budget.remaining("queue")))
    try:
        if state.episode:
            _report(state, "lean.lsp_recovering", "Recovering Lean server", budget, file_path)
        if not daemon.start(budget):
            raise daemon.failure or CheckFailure("startup", "Lean server initialization failed")
        with state.condition:
            if state.closing:
                daemon.shutdown()
                raise CheckFailure("cancelled", "Lean runtime is shutting down", retryable=False)
            daemon.stale = state.revision != revision
            _daemons[key] = daemon
            daemon._acquire_lease()
            if not state.episode and state.probing == budget.execution["check_id"]:
                # Initial startup is shared only through initialize. Do not
                # serialize unrelated first-document elaborations behind it.
                state.probing = False
                state.condition.notify_all()
            return daemon
    finally:
        with state.condition:
            state.starting = False
            state.starting_daemon = None
            state.condition.notify_all()


def check_via_lsp(file_path, content, lake_root, *, budget=None):
    budget = budget or CheckBudget(_CHECK_TIMEOUT)
    key, state = _root(lake_root)
    owner = budget.execution["check_id"]
    try:
        for attempt in range(2):
            daemon = None
            try:
                with state.condition:
                    while state.probing and state.probing != owner:
                        with budget.measure("queue"):
                            state.condition.wait(timeout=min(0.1, budget.remaining("queue")))
                    if time.monotonic() < state.retry_at:
                        raise CheckFailure("backoff", "Lean server recovery is waiting before its next attempt",
                                           retryable=False, cause=state.last_failure)
                    if key not in _daemons or state.failures:
                        state.probing = owner
                        if state.failures:
                            state.restarts = 0
                budget.remaining()
                budget.execution["attempts"] += 1
                daemon = _acquire_daemon(key, state, budget, file_path)
                budget.execution["daemon_generation"] = daemon.generation
                if daemon.stale:
                    raise CheckFailure("stale_generation", "Dependencies changed during Lean initialization")
                result = daemon.check(file_path, content, budget=budget)
                with state.condition:
                    if daemon.stale:
                        raise CheckFailure("stale_generation", "Dependencies changed during the Lean check")
                    # An older retiring generation cannot clear a newer outage.
                    if _daemons.get(key) is daemon and not daemon.stale:
                        recovered = state.episode is not None
                        state.state = "ready"
                        state.failures = 0
                        state.restarts = 0
                        state.retry_at = 0
                        if recovered:
                            _report(state, "lean.lsp_recovered", "Lean server recovered", budget, file_path)
                        state.episode = None
                return result
            except Exception as raw:
                exc = _failure(raw)
                if exc.kind in {"cancelled", "queue_timeout", "backoff"}:
                    raise exc
                budget.execution["failure"] = exc.as_dict()
                with state.condition:
                    current = _daemons.get(key)
                    # Join a sibling's recovery, or reuse its already healthy
                    # replacement. Late failures cannot reopen its outage.
                    joined = (state.probing and state.probing != owner) or (
                        daemon is not None and current is not None and current is not daemon
                        and state.state == "ready")
                    if joined:
                        if attempt == 0 and exc.kind != "timeout":
                            continue
                        raise exc
                    if daemon is not None:
                        _retire_locked(key, state, daemon)
                    state.last_failure = exc.as_dict()
                    if state.episode is None:
                        state.episode = uuid4().hex
                        state.restarts = 0
                    if not state.probing:
                        state.probing = owner
                    state.state = "recovering"
                if exc.kind == "timeout":
                    with state.condition:
                        state.state = "degraded"
                    raise exc
                if attempt == 0 and exc.retryable and state.restarts < 1:
                    _report(state, "lean.lsp_recovering", "Recovering Lean server", budget, file_path)
                    continue
                with state.condition:
                    if state.probing == owner:
                        state.failures += 1
                        state.retry_at = time.monotonic() + min(300, 30 * 2 ** min(state.failures - 1, 4))
                        state.state = "degraded"
                raise exc
            finally:
                if daemon is not None:
                    daemon._release_lease()
    finally:
        with state.condition:
            if state.probing == owner:
                state.probing = False
                if not state.episode and key not in _daemons:
                    state.state = "idle"
                state.condition.notify_all()


def mark_stale(lake_root):
    key, state = _root(lake_root)
    with state.condition:
        state.revision += 1
        daemon = _daemons.get(key)
        if daemon is not None:
            daemon.stale = True


def close_documents_under(dir_path):
    with _lock:
        states = list(_roots.items())
    total = 0
    for key, state in states:
        with state.condition:
            daemons = [*state.retired, *([_daemons[key]] if key in _daemons else [])]
        for daemon in daemons:
            try:
                total += daemon.close_documents_under(dir_path)
            except Exception:
                _log.exception("Could not close Lean scratch documents")
    return total


@atexit.register
def _shutdown_all():
    with _lock:
        states = list(_roots.items())
    threads = []
    for key, state in states:
        with state.condition:
            state.closing = True
            state.state = "idle"
            daemons = [*state.retired, *([_daemons.pop(key)] if key in _daemons else [])]
            if state.starting_daemon:
                daemons.append(state.starting_daemon)
        for daemon in daemons:
            thread = threading.Thread(target=daemon.shutdown, daemon=True)
            thread.start()
            threads.append(thread)
    deadline = time.monotonic() + 5
    for thread in threads:
        thread.join(timeout=max(0, deadline - time.monotonic()))
    # Cold checks observe the same closing flag in their deadline loop and
    # terminate their owned subprocess. Include their cleanup in this grace.
    while any(state.active_cold for _, state in states) and time.monotonic() < deadline:
        time.sleep(0.01)
