"""Preview reviewed main resolutions without changing Git refs or the real index.

--record writes a draft manifest only. Review and commit it before a new preview.
"""
import argparse
import json
from pathlib import Path
from main_merge import Repository, preview

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--main-ref', default='vida/main')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--record', action='store_true')
    args = parser.parse_args()
    repository = Repository(args.repo)
    manifest = args.manifest or repository.root / 'docs/main-readiness/merge-resolutions.json'
    result = preview(repository, manifest, args.main_ref, args.record)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
