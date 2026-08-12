"""Verdict-integrity under concurrency — v2.3 item 17, the adapter acceptance test.

Six runs execute at once through the REAL bridge pipeline: real per-session git
repos, real concurrent DB writes, real `timeline` ordering, and the real *derived*
session status (D14 — status is the latest code row's verdict, never stored). Three
runs are scripted provable, three false.

The `run_events` layer is faked (no model, no `lake`) so this is deterministic and
runs in milliseconds. That is deliberate division of labor, not a shortcut: the REAL
concurrent Lean-check path with no cross-talk is already pinned by the prover's
`tests/lsp/test_concurrent_checks.py` (real Mathlib, 3 good + 3 bad) and
`tests/lsp/test_dispatch_unit.py` (deterministic, id-collision + adversarial frame
order). Those own the *LSP* layer. THIS test owns the *adapter attribution* layer —
the seam neither of those touches.

The failure it guards: under six-way overlap, does each run's verdict and file land
on ITS OWN session, or does one run's outcome get stamped onto another's — a
mislabeled session? A `threading.Barrier` makes all six rendezvous mid-run (files
written, verdicts still pending) so any shared state — a global "current path", a
raced timeline position — has the maximum opportunity to cross the wires before the
verdicts commit. Then: exactly the three provable sessions must derive `proved`, the
three false ones `error`, and every session's stored proof must carry its OWN marker
and no other run's.
"""

import threading
from pathlib import Path
from queue import Queue

from lea.events import CheckResult, FileChanged, Finished, ToolCalled, TurnStarted
from lea.providers import Usage

from app import bridge, db, store
from app.config import LeaConfig

# 3 provable + 3 false, interleaved so "exactly three proved" can't pass by a run of
# contiguous verdicts lining up with insertion order. `PROVABLE`/`FALSE` are the two
# scripted outcomes; the marker is a unique substring stamped into each run's file.
_PLAN = [
    ("MARKER_00", "provable"),
    ("MARKER_01", "false"),
    ("MARKER_02", "provable"),
    ("MARKER_03", "false"),
    ("MARKER_04", "provable"),
    ("MARKER_05", "false"),
]


def _content(marker: str, verdict: str) -> str:
    # Distinct bytes per run so a content bleed is detectable: the stored code row for
    # session i must contain marker i and no other. (The scripted CheckResult, not this
    # text, decides the verdict — the bridge trusts the event, so content is free.)
    goal = "True := by trivial" if verdict == "provable" else "False := by sorry"
    return f"import Mathlib\n\n-- {marker}\ntheorem {marker} : {goal}\n"


def _dispatching_fake(plan_by_session: dict, barrier: threading.Barrier):
    """One fake `run_events` shared by all six runs (bridge.run_events is a single
    module global). It dispatches on `session_id` to the right marker/verdict, writes
    that run's file, then blocks on the barrier so no run emits its verdict until all
    six have written — the maximally adversarial overlap."""

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None):
        marker, verdict = plan_by_session[session_id]
        proof = Path(working_dir) / "Lea" / "Misc" / f"{marker}.lean"
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_text(_content(marker, verdict))

        yield TurnStarted(1)
        yield ToolCalled("write_file", {"path": str(proof)})
        yield FileChanged(str(proof))

        # All six rendezvous here: every file on disk, every verdict still pending.
        barrier.wait()

        yield ToolCalled("lean_check", {"path": str(proof)})
        if verdict == "provable":
            yield CheckResult(str(proof), "ok", None)
            yield Finished("completed", f"{marker} proved.", 1, session_id, "gemini/test",
                           Usage(input_tokens=10, output_tokens=5), 0.01, {})
        else:
            # A genuinely false claim: the file errors and the agent runs out of turns.
            yield CheckResult(str(proof), "error", f"{marker}: `False` is not provable")
            yield Finished("max_turns", f"{marker} did not compile.", 1, session_id,
                           "gemini/test", Usage(input_tokens=10, output_tokens=5), 0.01, {})

    return fake


