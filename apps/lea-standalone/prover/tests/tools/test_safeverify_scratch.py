"""Unit tests for item 7 / D74·H2·H9 — SafeVerify concurrency safety:

  * `interface.verify` gives every call its OWN scratch dir (a per-call
    `TemporaryDirectory` under `.sv_scratch`) and threads it down to
    `safeverify.verify_proof` via `scratch_dir=`, so two concurrent runs on two
    files with the SAME stem no longer share `{stem}_sv_target.lean` /
    `{stem}_sv_submission.lean` (H2 — verifying A's submission against B's
    target, and the `finally` unlink deleting a peer's mid-compile file); and
  * a semaphore in `safeverify.verify_proof` bounds the concurrent compile/replay
    fan-out (H9 — the run lock that used to serialize this is being deleted).

Both stub the real Lean subprocesses so no toolchain is needed; they assert the
concurrency/isolation shape, not Lean behavior. On `main` (shared stem-keyed
scratch / no `scratch_dir=` threaded through / no semaphore) these fail.

Run:  uv run python -m tests.tools.test_safeverify_scratch
Exits 0 if every check passes, 1 otherwise.
"""

import tempfile
import threading
import time
from pathlib import Path

import lea.interface as interface
import lea.safeverify as safeverify

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def test_verify_uses_per_call_scratch_no_cross_contamination():
    """Two threads verify two DIFFERENT files that share the stem `Same`. Each
    call must land in its own scratch dir and see only its own submission bytes —
    proving no shared stem-keyed path and no cross-deletion."""
    root = Path(tempfile.mkdtemp())
    a_dir, b_dir = root / "a", root / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    # Same stem, distinguishable bodies.
    (a_dir / "Same.lean").write_text("theorem t : True := by trivial  -- AAA\n")
    (b_dir / "Same.lean").write_text("theorem t : True := by exact trivial  -- BBB\n")

    seen_dirs: list[Path] = []
    mismatches: list[str] = []
    dlock = threading.Lock()
    started = threading.Barrier(2)

    def fake_verify_proof(target, submission, lake_project, scratch_dir=None, **kw):
        # Record which scratch dir this call got, then hold the files open long
        # enough that a shared-path implementation would clobber/unlink them.
        marker = "AAA" if "AAA" in submission.read_text() else "BBB"
        with dlock:
            seen_dirs.append(Path(scratch_dir) if scratch_dir else None)
        started.wait(timeout=5)
        time.sleep(0.1)
        # Files must still be present and still be THIS call's bytes.
        try:
            body = submission.read_text()
        except FileNotFoundError:
            with dlock:
                mismatches.append(f"{marker}: submission deleted mid-verify")
            return False, "deleted"
        if marker not in body:
            with dlock:
                mismatches.append(f"{marker}: submission bytes changed to {body!r}")
        if not target.exists():
            with dlock:
                mismatches.append(f"{marker}: target deleted mid-verify")
        return True, "OK"

    orig_avail, orig_vp = safeverify.is_available, safeverify.verify_proof
    safeverify.is_available = lambda: True
    safeverify.verify_proof = fake_verify_proof
    try:
        results: dict[str, object] = {}
        def run(key, path):
            results[key] = interface.verify(str(path))
        ta = threading.Thread(target=run, args=("a", a_dir / "Same.lean"))
        tb = threading.Thread(target=run, args=("b", b_dir / "Same.lean"))
        ta.start(); tb.start()
        ta.join(timeout=5); tb.join(timeout=5)

        check("both verify calls completed", not ta.is_alive() and not tb.is_alive())
        if mismatches:
            print(f"       {mismatches}")
        check("no cross-contamination / deletion observed", not mismatches)
        real_dirs = [d for d in seen_dirs if d is not None]
        check("scratch_dir was threaded through to verify_proof", len(real_dirs) == 2)
        check("each call got a DISTINCT scratch dir",
              len({str(d) for d in real_dirs}) == 2)
        check("both verdicts are ok",
              all(getattr(r, "status", None) == "ok" for r in results.values()))
    finally:
        safeverify.is_available = orig_avail
        safeverify.verify_proof = orig_vp


def test_verify_proof_semaphore_bounds_concurrency():
    """N threads through `verify_proof`; the semaphore caps concurrent
    compile/replay work at its permit count."""
    limit = 2
    n_threads = 6

    live = {"now": 0, "max": 0}
    mlock = threading.Lock()

    def fake_compile(target_src, out_olean, lake_project, timeout):
        with mlock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        time.sleep(0.04)
        with mlock:
            live["now"] -= 1
        out_olean.write_bytes(b"")  # so `out.exists()` bookkeeping stays sane
        return True, ""

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    root = Path(tempfile.mkdtemp())
    orig_compile = safeverify._compile_to_olean
    orig_run = safeverify.subprocess.run
    orig_sem = safeverify._verify_sem
    safeverify._compile_to_olean = fake_compile
    safeverify.subprocess.run = lambda *a, **k: _Done()
    safeverify._verify_sem = threading.BoundedSemaphore(limit)
    try:
        def one(i):
            d = root / f"c{i}"
            d.mkdir()
            tgt = d / "P.lean"; sub = d / "P.lean"
            tgt.write_text("theorem t : True := by sorry\n")
            safeverify.verify_proof(tgt, sub, root, scratch_dir=d)

        threads = [threading.Thread(target=one, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        check("all verify_proof calls completed", all(not t.is_alive() for t in threads))
        check(f"never more than {limit} compiles at once (saw {live['max']})",
              live["max"] <= limit)
        check("the bound was actually exercised (>1 concurrent)", live["max"] >= 2)
    finally:
        safeverify._compile_to_olean = orig_compile
        safeverify.subprocess.run = orig_run
        safeverify._verify_sem = orig_sem


def main():
    print("SafeVerify scratch/semaphore tests (item 7 / D74·H2·H9):")
    test_verify_uses_per_call_scratch_no_cross_contamination()
    test_verify_proof_semaphore_bounds_concurrency()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All SafeVerify scratch/semaphore tests passed.")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
