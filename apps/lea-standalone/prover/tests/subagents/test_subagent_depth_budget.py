"""Recursion guards + child wiring for `spawn_subagent` (v2.3 item 18).

The child activation is *stubbed* (a fake `run_events` that writes a candidate file
and yields a scripted CheckResult + Finished) so this is deterministic and needs no
model or Lean. It pins the parts that are pure orchestration:

  * the depth guard refuses a spawn at depth >= max_depth (a child cannot spawn);
  * a depth-0 coordinator DOES spawn, and the child is built correctly — its own
    toolset scoped (tools=None → no spawn_subagent), MCP dropped, turns bounded,
    depth incremented, working_dir pointed at an isolated .lea/tmp scratch tree;
  * the toolset exclusion guard (spawn_subagent is opt-in, absent from tools=None);
  * a distilled result comes back naming the candidate + its verdict;
  * malformed / out-of-context calls refuse cleanly instead of crashing.

Run:  uv run python -m tests.subagents.test_subagent_depth_budget
Exits 0 if every check passes, 1 otherwise.
"""

import tempfile
from pathlib import Path

import lea.agent as agent
from lea import subagents
from lea.config import LeaConfig
from lea.events import CheckResult, Finished
from lea.providers import Usage
from lea.registry import build_toolset
from lea.runctx import run_context
from lea.subagents import (
    DEFAULT_CHILD_MAX_TURNS,
    DEFAULT_MAX_DEPTH,
    _bounded_child_turns,
    spawn_subagent,
)

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def _cfg(**over) -> LeaConfig:
    base = dict(model="gemini/test", max_turns=None, mcp_servers={"srv": {"x": 1}})
    base.update(over)
    return LeaConfig(**base)


def _fake_run_events(verdict: str, calls: list):
    """A stand-in `run_events`: records the kwargs it was driven with, writes a
    candidate file into the child's working_dir, and yields a scripted verdict."""

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None, depth=0):
        calls.append({
            "config": config, "messages": messages, "session_id": session_id,
            "working_dir": working_dir, "depth": depth,
        })
        cand = Path(working_dir) / "Candidate.lean"
        cand.write_text("theorem c : True := by trivial\n")
        yield CheckResult(str(cand), verdict, None if verdict == "ok" else "type mismatch")
        yield Finished("completed", "tried a candidate", 1, session_id or "child",
                       config.model, Usage(input_tokens=1, output_tokens=1), 0.0, {})

    return fake


def test_depth_guard_blocks_a_child_from_spawning(monkeypatch):
    calls: list = []
    monkeypatch.setattr(agent, "run_events", _fake_run_events("ok", calls))
    # A subagent runs at depth == DEFAULT_MAX_DEPTH; from there a spawn must refuse.
    with run_context(depth=DEFAULT_MAX_DEPTH, config=_cfg(), working_dir="/tmp"):
        out = spawn_subagent({"description": "d", "prompt": "go deeper"})
    check("spawn at the depth cap is refused", out.startswith("Error:"))
    check("the refusal names the depth cap", str(DEFAULT_MAX_DEPTH) in out)
    check("no child activation was started when refused", calls == [])


