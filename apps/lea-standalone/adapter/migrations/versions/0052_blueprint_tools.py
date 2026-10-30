"""Blueprint tools: sessions bound to a blueprint node, idempotent blueprint tool calls.

`sessions.blueprint_node` is the key of the blueprint node a session proves
(docs/PLAN-blueprint-tools.md §2.10). `blueprint_invocations` stores the reply to
each host-mediated blueprint tool call per (run_id, invocation_key), so a replayed
call returns the same answer (§2.3), as `project_library_invocations` (0029) does
for the project library.
"""
from alembic import op
import sqlalchemy as sa

revision = '0052_blueprint_tools'
down_revision = '0051_idle_observation_renewal'
branch_labels = None
depends_on = None

# Schema coverage (see 0037): the replay cache is bookkeeping, not a displayed
# domain fact, so it gets no change-feed triggers. Blueprint writes record their
# own project-scoped domain change (§2.9).
DOMAIN_TABLES = set()
INTERNAL_TABLES = {'blueprint_invocations'}


def upgrade():
    columns = {row[1] for row in op.get_bind().exec_driver_sql('pragma table_info(sessions)')}
    if 'blueprint_node' not in columns:
        op.add_column('sessions', sa.Column('blueprint_node', sa.Text(), nullable=True))
    op.execute('''create table if not exists blueprint_invocations (
        run_id text not null, invocation_key text not null, request_hash text not null,
        response_json text not null, primary key(run_id, invocation_key))''')


def downgrade():
    op.drop_table('blueprint_invocations')
    op.drop_column('sessions', 'blueprint_node')
