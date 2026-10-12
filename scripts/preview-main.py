"""Resolve a main preview in temporary Git objects, without changing refs/index.

The resolution manifest records reviewed conflict paths. Unknown conflicts and
an advanced main fail closed. --record is used only after reviewing main's delta.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'docs/main-readiness/merge-resolutions.json'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--main-ref', default='vida/main')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--record', action='store_true')
    args = parser.parse_args()
    env = os.environ.copy()
    def git(*argv, input=None, check=True):
        return subprocess.run(['git', *argv], cwd=ROOT, env=env, input=input,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)
    main_sha = git('rev-parse', args.main_ref).stdout.decode().strip()
    head = git('rev-parse', 'HEAD').stdout.decode().strip()
    with tempfile.TemporaryDirectory(prefix='lea-merge-preview-') as tmp:
        objects = Path(tmp) / 'objects'
        objects.mkdir()
        env.update(GIT_OBJECT_DIRECTORY=str(objects),
                   GIT_ALTERNATE_OBJECT_DIRECTORIES=str(ROOT / '.git/objects'),
                   GIT_INDEX_FILE=str(Path(tmp) / 'index'),
                   GIT_AUTHOR_NAME='Lea preview', GIT_AUTHOR_EMAIL='preview@localhost',
                   GIT_COMMITTER_NAME='Lea preview', GIT_COMMITTER_EMAIL='preview@localhost')
        git('read-tree', head)
        paths = git('ls-files', '-z', '--cached', '--others', '--exclude-standard').stdout.split(b'\0')
        paths = [p.decode() for p in paths if p]
        # Only a temporary index is staged. Ignored credentials/data are excluded.
        for offset in range(0, len(paths), 100):
            git('add', '--', *paths[offset:offset + 100])
        feature_tree = git('write-tree').stdout.decode().strip()
        feature = git('commit-tree', feature_tree, '-p', head, input=b'preview only\n').stdout.decode().strip()
        preview = git('merge-tree', '--write-tree', '--name-only', '-z', feature, main_sha, check=False)
        if preview.returncode not in (0, 1):
            raise RuntimeError(preview.stderr.decode())
        chunks = preview.stdout.split(b'\0')
        tree = chunks.pop(0).decode()
        conflicts = []
        while chunks and chunks[0]:
            conflicts.append(chunks.pop(0).decode())
        changed_paths = git('diff', '--name-only', feature_tree, tree).stdout.decode().splitlines()
        if args.record:
            MANIFEST.parent.mkdir(parents=True, exist_ok=True)
            choices = {}
            for p in sorted(set(conflicts) | set(changed_paths)):
                entry = git('ls-tree', feature_tree, '--', p).stdout.decode().strip()
                choices[p] = {'choice': 'feature', 'feature_blob': entry.split('\t')[0].split(' ')[-1] if entry else None,
                    'reason': 'Preserve feature implementation after reconciling the main-only delta.'}
            MANIFEST.write_text(json.dumps({'main': main_sha, 'mirror_baseline': '91cc152',
                'resolutions': choices}, indent=2) + '\n')
        manifest = json.loads(MANIFEST.read_text())
        if manifest['main'] != main_sha:
            raise RuntimeError('Main advanced: review its changes and regenerate the resolution manifest.')
        unknown = (set(conflicts) | set(changed_paths)) - set(manifest['resolutions'])
        if unknown:
            raise RuntimeError('Unreviewed conflicts: ' + ', '.join(sorted(unknown)))
        git('read-tree', tree)
        for path in sorted(set(conflicts) | set(changed_paths)):
            if manifest['resolutions'][path]['choice'] != 'feature':
                raise RuntimeError('Unsupported resolution choice: ' + path)
            entry = git('ls-tree', feature_tree, '--', path).stdout.decode().strip()
            actual_blob = entry.split('\t')[0].split(' ')[-1] if entry else None
            if manifest['resolutions'][path].get('feature_blob') != actual_blob:
                raise RuntimeError('Resolution bytes changed since review: ' + path)
            if entry:
                mode, _, rest = entry.split(' ', 2)
                oid = rest.split('\t')[0]
                git('update-index', '--add', '--cacheinfo', f'{mode},{oid},{path}')
            else:
                git('update-index', '--force-remove', '--', path)
        resolved = git('write-tree').stdout.decode().strip()
        differences = git('diff', '--name-only', feature_tree, resolved).stdout.decode().splitlines()
        result = {'feature_head': head, 'main': main_sha, 'raw_conflicts': sorted(conflicts),
                  'resolved_conflicts': len(conflicts), 'unresolved_conflicts': [],
                  'resolved_tree': resolved, 'candidate_changes_from_feature': differences,
                  'refs_and_worktree_unchanged': True}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
        if differences:
            raise RuntimeError('The candidate has additional changes that require review.')


if __name__ == '__main__':
    main()
