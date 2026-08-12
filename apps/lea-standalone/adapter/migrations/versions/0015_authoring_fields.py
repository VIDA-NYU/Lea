"""guided authoring fields for skills and roles (v2.5 C1)

A mathematician staring at an empty markdown box does not know what to write. The form
asks four questions instead — what it is, when to use it, when NOT to, and how — and
those answers compile into the text the model already reads.

**The fields are stored, the compiled text is stored too, and that is deliberate.**
The fields must survive so they stay editable (re-opening the form shows the answers, not
a wall of prose to reverse-engineer). The compiled text must survive so every consumer —
`skills.body`, `agent_roles.system_prompt` — keeps working unchanged; nothing downstream
learns a new shape. One `authoring` JSON column per table rather than four columns each:
these are never queried individually, only round-tripped, exactly like `timeline.data`.

A row with `authoring = NULL` was hand-written and stays that way — the form is an
option, not a migration of everyone's existing prose.

Revision ID: 0015_authoring_fields
Revises: 0014_agent_roles
"""

from alembic import op


revision = "0015_authoring_fields"
down_revision = "0014_agent_roles"
branch_labels = None
depends_on = None


def _has_column(conn, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.exec_driver_sql(f"pragma table_info({table})"))


def upgrade() -> None:
    conn = op.get_bind()
    for table in ("skills", "agent_roles"):
        if not _has_column(conn, table, "authoring"):
            conn.exec_driver_sql(f"alter table {table} add column authoring text")


def downgrade() -> None:
    # SQLite can drop a column from 3.35 on; guarded so an older build degrades to a
    # no-op rather than failing a downgrade nobody is watching.
    conn = op.get_bind()
    for table in ("skills", "agent_roles"):
        if _has_column(conn, table, "authoring"):
            try:
                conn.exec_driver_sql(f"alter table {table} drop column authoring")
            except Exception:  # noqa: BLE001 — leaving an unused column is harmless
                pass
