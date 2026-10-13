"""Source refresh requests and explicit document bindings for chat handoff."""
from alembic import op
import sqlalchemy as sa

revision = "0028_overleaf_handoff"
down_revision = "0027_checkpoint_continuation"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "overleaf_bindings" not in tables:
        op.create_table("overleaf_bindings",
            sa.Column("formalization_id", sa.Text(), primary_key=True),
            sa.Column("document_id", sa.Text(), nullable=False),
            sa.Column("target_kind", sa.Text(), nullable=False),
            sa.Column("target_key", sa.Text(), nullable=False))
    if "overleaf_source_requests" not in tables:
        op.create_table("overleaf_source_requests",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("formalization_id", sa.Text(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False),
            sa.Column("source_bundle_json", sa.Text()),
            sa.Column("mirror_files_json", sa.Text()),
            sa.Column("error", sa.Text()),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.Text(), nullable=False),
            sa.Column("captured_at", sa.Text()))
    if "overleaf_source_requests" not in tables or not any(index["name"] == "ix_overleaf_source_pending" for index in inspector.get_indexes("overleaf_source_requests")):
        op.create_index("ix_overleaf_source_pending", "overleaf_source_requests", ["state", "expires_at"])
    if "source_context_ref" not in {column["name"] for column in inspector.get_columns("runs")}:
        op.add_column("runs", sa.Column("source_context_ref", sa.Text()))


def downgrade():
    op.drop_column("runs", "source_context_ref")
    op.drop_table("overleaf_source_requests")
    op.drop_table("overleaf_bindings")
