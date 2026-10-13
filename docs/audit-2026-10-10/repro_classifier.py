"""Print conservative classifier responses that are parsed as success."""
from pathlib import Path
import json
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / 'apps/lea-standalone/adapter'))
from lea.agent import _parse_result_kind

inputs = [
    'PROVED',
    'NEEDS_REVIEW',
    'NOT PROVED',
    'NEEDS_REVIEW: this is not PROVED',
    'The original theorem was not disproved. PROVED',
]
print(json.dumps({text: _parse_result_kind(text) for text in inputs}, indent=2))
