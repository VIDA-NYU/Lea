"""Persist Overleaf source bundles for post-import Lea Checks.

Revision ID: 0021_github_import_alignment
Revises: 0020_alignment_checks
"""

from alembic import op


revision = "0021_github_import_alignment"
down_revision = "0020_alignment_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    columns = {
        row[1] for row in conn.exec_driver_sql("pragma table_info(github_imports)")
    }
    if "targets_json" not in columns:
        conn.exec_driver_sql(
            "alter table github_imports add column targets_json text not null default '[]'"
        )


def downgrade() -> None:
    conn = op.get_bind()
    columns = {
        row[1] for row in conn.exec_driver_sql("pragma table_info(github_imports)")
    }
    if "targets_json" in columns:
        conn.exec_driver_sql("alter table github_imports drop column targets_json")
