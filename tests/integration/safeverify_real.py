"""Fail-closed kernel checks in an isolated project, with real imported siblings."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'apps/lea-standalone/prover'))
from lea import interface, safeverify, lsp_daemon


def main():
    assert shutil.which('lake'), 'Real Lean is required'
    assert safeverify.is_available(), 'Built SafeVerify is required'
    original = safeverify.WORKSPACE
    results = []
    with tempfile.TemporaryDirectory(prefix='lea-sv-real-') as tmp:
        root = Path(tmp)
        for name in ('lakefile.lean', 'lean-toolchain', 'lake-manifest.json'):
            shutil.copyfile(original / name, root / name)
        (root / '.lake').mkdir()
        (root / '.lake/packages').symlink_to(original / '.lake/packages', target_is_directory=True)
        (root / 'proofs/Lea/Readiness').mkdir(parents=True)
        helper = root / 'proofs/Lea/Readiness/Helper.lean'
        helper.write_text('namespace Lea.Readiness\ntheorem helper : True := by trivial\nend Lea.Readiness\n')
        subprocess.run(['lake', 'build', 'Lea.Readiness.Helper'], cwd=root, check=True, timeout=120)
        safeverify.WORKSPACE = root
        try:
            for name, code, expected in [
                ('Targeted', 'import Mathlib.Data.Nat.Prime.Basic\ntheorem targeted (n : Nat) : n = n := by rfl\n', 'ok'),
                ('Namespaced', 'import Lea.Readiness.Helper\nnamespace Lea.Readiness\ntheorem demo : True := helper\nend Lea.Readiness\n', 'ok'),
                ('Sorry', 'theorem unsafe_demo : True := by sorry\n', 'rejected'),
            ]:
                path = root / f'{name}.lean'
                path.write_text(code)
                result = interface.verify(str(path))
                assert result.status == expected, (name, result)
                assert result.execution and result.execution.get('environment_sha256'), result
                results.append({'scenario': name, 'status': result.status, 'evidence': True})
            print(json.dumps(results))
        finally:
            safeverify.WORKSPACE = original
            lsp_daemon._shutdown_all()


if __name__ == '__main__':
    main()
