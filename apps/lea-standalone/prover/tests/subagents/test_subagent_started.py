"""Spawn-started visibility (D1).

A child sub-agent used to materialize only on `SubagentFinished`, so a running child
was invisible — the list showed finished children as if they were the whole story.
D1 splits the blocking `spawn_subagent` into `prepare_spawn` (validate → id → scratch)
and `run_prepared` (the blocking child run), so the coordinator loop can emit a
`SubagentStarted` event BETWEEN them. These pin that contract:

  * `prepare_spawn` returns a `SpawnPlan` for a valid call (carrying the id the eventual
    `SubagentFinished` reuses), and an error STRING for every refusal — so a refused
    spawn emits no started event and leaves no phantom running child;
  * `run_prepared` runs the child and records exactly the same typed result the old
    monolithic path did (same result_id as the plan);
  * a child that RAISES still records a result (reason 'error') — the D1 invariant that
    keeps a started child from lingering as "running" forever;
  * driven through the real coordinator loop, a spawn yields `SubagentStarted` STRICTLY
    before `SubagentFinished`, both carrying the same `result_id`.

Run:  uv run python -m tests.subagents.test_subagent_started
Exits 0 if every check passes, 1 otherwise.
"""

import tempfile
from pathlib import Path

import lea.agent as agent
from lea.config import LeaConfig
from lea.events import CheckResult, Finished, SubagentStarted, SubagentFinished
from lea.providers import TextDelta, ToolCall, Done, _ToolMeta, Usage
from lea.runctx import run_context
from lea import subagents
from lea.subagents import SpawnPlan, prepare_spawn, run_prepared, drain_results

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


def _fake_child(verdict, finished, *, raises=False):
    """A stubbed child `run_events`. `raises=True` blows up mid-run (to exercise the
    always-record-a-result invariant)."""
    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None, depth=0):
        if raises:
            raise RuntimeError("child exploded")
        if verdict is not None:
            cand = Path(working_dir) / "C.lean"
            cand.write_text("theorem c : True := by trivial\n")
            yield CheckResult(str(cand), verdict, None if verdict == "ok" else "boom")
        if finished:
            yield Finished("completed", "child summary", 1, session_id or "c",
                           config.model, Usage(input_tokens=1, output_tokens=1), 0.0,
                           {"messages": [{"role": "user", "content": "prove it"}]})
    return fake


# --- prepare_spawn: plan vs. refusal -------------------------------------------

def test_prepare_returns_a_plan_for_a_valid_call():
    with tempfile.TemporaryDirectory() as d:
        wd = str(Path(d).resolve())
        with run_context(depth=0, config=_cfg(), working_dir=wd, run_key="sess"):
            plan = prepare_spawn({"description": "find sum lemma", "prompt": "search",
                                  "subagent_type": "generalist"})
        # This one has to run INSIDE the TemporaryDirectory block: the scratch dir
        # lives under `wd`, and leaving the block deletes that whole tree — so
        # asserting it afterwards was checking for a directory Python had just
        # thrown away, and failed no matter what prepare_spawn did.
        check("plan scratch dir was created", plan.candidate_dir.exists())
    # The rest are assertions about the returned plan object, which outlives the
    # temp dir, so they are fine here.
    check("a valid call yields a SpawnPlan", isinstance(plan, SpawnPlan))
    check("plan child depth is parent+1", plan.child_depth == 1)
    check("plan carries a result id", bool(plan.result_id))
    check("plan label prefers the description", plan.description == "find sum lemma")
    check("plan scratch dir is under the run's .lea/tmp", ".lea/tmp" in str(plan.candidate_dir))


def test_prepare_refuses_with_a_string_and_no_plan():
    with tempfile.TemporaryDirectory() as d:
        wd = str(Path(d).resolve())
        # No prompt.
        with run_context(depth=0, config=_cfg(), working_dir=wd, run_key="s"):
            no_prompt = prepare_spawn({"description": "x", "prompt": "  "})
        # Depth cap: a depth-1 child cannot spawn.
        with run_context(depth=1, config=_cfg(), working_dir=wd, run_key="s"):
            too_deep = prepare_spawn({"description": "x", "prompt": "y"})
        # Unknown role.
        with run_context(depth=0, config=_cfg(), working_dir=wd, run_key="s"):
            bad_role = prepare_spawn({"description": "x", "prompt": "y", "subagent_type": "nope"})
        # No live activation (no run_context/config).
        loose = prepare_spawn({"description": "x", "prompt": "y"})
    check("missing prompt refuses with a string", isinstance(no_prompt, str) and "prompt" in no_prompt)
    check("depth cap refuses with a string", isinstance(too_deep, str) and "cannot spawn" in too_deep)
    check("unknown role refuses with a string", isinstance(bad_role, str) and "Error" in bad_role)
    check("no activation refuses with a string", isinstance(loose, str) and "activation" in loose)


# --- run_prepared: records the result, even on a raise -------------------------

