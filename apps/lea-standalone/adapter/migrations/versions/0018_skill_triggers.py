"""keyword triggers for skills (v2.5 H9)

A skill that only matters sometimes should only be present sometimes. Without this every
assigned skill is in the prompt on every run, so a measure-theory skill costs context on
an algebra proof — and the more skills a user adds, the worse each one performs.

`triggers` is a JSON list of keywords. Empty/NULL means always-on, which is what every
existing skill is: this changes nothing until a user asks for it.

Revision ID: 0018_skill_triggers
Revises: 0017_skill_description
"""

from alembic import op

revision = "0018_skill_triggers"
down_revision = "0017_skill_description"
branch_labels = None
depends_on = None


def _has_column(conn, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.exec_driver_sql(f"pragma table_info({table})"))


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "skills", "triggers"):
        conn.exec_driver_sql("alter table skills add column triggers text")


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, "skills", "triggers"):
        try:
            conn.exec_driver_sql("alter table skills drop column triggers")
        except Exception:  # noqa: BLE001
            pass
