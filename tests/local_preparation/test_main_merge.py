"""Exercise pending merges only in disposable repositories, never the checkout."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from main_merge import MergePreparationError, Repository, prepare_integration, preview


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lea-main-tool-test-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / 'repo with spaces'
        self.root.mkdir()
        self.env = os.environ.copy()
        for key in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
                    'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
            self.env.pop(key, None)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@localhost')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'core.autocrlf', 'false')
        self.git('config', 'core.hooksPath', '/dev/null')
        for name in ('conflict.txt', 'history.md', 'unchanged-history.md', 'remove.txt',
                     'mode.sh', 'odd [name]\nλ.txt'):
            (self.root / name).write_text('base\n')
        (self.root / 'link').symlink_to('base-target')
        (self.root / '.gitignore').write_text('private-data/\n')
        self.commit('base')
        self.base = self.git('rev-parse', 'HEAD')
        for name in ('conflict.txt', 'remove.txt', 'mode.sh', 'odd [name]\nλ.txt'):
            (self.root / name).write_text('main\n')
        (self.root / 'history.md').unlink()
        (self.root / 'unchanged-history.md').unlink()
        (self.root / 'link').unlink()
        (self.root / 'link').symlink_to('main-target')
        (self.root / 'ported-main.txt').write_text('main behavior retained\n')
        self.commit('main')
        self.main = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-q', '-b', 'LeaCheckAddition', self.base)
        for name in ('conflict.txt', 'history.md', 'mode.sh', 'odd [name]\nλ.txt'):
            (self.root / name).write_text('feature\n')
        (self.root / 'remove.txt').unlink()
        (self.root / 'mode.sh').chmod(0o755)
        (self.root / 'link').unlink()
        (self.root / 'link').symlink_to('feature-target')
        (self.root / 'ported-main.txt').write_text('main behavior retained\n')
        self.commit('feature reconciles main')
        self.repository = Repository(self.root)
        self.manifest = self.root / 'docs/main-readiness/merge-resolutions.json'
        self.manifest.parent.mkdir(parents=True)
        choices = {}
        for path in ('conflict.txt', 'history.md', 'unchanged-history.md', 'remove.txt',
                     'mode.sh', 'link', 'odd [name]\nλ.txt'):
            entry = self.repository.entry('HEAD', path)
            choices[path] = {'choice': 'feature', 'feature_blob': entry['blob'] if entry else None,
                             'feature_mode': entry['mode'] if entry else None, 'reason': 'Reviewed fixture'}
        self.recipe = {'format_version': 2, 'review_state': 'reviewed',
                       'main': self.main, 'resolutions': choices}
        self.save_recipe()
        self.commit('review recipe')
        self.head = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/vida/main', self.main)
        self.remote = self.home / 'remote.git'
        subprocess.run(['git', 'clone', '-q', '--bare', str(self.root), str(self.remote)],
                       env=self.env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.git('remote', 'add', 'vida', str(self.remote))
        (self.root / 'private-data').mkdir()
        (self.root / 'private-data/proof.lean').write_text('untouched private fixture\n')

    def git(self, *args):
        return subprocess.check_output(['git', '--literal-pathspecs', *args], cwd=self.root,
                                       env=self.env, stderr=subprocess.PIPE).decode().strip()

    def commit(self, message):
        self.git('add', '--all')
        self.git('commit', '-q', '-m', message)

    def save_recipe(self):
        self.manifest.write_text(json.dumps(self.recipe, indent=2) + '\n')

    def snapshot(self):
        return (self.git('show-ref'), self.git('status', '--porcelain=v1', '-z'),
                (self.root / '.git/index').read_bytes(),
                (self.root / 'private-data/proof.lean').read_bytes())

    def approved(self):
        return preview(self.repository, self.manifest)

    def assert_rejected_without_mutation(self, approved, pattern):
        before = self.snapshot()
        with self.assertRaisesRegex(MergePreparationError, pattern):
            prepare_integration(self.repository, self.manifest, approved, apply=True)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / '.git/MERGE_HEAD').exists())

    def test_preview_preserves_refs_real_index_private_files_and_tree(self):
        before = self.snapshot()
        result = self.approved()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(result['resolved_tree'], self.git('rev-parse', 'HEAD^{tree}'))
        self.assertIn('unchanged-history.md', result['resolution_paths'])
        self.assertNotIn('unchanged-history.md', result['raw_conflicts'])
        self.assertEqual(result['candidate_changes_from_feature'], [])

    def test_default_dry_run_is_read_only(self):
        approved = self.approved()
        before = self.snapshot()
        result = prepare_integration(self.repository, self.manifest, approved)
        self.assertFalse(result['applied'])
        self.assertTrue(result['remote_main_verified'])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / '.git/MERGE_HEAD').exists())

    def test_preview_keeps_edits_to_tracked_ignored_files_and_excludes_private_files(self):
        tracked = self.root / 'generated/status.json'
        tracked.parent.mkdir()
        tracked.write_text('committed status\n')
        self.commit('track generated status')
        with (self.root / '.gitignore').open('a') as file:
            file.write('generated/\n')
        self.commit('ignore future generated files')
        tracked.write_text('updated tracked status\n')
        private = tracked.with_name('private.json')
        private.write_text('private data stays out of the preview\n')
        before = self.snapshot()
        result = self.approved()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(tracked.read_text(), 'updated tracked status\n')
        # Preview objects are disposable, so compare against a normal index
        # snapshot in the fixture without retaining any of those objects.
        self.git('add', '--update')
        expected = self.git('write-tree')
        self.git('reset', '-q')
        self.assertEqual(result['source_tree'], expected)
        self.assertNotIn('generated/private.json', self.git('ls-files').splitlines())

    def test_explicit_apply_stages_exact_tree_modes_links_deletions_and_history(self):
        approved = self.approved()
        result = prepare_integration(self.repository, self.manifest, approved, apply=True)
        self.assertTrue(result['applied'])
        self.assertFalse(result['committed'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.head)
        self.assertEqual(self.git('rev-parse', 'MERGE_HEAD'), self.main)
        self.assertEqual(self.git('write-tree'), approved['resolved_tree'])
        self.assertEqual(self.git('ls-files', '-u'), '')
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.repository.entry('HEAD', 'mode.sh')['mode'], '100755')
        self.assertTrue((self.root / 'mode.sh').stat().st_mode & 0o111)
        self.assertEqual(os.readlink(self.root / 'link'), 'feature-target')
        self.assertFalse((self.root / 'remove.txt').exists())
        self.assertEqual((self.root / 'history.md').read_text(), 'feature\n')
        self.assertEqual((self.root / 'unchanged-history.md').read_text(), 'base\n')
        self.assertEqual((self.root / 'ported-main.txt').read_text(), 'main behavior retained\n')
        self.assertEqual((self.root / 'private-data/proof.lean').read_text(), 'untouched private fixture\n')

    def test_integrated_candidate_previews_cleanly_and_cannot_merge_twice(self):
        prepare_integration(self.repository, self.manifest, self.approved(), apply=True)
        self.git('commit', '-q', '-m', 'fixture integration')
        approved = self.approved()
        self.assertEqual(approved['raw_conflicts'], [])
        self.assertEqual(approved['candidate_changes_from_feature'], [])
        self.assertFalse(prepare_integration(self.repository, self.manifest, approved)['applied'])
        self.assert_rejected_without_mutation(approved, 'already integrated')

    def test_integrated_candidate_can_change_or_untrack_historical_resolutions(self):
        prepare_integration(self.repository, self.manifest, self.approved(), apply=True)
        self.git('commit', '-q', '-m', 'fixture integration')
        (self.root / 'conflict.txt').write_text('new feature work after integration\n')
        (self.root / '.gitignore').write_text('private-data/\nhistory.md\n')
        self.git('rm', '--cached', '--', 'history.md')
        self.commit('update feature and untrack agent documentation')
        before = self.snapshot()
        result = self.approved()
        self.assertEqual(result['resolution_paths'], [])
        self.assertEqual(result['resolved_tree'], self.git('rev-parse', 'HEAD^{tree}'))
        self.assertEqual(self.snapshot(), before)
        self.assertTrue((self.root / 'history.md').exists())

    def test_ignored_local_file_cannot_be_overwritten_by_main(self):
        self.git('checkout', '-q', 'main')
        (self.root / 'private-data/proof.lean').write_text('new main file\n')
        self.git('add', '--force', '--', 'private-data/proof.lean')
        self.git('commit', '-q', '-m', 'main adds a colliding file')
        self.main = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-q', 'LeaCheckAddition')
        (self.root / 'private-data').mkdir(exist_ok=True)
        (self.root / 'private-data/proof.lean').write_text('untouched private fixture\n')
        self.recipe['main'] = self.main
        self.recipe['resolutions']['private-data/proof.lean'] = {
            'choice': 'feature', 'feature_blob': None, 'feature_mode': None,
            'reason': 'Preserve the ignored local file without importing main’s file.'}
        self.save_recipe()
        self.commit('review new main')
        self.git('update-ref', 'refs/remotes/vida/main', self.main)
        shutil.rmtree(self.remote)
        subprocess.run(['git', 'clone', '-q', '--bare', str(self.root), str(self.remote)],
                       env=self.env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assert_rejected_without_mutation(self.approved(), 'path would be replaced')

    def test_dirty_worktree_rejected(self):
        approved = self.approved()
        (self.root / 'conflict.txt').write_text('new local work\n')
        self.assert_rejected_without_mutation(approved, 'clean index')

    def test_dirty_index_rejected(self):
        approved = self.approved()
        (self.root / 'new.txt').write_text('new local work\n')
        self.git('add', '--', 'new.txt')
        self.assert_rejected_without_mutation(approved, 'clean index')

    def test_untracked_work_rejected(self):
        approved = self.approved()
        (self.root / 'untracked.txt').write_text('new local work\n')
        self.assert_rejected_without_mutation(approved, 'clean index')

    def test_wrong_branch_rejected(self):
        approved = self.approved()
        self.git('checkout', '-q', '-b', 'other')
        self.assert_rejected_without_mutation(approved, 'requires branch')

    def test_detached_head_rejected(self):
        approved = self.approved()
        self.git('checkout', '-q', '--detach')
        self.assert_rejected_without_mutation(approved, 'requires branch')

    def test_changed_feature_head_rejected(self):
        approved = self.approved()
        self.git('commit', '-q', '--allow-empty', '-m', 'changed head')
        self.assert_rejected_without_mutation(approved, 'HEAD changed')

    def test_changed_local_main_rejected(self):
        approved = self.approved()
        self.git('update-ref', 'refs/remotes/vida/main', self.base)
        self.assert_rejected_without_mutation(approved, 'Main advanced')

    def test_changed_remote_main_rejected(self):
        approved = self.approved()
        subprocess.run(['git', '-C', str(self.remote), 'update-ref', 'refs/heads/main', self.head],
                       env=self.env, check=True)
        self.assert_rejected_without_mutation(approved, 'Remote main changed')

    def test_missing_remote_rejected(self):
        approved = self.approved()
        self.git('config', '--remove-section', 'remote.vida')
        self.assert_rejected_without_mutation(approved, 'not configured')

    def test_tampered_approved_tree_rejected(self):
        approved = self.approved()
        approved['resolved_tree'] = '0' * 40
        self.assert_rejected_without_mutation(approved, 'resolved_tree')

    def test_tampered_approved_manifest_fingerprint_rejected(self):
        approved = self.approved()
        approved['manifest_sha256'] = '0' * 64
        self.assert_rejected_without_mutation(approved, 'manifest_sha256')

    def test_legacy_preview_cannot_authorize_apply(self):
        approved = self.approved()
        del approved['format_version']
        self.assert_rejected_without_mutation(approved, 'current.*preview')

    def test_unknown_conflict_rejected(self):
        del self.recipe['resolutions']['conflict.txt']
        self.save_recipe()
        with self.assertRaisesRegex(MergePreparationError, 'Unreviewed conflicts'):
            self.approved()

    def test_changed_selected_bytes_rejected(self):
        (self.root / 'conflict.txt').write_text('new unreviewed bytes\n')
        with self.assertRaisesRegex(MergePreparationError, 'bytes changed'):
            self.approved()

    def test_changed_selected_mode_rejected(self):
        (self.root / 'mode.sh').chmod(0o644)
        with self.assertRaisesRegex(MergePreparationError, 'mode changed'):
            self.approved()

    def test_deleted_selected_source_rejected(self):
        (self.root / 'conflict.txt').unlink()
        with self.assertRaisesRegex(MergePreparationError, 'bytes changed'):
            self.approved()

    def test_legacy_manifest_requires_reviewed_modes(self):
        del self.recipe['format_version']
        self.save_recipe()
        with self.assertRaisesRegex(MergePreparationError, 'version 2'):
            self.approved()

    def test_draft_manifest_cannot_approve_preview(self):
        self.recipe['review_state'] = 'draft'
        self.save_recipe()
        with self.assertRaisesRegex(MergePreparationError, 'draft manifest'):
            self.approved()

    def test_deletion_requires_explicit_reviewed_fields(self):
        del self.recipe['resolutions']['remove.txt']['feature_mode']
        self.save_recipe()
        with self.assertRaisesRegex(MergePreparationError, 'Explicit reviewed blob and mode'):
            self.approved()

    def test_path_escape_rejected(self):
        self.recipe['resolutions']['../outside'] = deepcopy(self.recipe['resolutions']['conflict.txt'])
        self.save_recipe()
        with self.assertRaisesRegex(MergePreparationError, 'Invalid resolution path'):
            self.approved()

    def test_existing_merge_rejected(self):
        approved = self.approved()
        (self.root / '.git/MERGE_HEAD').write_text(self.main + '\n')
        before = self.snapshot()
        with self.assertRaisesRegex(MergePreparationError, 'existing Git operation'):
            prepare_integration(self.repository, self.manifest, approved, apply=True)
        self.assertEqual(self.snapshot(), before)

    def test_existing_sequencer_rejected(self):
        approved = self.approved()
        (self.root / '.git/sequencer').mkdir()
        self.assert_rejected_without_mutation(approved, 'existing Git operation')

    def test_late_edit_during_remote_check_rejected_before_merge(self):
        approved = self.approved()
        original = self.repository.verify_remote_main
        def edited(remote, main):
            original(remote, main)
            (self.root / 'conflict.txt').write_text('late external work\n')
        self.repository.verify_remote_main = edited
        with self.assertRaisesRegex(MergePreparationError, 'clean index'):
            prepare_integration(self.repository, self.manifest, approved, apply=True)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.head)
        self.assertFalse((self.root / '.git/MERGE_HEAD').exists())
        self.assertEqual((self.root / 'conflict.txt').read_text(), 'late external work\n')

    def test_cli_defaults_to_dry_run(self):
        approved = self.home / 'approved.json'
        approved.write_text(json.dumps(self.approved()))
        before = self.snapshot()
        result = subprocess.check_output([sys.executable, str(ROOT / 'scripts/apply-main-resolutions.py'),
            '--repo', str(self.root), '--approved-preview', str(approved)], env=self.env, text=True)
        self.assertFalse(json.loads(result)['applied'])
        self.assertEqual(self.snapshot(), before)

    def test_cli_explicit_apply_leaves_pending_merge_in_fixture_only(self):
        approved = self.home / 'approved.json'
        approved.write_text(json.dumps(self.approved()))
        result = subprocess.check_output([sys.executable, str(ROOT / 'scripts/apply-main-resolutions.py'),
            '--repo', str(self.root), '--approved-preview', str(approved), '--apply'], env=self.env, text=True)
        self.assertTrue(json.loads(result)['applied'])
        self.assertFalse(json.loads(result)['committed'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.head)
        self.assertEqual(self.git('rev-parse', 'MERGE_HEAD'), self.main)


if __name__ == '__main__':
    unittest.main()
