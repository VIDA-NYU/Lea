"""Unit tests for item 6 / D74 — the bounds on the expensive fallbacks in
`lea/tools.py`:

  * a semaphore around the cold `lake env lean <file>` compile in `lean_check`,
    so concurrent runs can't each spawn a full-Mathlib subprocess at once; and
  * a per-lake_root lock around `rebuild_module`'s `lake build`, with the
    `lsp_daemon.mark_stale` call held INSIDE that lock.

Both stub `subprocess.run` (and `mark_stale`) so no real Lean toolchain is
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
import lea.lsp_daemon as lsp_daemon
from lea.tools import lean_check, rebuild_module

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
    n_threads = 6

    live = {"now": 0, "max": 0}
    mlock = threading.Lock()

    def fake_run(cmd, **kwargs):
        with mlock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        time.sleep(0.05)
        with mlock:
            live["now"] -= 1
        return _FakeCompleted(returncode=0, stdout="", stderr="")

    tmp = Path(tempfile.mkdtemp()) / "T.lean"  # no lakefile above -> cmd=["lean", ...]
    tmp.write_text("theorem t : True := trivial\n")

    orig_run, orig_sem = tools.subprocess.run, tools._cold_check_sem
    tools.subprocess.run = fake_run
    tools._cold_check_sem = threading.BoundedSemaphore(limit)
    try:
        threads = [threading.Thread(target=lambda: lean_check(str(tmp), use_lsp=False))
                   for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        check("all cold checks completed", all(not t.is_alive() for t in threads))
        check(f"never more than {limit} cold compiles at once (saw {live['max']})",
              live["max"] <= limit)
        check("the bound was actually exercised (>1 concurrent)", live["max"] >= 2)
    finally:
        tools.subprocess.run = orig_run
        tools._cold_check_sem = orig_sem


def _make_project():
    tmp = Path(tempfile.mkdtemp())
    ws = tmp / "workspace"
    (ws / "proofs" / "Lea").mkdir(parents=True)
    (ws / "lakefile.lean").write_text("package lea\n")
    f = ws / "proofs" / "Lea" / "T.lean"
    f.write_text("theorem t : True := trivial\n")
    return f


def test_lake_build_lock_serializes_and_marks_stale_inside():
    """Concurrent rebuilds of one lake_root serialize, and each build's
    mark_stale fires before the lock releases — so no other build can slip in
    between a build writing its olean and that build flagging the daemon."""
    path = _make_project()
    n = 3

    # [enter, stale] window per thread; if mark_stale were outside the lock a
    # sibling build would enter during this window and the intervals overlap.
    windows: dict[int, dict] = {}
    wlock = threading.Lock()

    def fake_run(cmd, **kwargs):
        tid = threading.get_ident()
        with wlock:
            windows.setdefault(tid, {})["enter"] = time.monotonic()
        time.sleep(0.05)
        return _FakeCompleted(returncode=0, stdout="", stderr="")

    def fake_mark_stale(lake_root):
        tid = threading.get_ident()
        with wlock:
            windows.setdefault(tid, {})["stale"] = time.monotonic()

    orig_run = tools.subprocess.run
    orig_stale = lsp_daemon.mark_stale
    tools.subprocess.run = fake_run
    lsp_daemon.mark_stale = fake_mark_stale
    try:
        threads = [threading.Thread(target=lambda: rebuild_module(str(path))) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        check("all rebuilds completed", all(not t.is_alive() for t in threads))
        check("mark_stale called once per successful build", len(windows) == n
              and all("stale" in w for w in windows.values()))

        intervals = sorted((w["enter"], w["stale"]) for w in windows.values())
        disjoint = all(intervals[i][1] <= intervals[i + 1][0] + 1e-9
                       for i in range(len(intervals) - 1))
        check("build+mark_stale windows are disjoint (stale is inside the lock)", disjoint)
    finally:
        tools.subprocess.run = orig_run
        lsp_daemon.mark_stale = orig_stale


def main():
    print("tools fallback-bounds tests (item 6 / D74):")
    test_cold_check_semaphore_bounds_concurrency()
    test_lake_build_lock_serializes_and_marks_stale_inside()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All fallback-bounds tests passed.")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
