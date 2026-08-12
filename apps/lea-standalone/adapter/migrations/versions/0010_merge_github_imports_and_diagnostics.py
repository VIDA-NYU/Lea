"""merge GitHub-import and diagnostic migration heads

Revision ID: 0010_merge_github_imports_and_diagnostics
Revises: 0009_github_project_imports, 0009_timeline_diagnostic_kind

The two feature branches each added a migration after
``0008_multi_formalization_sessions``.  Preserve both published revision IDs and
join them here so databases upgraded on either branch can advance to one head.
"""

revision = "0010_merge_github_imports_and_diagnostics"
down_revision = (
    "0009_github_project_imports",
    "0009_timeline_diagnostic_kind",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
