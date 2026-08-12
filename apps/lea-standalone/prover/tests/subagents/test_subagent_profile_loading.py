"""Declarative agent profiles — loading, validation, and wiring (v2.3 item 19).

A role is one YAML file in ``lea/agents/<name>.yaml``: config keys + an explicit
``system_prompt`` block. These pin:

  * the loader parses a real role file (scoped tools, cap, prompt head) and lists
    the roles shipped in ``lea/agents/``;
  * validation refuses the ways a profile can be *silently* wrong — an unknown key
    (a dropped ``tools:`` would hand a child the full toolset), a missing prompt, a
    malformed ``tools``/``max_turns``, a filename/``name`` mismatch, an unknown role;
  * ``_child_config`` maps a profile onto the child (``tools`` scopes via
    ``build_toolset``, an omitted ``model`` inherits, the prompt becomes the role
    head), and the no-profile generalist keeps item 18's behavior;
  * ``spawn_subagent(subagent_type=...)`` resolves a role and refuses an unknown one
    rather than downgrading to a full-toolset generalist.

Run:  uv run python -m tests.subagents.test_subagent_profile_loading
Exits 0 if every check passes, 1 otherwise.
"""

import tempfile
from pathlib import Path

import lea.agent as agent
from lea.config import LeaConfig
from lea.errors import ToolError
from lea.events import CheckResult, Finished
from lea.profiles import (
    AgentProfile,
    available_profiles,
    load_profile,
    parse_profile,
)
from lea.providers import Usage
from lea.registry import build_toolset
from lea.runctx import run_context
from lea.subagents import DEFAULT_CHILD_MAX_TURNS, _child_config, spawn_subagent

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def _raises_toolerror(fn) -> bool:
    try:
        fn()
        return False
    except ToolError:
        return True


def _cfg(**over) -> LeaConfig:
    base = dict(model="gemini/parent", max_turns=None, mcp_servers={"s": {}})
    base.update(over)
    return LeaConfig(**base)


def _fake_run_events(calls: list):
    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None, depth=0):
        calls.append({"config": config, "depth": depth, "working_dir": working_dir})
        cand = Path(working_dir) / "C.lean"
        cand.write_text("theorem c : True := by trivial\n")
        yield CheckResult(str(cand), "ok", None)
        yield Finished("completed", "done", 1, session_id or "c", config.model,
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})
    return fake


# --- loading a shipped role ----------------------------------------------------

def test_ships_the_starter_roles():
    roles = available_profiles()
    check("premise-search ships", "premise-search" in roles)
    check("proof-candidate ships", "proof-candidate" in roles)


def test_load_premise_search_is_read_only():
    p = load_profile("premise-search")
    check("premise-search omits model → inherits", p.model is None)
    check("premise-search is scoped read-only", p.tools == ["read_file", "search_mathlib"])
    check("premise-search caps turns", p.max_turns == 12)
    check("premise-search carries a prompt head", bool(p.system_prompt.strip()))
    # The scoped list resolves through the real toolset builder to exactly those tools
    # — no write/lean_check leaks in, by construction.
    resolved = [s["name"] for s in build_toolset(p.tools)[0]]
    check("read-only role cannot write or check", resolved == ["read_file", "search_mathlib"])


# --- validation refuses silent misconfiguration --------------------------------

def test_validation_refusals():
    check("unknown role errors with a hint", _raises_toolerror(lambda: load_profile("does-not-exist")))
    check("unknown key is rejected",
          _raises_toolerror(lambda: parse_profile("x", {"system_prompt": "p", "toolz": []})))
    check("missing system_prompt is rejected",
          _raises_toolerror(lambda: parse_profile("x", {"tools": []})))
    check("empty system_prompt is rejected",
          _raises_toolerror(lambda: parse_profile("x", {"system_prompt": "   "})))
    check("non-list tools is rejected",
          _raises_toolerror(lambda: parse_profile("x", {"system_prompt": "p", "tools": "read_file"})))
    check("non-string tool entry is rejected",
          _raises_toolerror(lambda: parse_profile("x", {"system_prompt": "p", "tools": [1]})))
    check("zero/negative max_turns is rejected",
          _raises_toolerror(lambda: parse_profile("x", {"system_prompt": "p", "max_turns": 0})))
    check("name mismatch with filename is rejected",
          _raises_toolerror(lambda: parse_profile("real", {"name": "other", "system_prompt": "p"})))
    # A minimal valid profile (only the required prompt) parses; everything else None.
    ok = parse_profile("min", {"system_prompt": "hello"})
    check("a prompt-only profile is valid", ok.system_prompt == "hello" and ok.tools is None)


