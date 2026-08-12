"""Database snapshots — the recovery path now that SQL owns the content.

While git owned proof bytes the database was disposable, so losing it cost
nothing. Once `timeline`/`artifact_blobs` hold the only copy of a proof, the
database *is* the user's work and needs a real recovery story. The old tables are
not it: they only hold everything up to the cutover and freeze the moment
`bridge.py` starts writing blobs — a snapshot of one past instant, not a backup.

## What this does

Takes a consistent snapshot immediately before any migration runs (see
`migrations.upgrade_to_head`). A bad revision is the single event most likely to
eat data, and it is the one moment we always know is coming. Snapshots land in
`data/backups/` (gitignored via `data/`) and the newest `KEEP` are retained.

## Why `conn.backup()` and not a file copy

`shutil.copy2` of a live SQLite file can capture pages from an in-flight
transaction without the journal needed to roll them back, yielding a corrupt copy.
A probe here did *not* reproduce that — but the write window was small, so that is
evidence the test was weak, not that the copy is safe. `sqlite3.Connection.backup`
is the API specified to produce a consistent snapshot under concurrent writers, so
correctness doesn't depend on getting lucky with timing.

## Failure is fatal on purpose

If the snapshot fails, the migration does not run. Refusing to start is
recoverable; migrating the only copy of the user's proofs without a fallback is
not. The one exception is "no database yet" — nothing to snapshot, nothing to lose.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import db

# Snapshots are cheap (one small file) but not free. Keep enough to step back
# through a bad run without letting them grow without bound.
KEEP = 5


# `from .db import DB_PATH` would bind the value at import time, so a test that
# redirects `db.DB_PATH` would still be pointed at the developer's real database —
# and this module *copies* whatever it is pointed at. That is the same hazard
# `migrations/env.py` resolves the URL at call time to avoid; it bit here too
# (snapshots of the real DB appeared while running checks "on a copy"). Resolve
# both through the module, every call.
def _db_path() -> Path:
    return db.DB_PATH


def backup_dir() -> Path:
    """Snapshots live beside the database they protect, so they follow it
    automatically — one less thing for a test (or a `LEA_SHARED_DATA_DIR`) to keep
    in sync."""
    return db.DB_PATH.parent / "backups"


class BackupError(RuntimeError):
    """Snapshotting failed. Raised to *stop* whatever was about to modify the DB."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _has_anything_to_protect() -> bool:
    """True when the database holds at least one table.

    `DB_PATH.exists()` is NOT a proxy for "there is a database": opening a
    connection *creates* the file, and `migrations.current_revision()` opens one
    before deciding whether to migrate — so on a fresh install the file is already
    there, empty, by the time we look. Checking for tables instead also keeps the
    case that matters: a legacy, unstamped database full of real rows is exactly
    what must be snapshotted, so "unstamped" can't be the skip condition."""
    try:
        with sqlite3.connect(_db_path()) as conn:
            return bool(
                conn.execute("select 1 from sqlite_master where type='table' limit 1").fetchone()
            )
    except sqlite3.DatabaseError:
        return True  # unreadable/corrupt: snapshot it before touching it


def snapshot(tag: str) -> Path | None:
    """Consistent copy of the database. Returns its path, or None if there's nothing
    worth copying.

    `tag` labels why it was taken (e.g. the revision being upgraded from), so a
    directory listing explains itself."""
    if not _db_path().exists() or not _has_anything_to_protect():
        return None  # fresh install: no schema, no rows, nothing to lose

    path = _db_path()
    directory = backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / f"{path.stem}-{_timestamp()}-{tag}.sqlite3"
    try:
        source = sqlite3.connect(path, timeout=db.BUSY_TIMEOUT_MS / 1000)
        try:
            with sqlite3.connect(dest) as target:
                source.backup(target)  # online backup API: safe under a live writer
        finally:
            source.close()
    except Exception as exc:  # noqa: BLE001 - any failure here must stop the caller
        dest.unlink(missing_ok=True)  # never leave a half-written "backup" behind
        raise BackupError(f"could not snapshot {path} to {dest}: {exc}") from exc

    _prune()
    return dest


def list_snapshots() -> list[Path]:
    """Newest first."""
    directory = backup_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{_db_path().stem}-*.sqlite3"), reverse=True)


def _prune() -> None:
    for stale in list_snapshots()[KEEP:]:
        stale.unlink(missing_ok=True)
