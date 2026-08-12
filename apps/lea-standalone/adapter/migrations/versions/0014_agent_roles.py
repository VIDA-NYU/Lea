"""user-authored sub-agent roles (v2.5 B2)

Until now a role was a YAML file vendored inside the prover package, so the Sub-agents
page could only *retune* the two that shipped. This is where a user's own role lives.

Deliberately **global** — no per-project join, unlike skills and MCP servers. A role is a
way of working, not a resource a project owns, and the Skills / MCP tab already tells the
user "sub-agent roles apply to every project". Adding scoping later is additive; claiming
it now and not honouring it would not be.

Columns mirror `AgentProfile` (the prover's parsed role) so materialization is a
field-for-field write, with no mapping layer to drift.

Revision ID: 0014_agent_roles
Revises: 0013_rename_skill_mcp_overrides
"""

from alembic import op


revision = "0014_agent_roles"
down_revision = "0013_rename_skill_mcp_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists agent_roles (
            id text primary key,
            name text not null,
            slug text not null unique,
            description text,
            system_prompt text not null,
            model text,
            tools text,
            max_turns integer,
            created_at text not null,
            updated_at text not null
        )
        """
    )


def downgrade() -> None:
    op.execute("drop table if exists agent_roles")
