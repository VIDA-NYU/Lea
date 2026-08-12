"""Live child streaming + per-child stop (E1 / D2).

Before E1 a child ran inside a blocking tool call and its steps were ABSORBED — the UI
saw nothing until it finished. E1 makes the child's own events flow up as
`SubagentProgress` (the coordinator loop `yield from`s them), so a running child is
watchable live, WITHOUT putting the child's steps into the coordinator's model context
(item 18). D2 adds a per-child cooperative stop so one runaway child can be killed
without cancelling the whole coordinator run. These pin:

  * `run_prepared_events` yields a `SubagentProgress` for EACH child event, in order,
    each tagged with the child's `result_id`, and still returns the render string +
    records the typed result (same as the non-streaming path);
  * the non-streaming `run_prepared` driver returns the identical render + records once;
  * `request_child_stop(id)` makes the child's composed stop predicate fire, so the
    child stops cleanly and returns partial findings;
  * the coordinator's own stop CASCADES into a running child (via the run context);
  * the per-child stop flag is cleaned up after the child finishes.

Run:  uv run python -m tests.subagents.test_subagent_streaming
Exits 0 if every check passes, 1 otherwise.
"""

import tempfile
from pathlib import Path

import lea.agent as agent
from lea.config import LeaConfig
from lea.events import (
    AssistantTextDelta, CheckResult, Finished, SubagentProgress, TurnStarted,
)
from lea.providers import Usage
from lea.runctx import run_context
from lea import subagents
from lea.subagents import (
    prepare_spawn, run_prepared, run_prepared_events, request_child_stop, drain_results,
)

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


def _fake_child_events(config, messages, *, namespace=None, session_id=None,
                       working_dir=None, should_stop=None, gate=None, depth=0):
    """A child that emits a few real events then a clean candidate + Finished."""
    cand = Path(working_dir) / "C.lean"
    cand.write_text("theorem c : True := by trivial\n")
    yield TurnStarted(1)
    yield AssistantTextDelta("thinking about it")
    yield CheckResult(str(cand), "ok", None)
    yield Finished("completed", "did it", 1, session_id or "c", config.model,
                   Usage(input_tokens=1, output_tokens=1), 0.0,
                   {"messages": [{"role": "user", "content": "go"}]})


def _stoppable_child(config, messages, *, namespace=None, session_id=None,
                     working_dir=None, should_stop=None, gate=None, depth=0):
    """A child that polls should_stop each 'turn'; stops cleanly when asked, else would
    loop well past a real run. Mirrors the D18 interrupt contract."""
    for turn in range(1, 1000):
        if should_stop is not None and should_stop():
            yield Finished("interrupted", "Run interrupted by the user.", turn,
                           session_id or "c", config.model, Usage(), 0.0, {"messages": []})
            return
        yield TurnStarted(turn)
    yield Finished("completed", "never got here", 999, session_id or "c", config.model,
                   Usage(), 0.0, {"messages": []})


# --- streaming ----------------------------------------------------------------

def test_run_prepared_events_streams_each_child_event(monkeypatch):
    monkeypatch.setattr(agent, "run_events", _fake_child_events)
    with tempfile.TemporaryDirectory() as d:
        wd = str(Path(d).resolve())
        with run_context(depth=0, config=_cfg(), working_dir=wd, run_key="sess"):
            token = subagents.begin_results_scope()
            plan = prepare_spawn({"description": "x", "prompt": "y"})
            gen = run_prepared_events(plan)
            progress = []
            try:
                while True:
                    progress.append(next(gen))
            except StopIteration as stop:
                render = stop.value
            drained = drain_results()
            subagents.end_results_scope(token)

    check("every yielded item is a SubagentProgress",
          all(isinstance(p, SubagentProgress) for p in progress))
    check("progress is tagged with the child's result_id",
          all(p.result_id == plan.result_id for p in progress))
    kinds = [type(p.event).__name__ for p in progress]
    check("the child's events streamed in order",
          kinds == ["TurnStarted", "AssistantTextDelta", "CheckResult", "Finished"])
    check("the generator returns the render string", render.startswith("[subagent:"))
    check("exactly one typed result was recorded", len(drained) == 1)
    check("the recorded result carries the child's verdict", drained[0].check_status == "ok")