def test_six_concurrent_runs_each_keep_their_own_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)

    # Six independent loose sessions (each gets its own proofs/<session-id> repo, so
    # there is no shared git state to serialize — the concurrency is real).
    contexts = []
    plan_by_session = {}
    for marker, verdict in _PLAN:
        session = store.create_session(f"prove {marker}")
        run = store.create_run(session["id"], "gemini/test", None, 3)
        plan_by_session[session["id"]] = (marker, verdict)
        ctx = bridge.RunnerContext(
            session_id=session["id"], run_id=run["id"], task=f"prove {marker}",
            config=config, events=Queue(),
        )
        contexts.append((ctx, marker, verdict))

    barrier = threading.Barrier(len(_PLAN))
    monkeypatch.setattr(bridge, "run_events", _dispatching_fake(plan_by_session, barrier))

    errors: list[str] = []
    err_lock = threading.Lock()

    def drive(ctx):
        try:
            bridge.run_lea(ctx)
        except Exception as exc:  # noqa: BLE001 — a driver thread must not die silently
            with err_lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=drive, args=(ctx,)) for ctx, _, _ in contexts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not any(t.is_alive() for t in threads), "a run thread hung (barrier deadlock?)"
    assert not errors, f"driver threads raised under concurrency: {errors}"

    all_markers = {marker for _, marker, _ in contexts}
    proved, errored = [], []
    for ctx, marker, verdict in contexts:
        detail = store.session_detail(ctx.session_id)

        # Exactly one code step, and it carries THIS run's bytes — no other run's.
        assert len(detail["code_steps"]) == 1, f"{marker}: expected one code step"
        code = detail["code_steps"][0]["code"]
        assert marker in code, f"{marker}: its own bytes went missing"
        for other in all_markers - {marker}:
            assert other not in code, f"{marker}'s stored proof leaked {other} — cross-talk"

        status = detail["status"]
        run = store.get_run(ctx.run_id)
        if verdict == "provable":
            assert status == "proved", f"{marker}: provable session derived {status!r}, not 'proved'"
            assert run["status"] == "proved", f"{marker}: run status {run['status']!r}"
            proved.append(marker)
        else:
            # The false run's file errored → the derived session status is the check
            # verdict, 'error' (NOT 'proved'). Its run lifecycle is 'max_turns'.
            assert status == "error", f"{marker}: false session derived {status!r}, not 'error'"
            assert run["status"] == "max_turns", f"{marker}: run status {run['status']!r}"
            errored.append(marker)

    # The headline invariant: exactly the three provable claims are labeled proved.
    assert len(proved) == 3, f"expected 3 proved, got {sorted(proved)}"
    assert len(errored) == 3, f"expected 3 errored, got {sorted(errored)}"


def test_timeline_positions_stay_unique_across_concurrent_runs(tmp_path, monkeypatch):
    """The ordering-key guarantee the derived status rests on. Every code + message
    row across all six sessions must occupy a distinct `timeline.id`; a duplicate
    position (the old per-session seq read-modify-write bug) would let `ORDER BY`
    return the wrong 'latest' step and mislabel a session even with correct verdicts."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)

    contexts = []
    plan_by_session = {}
    for marker, verdict in _PLAN:
        session = store.create_session(f"prove {marker}")
        run = store.create_run(session["id"], "gemini/test", None, 3)
        plan_by_session[session["id"]] = (marker, verdict)
        contexts.append(bridge.RunnerContext(
            session_id=session["id"], run_id=run["id"], task=f"prove {marker}",
            config=config, events=Queue(),
        ))

    barrier = threading.Barrier(len(_PLAN))
    monkeypatch.setattr(bridge, "run_events", _dispatching_fake(plan_by_session, barrier))

    threads = [threading.Thread(target=bridge.run_lea, args=(ctx,)) for ctx in contexts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    with db.connect() as conn:
        ids = [r["id"] for r in conn.execute("select id from timeline").fetchall()]
    assert len(ids) == len(set(ids)), "two timeline rows shared a position under concurrency"
