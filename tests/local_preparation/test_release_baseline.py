"""Fetch retained baselines from a separate Git repository and reject moved tags."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('release_baseline', ROOT / 'scripts/prepare-release-baseline.py')
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


class ReleaseBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lea-baseline-test-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.source, self.target = self.home / 'source', self.home / 'target'
        for repo in (self.source, self.target):
            repo.mkdir()
            self.git(repo, 'init', '-q')
            self.git(repo, 'config', 'user.name', 'Fixture')
            self.git(repo, 'config', 'user.email', 'fixture@localhost')
            self.git(repo, 'config', 'commit.gpgsign', 'false')
        (self.source / 'retained.txt').write_text('qualified baseline\n')
        self.git(self.source, 'add', '.')
        self.git(self.source, 'commit', '-qm', 'retained baseline')
        self.sha = self.git(self.source, 'rev-parse', 'HEAD')
        self.git(self.source, 'tag', 'strict-baseline')
        self.manifest = {'source': self.sha, 'ref': 'refs/tags/strict-baseline',
                         'repository': str(self.source)}
        self.destination = self.home / 'archive'

    def git(self, repository, *args):
        return subprocess.check_output(['git', *args], cwd=repository, stderr=subprocess.PIPE,
                                       text=True).strip()

    def test_fetches_baseline_missing_from_the_target_repository(self):
        self.assertEqual(baseline.prepare(self.target, self.manifest, self.destination), self.sha)
        self.assertEqual((self.destination / 'retained.txt').read_text(), 'qualified baseline\n')
        self.assertFalse((self.destination / '.git').exists())
        refs = subprocess.run(['git', 'show-ref'], cwd=self.target, capture_output=True, text=True)
        self.assertEqual(refs.stdout, '')

    def test_existing_immutable_commit_needs_no_remote(self):
        manifest = {**self.manifest, 'repository': 'unavailable-remote'}
        baseline.prepare(self.source, manifest, self.destination)
        self.assertEqual((self.destination / 'retained.txt').read_text(), 'qualified baseline\n')

    def test_rejects_a_tag_that_moved_to_another_commit(self):
        (self.source / 'retained.txt').write_text('unqualified replacement\n')
        self.git(self.source, 'commit', '-qam', 'different commit')
        self.git(self.source, 'tag', '-f', 'strict-baseline')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            baseline.prepare(self.target, self.manifest, self.destination)
        self.assertFalse(self.destination.exists())

    def test_rejects_a_missing_baseline_tag(self):
        self.git(self.source, 'tag', '-d', 'strict-baseline')
        with self.assertRaises(subprocess.CalledProcessError):
            baseline.prepare(self.target, self.manifest, self.destination)
        self.assertFalse(self.destination.exists())

    def test_never_overwrites_an_existing_archive(self):
        self.destination.mkdir()
        (self.destination / 'keep').write_text('retained evidence')
        with self.assertRaises(FileExistsError):
            baseline.prepare(self.target, self.manifest, self.destination)
        self.assertEqual((self.destination / 'keep').read_text(), 'retained evidence')


if __name__ == '__main__':
    unittest.main()
