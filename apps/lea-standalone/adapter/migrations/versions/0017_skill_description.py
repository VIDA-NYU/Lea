"""a skill's one-line description (v2.5 H3/H4)

Needed by progressive disclosure: a multi-file skill is advertised to the model as
name + description + location rather than injected whole, so without a description the
listing says nothing and the model has no basis to read further.

A SKILL.md already carries one in its frontmatter — this is where the author's own words
land instead of being stripped and thrown away.

Revision ID: 0017_skill_description
Revises: 0016_skill_files
"""

from alembic import op

revision = "0017_skill_description"
down_revision = "0016_skill_files"
branch_labels = None
depends_on = None


def _has_column(conn, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.exec_driver_sql(f"pragma table_info({table})"))


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "skills", "description"):
        conn.exec_driver_sql("alter table skills add column description text")


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, "skills", "description"):
        try:
            conn.exec_driver_sql("alter table skills drop column description")
        except Exception:  # noqa: BLE001
            pass
