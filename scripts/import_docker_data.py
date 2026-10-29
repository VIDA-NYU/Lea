"""Offline, non-overwriting import into Docker's Linux data volume.

Run only after stopping both native and Docker Lea processes. The source stays
read-only; SQLite recovery and verification happen on the staged Linux copy.
"""
from argparse import ArgumentParser
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

LOCK = '.lea-adapter.lock'
DATABASE = 'lea-interface.sqlite3'


@contextmanager
def lock(path, mode):
    with path.open(mode) as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('A Lea process still owns this data directory. Stop Lea before importing.') from error
        yield


def inventory(root):
    result = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise RuntimeError('Data import refuses symlinks: ' + relative)
        if path.is_dir():
            result[relative] = None
        elif path.is_file():
            if relative in {LOCK, DATABASE + '-shm'}:
                continue  # Locks and the derived WAL index belong to this host.
            digest = hashlib.sha256()
            with path.open('rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
            result[relative] = digest.hexdigest()
        else:
            raise RuntimeError('Data import refuses special files: ' + relative)
    return result


def import_data(source, destination):
    if any(path.is_symlink() for path in (source, destination, source / LOCK, destination / LOCK)):
        raise RuntimeError('Data directories and ownership locks must not be symlinks.')
    source, destination = source.resolve(), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise RuntimeError('Source and destination must be separate data directories.')
    if not source.is_dir() or not destination.is_dir():
        raise RuntimeError('Source and destination must already be mounted directories.')
    if any(path.name != LOCK for path in destination.iterdir()):
        raise RuntimeError('Destination is not empty. Existing volume data will never be overwritten.')
    # A stopped installation may have no lock file yet. Opening an existing
    # source lock read-only never modifies the retained host directory.
    from contextlib import nullcontext
    source_lock = lock(source / LOCK, 'rb') if (source / LOCK).exists() else nullcontext()
    with source_lock, lock(destination / LOCK, 'a+b'):
        before = inventory(source)
        with tempfile.TemporaryDirectory(prefix='.lea-import-', dir=destination) as folder:
            staging = Path(folder)
            for relative, digest in before.items():
                target = staging / relative
                if digest is None:
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source / relative, target)
            if inventory(staging) != before or inventory(source) != before:
                raise RuntimeError('Source changed during import. Stop every Lea process and retry with an empty destination.')
            database = staging / DATABASE
            if database.exists():
                # Opening the copy recovers retained WAL/journal data without
                # accessing shared-memory mappings on the macOS source mount.
                connection = sqlite3.connect(database)
                try:
                    if connection.execute('pragma integrity_check').fetchall() != [('ok',)]:
                        raise RuntimeError('Imported SQLite database failed its integrity check.')
                    connection.execute('pragma wal_checkpoint(truncate)')
                finally:
                    connection.close()
            # Publish only a complete, verified copy, and never replace a file.
            if any(path.name not in {LOCK, staging.name} for path in destination.iterdir()):
                raise RuntimeError('Destination changed during import; refusing to overwrite data.')
            for path in staging.iterdir():
                path.rename(destination / path.name)
    return {'imported_files': sum(value is not None for value in before.values()),
            'database_verified': DATABASE in before, 'source_unchanged': True}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('/source-data'))
    parser.add_argument('--destination', type=Path, default=Path('/app/data'))
    parser.add_argument('--source-stopped', action='store_true', help='Confirm every native and Docker Lea process using the source has stopped.')
    args = parser.parse_args()
    if not args.source_stopped:
        parser.error('Stop all Lea processes, then pass --source-stopped to import the retained data.')
    print(json.dumps(import_data(args.source, args.destination)))


if __name__ == '__main__':
    main()
