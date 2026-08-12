"""Concurrent children (E2) — real thread overlap, a concurrency cap, failure isolation.

E1 made one child stream live; E2 runs a turn's MANY children at once, each on its own
thread with an isolated context copy, merging their events. These pin:

  * `run_children_concurrently` truly OVERLAPS its children — two children that must meet
    at a `threading.Barrier` both pass it (they'd deadlock if run serially), and their
    progress interleaves up through the one generator;
  * `max_children` is a hard cap — with one slot the same two children CANNOT both reach
    the barrier, so they run one-at-a-time (the laptop-safety guard, B1/B3);
  * a child that raises is isolated into its own error render — its sibling still
    completes cleanly;
  * every child still records a typed result (for the coordinator's SubagentFinished).

Run:  uv run python -m tests.subagents.test_subagent_parallel
Exits 0 if every check passes, 1 otherwise.
"""

import dataclasses
import tempfile
import threading
import time
from pathlib import Path

import lea.agent as agent
from lea.config import LeaConfig
from lea.events import (
    CheckResult, Finished, SubagentProgress, SubagentStarted, SubagentFinished,
    ToolResulted, TurnStarted,
)
from lea.providers import TextDelta, ToolCall, Done, _ToolMeta, Usage
from lea.runctx import run_context
from lea import subagents
from lea.subagents import prepare_spawn, run_children_concurrently, drain_results

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def _cfg(**over):
    base = dict(model="m", max_turns=None,
                tools=["read_file", "write_file", "lean_check", "spawn_subagent"])
    base.update(over)
    return LeaConfig(**base)


def _barrier_child(barrier: threading.Barrier):
    """A child that must RENDEZVOUS with a sibling at `barrier` to make progress. If both
    children run concurrently they meet and pass (→ a clean 'ok' candidate); if they are
    serialized the wait times out and the child finishes 'SOLO' with no candidate. So the
    verdict encodes whether real overlap happened."""
    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None, depth=0):
        overlapped = True
        try:
            barrier.wait(timeout=2.0)
        except threading.BrokenBarrierError:
            overlapped = False
        if overlapped:
            cand = Path(working_dir) / "C.lean"
            cand.write_text("theorem c : True := by trivial\n")
            yield CheckResult(str(cand), "ok", None)
            yield Finished("completed", "overlapped", 1, session_id or "c", config.model,
                           Usage(1, 1), 0.0, {"messages": []})
        else:
            yield Finished("completed", "SOLO", 1, session_id or "c", config.model,
                           Usage(1, 1), 0.0, {"messages": []})
    return fake


def _drive(gen):
    prog = []
    try:
        while True:
            prog.append(next(gen))
    except StopIteration as stop:
        return prog, stop.value


def test_children_truly_overlap(monkeypatch):
    barrier = threading.Barrier(2)
    monkeypatch.setattr(agent, "run_events", _barrier_child(barrier))
    with tempfile.TemporaryDirectory() as d:
        with run_context(depth=0, config=_cfg(), working_dir=str(Path(d).resolve()), run_key="s"):
            token = subagents.begin_results_scope()
            plans = [prepare_spawn({"description": f"c{i}", "prompt": "go"}) for i in range(2)]
            prog, renders = _drive(run_children_concurrently(plans, max_children=2))
            drained = drain_results()
            subagents.end_results_scope(token)

    progress = [p for p in prog if isinstance(p, SubagentProgress)]
    finished = [p for p in prog if isinstance(p, SubagentFinished)]
    check("both children returned a render", len(renders) == 2)
    check("progress streamed from BOTH children",
          {p.result_id for p in progress} == set(renders.keys()) and len(progress) >= 2)
    # Each child's result is now YIELDED as it finishes, so the post-batch drain finds
    # nothing left — that emptiness IS the fix: a child no longer waits for its slowest
    # sibling before being reported done.
    check("both children reported a typed result", len(finished) == 2)
    check("the post-batch drain is empty (results already emitted)", len(drained) == 0)
    check("both OVERLAPPED (passed the barrier → clean candidate)",
          all(f.check_status == "ok" for f in finished))


