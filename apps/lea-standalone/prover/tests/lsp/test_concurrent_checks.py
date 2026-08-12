"""Real-Lean concurrency guard for `lea/lsp_daemon.py` (slow, opt-in).

Two threads hammer one warm `lake env lean --server` with distinct documents --
a `Good` file (`1 + 1 = 2`) and a `Bad` file (`1 + 1 = 3`) -- over several
staggered rounds. Every Good check must come back clean and every Bad check must
report an error attributed to the Bad file. On the old single-shared-queue
daemon this is exactly what broke: different files starve each other to a 900s
timeout, and same-stem/same-content races can hand thread A a verdict computed
from thread B's document. `LEAN_CHECK_TIMEOUT=30` here so that starvation fails
fast instead of after 15 minutes.

A second scenario (B3, the Phase-E prereq) models N parallel sub-agents: N
distinct documents — all sharing the basename `candidate.lean` and identical
content, the worst case for the same-stem/same-content race — checked
concurrently on the one shared daemon, each verdict required to be attributed to
its OWN file. It also exercises the B1 interaction: one "child" finishes early
and `close_documents_under` reaps its document WHILE its siblings are mid-check,
which must drop exactly that child's uri from the daemon and disturb no sibling.

This needs the real toolchain + a built workspace, so it is opt-in. It runs when
invoked directly if `lake` is on PATH; set `LEA_SKIP_LEAN_TESTS=1` to skip.

Run:  uv run python -m tests.lsp.test_concurrent_checks
Exits 0 if every check passes (or the suite is skipped), 1 otherwise.
"""
import os
import shutil
import sys
import threading
from pathlib import Path

os.environ.setdefault("LEAN_CHECK_TIMEOUT", "30")

import lea.lsp_daemon as lsp_daemon  # noqa: E402
from lea.lsp_daemon import check_via_lsp  # noqa: E402

_FAILURES: list[str] = []

WORKSPACE = (Path(__file__).resolve().parents[2] / "workspace").resolve()
PROOFS = WORKSPACE / "proofs" / "Lea"
GOOD = PROOFS / "_ConcGood.lean"
BAD = PROOFS / "_ConcBad.lean"

GOOD_SRC = "import Mathlib\n\nnamespace Lea.ConcGood\n\nexample : 1 + 1 = 2 := by rfl\n"
BAD_SRC = "import Mathlib\n\nnamespace Lea.ConcBad\n\nexample : 1 + 1 = 3 := by rfl\n"

ROUNDS = 3

# B3 multi-child scenario: N parallel sub-agents, each with its own scratch dir.
# Kept modest so peak = N concurrent `lean --worker` (each loads Mathlib) stays
# laptop-safe — the very resource this suite guards.
CHILDREN = 4
CONC_DIR = PROOFS / "_conc"


def note(msg: str) -> None:
    print(f"  {msg}")


def fail(msg: str) -> None:
    print(f"  FAIL {msg}")
    _FAILURES.append(msg)


def _skip(reason: str) -> None:
    print(f"lsp_daemon concurrent real-Lean test: SKIPPED ({reason})")
    sys.exit(0)


def _scenario_two_thread(lake_root: str) -> None:
    """Original guard: one Good + one Bad file, hammered concurrently."""
    GOOD.write_text(GOOD_SRC)
    BAD.write_text(BAD_SRC)

    verdicts: dict[str, list] = {"good": [], "bad": []}
    errors: list[str] = []
    lock = threading.Lock()

    def hammer(path: Path, src: str, key: str):
        for _ in range(ROUNDS):
            try:
                out = check_via_lsp(str(path), src, lake_root)
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(f"{key}: {e!r}")
                return
            with lock:
                verdicts[key].append(out)

    tg = threading.Thread(target=hammer, args=(GOOD, GOOD_SRC, "good"))
    tb = threading.Thread(target=hammer, args=(BAD, BAD_SRC, "bad"))
    tg.start()
    tb.start()  # staggered only by thread-start latency; opens interleave
    tg.join()
    tb.join()

    if errors:
        for e in errors:
            fail(f"check raised: {e}")
    note(f"good verdicts: {len(verdicts['good'])}/{ROUNDS}, bad: {len(verdicts['bad'])}/{ROUNDS}")
    if len(verdicts["good"]) != ROUNDS:
        fail(f"expected {ROUNDS} good verdicts, got {len(verdicts['good'])}")
    for i, out in enumerate(verdicts["good"]):
        if "no errors" not in out:
            fail(f"good round {i} not clean: {out!r}")
    if len(verdicts["bad"]) != ROUNDS:
        fail(f"expected {ROUNDS} bad verdicts, got {len(verdicts['bad'])}")
    for i, out in enumerate(verdicts["bad"]):
        # Must be an error, and attributed to the Bad file — not Good's.
        if "error" not in out.lower():
            fail(f"bad round {i} did not report an error: {out!r}")
        elif "_ConcBad.lean" not in out:
            fail(f"bad round {i} verdict not attributed to Bad file: {out!r}")


