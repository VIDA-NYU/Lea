"""Retained Docker data must survive the move off macOS shared storage."""
import fcntl
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('docker_data_import', ROOT / 'scripts/import_docker_data.py')
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


class DataImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'source'
        self.destination = Path(self.temp.name) / 'destination'
        self.source.mkdir()
        self.destination.mkdir()

    def test_retains_uncheckpointed_wal_and_files_without_changing_source(self):
        connection = sqlite3.connect(self.source / importer.DATABASE)
        self.addCleanup(connection.close)
        connection.execute('pragma journal_mode=wal')
        connection.execute('create table history(value)')
        connection.execute("insert into history values('retained conversation')")
        connection.commit()
        (self.source / 'worker-diagnostics').mkdir()
        (self.source / 'worker-diagnostics/events.jsonl').write_text('retained diagnostics')
        (self.source / importer.LOCK).write_text('old worker identity')
        before = {path.relative_to(self.source): path.read_bytes()
                  for path in self.source.rglob('*') if path.is_file()}
        result = importer.import_data(self.source, self.destination)
        self.assertTrue(result['database_verified'])
        self.assertEqual(before, {path.relative_to(self.source): path.read_bytes()
                                 for path in self.source.rglob('*') if path.is_file()})
        with sqlite3.connect(self.destination / importer.DATABASE) as copied:
            self.assertEqual(copied.execute('select * from history').fetchall(), [('retained conversation',)])
        self.assertEqual((self.destination / 'worker-diagnostics/events.jsonl').read_text(), 'retained diagnostics')

    def test_refuses_nonempty_destination(self):
        (self.destination / 'keep').write_text('existing data')
        with self.assertRaisesRegex(RuntimeError, 'not empty'):
            importer.import_data(self.source, self.destination)
        self.assertEqual((self.destination / 'keep').read_text(), 'existing data')

    def test_refuses_live_worker(self):
        with (self.source / importer.LOCK).open('w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with self.assertRaisesRegex(RuntimeError, 'still owns'):
                importer.import_data(self.source, self.destination)

    def test_corrupt_database_is_never_published(self):
        (self.source / importer.DATABASE).write_bytes(b'not a database')
        with self.assertRaises(sqlite3.DatabaseError):
            importer.import_data(self.source, self.destination)
        self.assertFalse((self.destination / importer.DATABASE).exists())

    def test_concurrent_source_edit_aborts_without_publishing(self):
        (self.source / 'history').write_text('before')
        copy = importer.shutil.copy2

        def edit_after_copy(source, destination):
            result = copy(source, destination)
            source.write_text('after')
            return result

        with patch.object(importer.shutil, 'copy2', side_effect=edit_after_copy):
            with self.assertRaisesRegex(RuntimeError, 'Source changed'):
                importer.import_data(self.source, self.destination)
        self.assertFalse((self.destination / 'history').exists())

    def test_symlinks_are_refused(self):
        (self.source / 'link').symlink_to(Path(self.temp.name))
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            importer.import_data(self.source, self.destination)

    def test_empty_installation_and_separate_paths(self):
        self.assertFalse(importer.import_data(self.source, self.destination)['database_verified'])
        with self.assertRaisesRegex(RuntimeError, 'separate'):
            importer.import_data(self.source, self.source)


if __name__ == '__main__':
    unittest.main()
