"""user-configured MCP servers (v2.5 E0)

Mirrors the `skills` / `skill_projects` pair deliberately: an MCP server is a library
item with the same two-tier scoping a skill has (global ∪ per-project, D47). Keeping the
two models identical is what stops them drifting once a skill starts declaring its own
servers via `.mcp.json` (H8) — a project-scoped skill activating a machine-global server
would be incoherent.

`env` holds only NON-SECRET literals; a credential is named in `env_from` and its value
read from the environment at spawn (A7). Nothing here ever stores a secret, so this table
is safe to dump, back up, and log.

Revision ID: 0011_mcp_servers
Revises: 0010_merge_github_imports_and_diagnostics
"""

from alembic import op


revision = "0011_mcp_servers"
down_revision = "0010_merge_github_imports_and_diagnostics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists mcp_servers (
            id text primary key,
            name text not null,
            slug text not null unique,
            transport text not null,
            -- stdio: a single executable; args/env/env_from are JSON arrays/objects.
            command text,
            args text not null default '[]',
            env text not null default '{}',
            env_from text not null default '[]',
            -- remote (sse / http): the endpoint. `api_key_name` NAMES the env var
            -- holding the credential — the value itself is never stored (A7).
            url text,
            api_key_name text,
            enabled integer not null default 1,
            is_global integer not null default 0,
            created_at text not null,
            updated_at text not null,
            check (transport in ('stdio', 'sse', 'http'))
        )
        """
    )
    op.execute(
        """
        -- Per-project assignment for non-global servers (D47), exactly as
        -- `skill_projects` works. Deleting a server cascades these rows explicitly in
        -- store.py — SQLite FKs aren't enforced here.
        create table if not exists mcp_server_projects (
            mcp_server_id text not null references mcp_servers(id),
            project_id text not null references projects(id),
            primary key (mcp_server_id, project_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("drop table if exists mcp_server_projects")
    op.execute("drop table if exists mcp_servers")