def test_a_fast_child_is_reported_before_a_slow_sibling_finishes(monkeypatch):
    """The stuck-spinner bug, pinned by timing rather than by counting.

    Every child's result used to be drained only after `run_children_concurrently`
    returned — i.e. after the SLOWEST sibling. So a child that finished early stayed
    `status='running'` in the adapter for as long as its slowest peer took, and its
    session view showed a live "Checking with Lean…" spinner over a finished
    transcript. Observed on an 8-child batch: all eight retired within 30ms of each
    other, ~51s after spawn.

    Here child A returns immediately and child B blocks until we release it. The fast
    child's SubagentFinished MUST arrive while the slow one is still running — if it
    only arrives at the end, we are back to batch-granularity reporting.
    """
    release = threading.Event()

    def two_speeds(config, messages, *, namespace=None, session_id=None, working_dir=None,
                   should_stop=None, gate=None, depth=0):
        # `run_key` is the child's own id; the slow one is whichever we named 'slow'.
        if "slow" in (session_id or ""):
            release.wait(timeout=5)
        yield Finished("completed", "done", 1, session_id or "c", config.model,
                       Usage(1, 1), 0.0, {"messages": []})

    monkeypatch.setattr(agent, "run_events", two_speeds)
    with tempfile.TemporaryDirectory() as d:
        with run_context(depth=0, config=_cfg(), working_dir=str(Path(d).resolve()), run_key="s"):
            token = subagents.begin_results_scope()
            fast = prepare_spawn({"description": "fast", "prompt": "go"})
            slow = prepare_spawn({"description": "slow", "prompt": "go"})
            # Force the ids so the stub can tell them apart.
            fast = dataclasses.replace(fast, result_id="fast-1")
            slow = dataclasses.replace(slow, result_id="slow-1")
            _child_stops_seed(fast, slow)

            gen = run_children_concurrently([fast, slow], max_children=2)
            early: list = []
            # Pull events until the FAST child reports finished — with the slow one
            # still blocked. A timeout here means the fix regressed.
            deadline = time.monotonic() + 5
            got_fast = False
            while time.monotonic() < deadline and not got_fast:
                try:
                    ev = next(gen)
                except StopIteration:
                    break
                early.append(ev)
                if isinstance(ev, SubagentFinished) and ev.result_id == "fast-1":
                    got_fast = True

            check("the fast child was reported while the slow one was still running", got_fast)
            check("the slow child had NOT been reported yet",
                  not any(isinstance(e, SubagentFinished) and e.result_id == "slow-1"
                          for e in early))
            release.set()
            try:
                while True:
                    next(gen)
            except StopIteration:
                pass
            subagents.end_results_scope(token)


def _child_stops_seed(*plans):
    """`prepare_spawn` registered a stop flag under the ORIGINAL id; re-register under
    the forced ids so `_compose_child_stop` finds one (it tolerates a missing flag, but
    seeding keeps the test exercising the real path)."""
    for plan in plans:
        subagents._child_stops.setdefault(plan.result_id, threading.Event())


def test_max_children_caps_concurrency(monkeypatch):
    # Same two barrier children, but ONE slot: they can't both reach the barrier, so it
    # breaks and each runs SOLO. Proves the cap actually serializes.
    barrier = threading.Barrier(2)
    monkeypatch.setattr(agent, "run_events", _barrier_child(barrier))
    with tempfile.TemporaryDirectory() as d:
        with run_context(depth=0, config=_cfg(), working_dir=str(Path(d).resolve()), run_key="s"):
            token = subagents.begin_results_scope()
            plans = [prepare_spawn({"description": f"c{i}", "prompt": "go"}) for i in range(2)]
            _prog, renders = _drive(run_children_concurrently(plans, max_children=1))
            drained = drain_results()
            subagents.end_results_scope(token)
    check("both children still returned a render under the cap", len(renders) == 2)
    check("neither overlapped (barrier broke → no candidate)",
          all(r.check_status is None for r in drained))
    check("both summaries are SOLO", all(r.summary == "SOLO" for r in drained))


