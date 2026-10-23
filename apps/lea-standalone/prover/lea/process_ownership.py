"""Durable, verifiable ownership of detached Lean server process groups.

A child inherits a locked file descriptor. After a hard adapter exit, the next
adapter can tell whether that exact child lineage still owns the lease; a reused
PID without the lease is never signalled. The editor's separate server never
gets a lease and is outside this cleanup path.
"""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path
import signal
import time
from uuid import uuid4


@dataclass
class Lease:
    fd: int
    path: Path


def _root() -> Path | None:
    value = os.environ.get("LEA_OWNED_PROCESS_DIR")
    return Path(value) if value and os.name == "posix" else None


def prepare() -> Lease | None:
    root = _root()
    if root is None:
        return None
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{uuid4().hex}.lease"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return Lease(fd, path)


def release_parent(lease: Lease | None, *, spawned: bool) -> None:
    if lease is None:
        return
    # Do not LOCK_UN: flock is shared with the inherited open-file description.
    # Closing only our copy leaves the child's ownership proof in place.
    os.close(lease.fd)
    if not spawned:
        lease.path.unlink(missing_ok=True)


def register(pid: int, lease: Lease | None) -> None:
    if lease is None:
        return
    if os.getpgid(pid) != pid:
        raise RuntimeError("Lean server did not become its own process-group leader")
    root = lease.path.parent
    receipt = {"pid": pid, "lease": lease.path.name,
               "owner_pid": os.getpid(), "kind": "lean_lsp"}
    temporary = root / f".{pid}.{uuid4().hex}.tmp"
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(receipt, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, root / f"{pid}.json")
    directory_fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _held(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def _receipt(root: Path, receipt_path: Path) -> tuple[int, Path] | None:
    value = json.loads(receipt_path.read_text(encoding="utf-8"))
    pid = int(value["pid"])
    lease_name = str(value["lease"])
    if (value.get("kind") != "lean_lsp" or pid != int(receipt_path.stem)
            or Path(lease_name).name != lease_name or not lease_name.endswith(".lease")):
        return None
    return pid, root / lease_name


def unregister(pid: int) -> None:
    root = _root()
    if root is None:
        return
    receipt_path = root / f"{pid}.json"
    if not receipt_path.exists():
        return
    try:
        identity = _receipt(root, receipt_path)
        if identity and not _held(identity[1]):
            receipt_path.unlink(missing_ok=True)
            identity[1].unlink(missing_ok=True)
    except (OSError, ValueError, KeyError, TypeError):
        pass


def reap_previous() -> list[int]:
    """Reap only process groups still holding their recorded inherited lease."""
    root = _root()
    if root is None or not root.exists():
        return []
    reaped = []
    unresolved = []
    for receipt_path in root.glob("*.json"):
        try:
            identity = _receipt(root, receipt_path)
            if identity is None:
                continue
            pid, lease = identity
            if not _held(lease):
                receipt_path.unlink(missing_ok=True)
                lease.unlink(missing_ok=True)
                continue
            # The leader must still be the group leader. If it exited and a
            # worker holds the lease, keep the receipt rather than risk a reused
            # process-group id that now belongs to someone else.
            if os.getpgid(pid) != pid:
                unresolved.append(str(pid))
                continue
            os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and _held(lease):
                time.sleep(0.05)
            if _held(lease) and os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
                forced_deadline = time.monotonic() + 2
                while time.monotonic() < forced_deadline and _held(lease):
                    time.sleep(0.05)
            reaped.append(pid)
            if not _held(lease):
                receipt_path.unlink(missing_ok=True)
                lease.unlink(missing_ok=True)
            else:
                unresolved.append(str(pid))
        except (OSError, ValueError, KeyError, TypeError):
            # An unverifiable receipt is retained for inspection.
            unresolved.append(receipt_path.name)
    # A crash between spawn and receipt registration can leave a live child
    # holding only its inherited lease. Without a verified PID it cannot be
    # signalled safely, and a new workspace owner must not publish ready.
    for lease in root.glob('*.lease'):
        if _held(lease):unresolved.append(lease.name)
        else:lease.unlink(missing_ok=True)
    if unresolved:
        raise RuntimeError("Could not verify cleanup of owned Lean server groups: "
                           + ", ".join(unresolved))
    return reaped
