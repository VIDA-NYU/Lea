"""Shared immutable source archives and accepted companion source bindings."""
from alembic import op
revision='0045_source_archive_references'
down_revision='0044_source_companion_projections'
branch_labels=None
depends_on=None
DOMAIN_TABLES=set()
INTERNAL_TABLES={'companion_source_bindings'}


def upgrade():
    conn=op.get_bind()
    for name in ('document_id','sha256'):
        if name not in {r[1] for r in conn.exec_driver_sql('pragma table_info(source_archives)')}:
            op.execute('alter table source_archives add column '+name+' text')
    if 'archive_path' not in {r[1] for r in conn.exec_driver_sql('pragma table_info(companion_imports)')}:
        op.execute("alter table companion_imports add column archive_path text not null default ''")
    op.execute('create unique index if not exists ix_source_archive_identity on source_archives(document_id,sha256)')
    op.execute('''create table if not exists companion_source_bindings(command_id text not null,
        publication_id text not null,primary key(command_id,publication_id))''')
    for action,ref in [('insert','new'),('update','new'),('delete','old')]:
        op.execute(f'''create trigger if not exists core_revision_source_heads_{action} after {action} on source_heads begin
            insert into core_scope_revisions select 'p:'||{ref}.project_id,1 where {ref}.project_id is not null
            on conflict(scope) do update set revision=revision+1; end''')


def downgrade():
    op.drop_table('companion_source_bindings')
    op.execute('drop index if exists ix_source_archive_identity')