def test_a_failing_child_is_isolated(monkeypatch):
    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None, depth=0):
        # the FIRST child (its scratch dir ends in '-0'... but ids are random) — decide by
        # a marker in the task instead: the prompt says 'BOOM' → raise.
        task = messages[0]["content"] if messages else ""
        if "BOOM" in task:
            raise RuntimeError("kaboom")
        cand = Path(working_dir) / "C.lean"
        cand.write_text("theorem c : True := by trivial\n")
        yield CheckResult(str(cand), "ok", None)
        yield Finished("completed", "fine", 1, session_id or "c", config.model,
                       Usage(1, 1), 0.0, {"messages": []})

    monkeypatch.setattr(agent, "run_events", fake)
    with tempfile.TemporaryDirectory() as d:
        with run_context(depth=0, config=_cfg(), working_dir=str(Path(d).resolve()), run_key="s"):
            token = subagents.begin_results_scope()
            bad = prepare_spawn({"description": "bad", "prompt": "BOOM please"})
            good = prepare_spawn({"description": "good", "prompt": "prove it"})
            prog, renders = _drive(run_children_concurrently([bad, good], max_children=2))
            # Results are now emitted per child as it finishes, so they arrive on the
            # event stream rather than sitting in the collector until the batch ends.
            drained = [p for p in prog if isinstance(p, SubagentFinished)]
            subagents.end_results_scope(token)
    by_id = {r.result_id: r for r in drained}
    check("both children produced a result (the failure didn't kill the sibling)", len(drained) == 2)
    check("the failing child rendered an error", "Error" in renders[bad.result_id])
    check("the failing child's result reason is 'error'", by_id[bad.result_id].stop_reason == "error")
    check("the healthy sibling still completed cleanly", by_id[good.result_id].check_status == "ok")


def test_loop_runs_two_spawns_in_one_turn_concurrently():
    # The REAL coordinator loop: ONE turn issues TWO spawn_subagent calls, and they must
    # run concurrently — the barrier children only pass if they truly overlap — with the
    # loop emitting two Started + two Finished and terminating cleanly.
    #
    # Scope note: this test is about SPAWN OVERLAP, not about what outcome the run
    # reaches. It used to assert `Finished.reason == "completed"`, which is a claim about
    # the coordinator's proof artifact and was always false here: this fake coordinator
    # delegates and then stops without writing a proof of its own, and a child's candidate
    # lives in the child's sandbox by design. So the run correctly ends as a chat turn.
    # Which outcome a run reaches is settled by the tests that own that question.
    barrier = threading.Barrier(2)
    real_run_events = agent.run_events
    saved_stream, saved_prompt = agent.stream, agent.load_system_prompt
    tmp = tempfile.TemporaryDirectory()
    state = {"n": 0}

    def coordinator_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        if "classify the mathematical outcome" in system:
            yield TextDelta("PROVED")
            yield Done(Usage(2, 1), 0.0001)
            return
        state["n"] += 1
        if state["n"] == 1:
            yield TextDelta("Delegating two lemmas. ")
            yield ToolCall("spawn_subagent", {"description": "L1", "prompt": "prove L1"})
            yield _ToolMeta("call_a")
            yield ToolCall("spawn_subagent", {"description": "L2", "prompt": "prove L2"})
            yield _ToolMeta("call_b")
            yield Done(Usage(100, 40), 0.003)
        else:
            yield TextDelta("Both done.")
            yield Done(Usage(20, 10), 0.001)

    try:
        agent.stream = coordinator_stream
        agent.load_system_prompt = lambda variant, skills=None, workspace=None, namespace=None: "SYS"
        agent.run_events = _barrier_child(barrier)  # children resolve THIS (late import)
        events = list(real_run_events(
            _cfg(), [{"role": "user", "content": "prove it"}],
            session_id="coord", working_dir=str(Path(tmp.name).resolve()),
        ))
    finally:
        agent.run_events = real_run_events
        agent.stream, agent.load_system_prompt = saved_stream, saved_prompt
        tmp.cleanup()

    started = [e for e in events if isinstance(e, SubagentStarted)]
    finished = [e for e in events if isinstance(e, SubagentFinished)]
    check("two children were announced (Started)", len(started) == 2)
    check("two children finished", len(finished) == 2)
    check("both spawns OVERLAPPED in the one turn (clean candidates)",
          all(f.check_status == "ok" for f in finished))
    # Both spawns belong to the SAME turn — the property this test is named for, and
    # what makes the overlap check above meaningful instead of incidental: children
    # issued in different turns could never have met at the barrier.
    second_turn_at = next((i for i, e in enumerate(events)
                           if isinstance(e, TurnStarted) and e.turn == 2), len(events))
    check("both spawns were issued in the SAME turn",
          all(i < second_turn_at for i, e in enumerate(events) if isinstance(e, SubagentStarted)))
    # The loop TERMINATED — both worker threads joined, nothing hung, nothing escaped.
    # A raw `Finished` here is the coordinator's: a child's own events are wrapped as
    # SubagentProgress, so this counts exactly one terminal event, whatever its reason.
    check("the loop terminated cleanly (exactly one terminal event)",
          len([e for e in events if isinstance(e, Finished)]) == 1)


