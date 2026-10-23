"""Retrieve the retained release baseline and verify its immutable commit identity."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def prepare(repository, manifest, destination):
    repository, destination = Path(repository), Path(destination)
    source, ref = manifest['source'], manifest['ref']
    if not re.fullmatch(r'[0-9a-f]{40}', source) or not ref.startswith('refs/tags/'):
        raise ValueError('The baseline requires a full commit SHA and a retained tag.')
    if destination.exists():
        raise FileExistsError('The baseline destination must not already exist.')

    def git(*args, **kwargs):
        return subprocess.run(['git', *args], cwd=repository, check=True, **kwargs)

    available = subprocess.run(['git', 'cat-file', '-e', source + '^{commit}'],
                               cwd=repository, capture_output=True).returncode == 0
    if not available:
        # The baseline is retained in its original repository, which need not
        # be the repository running this workflow. Never substitute its main.
        git('fetch', '--no-tags', '--', manifest['repository'], ref)
        fetched = git('rev-parse', 'FETCH_HEAD^{commit}', capture_output=True,
                      text=True).stdout.strip()
        if fetched != source:
            raise ValueError('The retained baseline tag does not match the pinned commit.')
    git('cat-file', '-e', source + '^{commit}')
    with tempfile.TemporaryFile() as archive:
        git('archive', '--format=tar', source, stdout=archive)
        archive.seek(0)
        destination.mkdir(parents=True)
        with tarfile.open(fileobj=archive) as bundle:
            bundle.extractall(destination, filter='data')
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((ROOT / '.github/stage8-release-baseline.json').read_text())
    source = prepare(ROOT, manifest, args.destination)
    if os.environ.get('GITHUB_ENV'):
        with open(os.environ['GITHUB_ENV'], 'a') as environment:
            environment.write(f'BASELINE_SOURCE={source}\n')
    print(json.dumps({'source': source, 'destination': str(args.destination)}))


if __name__ == '__main__':
    main()
