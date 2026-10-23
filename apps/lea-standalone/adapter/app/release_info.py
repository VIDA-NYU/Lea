"""Non-secret identity of the packaged binaries for upgrade/rollback checks."""
import json
from pathlib import Path


def identity():
    path=Path(__file__).with_name('_release.json')
    value=json.loads(path.read_text()) if path.exists() else {}
    from .bootstrap import SCHEMA
    return {'version':1,'source':value.get('source','development'),'schema':SCHEMA}
