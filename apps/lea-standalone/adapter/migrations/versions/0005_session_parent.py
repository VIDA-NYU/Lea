"""session tree: parent_id + role + spawned_at_turn (v2.3 item 24 — sub-agents in the UI)

Revision ID: 0005_session_parent
Revises: 0004_backfill_timeline

A sub-agent run IS a session — a child of the coordinator that spawned it. Three
nullable columns on `sessions` make the tree explicit:

  * `parent_id`   — the coordinator session this child was delegated from (NULL for
                    every ordinary/root session, so the sidebar's root list is just
                    `parent_id is null`). Self-referential FK into `sessions`.
  * `role`        — the child's `subagent_type` ('proof-candidate', 'premise-search',
                    …); NULL for a root.
  * `spawned_at_turn` — the coordinator turn the delegation happened on, so the child
                    can render "delegated by … turn 4" and the timeline node can place
                    itself.

All three are NULL on every existing row, so this is additive and the root path is
byte-identical to before. The child's transcript is NOT stored here — it rides the
child's own `runs.transcript` (the existing set_run_transcript pattern), and the
child's exploration is materialized into its own `timeline` so it renders read-only
through the ordinary session view.

Runs automatically on startup (db.init_db → upgrade_to_head). Nothing to run by hand.
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_session_parent"
down_revision = "0004_backfill_timeline"
branch_labels = None
depends_on = None

# (column, type) added to `sessions`. All nullable — every existing row predates the
# tree and is a root.
NEW_COLUMNS = [
    ("parent_id", sa.Text()),
    ("role", sa.Text()),
    ("spawned_at_turn", sa.Integer()),
]


def _existing_columns(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.exec_driver_sql(f"pragma table_info({table})")}


def upgrade() -> None:
    have = _existing_columns("sessions")
    for column, type_ in NEW_COLUMNS:
        if column not in have:
            # SQLite ALTER ADD COLUMN can't add a REFERENCES constraint after the fact,
            # and it doesn't matter: parent_id is a plain self-referential pointer the
            # app maintains (children are created by the bridge, never by the user), and
            # keeping it constraint-free avoids a full table rebuild on a live DB.
            op.add_column("sessions", sa.Column(column, type_, nullable=True))


def downgrade() -> None:
    # Not usefully reversible: we can't know whether a given database originally had
    # the column, so dropping it could destroy a column that was never this revision's.
    raise NotImplementedError("session-tree columns are not reversibly droppable")
