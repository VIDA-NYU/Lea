"""Persist namespace generations used by application readers."""
from alembic import op
revision='0046_project_namespace_generation'
down_revision='0045_source_archive_references'
branch_labels=None
depends_on=None
DOMAIN_TABLES=set()
INTERNAL_TABLES=set()


def upgrade():
    if 'namespace_generation' not in {r[1] for r in op.get_bind().exec_driver_sql('pragma table_info(projects)')}:
        op.execute('alter table projects add column namespace_generation integer not null default 1')
    op.execute('create trigger if not exists project_namespace_generation after update of namespace,repo_path on projects\n        when old.namespace is not new.namespace or old.repo_path is not new.repo_path begin\n        update projects set namespace_generation=old.namespace_generation+1 where id=new.id; end')


def downgrade():
    op.execute('drop trigger if exists project_namespace_generation')
    op.execute('alter table projects drop column namespace_generation')
