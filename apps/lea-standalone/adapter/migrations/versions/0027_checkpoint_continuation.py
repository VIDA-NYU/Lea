"""Pin ordinary continuations to a durable predecessor execution boundary."""
from alembic import op
import sqlalchemy as sa

revision = "0027_checkpoint_continuation"
down_revision = "0026_run_recovery"
branch_labels = None
depends_on = None


def upgrade():
    # An unstamped legacy database may already contain the current columns.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("runs")}
    if "resume_checkpoint_id" not in columns:
        op.add_column("runs", sa.Column("resume_checkpoint_id", sa.Integer(), nullable=True))
    if "continuation_best_effort" not in columns:
        op.add_column("runs", sa.Column("continuation_best_effort", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("runs", "continuation_best_effort")
    op.drop_column("runs", "resume_checkpoint_id")
