"""Reviewed main integration: temporary previews and an explicit pending merge.

Nothing here commits, pushes, publishes, fetches, or changes repository settings.
The apply entry point is reserved for a separately authorized integration.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile


class MergePreparationError(RuntimeError):
    pass


class Repository:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.env = os.environ.copy()
        for name in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
                     'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
            self.env.pop(name, None)
        self.env['GIT_TERMINAL_PROMPT'] = '0'
        actual = Path(self.text('rev-parse', '--show-toplevel')).resolve()
        if actual != self.root:
            raise MergePreparationError('Select the repository root, not a subdirectory.')

    def git(self, *args, env=None, input=None, check=True):
        result = subprocess.run(['git', '--literal-pathspecs', *args], cwd=self.root,
                                env=env or self.env, input=input, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=120)
        if check and result.returncode:
            raise MergePreparationError(result.stderr.decode(errors='replace').strip()
                                        or 'Git operation failed.')
        return result

    def text(self, *args, **kwargs):
        return self.git(*args, **kwargs).stdout.decode().strip()

    def commit(self, ref):
        return self.text('rev-parse', '--verify', ref + '^{commit}')

    def entry(self, tree, path, env=None):
        raw = self.git('ls-tree', '-z', tree, '--', path, env=env).stdout
        if not raw:
            return None
        records = raw.rstrip(b'\0').split(b'\0')
        if len(records) != 1:
            raise MergePreparationError('Resolution must select a single file: ' + path)
        header, actual = records[0].split(b'\t', 1)
        mode, kind, oid = header.decode().split()
        if os.fsdecode(actual) != path or kind != 'blob':
            raise MergePreparationError('Resolution must select a file or symlink: ' + path)
        return {'mode': mode, 'blob': oid}

    def ensure_clean(self, branch, head):
        current = self.git('symbolic-ref', '--quiet', '--short', 'HEAD', check=False)
        if current.returncode or current.stdout.decode().strip() != branch:
            raise MergePreparationError('Integration requires branch ' + branch + '.')
        if self.commit('HEAD') != head:
            raise MergePreparationError('Feature HEAD changed since approval.')
        if self.git('status', '--porcelain=v1', '-z', '--untracked-files=all').stdout:
            raise MergePreparationError('Integration requires a clean index and working tree.')
        for state in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge',
                      'rebase-apply', 'sequencer'):
            if Path(self.text('rev-parse', '--path-format=absolute', '--git-path', state)).exists():
                raise MergePreparationError('An existing Git operation must be settled first.')

    def verify_remote_main(self, remote, main):
        if remote not in self.text('remote').splitlines():
            raise MergePreparationError('Remote is not configured: ' + remote)
        raw = self.text('ls-remote', '--exit-code', '--', remote, 'refs/heads/main')
        records = raw.splitlines()
        if len(records) != 1 or records[0].split() != [main, 'refs/heads/main']:
            raise MergePreparationError('Remote main changed: review and refresh the baseline.')

    def ensure_no_local_collisions(self, main, head):
        def files(tree):
            return {os.fsdecode(p) for p in self.git('ls-tree', '-r', '--name-only', '-z', tree).stdout.split(b'\0') if p}
        tracked = files(head)
        for path in files(main):
            parts = PurePosixPath(path).parts
            for count in range(1, len(parts) + 1):
                prefix = '/'.join(parts[:count])
                local = self.root / prefix
                if (prefix not in tracked and os.path.lexists(local)
                        and (count == len(parts) or local.is_symlink() or not local.is_dir())):
                    raise MergePreparationError('Ignored or untracked path would be replaced: ' + prefix)

    @contextmanager
    def temporary_index(self):
        with tempfile.TemporaryDirectory(prefix='lea-merge-preview-') as directory:
            objects = Path(directory) / 'objects'
            objects.mkdir()
            original = self.text('rev-parse', '--path-format=absolute', '--git-path', 'objects')
            env = self.env | {
                'GIT_OBJECT_DIRECTORY': str(objects),
                'GIT_ALTERNATE_OBJECT_DIRECTORIES': json.dumps(original, ensure_ascii=False),
                'GIT_INDEX_FILE': str(Path(directory) / 'index'),
                'GIT_AUTHOR_NAME': 'Lea preview', 'GIT_AUTHOR_EMAIL': 'preview@localhost',
                'GIT_COMMITTER_NAME': 'Lea preview', 'GIT_COMMITTER_EMAIL': 'preview@localhost',
            }
            yield env


def read_manifest(path):
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if (not isinstance(data, dict) or data.get('format_version') != 2
            or not isinstance(data.get('resolutions'), dict)):
        raise MergePreparationError('A version 2 manifest with reviewed file modes is required.')
    if data.get('review_state') != 'reviewed':
        raise MergePreparationError('The draft manifest must be reviewed before preview approval.')
    for path, rule in data['resolutions'].items():
        if not isinstance(rule, dict) or not {'feature_blob', 'feature_mode'} <= rule.keys():
            raise MergePreparationError('Explicit reviewed blob and mode are required: ' + path)
        parts = PurePosixPath(path)
        if (not path or '\0' in path or parts.is_absolute() or str(parts) != path
                or '..' in parts.parts or any(p.casefold() == '.git' for p in parts.parts)):
            raise MergePreparationError('Invalid resolution path: ' + path)
        if rule.get('choice') != 'feature':
            raise MergePreparationError('Unsupported resolution choice: ' + path)
        blob, mode = rule.get('feature_blob'), rule.get('feature_mode')
        if blob is None:
            if mode is not None:
                raise MergePreparationError('A deletion must have a null mode: ' + path)
        elif (not isinstance(blob, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', blob)
              or mode not in ('100644', '100755', '120000')):
            raise MergePreparationError('Invalid reviewed blob or file mode: ' + path)
    return data, hashlib.sha256(raw).hexdigest()


def preview(repository, manifest_path, main_ref='vida/main', record=False):
    main = repository.commit(main_ref)
    head = repository.commit('HEAD')
    with repository.temporary_index() as env:
        repository.git('read-tree', head, env=env)
        # Update tracked paths separately: a file may become ignored after it
        # was committed, and explicit `git add path` rejects it in a fresh index.
        repository.git('add', '--update', env=env)
        paths = repository.git('ls-files', '-z', '--others', '--exclude-standard',
                               env=env).stdout.split(b'\0')
        paths = [os.fsdecode(path) for path in paths if path]
        for offset in range(0, len(paths), 100):
            repository.git('add', '--', *paths[offset:offset + 100], env=env)
        source_tree = repository.text('write-tree', env=env)
        feature = repository.text('-c', 'commit.gpgsign=false', 'commit-tree', source_tree, '-p', head,
                                  input=b'preview only\n', env=env)
        raw = repository.git('merge-tree', '--write-tree', '--name-only', '-z',
                             feature, main, env=env, check=False)
        if raw.returncode not in (0, 1):
            raise MergePreparationError(raw.stderr.decode(errors='replace'))
        chunks = raw.stdout.split(b'\0')
        merged_tree = chunks.pop(0).decode()
        conflicts = []
        while chunks and chunks[0]:
            conflicts.append(os.fsdecode(chunks.pop(0)))
        changed = repository.git('diff', '--name-only', '-z', source_tree, merged_tree,
                                 env=env).stdout.split(b'\0')
        required = set(conflicts) | {os.fsdecode(p) for p in changed if p}
        if record:
            choices = {}
            for path in sorted(required):
                entry = repository.entry(source_tree, path, env=env)
                choices[path] = {
                    'choice': 'feature', 'feature_blob': entry['blob'] if entry else None,
                    'feature_mode': entry['mode'] if entry else None,
                    'reason': 'Draft: review reconciliation with main before approving.',
                }
            Path(manifest_path).write_text(json.dumps({
                'format_version': 2, 'review_state': 'draft', 'main': main, 'resolutions': choices,
            }, indent=2) + '\n')
            return {'draft_manifest_recorded': True, 'approval_required': True,
                    'feature_head': head, 'main': main}
        manifest, digest = read_manifest(manifest_path)
        if manifest.get('main') != main:
            # A historical integration recipe need not pin every later PR's
            # base. Both the reviewed baseline and current main must already
            # be integrated, with no new changes requiring resolution.
            integrated = (not required and isinstance(manifest.get('main'), str)
                          and repository.git('merge-base', '--is-ancestor', manifest['main'],
                                             main, check=False).returncode == 0
                          and repository.git('merge-base', '--is-ancestor', main,
                                             head, check=False).returncode == 0)
            if not integrated:
                raise MergePreparationError('Main advanced: review its changes and refresh the manifest.')
        unknown = required - manifest['resolutions'].keys()
        if unknown:
            raise MergePreparationError('Unreviewed conflicts: ' + ', '.join(sorted(unknown)))
        # Historical resolutions stop pinning feature bytes once main is
        # integrated. Only paths this merge would change need those choices.
        for path in sorted(required):
            rule = manifest['resolutions'][path]
            entry = repository.entry(source_tree, path, env=env)
            if rule['feature_blob'] != (entry['blob'] if entry else None):
                raise MergePreparationError('Resolution bytes changed since review: ' + path)
            if rule.get('feature_mode') != (entry['mode'] if entry else None):
                raise MergePreparationError('Resolution mode changed since review: ' + path)
        repository.git('read-tree', merged_tree, env=env)
        for path in sorted(required):
            entry = repository.entry(source_tree, path, env=env)
            if entry:
                repository.git('update-index', '--add', '--cacheinfo', entry['mode'],
                               entry['blob'], path, env=env)
            else:
                repository.git('update-index', '--force-remove', '--', path, env=env)
        resolved = repository.text('write-tree', env=env)
        differences = repository.git('diff', '--name-only', '-z', source_tree, resolved,
                                     env=env).stdout.split(b'\0')
        differences = [os.fsdecode(p) for p in differences if p]
        if differences:
            raise MergePreparationError('Candidate has additional changes requiring review.')
        if repository.commit('HEAD') != head or repository.commit(main_ref) != main:
            raise MergePreparationError('Git refs changed during the preview.')
        return {
            'format_version': 1, 'feature_head': head, 'main': main,
            'source_tree': source_tree, 'manifest_sha256': digest,
            'raw_conflicts': sorted(conflicts), 'resolution_paths': sorted(required),
            'resolved_conflicts': len(conflicts), 'unresolved_conflicts': [],
            'resolved_tree': resolved, 'candidate_changes_from_feature': differences,
            'refs_and_worktree_unchanged': True,
        }


def prepare_integration(repository, manifest_path, approved, main_ref='vida/main',
                        remote='vida', branch='LeaCheckAddition', apply=False):
    if (not isinstance(approved, dict) or approved.get('format_version') != 1
            or approved.get('unresolved_conflicts') != []
            or approved.get('candidate_changes_from_feature') != []):
        raise MergePreparationError('A current, successful approved preview is required.')
    head = approved.get('feature_head')
    repository.ensure_clean(branch, head)
    current = preview(repository, manifest_path, main_ref)
    for key in ('feature_head', 'main', 'source_tree', 'resolved_tree', 'manifest_sha256',
                'resolution_paths', 'raw_conflicts'):
        if approved.get(key) != current[key]:
            raise MergePreparationError('Approved preview no longer matches: ' + key)
    if current['source_tree'] != repository.text('rev-parse', head + '^{tree}'):
        raise MergePreparationError('Approved source must be a committed, clean tree.')
    repository.verify_remote_main(remote, current['main'])
    repository.ensure_clean(branch, head)
    repository.ensure_no_local_collisions(current['main'], head)
    result = current | {'remote_main_verified': True, 'applied': False, 'committed': False}
    if not apply:
        return result
    if repository.git('merge-base', '--is-ancestor', current['main'], head,
                      check=False).returncode == 0:
        raise MergePreparationError('Main is already integrated; no merge is needed.')
    repository.verify_remote_main(remote, current['main'])
    repository.ensure_clean(branch, head)
    repository.ensure_no_local_collisions(current['main'], head)
    raw = repository.git('-c', 'core.hooksPath=/dev/null', '-c', 'rerere.enabled=false',
                         'merge', '--no-commit', '--no-ff', '--no-autostash',
                         '--no-overwrite-ignore', '--', current['main'], check=False)
    pending = repository.git('rev-parse', '--verify', 'MERGE_HEAD', check=False)
    if (raw.returncode not in (0, 1) or pending.returncode
            or pending.stdout.decode().strip() != current['main']):
        raise MergePreparationError('Git could not begin the pending merge. Inspect its state.')
    for path in current['resolution_paths']:
        if repository.entry(current['source_tree'], path):
            repository.git('restore', '--source', head, '--staged', '--worktree', '--', path)
        else:
            repository.git('rm', '--force', '--ignore-unmatch', '--', path)
    actual_tree = repository.text('write-tree')
    if (repository.git('ls-files', '--unmerged', '-z').stdout
            or actual_tree != current['resolved_tree']
            or repository.commit('HEAD') != head
            or repository.git('diff', '--quiet', check=False).returncode):
        raise MergePreparationError('Pending merge does not match approval. Inspect or abort it; do not commit.')
    return result | {'applied': True, 'pending_merge': True, 'head_unchanged': True,
                     'actual_tree': actual_tree}
