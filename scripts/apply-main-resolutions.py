"""Validate an approved preview; --apply stages a separately authorized merge.

The default is read-only. --apply leaves MERGE_HEAD pending and never commits.
Use only after explicit authorization to integrate main into the feature branch.
"""
import argparse
import json
from pathlib import Path
from main_merge import Repository, prepare_integration

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--main-ref', default='vida/main')
    parser.add_argument('--remote', default='vida')
    parser.add_argument('--branch', default='LeaCheckAddition')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--approved-preview', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='Stage a pending merge; requires separate authorization.')
    args = parser.parse_args()
    repository = Repository(args.repo)
    manifest = args.manifest or repository.root / 'docs/main-readiness/merge-resolutions.json'
    approved = json.loads(args.approved_preview.read_text())
    result = prepare_integration(repository, manifest, approved, args.main_ref,
                                 args.remote, args.branch, args.apply)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
