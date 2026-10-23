"""Freshness renewal and scan bookkeeping do not change displayed domain facts."""
import re
from alembic import op

revision = '0051_idle_observation_renewal'
down_revision = '0050_target_observations'
branch_labels = None
depends_on = None

FIELDS = {
    'core_observations': ('id', 'scope', 'input_revision', 'cursor', 'state', 'environment_revision', 'inputs_json'),
    'core_projection_enrollment': ('scope', 'enabled', 'observation_id'),
    'workspace_observations': ('id', 'project_id', 'environment_revision', 'input_cursor', 'state', 'provenance', 'manifest_revision'),
    'projection_enrollment': ('project_id', 'version', 'enabled', 'observation_id'),
}


def _replace(table, guard):
    name = f'changes_{table}_update'
    sql = op.get_bind().exec_driver_sql('select sql from sqlite_master where type=\'trigger\' and name=?', (name,)).scalar_one()
    start = re.search(r'\bbegin\b', sql, re.I).start()
    header = re.split(r'\bwhen\b', sql[:start], maxsplit=1, flags=re.I)[0].rstrip()
    op.execute(f'drop trigger {name}')
    op.execute(header + (' when ' + guard if guard else '') + ' ' + sql[start:])


def upgrade():
    for table, fields in FIELDS.items():
        guard = ' or '.join(f'new.{field} is not old.{field}' for field in fields)
        if table.endswith('observations'):
            # A worker can explicitly retract freshness before its old expiry
            # (e.g. a disappeared workspace); those invalidations still notify.
            guard += ' or new.expires_at < old.expires_at'
        _replace(table, guard)


def downgrade():
    for table in FIELDS:
        _replace(table, None)
