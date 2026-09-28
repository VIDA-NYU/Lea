"""Real Lean acceptance in disposable Lake projects; no user files or databases.
Run: python -m tests.lsp.test_recovery_integration
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import signal
import statistics
import subprocess
import time
import tempfile
import unittest
from unittest.mock import patch

from lea.check_runtime import CheckBudget, CheckFailure
from lea.lean_checks import check_file
from lea import lsp_daemon as lsp


@unittest.skipUnless(shutil.which('lake') and not os.environ.get('LEA_SKIP_LEAN_TESTS'), 'Lean toolchain required')
class RealLeanRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lea-check-recovery-')
        self.root = Path(self.temp.name).resolve()
        pin = Path(__file__).resolve().parents[2] / 'workspace' / 'lean-toolchain'
        (self.root / 'lean-toolchain').write_text(pin.read_text())
        (self.root / 'lakefile.toml').write_text('name = "recovery_test"\nversion = "0.1.0"\n[[lean_lib]]\nname = "Fixture"\nsrcDir = "proofs"\n')
        self.proofs = self.root / 'proofs' / 'Fixture'
        self.proofs.mkdir(parents=True)
        self.good = self.proofs / 'Good.lean'
        self.bad = self.proofs / 'Bad.lean'
        self.good.write_text('example : True := trivial\n')
        self.bad.write_text('example : False := by trivial\n')
    def tearDown(self):
        lsp._shutdown_all()
        lsp._roots.clear()
        lsp._daemons.clear()
        self.temp.cleanup()
    def check(self, path, **kwargs):
        return check_file(path, str(self.root), budget=CheckBudget(30), **kwargs)

    def test_concurrent_revision_checks_keep_warm_server(self):
        first = self.check(self.good)
        self.assertEqual(first.status, 'ok', first.output)
        generation = first.execution['daemon_generation']
        elapsed = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            for revision in range(6):
                self.good.write_text(f'-- revision {revision}\nexample : True := trivial\n')
                futures = [executor.submit(self.check, path) for path in (self.good, self.bad)]
                good, bad = [f.result() for f in futures]
                self.assertEqual(good.status, 'ok', good.output)
                self.assertEqual(bad.status, 'error', bad.output)
                for result in (good, bad):
                    self.assertEqual(result.execution['backend'], 'lsp', result.output)
                    self.assertEqual(result.execution['daemon_generation'], generation)
                    self.assertIsNone(result.execution['cold_reason'])
                    elapsed.append(result.execution['timings_ms']['total'])
        cold = [self.check(self.good, use_lsp=False) for _ in range(3)]
        self.assertTrue(all(c.status == 'ok' for c in cold))
        print(json.dumps({'warm_ms_median': statistics.median(elapsed),
                          'warm_ms_p95': sorted(elapsed)[-1],
                          'cold_ms_median': statistics.median(c.execution['timings_ms']['total'] for c in cold),
                          'healthy_fallbacks': 0}))

    def test_death_during_check_recovers_without_adapter_restart(self):
        first = self.check(self.good)
        self.assertEqual(first.status, 'ok', first.output)
        daemon = lsp._daemons[str(self.root)]
        original = daemon._request
        killed = False
        def kill_on_wait(method, params, timeout, budget=None):
            nonlocal killed
            if method == 'textDocument/waitForDiagnostics' and not killed:
                killed = True
                os.killpg(daemon._transport._proc.pid, signal.SIGKILL)
            return original(method, params, timeout, budget)
        with patch.object(daemon, '_request', kill_on_wait):
            result = self.check(self.good)
        self.assertEqual(result.status, 'ok', result.output)
        self.assertEqual(result.execution['backend'], 'lsp')
        self.assertEqual(result.execution['attempts'], 2)
        self.assertNotEqual(result.execution['daemon_generation'], first.execution['daemon_generation'])
        self.assertTrue(daemon.closed.wait(5))
        following = self.check(self.bad)
        self.assertEqual(following.execution['daemon_generation'], result.execution['daemon_generation'])
        self.assertEqual(following.status, 'error')

    def test_failed_start_falls_back_then_recovers(self):
        def fail(self, budget=None):
            self.failure = CheckFailure('startup', 'injected initialization failure')
            self.shutdown()
            return False
        with patch.object(lsp.LeanDaemon, 'start', fail):
            result = self.check(self.good)
            self.assertEqual(result.status, 'ok', result.output)
            self.assertEqual(result.execution['cold_reason'], 'fallback')
            self.assertEqual(result.execution['attempts'], 3)
        lsp._roots[str(self.root)].retry_at = 0
        result = self.check(self.good)
        self.assertEqual(result.execution['backend'], 'lsp', result.output)
        self.assertEqual(lsp.runtime_snapshot(str(self.root))['state'], 'ready')

    def test_rebuilt_dependency_rename_is_not_hidden_by_warm_cache(self):
        from lea.tools import rebuild_module, _lean_check_has_error
        upstream = self.proofs / 'Upstream.lean'
        downstream = self.proofs / 'Downstream.lean'
        upstream.write_text('namespace Fixture\ntheorem beforeRename : True := trivial\nend Fixture\n')
        downstream.write_text('import Fixture.Upstream\nexample : True := Fixture.beforeRename\n')
        built = rebuild_module(str(upstream))
        self.assertFalse(_lean_check_has_error(built), built)
        before = self.check(downstream)
        self.assertEqual(before.status, 'ok', before.output)
        upstream.write_text('namespace Fixture\ntheorem afterRename : True := trivial\nend Fixture\n')
        built = rebuild_module(str(upstream))
        self.assertFalse(_lean_check_has_error(built), built)
        after = self.check(downstream, allow_cold=False)
        self.assertEqual(after.status, 'error', after.output)
        self.assertNotEqual(after.execution['daemon_generation'], before.execution['daemon_generation'])
        # Cascades remain LSP-only until cold checks are also verified against
        # the product's Mathlib project layout.
        cold = self.check(downstream, use_lsp=False)
        self.assertEqual(cold.status, 'error', cold.output)

    def test_temporary_documents_are_evicted_and_shutdown_reaps_workers(self):
        for i in range(12):
            path = self.proofs / f'Temporary{i}.lean'
            path.write_text('example : True := trivial\n')
            checked = self.check(path)
            self.assertEqual(checked.execution['backend'], 'lsp', checked.output)
        daemon = lsp._daemons[str(self.root)]
        self.assertLessEqual(len(daemon.opened), 8)
        proc = daemon._transport._proc
        pid = proc.pid
        lsp._shutdown_all()
        self.assertIsNotNone(proc.poll())
        # Darwin can return EPERM for killpg(..., 0) even after the group
        # disappears. Inspect only numeric process/group ids instead.
        remaining = []
        for _ in range(50):
            listing = subprocess.run(['ps', '-axo', 'pid=,pgid='], capture_output=True, text=True, check=True)
            remaining = [line for line in listing.stdout.splitlines()
                         if len(line.split()) == 2 and int(line.split()[1]) == pid]
            if not remaining:
                break
            time.sleep(.02)
        self.assertEqual(remaining, [], 'test-owned Lean workers must exit')


if __name__ == '__main__':
    unittest.main()
