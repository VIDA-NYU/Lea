"""declarative HTTP tools (v2.5 F1)

A REST endpoint as a tool, with no code. `params` is a JSON Schema the model fills in;
`url` may interpolate `{name}` placeholders from it. `auth_key_name` NAMES an environment
variable — the value is never stored, exactly as an MCP spec never stores one (A7).

Scoped like skills and MCP servers (global ∪ per-project) so one concept covers all three.

Revision ID: 0019_custom_tools
Revises: 0018_skill_triggers
"""

from alembic import op

revision = "0019_custom_tools"
down_revision = "0018_skill_triggers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists custom_tools (
            id text primary key,
            name text not null,
            slug text not null unique,
            description text not null default '',
            authoring text,
            method text not null default 'GET',
            url text not null,
            params text not null default '{}',
            headers text not null default '{}',
            auth_key_name text,
            auth_header text,
            timeout integer,
            enabled integer not null default 1,
            is_global integer not null default 0,
            created_at text not null,
            updated_at text not null,
            check (method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'))
        )
        """
    )
    op.execute(
        """
        create table if not exists custom_tool_projects (
            custom_tool_id text not null references custom_tools(id),
            project_id text not null references projects(id),
            primary key (custom_tool_id, project_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("drop table if exists custom_tool_projects")
    op.execute("drop table if exists custom_tools")
