"""Explicit schema bootstrap. Neither serving process runs migrations."""
import sqlite3
import os
import stat
from pathlib import Path
from . import db
SCHEMA='0048_process_boundary'
MAX_INTAKE_ENTRIES=10000


def prepare_storage_access():
    """The production API/worker share a service UID, without DAC override.

    Only the dedicated DB mount, SQLite files and private intake are prepared.
    Proofs, configuration, exports and backups are not recursively re-owned.
    The migration owner holds workspace ownership before calling this function.
    """
    if os.environ.get('LEA_REQUIRE_SHARED_OWNERSHIP')!='1':return
    root=db.DB_PATH.parent
    if root.is_symlink() or not os.path.ismount(root):
        raise RuntimeError('Production database storage must be a dedicated shared mount.')
    planned=[root]
    for suffix in ('','-wal','-shm','-journal'):
        path=Path(str(db.DB_PATH)+suffix)
        if path.exists() or path.is_symlink():planned.append(path)
    for name in ('http-intake','credential-intake'):
        path=root/name
        if path.exists() or path.is_symlink():
            pending=[path]
            while pending:
                node=pending.pop();planned.append(node)
                if len(planned)>MAX_INTAKE_ENTRIES:raise RuntimeError('Private intake requires offline cleanup before migration.')
                if node.is_symlink():raise RuntimeError('Shared database storage contains an unexpected symlink.')
                if node.is_dir():pending.extend(node.iterdir())
    for path in planned:
        mode=path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise RuntimeError('Shared database storage contains an unexpected file type.')
    for path in planned:
        os.chown(path,os.geteuid(),os.getegid(),follow_symlinks=False)
        path.chmod(0o700 if path.is_dir() else 0o600)
    for name in ('http-intake','credential-intake'):(root/name).mkdir(mode=0o700,exist_ok=True)

def require_schema():
    try:
        with sqlite3.connect(f'file:{db.DB_PATH}?mode=ro',uri=True) as conn:
            versions={r[0] for r in conn.execute('select version_num from alembic_version')}
    except sqlite3.Error as exc:
        raise RuntimeError('Database is not prepared; run bootstrap.py before API and worker.') from exc
    if versions!={SCHEMA}:raise RuntimeError(f'Incompatible database schema {versions}; expected {SCHEMA}. Run the supervised bootstrap before starting either process.')


def migrate():
    # Same lock as older in-process binaries: never migrate underneath a live
    # workspace owner, even during the one-time deployment transition.
    from . import runtime
    runtime.acquire_data_lock()
    try:
        from .config import ROOT,read_config_data
        data=read_config_data(Path(os.environ.get('LEA_CONFIG_FILE',ROOT/'config/lea.local.toml')))
        root=Path(data.get('lea_root') or 'prover')
        if not root.is_absolute():root=ROOT/root
        runtime.acquire_workspace_lock(root/'workspace')
        prepare_storage_access()
        db.init_db()
        prepare_storage_access()
    finally:
        runtime.release_workspace_lock()
        runtime.release_data_lock()
