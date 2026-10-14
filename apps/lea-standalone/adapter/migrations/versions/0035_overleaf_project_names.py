"""Remember whether an Overleaf project still needs a readable default name."""

import re

from alembic import op

revision = "0035_overleaf_project_names"
down_revision = "0034_overleaf_approval_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {row[1] for row in bind.exec_driver_sql("pragma table_info(projects)")}
    if "name_source" in columns:
        return  # Unstamped adoption must preserve explicit naming choices.
    op.execute("alter table projects add column name_source text not null default 'custom' "
               "check (name_source in ('generated', 'overleaf', 'custom'))")
    for project_id, slug, title in bind.exec_driver_sql(
        "select id, slug, title from projects where overleaf_linked = 1"
    ).fetchall():
        parts = re.split(r"[-_\s]+", slug.strip())
        segment = re.sub(r"[^A-Za-z0-9]", "", "".join(p[:1].upper() + p[1:] for p in parts if p))
        if not segment or not segment[0].isalpha():
            segment = "P" + segment
        if title in {slug, segment, "Overleaf Project"}:
            bind.exec_driver_sql("update projects set name_source = 'generated' where id = ?", (project_id,))


def downgrade() -> None:
    op.drop_column("projects", "name_source")
