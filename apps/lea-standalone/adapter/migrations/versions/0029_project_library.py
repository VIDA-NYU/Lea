"""Project declaration catalog and durable shared dependency ownership."""
from alembic import op

revision = "0029_project_library"
down_revision = "0028_overleaf_handoff"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""create table if not exists project_library_modules (
        project_id text not null, path text not null, source_hash text not null,
        environment_hash text not null, declarations_json text not null,
        build_status text not null default 'unchecked', verify_status text,
        detail text, updated_at text not null, primary key(project_id, path))""")
    op.execute("""create table if not exists project_dependencies (
        id text primary key, project_id text not null, semantic_key text not null,
        formalization_id text not null, specification_json text not null,
        source_hash text not null, generation integer not null default 1,
        path text not null, module_name text not null, declaration_name text not null,
        state text not null, owner_run_id text, blocker_json text, result_json text,
        created_at text not null, updated_at text not null,
        unique(project_id, semantic_key))""")
    op.execute("""create table if not exists project_dependency_attempts (
        dependency_id text not null, generation integer not null,
        run_id text, state text not null, detail text,
        created_at text not null, updated_at text not null,
        primary key(dependency_id, generation))""")
    op.execute("""create table if not exists project_dependency_edges (
        project_id text not null, requester_id text not null, dependency_id text not null,
        created_at text not null, primary key(project_id, requester_id, dependency_id))""")
    op.execute("""create table if not exists project_library_invocations (
        run_id text not null, invocation_key text not null, request_hash text not null,
        response_json text not null, primary key(run_id, invocation_key))""")
    columns = {row[1] for row in op.get_bind().exec_driver_sql("pragma table_info(runs)")}
    for name in ("dependency_id", "waiting_dependencies_json", "automation_budget_json"):
        if name not in columns:
            op.execute(f"alter table runs add column {name} text")
    op.execute("create index if not exists ix_dependency_owner on project_dependencies(owner_run_id)")


def downgrade():
    for name in ("automation_budget_json", "waiting_dependencies_json", "dependency_id"):
        op.drop_column("runs", name)
    for name in ("project_library_invocations", "project_dependency_edges",
                 "project_dependency_attempts", "project_dependencies", "project_library_modules"):
        op.drop_table(name)
