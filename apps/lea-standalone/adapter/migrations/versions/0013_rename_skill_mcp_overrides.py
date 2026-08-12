"""rename session_capability_overrides -> session_skill_mcp_overrides

"Capabilities" was invented jargon. Everything user-facing now says *Skills* where a
surface covers only skills and *Skills / MCP* where it covers both; the schema follows,
so the table name matches the concept people actually read.

Pure rename — no data reshaping. `0012` is left untouched because an applied revision is
never edited.

**Why this is defensive rather than a bare `ALTER TABLE ... RENAME`.** The migration
suite replays the whole chain against a database that ALREADY carries the modern schema
(`test_baseline_is_a_noop_on_a_preexisting_database`). There, `0012`'s
`create table if not exists` re-creates the OLD name beside the existing NEW one, and a
bare rename dies on "there is already another table with this name". So each of the three
reachable states is handled explicitly, and rows are moved before anything is dropped —
a rename must never be able to lose an override.

Revision ID: 0013_rename_skill_mcp_overrides
Revises: 0012_session_capability_overrides
"""

from alembic import op


revision = "0013_rename_skill_mcp_overrides"
down_revision = "0012_session_capability_overrides"
branch_labels = None
depends_on = None

OLD = "session_capability_overrides"
NEW = "session_skill_mcp_overrides"


def _exists(conn, table: str) -> bool:
    return conn.exec_driver_sql(
        "select 1 from sqlite_master where type='table' and name=?", (table,)
    ).fetchone() is not None


def _reindex(conn, table: str, index: str) -> None:
    conn.exec_driver_sql(f"drop index if exists {index}")
    conn.exec_driver_sql(f"create index if not exists {index} on {table}(session_id)")


def upgrade() -> None:
    conn = op.get_bind()
    has_old, has_new = _exists(conn, OLD), _exists(conn, NEW)
    if has_old and has_new:
        # A replayed chain re-created the old name beside the new one. Move anything it
        # holds across (ignoring rows already present) before dropping it, so this can
        # never silently discard an override.
        conn.exec_driver_sql(f"insert or ignore into {NEW} select * from {OLD}")
        conn.exec_driver_sql(f"drop table {OLD}")
    elif has_old:
        conn.exec_driver_sql(f"alter table {OLD} rename to {NEW}")
    # else: already renamed — nothing to do.
    conn.exec_driver_sql(f"drop index if exists idx_{OLD}_session")
    if _exists(conn, NEW):
        _reindex(conn, NEW, f"idx_{NEW}_session")


def downgrade() -> None:
    conn = op.get_bind()
    if _exists(conn, NEW) and not _exists(conn, OLD):
        conn.exec_driver_sql(f"alter table {NEW} rename to {OLD}")
    conn.exec_driver_sql(f"drop index if exists idx_{NEW}_session")
    if _exists(conn, OLD):
        _reindex(conn, OLD, f"idx_{OLD}_session")