def test_loop_parallelizes_two_readonly_tools():
    # E3: a turn issuing two INDEPENDENT read_file calls runs them through the concurrent
    # read-only path — both results must come back correct and IN ORDER (the provider
    # matches tool_results to calls). Correctness through the parallel branch; the overlap
    # mechanism itself is proven by the barrier test above.
    saved_stream, saved_prompt = agent.stream, agent.load_system_prompt
    tmp = tempfile.TemporaryDirectory()
    fa = Path(tmp.name) / "a.txt"
    fb = Path(tmp.name) / "b.txt"
    fa.write_text("ALPHA-CONTENT")
    fb.write_text("BETA-CONTENT")
    state = {"n": 0}

    def coordinator_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        if "classify the mathematical outcome" in system:
            yield TextDelta("PROVED")
            yield Done(Usage(2, 1), 0.0001)
            return
        state["n"] += 1
        if state["n"] == 1:
            yield ToolCall("read_file", {"path": str(fa)})
            yield _ToolMeta("ra")
            yield ToolCall("read_file", {"path": str(fb)})
            yield _ToolMeta("rb")
            yield Done(Usage(100, 40), 0.003)
        else:
            yield TextDelta("Read both.")
            yield Done(Usage(20, 10), 0.001)

    try:
        agent.stream = coordinator_stream
        agent.load_system_prompt = lambda variant, skills=None, workspace=None, namespace=None: "SYS"
        events = list(agent.run_events(
            _cfg(tools=["read_file"]), [{"role": "user", "content": "read them"}],
            session_id="c", working_dir=str(Path(tmp.name).resolve()),
        ))
    finally:
        agent.stream, agent.load_system_prompt = saved_stream, saved_prompt
        tmp.cleanup()

    reads = [e for e in events if isinstance(e, ToolResulted) and e.name == "read_file"]
    check("both read_file calls produced a result", len(reads) == 2)
    check("first result is file a (order preserved)", "ALPHA-CONTENT" in reads[0].content)
    check("second result is file b (order preserved)", "BETA-CONTENT" in reads[1].content)


# --- standalone runner ---------------------------------------------------------

class _MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


def main():
    print("subagent concurrency tests (E2):")
    for fn in (test_children_truly_overlap, test_max_children_caps_concurrency,
               test_a_failing_child_is_isolated,
               test_a_fast_child_is_reported_before_a_slow_sibling_finishes):
        mp = _MonkeyPatch()
        try:
            fn(mp)
        finally:
            mp.undo()
    test_loop_runs_two_spawns_in_one_turn_concurrently()
    test_loop_parallelizes_two_readonly_tools()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All subagent concurrency tests passed (E2).")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
