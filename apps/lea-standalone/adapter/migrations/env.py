"""Alembic environment for the adapter's SQLite database.

Two things here are load-bearing and easy to get wrong:

1. **The URL is resolved at call time from `app.db.DB_PATH`** — deliberately NOT
   read from `alembic.ini`. Tests point at a scratch database by monkeypatching
   `db.DB_PATH`; a URL hardcoded in the ini would silently migrate the developer's
   real database during a test run.

2. **`transaction_per_migration=True`** (Alembic's default is off, which wraps the
   *whole* upgrade in one transaction). With it on, each revision commits on its
   own, so a failure at revision 5 leaves 1-4 durably applied instead of discarding
   the batch. Asserted by `tests/test_migrations.py` rather than assumed.

`target_metadata` is None on purpose: there are no SQLAlchemy models. Every query
in this app is raw `sqlite3`; SQLAlchemy exists only as the engine Alembic drives.
So **autogenerate is unavailable and every revision is hand-written** — Alembic was
picked for `batch_alter_table` (SQLite cannot alter a column constraint any other
way), not for autogenerate. See the v2.3 tracker, item 3.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from app import db as app_db

config = context.config

# NOTE: the generated `fileConfig(config.config_file_name)` call is deliberately
# absent. `logging.config.fileConfig` mutates global logging state and is not
# thread-safe; with several workers calling `upgrade_to_head()` at once it raced
# and threw `KeyError: 'config'`. The app configures its own logging.

# No models -> no autogenerate. Revisions are written by hand.
target_metadata = None

# Making a revision all-or-nothing on SQLite takes THREE things, and any one of
# them missing silently degrades to "a failed revision leaves a half-built schema".
# Measured: without these, a revision that failed after `create table c` left `c`
# behind. `tests/test_migrations.py::test_failed_revision_*` is what keeps it honest.
#
#   1. transaction_per_migration — else the whole upgrade shares one transaction
#      and a late failure discards the revisions that already succeeded.
#   2. transactional_ddl=True — Alembic's SQLite dialect hardcodes it to False and
#      therefore does not wrap DDL at all.
#   3. The `_force_real_begin` listeners below. This is the load-bearing one, and it
#      is the SAME pysqlite quirk that caused the item-1 seq bug: the driver opens
#      its implicit transaction only before INSERT/UPDATE/DELETE — not before SELECT
#      (item 1) and NOT BEFORE DDL. So `create table` autocommits and is
#      unrollbackable unless we emit BEGIN ourselves. (1) and (2) alone do nothing.
_CONTEXT_OPTS = dict(
    target_metadata=target_metadata,
    transactional_ddl=True,
    transaction_per_migration=True,
)


def _force_real_begin(engine) -> None:
    """Make DDL rollback-able on SQLite (SQLAlchemy's documented pysqlite recipe).

    Disable pysqlite's implicit-transaction handling entirely, then emit BEGIN
    explicitly. IMMEDIATE, not plain BEGIN: a deferred BEGIN starts as a reader and
    upgrades on first write, and SQLite does not run the busy handler for that
    upgrade — so concurrent migrators would get an instant "database is locked"
    that no timeout resolves. IMMEDIATE takes the write lock up front, so a second
    migrator queues on `timeout` instead."""

    @event.listens_for(engine, "connect")
    def _no_implicit_txn(dbapi_connection, _record):  # noqa: ANN001
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _explicit_begin(conn):  # noqa: ANN001
        conn.exec_driver_sql("BEGIN IMMEDIATE")


def _url() -> str:
    """Resolve the DB URL *now*, so a monkeypatched `db.DB_PATH` is honoured."""
    return f"sqlite:///{app_db.DB_PATH}"


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_CONTEXT_OPTS,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # SQLAlchemy's default sqlite3 timeout does not inherit db.BUSY_TIMEOUT_MS,
        # so concurrent startups collided with "database is locked". Give the
        # migration lock room to queue instead.
        connect_args={"timeout": app_db.BUSY_TIMEOUT_MS / 1000},
    )
    _force_real_begin(connectable)

    with connectable.connect() as connection:
        context.configure(connection=connection, **_CONTEXT_OPTS)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
