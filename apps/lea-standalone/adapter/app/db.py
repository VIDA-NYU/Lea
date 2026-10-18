from __future__ import annotations

import sqlite3
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
_configured_data_dir = os.environ.get("LEA_SHARED_DATA_DIR")
_data_dir = Path(_configured_data_dir) if _configured_data_dir else ROOT / "data"
if not _data_dir.is_absolute():
    _data_dir = (ROOT.parent.parent if ROOT.parent.name=='apps' else ROOT) / _data_dir
DB_PATH = Path(os.environ.get('LEA_DATABASE_FILE', _data_dir / "lea-interface.sqlite3"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# How long a connection waits for another's write lock before raising "database is
# locked". This is what sqlite3.connect(timeout=5.0) already applies by default; it
# is set explicitly because `write()` depends on it (with it at 0, BEGIN IMMEDIATE
# fails ~90% of concurrent attempts instead of queueing).
BUSY_TIMEOUT_MS = 5000
_snapshot_connection: ContextVar[sqlite3.Connection | None] = ContextVar("db_snapshot", default=None)
_write_connection: ContextVar[sqlite3.Connection | None] = ContextVar("db_writer", default=None)
caller_worker_epoch: ContextVar[str | None] = ContextVar('caller_worker_epoch',default=None)
caller_parent_command: ContextVar[str | None] = ContextVar('caller_parent_command',default=None)
caller_batch: ContextVar[str | None] = ContextVar('caller_batch',default=None)


def register_change_batch(conn: sqlite3.Connection) -> None:
    """Give trigger writes one identity per transaction, including implicit BEGIN.

    All adapter and Alembic connections register this function. External SQL
    writers must use this connection factory too; missing registration fails a
    domain write rather than silently bypassing the durable change log.
    """
    batch = uuid4().hex

    def trace(statement: str) -> None:
        nonlocal batch
        if statement.lstrip().upper().startswith("BEGIN"):
            batch = uuid4().hex

    conn.create_function("lea_change_batch", 0, lambda: batch)
    conn.set_trace_callback(trace)


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
    register_change_batch(conn)
    conn.execute(f"pragma busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """The default connection: stdlib deferred transaction, committed on exit.

    Correct for reads, and for writes whose values don't depend on a row this same
    block just read. NOT safe for read-modify-write — use `write()`."""
    shared = _write_connection.get() or _snapshot_connection.get()
    if shared is not None:
        yield shared
        return
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
    shared = _write_connection.get()
    if shared is not None:
        savepoint='nested_'+uuid4().hex
        shared.execute('savepoint '+savepoint)
        try:yield shared
        except BaseException:
            shared.execute('rollback to '+savepoint)
            shared.execute('release '+savepoint)
            raise
        shared.execute('release '+savepoint)
        return
    if _snapshot_connection.get() is not None:
        raise RuntimeError("A database snapshot cannot perform writes")
    conn = _open(isolation_level=None)
    token = _write_connection.set(conn)
    try:
        conn.execute("begin immediate")
        try:
            epoch=caller_worker_epoch.get()
            if epoch and not conn.execute("select 1 from command_workers where id='lea-command-host' and epoch=? and lease_until>?",(epoch,utc_now())).fetchone():
                from fastapi import HTTPException
                raise HTTPException(409,'The calling business worker lost ownership')
            parent=caller_parent_command.get()
            if parent:
                row=conn.execute("select * from commands where id=? and kind like 'companion.%' and state in ('claimed','succeeded')",(parent,)).fetchone()
                if not row or (row['state']=='claimed' and (row['worker_epoch']!=epoch or row['lease_until']<=utc_now())) or (row['project_id'] and not conn.execute('select 1 from projects where id=?',(row['project_id'],)).fetchone()):
                    from fastapi import HTTPException
                    raise HTTPException(409,'The accepted business operation needs reconciliation or its project was deleted')
            batch=caller_batch.get()
            if batch:
                import json
                row=conn.execute("select json_extract(payload,?) from companion_documents where name='jobs.json.batches.json'",('$.'+json.dumps(batch)+'.cancelRequested',)).fetchone()
                if row and row[0]:
                    from fastapi import HTTPException
                    raise HTTPException(409,'The batch was cancelled before this new admission')
            yield conn
        except BaseException:
            conn.execute("rollback")
            raise
        conn.execute("commit")
    finally:
        _write_connection.reset(token)
        conn.close()


@contextmanager
def read_snapshot() -> Iterator[sqlite3.Connection]:
    """One short, read-only transaction for projection data AND its watermark.

    Nested store.connect() calls reuse this connection. Never keep the context
    open while yielding SSE frames, awaiting network IO, or running a worker.
    """
    shared = _write_connection.get() or _snapshot_connection.get()
    if shared is not None:
        yield shared
        return
    conn = _open(isolation_level=None)
    token = _snapshot_connection.set(conn)
    try:
        conn.execute("pragma query_only = on")
        conn.execute("begin")
        yield conn
    finally:
        _snapshot_connection.reset(token)
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
