"""reconcile pre-Alembic schema drift

Revision ID: 0002_reconcile_legacy_drift
Revises: 0001_baseline

Databases created before Alembic are in an **unknown** state, and this revision
exists to end that.

The old policy was `create table if not exists` + "a schema change means a fresh
DB (`npm run reset:local`), not a migration". That only holds if every developer
resets on every schema change. They don't — measured, on the real database in this
repo: `code_steps.artifact_kind` (added in 98d69ef) was **absent**, because the
table already existed and `if not exists` never alters. The result was a database
that looked fine and threw `OperationalError: table code_steps has no column named
artifact_kind` on the next write. Silent drift, then a hard failure, which is the
same shape as every other bug in this workstream.

**Why the checks are conditional.** There is no single pre-Alembic state to migrate
*from*: a database created after 98d69ef already has the column, one created before
it does not, and both get stamped `0001_baseline` because the baseline no-ops on
whatever exists. An unconditional `add_column` would fail on the first group; doing
nothing fails the second. So this inspects and repairs. That asymmetry is a one-time
cost of the old policy — from here on, every schema change is a revision and the
state is known, so no later migration should ever need to look before it leaps.

**This is not a general drift-fixer.** It repairs the drift we could observe on the
databases we have. Another pre-Alembic database could have drifted in a way nothing
here knows about; for those, `reset:local` is still the fallback — and *now* is the
last moment that's a safe answer, because git still owns proof content. After the
blob migration, resetting means deleting the user's proofs.
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_reconcile_legacy_drift"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

# (table, column, type) declared by 0001_baseline that a legacy database may lack.
EXPECTED_COLUMNS = [
    ("code_steps", "artifact_kind", sa.Text()),
]


def _existing_columns(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.exec_driver_sql(f"pragma table_info({table})")}


def upgrade() -> None:
    for table, column, type_ in EXPECTED_COLUMNS:
        if column not in _existing_columns(table):
            # Nullable by necessity: existing rows have no value for it, and
            # `artifact_kind` is meaningful only where check_status='ok' anyway.
            op.add_column(table, sa.Column(column, type_, nullable=True))


def downgrade() -> None:
    # Not reversible in a useful sense: we cannot know whether a given database
    # originally had the column, so dropping it could destroy a column that was
    # never this revision's to add.
    raise NotImplementedError("legacy drift reconciliation is not reversible")
