"""Unit tests for item 6 / D74 — the bounds on the expensive fallbacks in
`lea/tools.py`:

  * a semaphore around the cold `lake env lean <file>` compile in `lean_check`,
    so concurrent runs can't each spawn a full-Mathlib subprocess at once; and
  * a per-lake_root lock around `rebuild_module`'s `lake build`, with the
    daemon reconciliation held INSIDE that lock.

Both stub `subprocess.run` (and reconciliation) so no real Lean toolchain is
needed; they assert the concurrency shape, not Lean behavior. On `main` (no
semaphore / no build lock) the concurrency assertions fail.

Run:  uv run python -m tests.tools.test_fallback_bounds
Exits 0 if every check passes, 1 otherwise.
"""

import tempfile
import threading
import time
from pathlib import Path

import lea.tools as tools
import lea.lean_checks as lean_checks
import lea.check_evidence as check_evidence
from unittest.mock import patch
from lea.tools import lean_check, rebuild_module
from tests.check_fixtures import unit_environment

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_cold_check_semaphore_bounds_concurrency():
    """N threads on the cold subprocess path; no more than the semaphore's
    permit count run their compile at once."""
    limit = 2
    _FAILURES.clear()
    n_threads = 6

    live = {"now": 0, "max": 0}
    mlock = threading.Lock()

    class FakeProcess:
        returncode = 0
        stdin = stdout = stderr = None
        def __init__(self, *args, **kwargs):
            with mlock:
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
        def communicate(self, timeout=None, input=None):
            time.sleep(0.05)
            with mlock:
                live["now"] -= 1
            return "", ""

    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory) / "T.lean"
        tmp.write_text("theorem t : True := trivial\n")
        with patch.object(lean_checks.subprocess, 'Popen', FakeProcess), \
             patch('lea.check_evidence.prepared_environment', unit_environment), \
             patch.object(lean_checks, 'stop_process'), \
             patch.object(lean_checks, '_cold_check_sem', threading.BoundedSemaphore(limit)):
            results = []
            threads = [threading.Thread(target=lambda: results.append(lean_check(str(tmp), use_lsp=False)))
                       for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            check("all cold checks completed", all(not t.is_alive() for t in threads))
            check("all cold checks succeeded", len(results) == n_threads and all(r.startswith('OK') for r in results))
            check(f"never more than {limit} cold compiles at once (saw {live['max']})", live["max"] <= limit)
            check("the bound was actually exercised (>1 concurrent)", live["max"] >= 2)
            assert not _FAILURES


def _make_project():
    tmp = Path(tempfile.mkdtemp())
    ws = tmp / "workspace"
    (ws / "proofs" / "Lea").mkdir(parents=True)
    (ws / "lakefile.lean").write_text("package lea\n")
    f = ws / "proofs" / "Lea" / "T.lean"
    f.write_text("theorem t : True := trivial\n")
    return f


def test_lake_build_lock_serializes_and_reconciles_inside():
    """Concurrent rebuilds of one lake_root serialize, and each build's
    reconciliation completes before the lock releases, before another build or
    check can use potentially changed artifacts."""
    path = _make_project()
    _FAILURES.clear()
    n = 3

    # [enter, reconciled] window per thread; if reconciliation were outside the lock a
    # sibling build would enter during this window and the intervals overlap.
    windows: dict[int, dict] = {}
    wlock = threading.Lock()

    def fake_run(cmd, **kwargs):
        tid = threading.get_ident()
        with wlock:
            windows.setdefault(tid, {})["enter"] = time.monotonic()
        time.sleep(0.05)
        return _FakeCompleted(returncode=0, stdout="", stderr="")

    def fake_reconcile(lake_root):
        tid = threading.get_ident()
        with wlock:
            windows.setdefault(tid, {})["reconciled"] = time.monotonic()

    orig_run = tools.subprocess.run
    orig_stale = check_evidence.reconcile_daemon
    tools.subprocess.run = fake_run
    check_evidence.reconcile_daemon = fake_reconcile
    try:
        threads = [threading.Thread(target=lambda: rebuild_module(str(path))) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        check("all rebuilds completed", all(not t.is_alive() for t in threads))
        check("daemon reconciliation called once per build", len(windows) == n
              and all("reconciled" in w for w in windows.values()))

        intervals = sorted((w["enter"], w["reconciled"]) for w in windows.values())
        disjoint = all(intervals[i][1] <= intervals[i + 1][0] + 1e-9
                       for i in range(len(intervals) - 1))
        check("build+reconciliation windows are disjoint", disjoint)
        assert not _FAILURES
    finally:
        tools.subprocess.run = orig_run
        check_evidence.reconcile_daemon = orig_stale


def main():
    print("tools fallback-bounds tests (item 6 / D74):")
    test_cold_check_semaphore_bounds_concurrency()
    test_lake_build_lock_serializes_and_reconciles_inside()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All fallback-bounds tests passed.")
    raise SystemExit(0)


if __name__ == "__main__":
    from unittest.mock import patch
    from tests.check_fixtures import unit_environment
    with patch('lea.check_evidence.prepared_environment', unit_environment):
        main()
