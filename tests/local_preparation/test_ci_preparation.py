"""CI compiler preparation with fake commands inside an isolated directory."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class CompilerPreparationTests(unittest.TestCase):
    def run_preparation(self, cache_failures=0, fail_build='', missing_executable=False):
        with tempfile.TemporaryDirectory(prefix='lea-ci-preparation-test-') as directory:
            home = Path(directory)
            (home / 'scripts').mkdir()
            shutil.copyfile(ROOT / 'scripts/prepare-ci-lean.sh', home / 'scripts/prepare-ci-lean.sh')
            (home / 'apps/lea-standalone/prover/workspace').mkdir(parents=True)
            (home / 'apps/lea-standalone/prover/workspace/lean-toolchain').write_text('leanprover/lean4:v4.29.0\n')
            verifier = home / 'apps/lea-standalone/prover/third_party/SafeVerify'
            verifier.mkdir(parents=True)
            commands = home / 'commands'
            commands.mkdir()
            (commands / 'elan').write_text('#!/usr/bin/env bash\nexit 0\n')
            (commands / 'elan').chmod(0o755)
            (commands / 'lake').write_text('''#!/usr/bin/env bash
set -eu
[[ "$ELAN_TOOLCHAIN" == 'leanprover/lean4:v4.29.0' ]]
printf '%s\\n' "$*" >> "$LEA_TEST_CALLS"
if [[ "$*" == 'exe cache get' ]]; then
  attempts=0
  if [[ -f "$LEA_TEST_ATTEMPTS" ]]; then read -r attempts < "$LEA_TEST_ATTEMPTS"; fi
  attempts=$((attempts+1))
  printf '%s\\n' "$attempts" > "$LEA_TEST_ATTEMPTS"
  if (( attempts <= LEA_TEST_CACHE_FAILURES )); then exit 7; fi
fi
if [[ "$*" == "$LEA_TEST_FAIL_BUILD" ]]; then exit 8; fi
if [[ "$*" == 'build safe_verify' && "$LEA_TEST_MISSING_EXE" == 0 ]]; then
  mkdir -p .lake/build/bin
  printf '#!/usr/bin/env bash\\nexit 0\\n' > .lake/build/bin/safe_verify
  chmod +x .lake/build/bin/safe_verify
fi
''')
            (commands / 'lake').chmod(0o755)
            env = os.environ.copy()
            env.pop('GITHUB_PATH', None)
            env.update(PATH=str(commands) + ':' + env['PATH'],
                       ELAN_HOME=str(home / 'isolated-elan'),
                       ELAN_TOOLCHAIN='incorrect-parent-toolchain',
                       GITHUB_ENV=str(home / 'github-environment'),
                       LEA_TEST_CALLS=str(home / 'calls'), LEA_TEST_ATTEMPTS=str(home / 'attempts'),
                       LEA_TEST_CACHE_FAILURES=str(cache_failures), LEA_TEST_FAIL_BUILD=fail_build,
                       LEA_TEST_MISSING_EXE=str(int(missing_executable)))
            result = subprocess.run(['bash', str(home / 'scripts/prepare-ci-lean.sh')],
                                    env=env, text=True, capture_output=True, timeout=15)
            calls = home / 'calls'
            self.assertTrue(calls.exists(), result.stderr)
            return result, calls.read_text().splitlines(), (home / 'github-environment').read_text().splitlines()

    def test_successful_cache_does_not_retry(self):
        result, calls, _ = self.run_preparation()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ['exe cache get', 'build Mathlib', 'build --no-build Mathlib', 'build safe_verify'])

    def test_transient_failure_retries_once_then_runs_all_gates(self):
        result, calls, _ = self.run_preparation(cache_failures=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ['exe cache get', 'exe cache get', 'build Mathlib',
                                 'build --no-build Mathlib', 'build safe_verify'])

    def test_two_failures_fall_back_to_mandatory_build_not_more_downloads(self):
        result, calls, _ = self.run_preparation(cache_failures=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count('exe cache get'), 2)
        self.assertEqual(calls[-3:], ['build Mathlib', 'build --no-build Mathlib', 'build safe_verify'])
        self.assertIn('compiling missing targets', result.stdout)

    def test_failed_mathlib_build_stops_before_verifier(self):
        result, calls, _ = self.run_preparation(cache_failures=2, fail_build='build Mathlib')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('build safe_verify', calls)
        self.assertNotIn('build --no-build Mathlib', calls)

    def test_failed_completeness_check_stops_before_verifier(self):
        result, calls, _ = self.run_preparation(fail_build='build --no-build Mathlib')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('build safe_verify', calls)

    def test_failed_safe_verify_build_is_fatal(self):
        result, calls, _ = self.run_preparation(fail_build='build safe_verify')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls[-1], 'build safe_verify')

    def test_missing_verifier_executable_is_fatal(self):
        result, calls, _ = self.run_preparation(missing_executable=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls[-1], 'build safe_verify')

    def test_selected_toolchain_is_exported_for_following_ci_steps(self):
        result, _, environment = self.run_preparation()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(environment, ['ELAN_TOOLCHAIN=leanprover/lean4:v4.29.0'])


if __name__ == '__main__':
    unittest.main()