def test_run_prepared_records_the_result_with_the_plan_id(monkeypatch):
    monkeypatch.setattr(agent, "run_events", _fake_child("ok", True))
    with tempfile.TemporaryDirectory() as d:
        wd = str(Path(d).resolve())
        with run_context(depth=0, config=_cfg(), working_dir=wd, run_key="sess"):
            token = subagents.begin_results_scope()
            plan = prepare_spawn({"description": "find", "prompt": "search"})
            out = run_prepared(plan)
            drained = drain_results()
            subagents.end_results_scope(token)
    check("run_prepared recorded one result", len(drained) == 1)
    check("recorded result reuses the plan's id", drained[0].result_id == plan.result_id)
    check("the plan id shows in the model-facing render", plan.result_id in out)
    check("the verdict is typed through", drained[0].check_status == "ok")


def test_a_raising_child_still_records_an_error_result(monkeypatch):
    # The D1 invariant: without a recorded result, a started child's running row never
    # resolves. A raise must still produce a SubagentFinished (reason 'error').
    monkeypatch.setattr(agent, "run_events", _fake_child(None, False, raises=True))
    with tempfile.TemporaryDirectory() as d:
        wd = str(Path(d).resolve())
        with run_context(depth=0, config=_cfg(), working_dir=wd, run_key="sess"):
            token = subagents.begin_results_scope()
            plan = prepare_spawn({"description": "x", "prompt": "y"})
            out = run_prepared(plan)
            drained = drain_results()
            subagents.end_results_scope(token)
    check("a raising child STILL records a result", len(drained) == 1)
    check("the error result reuses the plan id", drained[0].result_id == plan.result_id)
    check("the error result has reason 'error'", drained[0].stop_reason == "error")
    check("the error result carries no candidate", drained[0].candidate_path is None)
    check("the tool_result is the explicit failure string", out.startswith("Error: subagent"))


# --- the loop: Started strictly before Finished, same id -----------------------

def _install_coordinator_stream():
    """Coordinator provider: turn 1 emits one spawn_subagent tool call, turn 2 finishes.
    Also answers the terminal outcome-classification stream."""
    tmp = tempfile.TemporaryDirectory()
    state = {"n": 0, "tmp": tmp}

    def fake_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        if "classify the mathematical outcome" in system:
            yield TextDelta("PROVED")
            yield Done(Usage(2, 1), 0.0001)
            return
        state["n"] += 1
        if state["n"] == 1:
            yield TextDelta("Delegating. ")
            yield ToolCall("spawn_subagent",
                           {"description": "prove L1", "prompt": "prove 1+1=2",
                            "subagent_type": "generalist"})
            yield _ToolMeta("call_spawn")
            yield Done(Usage(100, 40), 0.003)
        else:
            yield TextDelta("Collated, done.")
            yield Done(Usage(20, 10), 0.001)

    agent.stream = fake_stream
    agent.load_system_prompt = lambda variant, skills=None, workspace=None, namespace=None: "SYS"
    return state


def test_loop_emits_started_before_finished_same_id():
    real_run_events = agent.run_events          # the coordinator drives THIS...
    saved_stream = agent.stream
    saved_prompt = agent.load_system_prompt
    tmp = tempfile.TemporaryDirectory()
    try:
        state = _install_coordinator_stream()
        # ...while the child's late `from .agent import run_events` resolves to the stub.
        agent.run_events = _fake_child("ok", True)
        wd = str(Path(tmp.name).resolve())
        events = list(real_run_events(
            _cfg(), [{"role": "user", "content": "prove it"}],
            session_id="coord", working_dir=wd,
        ))
    finally:
        agent.run_events = real_run_events
        agent.stream = saved_stream
        agent.load_system_prompt = saved_prompt
        state["tmp"].cleanup()
        tmp.cleanup()

    started = [e for e in events if isinstance(e, SubagentStarted)]
    finished = [e for e in events if isinstance(e, SubagentFinished)]
    check("exactly one SubagentStarted was emitted", len(started) == 1)
    check("exactly one SubagentFinished was emitted", len(finished) == 1)
    if started and finished:
        i_start = events.index(started[0])
        i_finish = events.index(finished[0])
        check("Started is emitted STRICTLY before Finished", i_start < i_finish)
        check("Started and Finished share the result_id",
              started[0].result_id == finished[0].result_id)
        check("Started carries the delegated task label", started[0].description == "prove L1")
        check("Started names the role", started[0].subagent_type == "generalist")


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
    print("subagent spawn-started tests (D1):")
    test_prepare_returns_a_plan_for_a_valid_call()
    test_prepare_refuses_with_a_string_and_no_plan()
    for fn in (test_run_prepared_records_the_result_with_the_plan_id,
               test_a_raising_child_still_records_an_error_result):
        mp = _MonkeyPatch()
        try:
            fn(mp)
        finally:
            mp.undo()
    test_loop_emits_started_before_finished_same_id()

    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All subagent spawn-started D1 tests passed.")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