def test_run_prepared_driver_matches_the_streaming_path(monkeypatch):
    monkeypatch.setattr(agent, "run_events", _fake_child_events)
    with tempfile.TemporaryDirectory() as d:
        wd = str(Path(d).resolve())
        with run_context(depth=0, config=_cfg(), working_dir=wd, run_key="sess"):
            token = subagents.begin_results_scope()
            plan = prepare_spawn({"description": "x", "prompt": "y"})
            render = run_prepared(plan)          # non-streaming driver
            drained = drain_results()
            subagents.end_results_scope(token)
    check("driver returns a render string", render.startswith("[subagent:"))
    check("driver records exactly one result", len(drained) == 1)
    check("driver result id matches the plan", drained[0].result_id == plan.result_id)


# --- per-child stop (D2) ------------------------------------------------------

def test_request_child_stop_makes_the_child_stop(monkeypatch):
    monkeypatch.setattr(agent, "run_events", _stoppable_child)
    with tempfile.TemporaryDirectory() as d:
        wd = str(Path(d).resolve())
        with run_context(depth=0, config=_cfg(), working_dir=wd, run_key="sess"):
            token = subagents.begin_results_scope()
            plan = prepare_spawn({"description": "x", "prompt": "y"})
            # Ask this specific child to stop BEFORE we start draining it — the flag was
            # registered in prepare_spawn, so the endpoint can set it the moment the
            # child is known.
            found = request_child_stop(plan.result_id)
            gen = run_prepared_events(plan)
            events = []
            try:
                while True:
                    events.append(next(gen))
            except StopIteration:
                pass
            drained = drain_results()
            subagents.end_results_scope(token)

    check("request_child_stop found the live child", found is True)
    inner = [p.event for p in events]
    check("the child stopped almost immediately (a couple of turns at most)", len(inner) <= 3)
    check("the child ended on an interrupted Finished",
          any(isinstance(e, Finished) and e.reason == "interrupted" for e in inner))
    check("the interrupted child still records a typed result", len(drained) == 1)
    check("stopping an unknown child returns False", request_child_stop("nope-123") is False)


def test_coordinator_stop_cascades_into_a_running_child(monkeypatch):
    monkeypatch.setattr(agent, "run_events", _stoppable_child)
    with tempfile.TemporaryDirectory() as d:
        wd = str(Path(d).resolve())
        # The coordinator's own stop is flipped on — a running child must honor it too,
        # so Stop on the coordinator doesn't hang waiting for the child to finish.
        with run_context(depth=0, config=_cfg(), working_dir=wd, run_key="sess",
                         should_stop=lambda: True):
            token = subagents.begin_results_scope()
            plan = prepare_spawn({"description": "x", "prompt": "y"})
            gen = run_prepared_events(plan)
            events = []
            try:
                while True:
                    events.append(next(gen))
            except StopIteration:
                pass
            drain_results()
            subagents.end_results_scope(token)
    inner = [p.event for p in events]
    check("coordinator stop halted the child",
          any(isinstance(e, Finished) and e.reason == "interrupted" for e in inner))


def test_child_stop_flag_is_cleaned_up_after_the_run(monkeypatch):
    monkeypatch.setattr(agent, "run_events", _fake_child_events)
    with tempfile.TemporaryDirectory() as d:
        wd = str(Path(d).resolve())
        with run_context(depth=0, config=_cfg(), working_dir=wd, run_key="sess"):
            token = subagents.begin_results_scope()
            plan = prepare_spawn({"description": "x", "prompt": "y"})
            check("flag registered at prepare time", plan.result_id in subagents._child_stops)
            run_prepared(plan)
            check("flag cleared after the child finishes", plan.result_id not in subagents._child_stops)
            drain_results()
            subagents.end_results_scope(token)


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
    print("subagent live-streaming + stop tests (E1 / D2):")
    for fn in (
        test_run_prepared_events_streams_each_child_event,
        test_run_prepared_driver_matches_the_streaming_path,
        test_request_child_stop_makes_the_child_stop,
        test_coordinator_stop_cascades_into_a_running_child,
        test_child_stop_flag_is_cleaned_up_after_the_run,
    ):
        mp = _MonkeyPatch()
        try:
            fn(mp)
        finally:
            mp.undo()

    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All subagent streaming + stop tests passed (E1 / D2).")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
