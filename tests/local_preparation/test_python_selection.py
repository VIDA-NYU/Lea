"""Exercise uv discovery without changing a user's backend environments."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which('uv'), 'uv is required for interpreter discovery')
class PythonSelectionTests(unittest.TestCase):
    def test_backend_projects_select_ci_python_without_an_explicit_override(self):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('UV_') and key not in
               {'VIRTUAL_ENV', 'CONDA_PREFIX', 'CONDA_DEFAULT_ENV'}}
        uv = shutil.which('uv')
        flags = ['--offline', '--no-cache', '--no-python-downloads']
        available = subprocess.run([uv, 'python', 'find', '--system', '--no-project',
                                    '3.13', *flags], env=env, text=True,
                                   capture_output=True, timeout=30)
        if available.returncode:
            self.skipTest('Install Python 3.13 before running interpreter discovery')
        with tempfile.TemporaryDirectory(prefix='lea-python-selection-') as directory:
            root = Path(directory)
            (root / '.git').mkdir()
            # A repository-level request must not supply the nested projects' pin.
            (root / '.python-version').write_text('3.99\n')
            for component in ('adapter', 'prover'):
                with self.subTest(component=component):
                    relative = Path('apps/lea-standalone') / component
                    project = root / relative
                    project.mkdir(parents=True)
                    for name in ('pyproject.toml', '.python-version'):
                        shutil.copyfile(ROOT / relative / name, project / name)
                    result = subprocess.run([uv, 'python', 'find', '--system',
                                             '--show-version', *flags], cwd=project,
                                            env=env, text=True, capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertRegex(result.stdout.strip(), r'^3\.13\.\d+$')
