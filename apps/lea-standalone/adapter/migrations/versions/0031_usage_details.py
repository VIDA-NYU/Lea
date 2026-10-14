"""Retain cache-aware usage without rewriting historical dollar amounts."""
from alembic import op
import sqlalchemy as sa

revision = "0031_usage_details"
down_revision = "0030_dependency_initiator"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("runs", "run_usage_breakdown", "alignment_checks", "formalize_batch_reports"):
        columns = {row[1] for row in op.get_bind().exec_driver_sql(f"pragma table_info({table})")}
        if "usage_details_json" not in columns:
            op.add_column(table, sa.Column("usage_details_json", sa.Text(), nullable=True))


def downgrade():
    for table in ("runs", "run_usage_breakdown", "alignment_checks", "formalize_batch_reports"):
        op.drop_column(table, "usage_details_json")
