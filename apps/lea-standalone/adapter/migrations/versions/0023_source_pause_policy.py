"""Snapshot the Overleaf source-obstruction pause policy on each run."""
from alembic import op

revision = "0023_source_pause_policy"
down_revision = "0022_live_lea_status"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        row[1] for row in op.get_bind().exec_driver_sql("pragma table_info(runs)")
    }
    if "allow_source_pause" not in columns:
        op.execute("alter table runs add column allow_source_pause integer not null default 0")


def downgrade():
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("allow_source_pause")
