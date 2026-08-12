"""per-session skill / MCP overrides (v2.5 E0e)

The second tier of the two-tier model: a project selects capabilities for **all** its
sessions, and a session may then add or drop items for itself.

**Stores the DIFF, never the resulting set.** If a session persisted its absolute list it
would snapshot the project at creation, and a skill added at project level later would
never reach sessions that already exist — contradicting "applies to every session in the
project". The effective set is derived (project's set ± this diff), and derived state is
computed, not stored. Same discipline as `subagent_overrides`, which persists only the
fields that differ from a role's default so untouched defaults keep flowing through.

A row naming an item later deleted from the library is ignored at resolution (soft-drop),
never an error — matching the policy for a sub-agent role that names a removed tool.

Revision ID: 0012_session_capability_overrides
Revises: 0011_mcp_servers
"""

from alembic import op


revision = "0012_session_capability_overrides"
down_revision = "0011_mcp_servers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists session_capability_overrides (
            session_id text not null references sessions(id),
            kind text not null,
            item_id text not null,
            action text not null,
            created_at text not null,
            primary key (session_id, kind, item_id),
            check (kind in ('skill', 'mcp_server')),
            check (action in ('add', 'remove'))
        )
        """
    )
    op.execute(
        "create index if not exists idx_session_capability_overrides_session "
        "on session_capability_overrides(session_id)"
    )


def downgrade() -> None:
    op.execute("drop index if exists idx_session_capability_overrides_session")
    op.execute("drop table if exists session_capability_overrides")
