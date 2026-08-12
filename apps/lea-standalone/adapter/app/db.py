from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "lea-interface.sqlite3"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# How long a connection waits for another's write lock before raising "database is
# locked". This is what sqlite3.connect(timeout=5.0) already applies by default; it
# is set explicitly because `write()` depends on it (with it at 0, BEGIN IMMEDIATE
# fails ~90% of concurrent attempts instead of queueing).
BUSY_TIMEOUT_MS = 5000


def _open(isolation_level: str | None = "") -> sqlite3.Connection:
    """Open a configured connection. `isolation_level=""` is the stdlib default
    (implicit deferred transactions); `None` means autocommit, leaving BEGIN/COMMIT
    to the caller — what `write()` needs.

    `foreign_keys` is deliberately left OFF: the schema's deletes are explicit
    cascades that depend on that (see `store.delete_project_cascade`), so enabling
    it here would change delete semantics repo-wide.

    WAL is deliberately NOT enabled. It is not needed for correctness — `write()`
    is safe under the rollback journal — and enabling it *without* `write()` makes
    things worse: on the deferred read-modify-write below it widens the race,
    measured at ~110 -> ~155 duplicate seqs per 200 inserts. If WAL is ever turned
    on for read throughput, it must land after every read-modify-write has moved to
    `write()`, and on its own merits.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=isolation_level)
    conn.row_factory = sqlite3.Row
    conn.execute(f"pragma busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """The default connection: stdlib deferred transaction, committed on exit.

    Correct for reads, and for writes whose values don't depend on a row this same
    block just read. NOT safe for read-modify-write — use `write()`."""
    conn = _open()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def write() -> Iterator[sqlite3.Connection]:
    """A serialized read-modify-write transaction (`BEGIN IMMEDIATE`).

    Use this whenever a value being written was computed from a value read in the
    same block — `max(seq) + 1` being the case that forced this to exist.

    `connect()` cannot do this safely, and it fails *silently*: Python's sqlite3
    opens its implicit transaction before INSERT/UPDATE/DELETE but not before
    SELECT, so the SELECT runs in autocommit, outside any transaction. Concurrent
    callers therefore read the same `max(seq)` and both insert it. No exception, no
    lock contention — just rows sharing a seq. Measured on this schema: 8 threads x
    25 inserts gave 0 errors and 99-119 duplicate seqs out of 200.

    It must be BEGIN IMMEDIATE, not BEGIN. A deferred BEGIN starts the SELECT as a
    reader and tries to upgrade on the INSERT; SQLite does not run the busy handler
    for that upgrade (waiting cannot resolve it), so it returns "database is locked"
    immediately regardless of `busy_timeout` — measured: ~145/200 inserts lost, in
    0.15s rather than after any wait. BEGIN IMMEDIATE takes the write lock up front,
    so callers queue on `busy_timeout` instead: 200/200, 0 duplicates.
    """
    conn = _open(isolation_level=None)
    try:
        conn.execute("begin immediate")
        try:
            yield conn
        except BaseException:
            conn.execute("rollback")
            raise
        conn.execute("commit")
    finally:
        conn.close()


def init_db() -> None:
    """Bring the database to the latest schema. Safe to call on every startup.

    This is now a thin wrapper over Alembic. The schema itself lives in
    `migrations/versions/` — `0001_baseline` is the frozen copy of what used to be
    inlined here, so a fresh database and a pre-existing one walk the *same*
    revision chain and cannot drift. See `app/migrations.py` for why the old
    "disposable DB, no migrations" policy had to go."""
    from .migrations import upgrade_to_head

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    upgrade_to_head()


def row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}
