"""Verify live preservation, migrate a copy and exercise restore on a copy."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT / 'apps/lea-standalone/adapter'))
from app import db, migrations


def table_hashes(database, columns=None):
    with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as conn:
        result = {}
        for table in ('sessions','runs','timeline','artifact_blobs'):
            names = columns[table] if columns else [r[1] for r in conn.execute(f'pragma table_info({table})')]
            rows = conn.execute(f'select {",".join(names)} from {table} order by rowid').fetchall()
            result[table] = hashlib.sha256(repr(rows).encode()).hexdigest()
        return result


def backup_file(root, name):
    for prefix, directory in (
        ('apps/lea-standalone/prover/workspace/proofs/', 'proofs'),
        ('apps/lea-standalone/prover/workspace/projects/', 'projects'),
    ):
        if name.startswith(prefix):
            return root / directory / name.removeprefix(prefix)
    if name == 'apps/lea-standalone/config/lea.local.toml':
        return root / 'lea.local.toml'
    raise AssertionError('Unexpected backup inventory path: ' + name)


def verify_files(root, inventory):
    assert all(hashlib.sha256(backup_file(root, name).read_bytes()).hexdigest() == value
               for name, value in inventory['files'].items()), 'Backup/restore file mismatch'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--backup',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    inventory=json.loads((args.backup/'inventory.json').read_text())
    live=ROOT/'apps/lea-standalone/data/lea-interface.sqlite3'
    assert table_hashes(live) == inventory['table_hashes'], 'Live rows changed'
    assert all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == value
               for name,value in inventory['files'].items()), 'A proof/project/config file changed'
    verify_files(args.backup, inventory)
    backup=args.backup/'lea-interface.sqlite3'
    if not backup.exists():
        candidates=list(args.backup.glob('*.sqlite3'))
        assert len(candidates)==1
        backup=candidates[0]
    assert table_hashes(backup)==inventory['table_hashes']
    with tempfile.TemporaryDirectory(prefix='lea-data-upgrade-') as tmp:
        original_db=db.DB_PATH
        try:
            copy=Path(tmp)/'copy.sqlite3'
            shutil.copyfile(backup,copy)
            with sqlite3.connect(copy) as conn:
                columns={t:[r[1] for r in conn.execute(f'pragma table_info({t})')] for t in inventory['table_hashes']}
            db.DB_PATH=copy
            db.init_db()
            assert table_hashes(copy,columns)==inventory['table_hashes']
            with sqlite3.connect(copy) as conn:
                assert conn.execute('pragma integrity_check').fetchone()[0]=='ok'
                assert conn.execute('select count(*) from proof_check_evidence').fetchone()[0]==0
                assert conn.execute('select count(*) from timeline where check_evidence_id is not null').fetchone()[0]==0
            assert migrations.current_revision()=='0032_proof_check_evidence'
            # Restore only the disposable copy, never the live data directory.
            # All connections are closed; discard migrated sidecars before restore.
            for suffix in ('-wal', '-shm', '-journal'):
                Path(str(copy) + suffix).unlink(missing_ok=True)
            shutil.copyfile(backup,copy)
            assert table_hashes(copy)==inventory['table_hashes']
            assert migrations.current_revision()==inventory['revision']
            restored = Path(tmp) / 'restored-files'
            restored.mkdir()
            for directory in ('proofs', 'projects'):
                shutil.copytree(args.backup / directory, restored / directory)
            shutil.copy2(args.backup / 'lea.local.toml', restored / 'lea.local.toml')
            verify_files(restored, inventory)
        finally:
            db.DB_PATH=original_db
    result={'live_database_unchanged':True,'files_unchanged':len(inventory['files']),
            'backup_files_verified':len(inventory['files']),
            'restored_files_verified':len(inventory['files']),
            'counts':inventory['counts'],'copy_upgrade':'0032_proof_check_evidence',
            'unsupported_evidence_backfilled':False,'restore_on_copy':True,
            'live_revision':inventory['revision']}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':
    main()
