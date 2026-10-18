"""Workspace worker ownership and graceful restart coordination."""

from __future__ import annotations

import fcntl
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from . import db, runregistry

IDENTITY = uuid4().hex
RECOVERY_CAPABILITY_VERSION = 1
_guard = threading.RLock()
_state = "ready"  # in-process tests do not run the FastAPI startup hook
_restart_requested = False
_force = False
_server = None
_lock_handle = None
_workspace_lock_handle = None
_workspace_owned = None
_drain_worker = None
_active_operations = 0
_log = logging.getLogger("lea.adapter.runtime")


def owned_process_dir() -> Path:
    return _workspace_owned or db.DB_PATH.parent / "owned-processes"


def acquire_data_lock() -> None:
    global _lock_handle
    path = db.DB_PATH.parent / ".lea-adapter.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(f"Another workspace worker owns data directory {path.parent}") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} runtime={IDENTITY}\n")
    handle.flush()
    _lock_handle = handle


def release_data_lock() -> None:
    global _lock_handle
    if _lock_handle is not None:
        fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_UN)
        _lock_handle.close()
        _lock_handle = None


def release_workspace_lock() -> None:
    """Only the one-shot bootstrap releases ownership before process exit."""
    global _workspace_lock_handle,_workspace_owned
    if _workspace_lock_handle is not None:
        _workspace_lock_handle.close()
        _workspace_lock_handle=None
        _workspace_owned=None


def set_state(state: str) -> None:
    global _state
    with _guard:
        _state = state


def state() -> str:
    with _guard:
        return _state


def accepting_work() -> bool:
    return state() == "ready"


@contextmanager
def operation():
    """Keep a long manual check alive until drain closes its shared resources."""
    global _active_operations
    with _guard:
        if _state != "ready":
            raise RuntimeError("adapter_draining")
        _active_operations += 1
    try:
        yield
    finally:
        with _guard:
            _active_operations -= 1


def bind_server(server) -> None:
    global _server
    _server = server


def snapshot() -> dict:
    from . import store
    try:
        queued = len(store.list_runs_by_status("pending"))
    except sqlite3.OperationalError:
        # Health probes used by in-process clients can run before lifespan
        # startup. The real server acquires its lock and migrates first.
        queued = 0
    return {
        "runtime_id": IDENTITY,
        "ready": accepting_work(),
        "restart_supported": _server is not None,
        "lifecycle_state": state(),
        "active_runs": runregistry.registry.active_count(),
        "active_operations": _active_operations,
        "queued_runs": queued,
        "recovery_capability_version": RECOVERY_CAPABILITY_VERSION,
    }


def restart_requested() -> bool:
    return _restart_requested


def begin_drain(*, force: bool = False, restart: bool = True) -> dict:
    global _restart_requested, _force, _drain_worker
    with _guard:
        if _server is None:
            from fastapi import HTTPException
            raise HTTPException(409, detail={"code": "restart_unavailable",
                "message": "Start the workspace worker through run_worker.py to enable restart control."})
        if _state == "draining":
            if not restart:
                _restart_requested = False
            if force:
                _force = True
            return snapshot()
        if _state == "stopping":
            return snapshot()
        _restart_requested = restart
        _force = force
        set_state("draining")
        _drain_worker = threading.Thread(target=_drain, name="lea-adapter-drain", daemon=True)
        _drain_worker.start()
    from . import diagnostics, store
    for run_id in runregistry.registry.active_run_ids():
        try:
            row = store.get_run(run_id)
            if row:
                store.add_diagnostic(row["session_id"], run_id, diagnostics.resolve(
                    "info", "run.restart_requested",
                    "Waiting for this run to finish before restarting the adapter.",
                    source="runtime", context={"runtime_id": IDENTITY,
                                               "attempt_id": row.get("attempt_id"),
                                               "forced": force},
                ))
        except Exception:
            _log.exception("Could not record restart request for run %s", run_id)
    return snapshot()


def cancel_drain() -> dict:
    global _restart_requested
    with _guard:
        if _state == "draining" and not _force and (runregistry.registry.active_count() > 0 or _active_operations > 0):
            _restart_requested = False
            set_state("ready")
    return snapshot()


def _drain() -> None:
    deadline = None
    while state() == "draining" and (runregistry.registry.active_count() or _active_operations):
        if _force and deadline is None:
            # A force request may arrive after a graceful drain has begun.
            # It is an adapter interruption, never a user Stop.
            from . import bridge
            for run_id in runregistry.registry.active_run_ids():
                bridge.request_stop(run_id, reason="adapter_force", source="adapter")
            deadline = time.monotonic() + 30
        if deadline is not None and time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    if state() != "draining":
        return
    set_state("stopping")
    if _server is not None:
        if _force:
            _server.config.timeout_graceful_shutdown = 5
        _server.should_exit = True


def acquire_workspace_lock(workspace):
    """Fence the physical workspace even when two DB paths name the same tree."""
    global _workspace_lock_handle,_workspace_owned
    import json
    workspace=Path(workspace).resolve()
    binding=db.DB_PATH.parent/'.lea-workspace.json'
    saved=json.loads(binding.read_text()) if binding.exists() else None
    if saved is not None:
        if saved.get('version') not in {1,2} or saved.get('workspace')!=str(workspace):
            raise RuntimeError('This database is bound to a different physical workspace; restore its configured root or migrate it offline before starting a worker.')
    workspace.mkdir(parents=True,exist_ok=True)
    # Container roots differ even when their proof/project bind mounts match.
    # Compose supplies the same ownership volume to every writer of that tree.
    ownership=Path(os.environ.get('LEA_WORKSPACE_OWNERSHIP_DIR',workspace)).resolve()
    if os.environ.get('LEA_REQUIRE_SHARED_OWNERSHIP')=='1' and (
            not os.environ.get('LEA_WORKSPACE_OWNERSHIP_DIR') or not os.path.ismount(ownership)):
        raise RuntimeError('Production workers and migrations require a shared Docker local ownership volume at LEA_WORKSPACE_OWNERSHIP_DIR.')
    ownership.mkdir(parents=True,exist_ok=True)
    handle=(ownership/'.lea-worker.lock').open('a+')
    try:fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close();raise RuntimeError('Another worker owns this physical workspace')
    try:
        # Some virtualized host bind mounts silently ignore flock. A second
        # independent open must conflict even in this same process.
        with (ownership/'.lea-worker.lock').open('a+') as probe:
            try:fcntl.flock(probe.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:pass
            else:raise RuntimeError('The ownership filesystem does not enforce exclusive locks; use a Docker local ownership volume or a supported local filesystem.')
        from .config import write_private_text
        marker=ownership/'.lea-workspace-id'
        identity=marker.read_text().strip() if marker.exists() else uuid4().hex
        if len(identity)!=32 or any(c not in '0123456789abcdef' for c in identity):
            raise RuntimeError('Workspace ownership identity is invalid; reconcile it offline.')
        if saved is not None and saved.get('version')==2 and saved.get('ownership_id')!=identity:
            raise RuntimeError('This database is bound to a different workspace ownership volume; restore its mounts or migrate it offline.')
        if not marker.exists():write_private_text(marker,identity)
        write_private_text(binding,json.dumps({'version':2,'workspace':str(workspace),'ownership_id':identity}))
        handle.seek(0);handle.truncate();handle.write(f'pid={os.getpid()} runtime={IDENTITY}\n');handle.flush()
    except BaseException:
        handle.close()
        raise
    _workspace_lock_handle=handle
    _workspace_owned=ownership/'.lea-owned-processes'
