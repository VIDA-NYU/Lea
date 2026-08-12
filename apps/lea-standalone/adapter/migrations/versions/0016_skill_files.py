"""multi-file skills (v2.5 H2)

A real skill is a DIRECTORY, not a markdown file: an entry point plus `references/`,
`scripts/`, `assets/`. `cameronfreer/lean4-skills` ships a 29 KB SKILL.md beside 41
reference files — the entry point links them and expects them readable on demand.

`skills.body` stays the ENTRY POINT, so every existing consumer is untouched; these rows
are the rest. A single-file skill has no rows here and behaves exactly as before.

Revision ID: 0016_skill_files
Revises: 0015_authoring_fields
"""

from alembic import op


revision = "0016_skill_files"
down_revision = "0015_authoring_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists skill_files (
            skill_id text not null references skills(id),
            path text not null,
            content text not null,
            created_at text not null,
            primary key (skill_id, path)
        )
        """
    )


def downgrade() -> None:
    op.execute("drop table if exists skill_files")
