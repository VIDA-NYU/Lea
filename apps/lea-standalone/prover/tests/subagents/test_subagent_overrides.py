"""Per-role sub-agent overrides + per-run cost cap (D6).

The Sub-agents page lets a user retune a built-in role (model / max_turns / max_cost /
system_prompt / tools) WITHOUT mutating the vendored YAML. The adapter carries the edits
on `LeaConfig.subagent_overrides`; `_child_config` merges the matching entry over the
role's defaults at spawn, and the loop honours `max_cost_usd` as a per-run spend ceiling.

Run:  uv run python -m tests.subagents.test_subagent_overrides
Exits 0 if every check passes, 1 otherwise.
"""

import tempfile
from pathlib import Path

import lea.agent as agent
from lea.config import LeaConfig
from lea.profiles import AgentProfile
from lea.providers import TextDelta, ToolCall, Done, _ToolMeta, Usage
from lea.events import Finished
from lea.subagents import _child_config

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def _parent(**over):
    base = dict(model="parent/model", max_turns=None,
                tools=["read_file", "write_file", "search_mathlib", "lean_check", "spawn_subagent"])
    base.update(over)
    return LeaConfig(**base)


_PC = AgentProfile(name="proof-candidate", system_prompt="DEFAULT PROMPT", model=None,
                   tools=["read_file", "lean_check"], max_turns=150)
_PS = AgentProfile(name="premise-search", system_prompt="SCOUT", model=None,
                   tools=["read_file", "search_mathlib"], max_turns=12)


# --- _child_config merge -------------------------------------------------------

def test_override_merges_over_profile():
    parent = _parent(subagent_overrides={"proof-candidate": {
        "model": "override/model", "max_turns": 42, "max_cost": 0.5, "system_prompt": "CUSTOM",
    }})
    c = _child_config(parent, _PC)
    check("override model wins", c.model == "override/model")
    check("override max_turns wins", c.max_turns == 42)
    check("override max_cost → max_cost_usd", c.max_cost_usd == 0.5)
    check("override system_prompt → prompt head", c.system_prompt_head == "CUSTOM")
    check("profile tools kept (⊆ parent)", "read_file" in c.tools and "lean_check" in c.tools)


def test_no_override_uses_profile_defaults():
    c = _child_config(_parent(), _PS)
    check("no model override → inherit coordinator", c.model == "parent/model")
    check("profile max_turns used", c.max_turns == 12)
    check("no max_cost → uncapped", c.max_cost_usd is None)
    check("profile system_prompt used", c.system_prompt_head == "SCOUT")


def test_override_only_applies_to_the_named_role():
    # an override for proof-candidate must not leak onto premise-search
    parent = _parent(subagent_overrides={"proof-candidate": {"model": "override/model"}})
    c = _child_config(parent, _PS)
    check("other role unaffected by a different role's override", c.model == "parent/model")


def test_override_tools_still_tighten_against_parent():
    # a role can be given a tool subset, but never one the parent lacks (item 21 holds)
    parent = _parent(tools=["read_file", "lean_check", "spawn_subagent"])  # no write_file
    parent = _parent(subagent_overrides={"proof-candidate": {"tools": ["read_file", "write_file"]}},
                     tools=["read_file", "lean_check", "spawn_subagent"])
    c = _child_config(parent, _PC)
    check("override tool the parent has is kept", "read_file" in c.tools)
    check("override tool the parent LACKS is tightened away", "write_file" not in c.tools)


def test_malformed_override_does_not_crash():
    parent = _parent(subagent_overrides={"proof-candidate": None})  # garbage entry
    c = _child_config(parent, _PC)
    check("a garbage override falls back to profile defaults", c.max_turns == 150)


# --- max_cost loop enforcement -------------------------------------------------

def test_max_cost_stops_the_run(monkeypatch):
    # A run with a low max_cost_usd: turn 1 spends over the cap, so turn 2 ends via
    # summarize-on-cap instead of continuing. Mirrors the max_turns path.
    tmp = tempfile.TemporaryDirectory()
    proof = str(Path(tmp.name) / "P.lean")
    state = {"n": 0}

    def fake_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        if "classify the mathematical outcome" in system:
            yield TextDelta("PROVED")
            yield Done(Usage(1, 1), 0.0)
            return
        state["n"] += 1
        if state["n"] == 1:
            yield ToolCall("write_file", {"path": proof, "content": "theorem p : True := by trivial\n"})
            yield _ToolMeta("w1")
            yield Done(Usage(100, 40), 1.0)   # cost 1.0 ≫ the 0.25 cap
        else:
            # the summarize-on-cap call (and any further turn) hands back a short summary
            yield TextDelta("Best attempt so far: p via trivial.")
            yield Done(Usage(5, 3), 0.01)

    monkeypatch.setattr(agent, "stream", fake_stream)
    monkeypatch.setattr(agent, "load_system_prompt", lambda variant, skills=None, workspace=None, namespace=None: "SYS")
    agent._tools.lean_check = lambda path: "OK — no errors, no warnings."
    cfg = LeaConfig(model="m", max_turns=None, max_cost_usd=0.25, tools=["write_file", "lean_check"])
    try:
        events = list(agent.run_events(cfg, [{"role": "user", "content": "prove p"}],
                                       session_id="s", working_dir=tmp.name))
    finally:
        tmp.cleanup()

    finished = [e for e in events if isinstance(e, Finished)]
    check("the run finished", len(finished) == 1)
    check("it stopped after exactly one real turn (cost cap)", finished[0].turns == 1)
    check("the cost that tripped the cap is reported", finished[0].cost >= 1.0)


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
    print("subagent override + cost-cap tests (D6):")
    test_override_merges_over_profile()
    test_no_override_uses_profile_defaults()
    test_override_only_applies_to_the_named_role()
    test_override_tools_still_tighten_against_parent()
    test_malformed_override_does_not_crash()
    mp = _MonkeyPatch()
    try:
        test_max_cost_stops_the_run(mp)
    finally:
        mp.undo()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All subagent override + cost-cap tests passed (D6).")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