def test_coordinator_at_depth_0_spawns_a_child(monkeypatch):
    calls: list = []
    monkeypatch.setattr(agent, "run_events", _fake_run_events("ok", calls))
    with tempfile.TemporaryDirectory() as d:
        wd = str(Path(d).resolve())
        with run_context(depth=0, config=_cfg(max_turns=30), working_dir=wd, run_key="sess1"):
            out = spawn_subagent({"description": "find lemmas", "prompt": "search Mathlib"})

        check("a depth-0 coordinator started exactly one child", len(calls) == 1)
        c = calls[0] if calls else {}
        check("the child runs at depth 1", c.get("depth") == 1)
        # Guard 2, at the config level: the child's toolset is the generalist default
        # tightened to the parent's (item 21) — the built-ins, never spawn_subagent.
        child_tools = c.get("config").tools if c.get("config") else None
        check("the child's toolset excludes spawn_subagent", "spawn_subagent" not in (child_tools or []))
        check("the child's toolset is the built-in default",
              set(child_tools or []) == {"read_file", "write_file", "edit_file",
                                         "lean_check", "bash", "search_mathlib",
                                         "suggest_imports"})
        check("the child drops MCP servers", c.get("config").mcp_servers == {})
        # Parent cap 30 is above the runaway ceiling, so the child is clamped to it.
        check("the child's turns are bounded to the ceiling",
              c.get("config").max_turns == DEFAULT_CHILD_MAX_TURNS)
        # The child writes into an isolated scratch tree under the parent's wd.
        child_wd = c.get("working_dir", "")
        check("the child works in an isolated .lea/tmp scratch dir",
              "/.lea/tmp/sess1/" in child_wd.replace("\\", "/"))
        check("the scratch dir is inside the parent's working_dir", child_wd.startswith(wd))
        check("the scratch dir was actually created", Path(child_wd).is_dir())
        # The distilled result names the candidate and its verdict, relative to wd.
        check("result reports the candidate", "candidate: .lea/tmp/sess1/" in out)
        check("result reports the lean_check verdict ok", "lean_check: ok" in out)
        check("result carries the child's summary", "tried a candidate" in out)


def test_error_verdict_is_surfaced_with_detail(monkeypatch):
    calls: list = []
    monkeypatch.setattr(agent, "run_events", _fake_run_events("error", calls))
    with tempfile.TemporaryDirectory() as d:
        with run_context(depth=0, config=_cfg(), working_dir=str(Path(d).resolve()), run_key="s"):
            out = spawn_subagent({"description": "try", "prompt": "attempt a proof"})
    check("an errored candidate surfaces the error verdict", "lean_check: error" in out)
    check("the error detail is included", "type mismatch" in out)


def test_child_default_toolset_excludes_spawn_subagent():
    # Guard 2, at the registry level: build_toolset(None) — what a child gets — never
    # contains spawn_subagent; an explicit selection may.
    default_names = [s["name"] for s in build_toolset(None)[0]]
    check("spawn_subagent absent from the default toolset", "spawn_subagent" not in default_names)
    coord_names = [s["name"] for s in build_toolset(["lean_check", "spawn_subagent"])[0]]
    check("spawn_subagent present when named explicitly", "spawn_subagent" in coord_names)


def test_spawn_refuses_without_config_or_prompt(monkeypatch):
    calls: list = []
    monkeypatch.setattr(agent, "run_events", _fake_run_events("ok", calls))
    # Outside any activation (no config in context) → clean refusal, not a crash.
    out = spawn_subagent({"description": "d", "prompt": "go"})
    check("spawn outside an activation refuses cleanly", out.startswith("Error:") and not calls)
    # Empty prompt → refusal.
    with run_context(depth=0, config=_cfg(), working_dir="/tmp"):
        out2 = spawn_subagent({"description": "d", "prompt": "   "})
    check("an empty prompt is refused", out2.startswith("Error:") and not calls)


def test_bounded_child_turns():
    check("unlimited parent → default child cap", _bounded_child_turns(None) == DEFAULT_CHILD_MAX_TURNS)
    check("a large parent cap is clamped to the ceiling",
          _bounded_child_turns(1000) == DEFAULT_CHILD_MAX_TURNS)
    check("a small parent cap is inherited", _bounded_child_turns(5) == 5)
    check("a zero/negative parent cap floors at 1", _bounded_child_turns(0) == 1)


# --- a tiny monkeypatch shim so this runs standalone (no pytest) ----------------
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
    print("spawn_subagent depth/budget/wiring tests (v2.3 item 18):")
    for fn in (
        test_depth_guard_blocks_a_child_from_spawning,
        test_coordinator_at_depth_0_spawns_a_child,
        test_error_verdict_is_surfaced_with_detail,
        test_spawn_refuses_without_config_or_prompt,
    ):
        mp = _MonkeyPatch()
        try:
            fn(mp)
        finally:
            mp.undo()
    test_child_default_toolset_excludes_spawn_subagent()
    test_bounded_child_turns()

    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All spawn_subagent item-18 tests passed.")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
