"""Schema migrations — the DB is no longer disposable.

`db.py` used to say: *"No in-place ALTER migrations anywhere: v2 is a clean
rebuild (no backward compat — single user, disposable/rebuildable DB) … a schema
change means a fresh DB (`npm run reset:local`), not a migration."*

That policy was correct **while git owned proof content** — the database was only
an index, so throwing it away and rebuilding cost nothing. It stops being true the
moment SQL holds the only copy of a proof: then "just reset it" means "delete the
user's work". The premise died, so the policy did too.

Everything runs through Alembic (`migrations/`, revision `0001_baseline` onward).
Autogenerate is unavailable — there are no SQLAlchemy models, and every query in
this app is raw `sqlite3` — so revisions are hand-written. Alembic earns its place
via `op.batch_alter_table`, the only correct way to change a column constraint in
SQLite (it does the 12-step create/copy/drop/rename rebuild, including recreating
indexes). Hand-writing that is high-risk here because `foreign_keys` is OFF and
`store.delete_project_cascade`'s cascades depend on that, so a botched rebuild
fails *silently*.

Concurrency: several workers may start at once and all call `upgrade_to_head()`.
They serialize on an exclusive **file lock** held for the whole call — see
`_migration_lock` for why SQLite's own write lock is not sufficient.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from alembic import command
from alembic.config import Config

logger = logging.getLogger("lea-interface.migrations")

ADAPTER_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = ADAPTER_ROOT / "alembic.ini"


def _lock_path() -> Path:
    """The migration lock file, beside the database it guards.

    Resolved through the `db` module on every call, never bound at import: tests
    redirect `db.DB_PATH`, and a lock taken next to the developer's real database
    while "running on a copy" would be the same hazard `backup.py` documents."""
    from . import db

    return db.DB_PATH.parent / f"{db.DB_PATH.name}.migrate.lock"


@contextmanager
def _migration_lock() -> Iterator[None]:
    """Hold an exclusive, cross-process lock for the duration of an upgrade.

    This exists because SQLite's write lock is **not** enough, which is what this
    module used to claim (AUDIT-2026-07-24 X6). Alembic computes the upgrade plan
    from the version it reads *before* it writes anything; the database lock
    serializes the individual writes, not plan-then-apply. On a linear chain the
    loser's plan happens to collapse to a no-op, so the claim held by luck. It stopped
    holding when the graph branched: `0005_session_parent` has two children
    (`0006_artifact_index`, `0006_timeline_compaction_kind`) merged by `0007`, so two
    processes can legitimately be on *different* heads, and the merge revision then
    fails with either

        CommandError: Requested revision 0007_… overlaps with other requested
                      revisions 0006_artifact_index

    or an `UPDATE alembic_version` that matches zero rows. Reproduced 4 times in 10
    parallel runs of `test_concurrent_startup_migrates_exactly_once`; the exception
    propagates out of `main.startup()`, so the adapter fails to boot.

    The lock covers the snapshot decision too, so N simultaneous workers take one
    backup between them rather than one each.

    **Blocking is safe here, and deliberate.** `flock` is released by the kernel when
    the holding process dies, so a crashed migrator cannot leave a stale lock — the
    only thing that can make a waiter wait a long time is another process genuinely
    migrating, which is exactly when waiting is correct. That is why there is no
    timeout: a timeout could only turn "someone is still working" into a spurious
    startup failure.

    Platforms without `fcntl` (Windows) fall back to no lock: single-process startup
    there is unaffected, and degrading to the previous behaviour is better than
    refusing to start. The supported deployment shapes (macOS dev, Linux/Docker) all
    have it."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - POSIX-only lock; see docstring
        logger.warning("fcntl unavailable: migrating without a cross-process lock")
        yield
        return

    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _config() -> Config:
    cfg = Config(str(ALEMBIC_INI))
    # Absolute, so the runner works regardless of the process's cwd (uvicorn, pytest,
    # a script). The URL itself is resolved inside env.py from db.DB_PATH at call
    # time — deliberately not set here, so tests keep hitting their scratch DB.
    cfg.set_main_option("script_location", str(ADAPTER_ROOT / "migrations"))
    return cfg


def head_revision() -> str | None:
    """The newest revision on disk (what `upgrade` would migrate *to*)."""
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_config()).get_current_head()


def upgrade_to_head() -> None:
    """Bring the database at `db.DB_PATH` up to the latest revision.

    Idempotent: already-current is a no-op, and takes no snapshot — startup must not
    accumulate a backup per boot.

    When a migration *is* pending, the database is snapshotted first (`backup.py`).
    Now that SQL owns proof content, a bad revision is the event most likely to
    destroy the user's work, and it is the one moment we always see coming. If the
    snapshot fails the migration does not run: refusing to start is recoverable,
    migrating the only copy without a fallback is not.

    The whole body runs under `_migration_lock()`. Reading the current revision,
    deciding to snapshot, and applying the plan have to be one atomic step — Alembic
    plans against the version it reads first, so splitting them is what let concurrent
    workers collide (X6). A worker that waits here re-reads the version afterwards and
    finds the work already done, which is the "exactly-once" this module always
    claimed."""
    with _migration_lock():
        current = current_revision()
        if current != head_revision():
            from .backup import snapshot

            snapshot(tag=current or "unstamped")  # raises BackupError -> no migration

        command.upgrade(_config(), "head")


def current_revision() -> str | None:
    """The revision the database is stamped at; None if unstamped or absent.

    Reads `alembic_version` directly rather than via `MigrationContext.configure`,
    which requires a SQLAlchemy connection and raises `AttributeError: 'sqlite3.
    Connection' object has no attribute 'dialect'` if handed one of ours."""
    import sqlite3

    from .db import connect

    with connect() as conn:
        try:
            row = conn.execute("select version_num from alembic_version").fetchone()
        except sqlite3.OperationalError:
            return None  # table absent -> never migrated
    return row["version_num"] if row else None