def _opened_snapshot(daemon, uri: str) -> bool:
    """Thread-safe membership read of the daemon's `opened` set (siblings mutate
    it under `_state_lock` as they open documents)."""
    if daemon is None:
        return False
    with daemon._state_lock:
        return uri in daemon.opened


def _scenario_multi_child(lake_root: str) -> None:
    """B3: N distinct docs checked concurrently on the one shared daemon, plus a
    close-while-siblings-check reap (the B1 interaction)."""
    children = []
    for i in range(CHILDREN):
        d = CONC_DIR / f"child{i}"
        d.mkdir(parents=True, exist_ok=True)
        good = (i % 2 == 0)
        p = d / "candidate.lean"          # same basename for every child on purpose
        src = GOOD_SRC if good else BAD_SRC
        p.write_text(src)
        children.append((d, p, src, good))

    results: dict[int, list] = {i: [] for i in range(CHILDREN)}
    errors: list[str] = []
    lock = threading.Lock()
    # child0 finishes after ONE round (an early "return"); siblings run longer, so
    # the reap below lands while they are still mid-check.
    rounds_for = {i: (1 if i == 0 else ROUNDS + 2) for i in range(CHILDREN)}

    def hammer(i: int, path: Path, src: str):
        for _ in range(rounds_for[i]):
            try:
                out = check_via_lsp(str(path), src, lake_root)
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(f"child{i}: {e!r}")
                return
            with lock:
                results[i].append(out)

    threads = [threading.Thread(target=hammer, args=(i, p, src))
               for i, (_d, p, src, _g) in enumerate(children)]
    for t in threads:
        t.start()

    # Reap child0's scratch dir once its single-round thread is done, while the
    # longer-running siblings are still hammering their own files.
    child0_dir, child0_path = children[0][0], children[0][1]
    child0_uri = Path(child0_path).resolve().as_uri()
    sibling_uris = {i: Path(children[i][1]).resolve().as_uri() for i in range(1, CHILDREN)}
    threads[0].join()
    daemon = lsp_daemon._daemons.get(lake_root)
    open_before = _opened_snapshot(daemon, child0_uri)
    n_closed = lsp_daemon.close_documents_under(str(child0_dir))
    open_after = _opened_snapshot(daemon, child0_uri)
    siblings_intact = all(_opened_snapshot(daemon, u) for u in sibling_uris.values())
    for t in threads[1:]:
        t.join()

    # --- correctness: every verdict attributed to its OWN file, no cross-talk ---
    if errors:
        for e in errors:
            fail(f"multi-child check raised: {e}")
    for i, (_d, _p, _src, good) in enumerate(children):
        outs = results[i]
        if not outs:
            fail(f"child{i} produced no verdict")
            continue
        for j, out in enumerate(outs):
            if good and "no errors" not in out:
                fail(f"child{i} (good) round {j} not clean: {out!r}")
            elif not good and "error" not in out.lower():
                fail(f"child{i} (bad) round {j} not an error: {out!r}")
            elif not good and f"child{i}/candidate.lean" not in out:
                fail(f"child{i} (bad) round {j} cross-talked (wrong file): {out!r}")
    total = sum(len(v) for v in results.values())
    note(f"multi-child: {CHILDREN} concurrent same-basename docs, {total} checks, no cross-talk")

    # --- B1 interaction: reap-while-busy closed exactly child0, siblings intact ---
    if not open_before:
        fail("child0 doc was not open before reap (nothing to verify)")
    if n_closed < 1:
        fail(f"close_documents_under reaped nothing (got {n_closed})")
    if open_after:
        fail("child0 doc still open after reap")
    if not siblings_intact:
        fail("reap-while-busy disturbed a sibling's open document")
    note(f"reap-while-busy: closed {n_closed} (child0), siblings still open: {siblings_intact}")


def main():
    if os.environ.get("LEA_SKIP_LEAN_TESTS"):
        _skip("LEA_SKIP_LEAN_TESTS set")
    if shutil.which("lake") is None:
        _skip("lake not on PATH")
    if not (WORKSPACE / "lakefile.lean").exists():
        _skip(f"no workspace at {WORKSPACE}")

    print("lsp_daemon concurrent real-Lean test (this is slow — cold Mathlib load):")
    lake_root = str(WORKSPACE)
    try:
        _scenario_two_thread(lake_root)      # shares the warm daemon with…
        _scenario_multi_child(lake_root)     # …the B3 multi-child scenario
    finally:
        lsp_daemon._shutdown_all()
        for p in (GOOD, BAD):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        shutil.rmtree(CONC_DIR, ignore_errors=True)

    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)})")
        sys.exit(1)
    print("Concurrent real-Lean checks passed: 2-thread + multi-child, no cross-talk, "
          "reap-while-busy clean.")
    sys.exit(0)


if __name__ == "__main__":
    main()
