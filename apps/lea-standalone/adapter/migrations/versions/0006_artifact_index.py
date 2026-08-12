"""structured artifact index for declaration-to-file lookup

Revision ID: 0006_artifact_index
Revises: 0005_session_parent

The Overleaf blueprint and target-status APIs need a durable answer to "which
checked declaration currently lives in which file?". The run finalizer upserts
one row per (scope, declaration); ``scope`` is a project id for shared project
repositories and a session id for loose runs.

This was originally added to gen_repair's inline bootstrap schema. Main now uses
Alembic, so the integration carries it as an additive revision that upgrades both
fresh and existing databases identically.
"""

from alembic import op


revision = "0006_artifact_index"
down_revision = "0005_session_parent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists artifacts (
            id text primary key,
            scope text not null,
            project_id text references projects(id),
            session_id text references sessions(id),
            run_id text references runs(id),
            declaration_name text not null,
            kind text,
            path text not null,
            module_name text,
            created_at text not null,
            updated_at text not null,
            unique (scope, declaration_name)
        )
        """
    )
    op.execute(
        "create index if not exists ix_artifacts_project_path "
        "on artifacts(project_id, path)"
    )


def downgrade() -> None:
    raise NotImplementedError("the artifact index is not reversibly droppable")
