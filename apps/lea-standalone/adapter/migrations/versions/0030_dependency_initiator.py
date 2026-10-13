"""Record dependency attempt initiators without rewriting historical evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0030_dependency_initiator"
down_revision = "0029_project_library"
branch_labels = None
depends_on = None


def upgrade():
    columns = {row[1] for row in op.get_bind().exec_driver_sql("pragma table_info(project_dependency_attempts)")}
    if "initiating_run_id" not in columns:
        op.add_column("project_dependency_attempts", sa.Column("initiating_run_id", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("project_dependency_attempts", "initiating_run_id")