# --- mapping a profile onto the child config -----------------------------------

def test_child_config_from_profile():
    parent = _cfg(model="gemini/parent", max_turns=50)
    prof = AgentProfile(name="r", system_prompt="ROLE HEAD", model=None,
                        tools=["read_file"], max_turns=8)
    child = _child_config(parent, prof)
    check("an omitted profile model inherits the parent's", child.model == "gemini/parent")
    check("the profile's scoped tools are applied", child.tools == ["read_file"])
    check("the profile's explicit cap is honored", child.max_turns == 8)
    check("the profile prompt becomes the child's role head", child.system_prompt_head == "ROLE HEAD")
    check("the child drops MCP", child.mcp_servers == {})

    # An explicit profile model overrides; a profile with no cap clamps the unlimited
    # parent to the runaway ceiling.
    prof2 = AgentProfile(name="r2", system_prompt="H", model="gemini/mini", tools=None, max_turns=None)
    child2 = _child_config(_cfg(max_turns=None), prof2)
    check("an explicit profile model overrides", child2.model == "gemini/mini")
    check("an uncapped profile clamps the unlimited parent", child2.max_turns == DEFAULT_CHILD_MAX_TURNS)


def test_generalist_has_no_profile_and_keeps_item18_behavior():
    child = _child_config(_cfg(max_turns=30), None)
    # item 21: the generalist toolset is now the built-in default tightened to the
    # parent (parent here has tools=None → all built-ins), never None, never spawn.
    check("generalist toolset excludes spawn_subagent", "spawn_subagent" not in child.tools)
    check("generalist toolset is the built-in default",
          set(child.tools) == {"read_file", "write_file", "edit_file",
                               "lean_check", "bash", "search_mathlib",
                               "suggest_imports"})
    check("generalist has no role head", child.system_prompt_head is None)
    check("generalist turns are bounded", child.max_turns == DEFAULT_CHILD_MAX_TURNS)


# --- spawn_subagent resolves the role ------------------------------------------

def test_spawn_with_a_role_uses_its_profile(monkeypatch):
    calls: list = []
    monkeypatch.setattr(agent, "run_events", _fake_run_events(calls))
    with tempfile.TemporaryDirectory() as d:
        with run_context(depth=0, config=_cfg(), working_dir=str(Path(d).resolve()), run_key="s"):
            spawn_subagent({"description": "find", "prompt": "search", "subagent_type": "premise-search"})
    check("a named role spawned exactly one child", len(calls) == 1)
    cc = calls[0]["config"] if calls else None
    check("the child ran the role's scoped toolset", cc and cc.tools == ["read_file", "search_mathlib"])
    check("the child carries the role's prompt head",
          cc and cc.system_prompt_head and "premise scout" in cc.system_prompt_head)
    check("the child ran at depth 1", calls and calls[0]["depth"] == 1)


def test_spawn_with_unknown_role_refuses(monkeypatch):
    calls: list = []
    monkeypatch.setattr(agent, "run_events", _fake_run_events(calls))
    with run_context(depth=0, config=_cfg(), working_dir="/tmp", run_key="s"):
        out = spawn_subagent({"description": "x", "prompt": "go", "subagent_type": "typo-role"})
    check("an unknown role is refused, not downgraded", out.startswith("Error:"))
    check("no child was spawned for an unknown role", calls == [])


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
    print("declarative agent profile tests (v2.3 item 19):")
    test_ships_the_starter_roles()
    test_load_premise_search_is_read_only()
    test_validation_refusals()
    test_child_config_from_profile()
    test_generalist_has_no_profile_and_keeps_item18_behavior()
    for fn in (test_spawn_with_a_role_uses_its_profile, test_spawn_with_unknown_role_refuses):
        mp = _MonkeyPatch()
        try:
            fn(mp)
        finally:
            mp.undo()

    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All agent-profile item-19 tests passed.")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
