"""merge artifact-index and timeline-compaction migration branches

Revision ID: 0007_merge_artifact_and_compaction
Revises: 0006_artifact_index, 0006_timeline_compaction_kind

The artifact index and context-compaction timeline support were developed on
separate branches from the same 0005 revision. Both migrations are additive and
must run; this no-op revision joins their histories into one Alembic head.
"""


revision = "0007_merge_artifact_and_compaction"
down_revision = ("0006_artifact_index", "0006_timeline_compaction_kind")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
