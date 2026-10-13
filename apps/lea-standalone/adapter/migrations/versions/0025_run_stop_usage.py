"""Stop provenance and completeness of observed run usage.

Existing rows remain unclassified: a historical zero alone is not proof that a
provider reported zero cost. New runs explicitly start with pending usage.
"""
from alembic import op

revision = "0025_run_stop_usage"
down_revision = "0024_formalize_batch_reports"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    columns = {row[1] for row in conn.exec_driver_sql("pragma table_info(runs)")}
    additions = {
        "stop_requested_reason": "text",
        "stop_requested_at": "text",
        "stop_requested_by": "text",
        "usage_status": "text",
        "usage_revision": "integer not null default 0",
        "usage_updated_at": "text",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.exec_driver_sql(f"alter table runs add column {name} {definition}")


def downgrade():
    conn = op.get_bind()
    for column in ("usage_updated_at", "usage_revision", "usage_status",
                   "stop_requested_by", "stop_requested_at", "stop_requested_reason"):
        columns = {row[1] for row in conn.exec_driver_sql("pragma table_info(runs)")}
        if column in columns:
            try:
                conn.exec_driver_sql(f"alter table runs drop column {column}")
            except Exception:  # older SQLite cannot drop columns
                pass
