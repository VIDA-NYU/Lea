"""durable additive GitHub project imports

Revision ID: 0009_github_project_imports
Revises: 0008_multi_formalization_sessions
"""

from alembic import op


revision = "0009_github_project_imports"
down_revision = "0008_multi_formalization_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists github_imports (
            id text primary key,
            project_id text not null references projects(id),
            session_id text references sessions(id),
            source_url text not null,
            source_ref text,
            source_commit_sha text not null,
            source_namespace text,
            destination_namespace text not null,
            status text not null,
            destination_snapshot text,
            commit_sha text,
            error_detail text,
            created_at text not null,
            updated_at text not null,
            check (status in (
                'applying', 'checking', 'complete', 'complete_with_issues', 'failed'
            ))
        )
        """
    )
    op.execute(
        """
        create table if not exists github_import_files (
            import_id text not null references github_imports(id),
            source_path text not null,
            destination_path text,
            disposition text not null,
            reason text,
            content_sha256 text,
            code_step_id integer references timeline(id),
            check_status text,
            check_detail text,
            created_at text not null,
            updated_at text not null,
            primary key (import_id, source_path),
            check (disposition in (
                'add', 'already_present', 'path_conflict', 'declaration_conflict',
                'unsupported_module_layout', 'excluded'
            )),
            check (check_status is null or check_status in ('pending', 'ok', 'error'))
        )
        """
    )
    op.execute(
        """
        create table if not exists github_import_declarations (
            id text primary key,
            import_id text not null references github_imports(id),
            project_id text not null references projects(id),
            destination_path text not null,
            declaration_name text not null,
            full_name text not null,
            kind text not null,
            module_name text not null,
            formalization_id text references formalizations(id),
            source_hash_at_match text,
            created_at text not null,
            updated_at text not null,
            unique (project_id, destination_path, declaration_name)
        )
        """
    )
    op.execute(
        "create index if not exists ix_github_imports_project_created "
        "on github_imports(project_id, created_at desc)"
    )
    op.execute(
        "create index if not exists ix_github_imports_recoverable "
        "on github_imports(status, created_at)"
    )
    op.execute(
        "create index if not exists ix_github_import_declarations_project_name "
        "on github_import_declarations(project_id, declaration_name)"
    )
    op.execute(
        "create index if not exists ix_github_import_declarations_formalization "
        "on github_import_declarations(formalization_id)"
    )


def downgrade() -> None:
    raise NotImplementedError("GitHub import provenance is not reversibly droppable")
