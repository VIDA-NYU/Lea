"""Keep Overleaf project approvals separate from autonomous execution.

Linkage is durable provenance; approval_mode snapshots each run's policy.
"""

from alembic import op

revision = "0034_overleaf_approval_policy"
down_revision = "0033_formalization_reviews"
branch_labels = None
depends_on = None


def _add_column_if_missing(table: str, column: str, definition: str) -> bool:
    columns = {
        row[1]
        for row in op.get_bind().exec_driver_sql(f"pragma table_info({table})")
    }
    if column in columns:
        return False
    op.execute(f"alter table {table} add column {column} {definition}")
    return True


def upgrade() -> None:
    _add_column_if_missing(
        "projects", "overleaf_linked",
        "integer not null default 0 check (overleaf_linked in (0, 1))",
    )
    new_approval_column = _add_column_if_missing(
        "runs", "approval_mode",
        "text not null default 'stepwise' check (approval_mode in ('stepwise', 'none'))",
    )
    op.execute("""
        update projects set overleaf_linked = 1
        where id in (
            select project_id from sessions where origin = 'overleaf'
            union
            select r.project_id from runs r join sessions s on s.id = r.session_id
            where s.origin = 'overleaf'
            union
            select project_id from formalizations where origin = 'overleaf'
            union
            select project_id from project_files where kind = 'overleaf'
        )
    """)
    # Unstamped adoption must preserve a policy already recorded by newer code.
    if new_approval_column:
        op.execute("update runs set approval_mode = 'none' where autonomous = 1")


def downgrade() -> None:
    op.drop_column("runs", "approval_mode")
    op.drop_column("projects", "overleaf_linked")
