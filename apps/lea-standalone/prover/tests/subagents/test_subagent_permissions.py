"""Permissions compose by tightening only (v2.3 item 21, D79).

A child's effective capability = its declared capability INTERSECTED with the
parent's. A child can never call a tool the parent lacked, so spawning is not a
privilege-escalation path. In Lea the composable permission axis is the toolset
(absence from a toolset IS the deny rule — there is no separate deny structure),
and path rules already tighten: a child's scratch dir is a subdir of the parent's,
sandboxed by F3.

These pin:
  * intersection — a declared tool the parent has is kept; one the parent lacks is
    tightened away; declared order is preserved;
  * the generalist (no profile) is tightened to the parent too;
  * `spawn_subagent` (opt-in) is stripped from a child even when the parent has it
    and the child asks for it — the capability-layer twin of the depth guard;
  * an unknown tool name is SOFT-DROPPED with a diagnostic, not raised (B4) — deleting a
    custom tool must not break every role that named it;
  * the invariant holds over a matrix of (parent, declared) combos: effective ⊆
    parent, and never contains spawn_subagent;
  * end to end: a write-less coordinator spawning a write-wanting role yields a
    child with no write tool (escalation refused);
  * path rules tighten: the child's working_dir is under the parent's.

Run:  uv run python -m tests.subagents.test_subagent_permissions
Exits 0 if every check passes, 1 otherwise.
"""

import tempfile
from pathlib import Path

import lea.agent as agent
from lea.config import LeaConfig
from lea import diagnostics
from lea.errors import ToolError
from lea.events import CheckResult, Finished
from lea.profiles import AgentProfile
from lea.providers import Usage
from lea.runctx import run_context
from lea.subagents import _child_config, _parent_tool_names, compose_child_tools, spawn_subagent

_FAILURES: list[str] = []

_BUILTINS = {
    "read_file", "write_file", "edit_file", "lean_check", "bash",
    "search_mathlib", "suggest_imports",
}
_FULL = [
    "read_file", "write_file", "edit_file", "lean_check", "bash",
    "search_mathlib", "suggest_imports", "spawn_subagent",
]
_READONLY = ["read_file", "search_mathlib", "spawn_subagent"]


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def _cfg(tools=None, **over) -> LeaConfig:
    base = dict(model="gemini/parent", max_turns=None, tools=tools)
    base.update(over)
    return LeaConfig(**base)


def _raises_toolerror(fn) -> bool:
    try:
        fn()
        return False
    except ToolError:
        return True


def _fake_run_events(calls: list):
    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None, depth=0):
        calls.append({"config": config, "working_dir": working_dir})
        cand = Path(working_dir) / "C.lean"
        cand.write_text("x\n")
        yield CheckResult(str(cand), "ok", None)
        yield Finished("completed", "done", 1, session_id or "c", config.model,
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})
    return fake


# --- the intersection ----------------------------------------------------------

def test_declared_tools_the_parent_has_are_kept():
    parent = _cfg(tools=_FULL)
    check("a declared subset of the parent's is kept",
          compose_child_tools(parent, ["read_file", "write_file"]) == ["read_file", "write_file"])
    check("declared order is preserved",
          compose_child_tools(parent, ["search_mathlib", "read_file"]) == ["search_mathlib", "read_file"])


def test_declared_tools_the_parent_lacks_are_tightened_away():
    # A write-less coordinator: a child asking for write/edit/bash/lean_check loses them.
    parent = _cfg(tools=_READONLY)
    got = compose_child_tools(parent, ["read_file", "write_file", "lean_check", "bash"])
    check("tools the parent lacks are dropped", got == ["read_file"])


def test_generalist_is_tightened_to_the_parent():
    check("generalist under a full parent = the built-ins",
          set(compose_child_tools(_cfg(tools=_FULL), None)) == _BUILTINS)
    check("generalist under a read-only parent = only its tools",
          compose_child_tools(_cfg(tools=_READONLY), None) == ["read_file", "search_mathlib"])


def test_spawn_subagent_is_stripped_even_when_the_parent_has_it():
    parent = _cfg(tools=_FULL)  # a coordinator that itself holds spawn_subagent
    check("a child cannot inherit spawn_subagent by declaring it",
          compose_child_tools(parent, ["spawn_subagent", "read_file"]) == ["read_file"])
    check("the generalist child never gets spawn_subagent",
          "spawn_subagent" not in compose_child_tools(parent, None))


def test_unknown_tool_name_is_dropped_and_reported():
    """v2.5 B4 — deliberately changed from raising.

    Raising meant that deleting a custom tool (or turning off the MCP server that provided
    it) silently broke every role naming it, with the error surfacing far from the cause.
    The child now runs without it and the human is told."""
    token = diagnostics.begin_scope()
    try:
        effective = compose_child_tools(_cfg(tools=_FULL), ["read_file", "read_fil"])
        reported = diagnostics.drain()
    finally:
        diagnostics.end_scope(token)

    check("an unknown declared tool no longer raises", effective is not None)
    check("the unknown tool is absent from the child", "read_fil" not in effective)
    check("the known tools survive alongside it", "read_file" in effective)
    check("the human is told which tool went missing",
          any(d.code == "subagent.tool_dropped" and "read_fil" in d.message for d in reported))


def test_mcp_tools_never_reach_a_child(monkeypatch):
    """v2.5 — the spawn-killer. A child is built with `mcp_servers={}`, so MCP tools exist
    in the PARENT's registry and not the child's. Composing against the parent alone let
    those names through and the child died in `build_toolset` with
    "unknown tool 'lean_build'" — a hard failure, for a name that was valid where checked.

    It hit the GENERALIST too: its wanted-set is the parent's whole live toolset. An
    imported role naming `lean_goal` and a role naming nothing at all both broke.
    HTTP tools are inherited and must survive.
    """
    from lea.http_tools import register_http_tools
    from lea.registry import Tool, pop_scope, push_scope, register

    scope = push_scope()
    try:
        register_http_tools([{"name": "loogle", "description": "d",
                              "url": "https://api.github.com/x"}])
        register(Tool(name="lean_build", schema={"name": "lean_build", "description": "",
                                                 "input_schema": {}},
                      handler=lambda a: ""), scoped=True)
        parent = _cfg(tools=None, http_tools=[{"name": "loogle", "url": "https://api.github.com/x"}])

        generalist = compose_child_tools(parent, None)
        check("a generalist child gets no MCP tool", "lean_build" not in generalist)
        check("a generalist child keeps the http tool", "loogle" in generalist)

        declared = compose_child_tools(parent, ["read_file", "lean_build", "loogle"])
        check("a role naming an MCP tool does not raise", declared is not None)
        check("the MCP name is dropped from the child", "lean_build" not in declared)
        check("the rest of the role's toolset survives", declared == ["read_file", "loogle"])
    finally:
        pop_scope(scope)


def test_invariant_holds_over_a_matrix():
    # For every combination, effective ⊆ parent, and spawn is never present.
    parents = [_cfg(tools=_FULL), _cfg(tools=_READONLY),
               _cfg(tools=["read_file", "lean_check"]), _cfg(tools=None)]
    declares = [None, ["read_file"], ["write_file", "bash"],
                ["read_file", "search_mathlib", "lean_check"], ["spawn_subagent"]]
    ok = True
    for p in parents:
        allowed = _parent_tool_names(p)
        for d in declares:
            eff = set(compose_child_tools(p, d))
            if not eff <= allowed or "spawn_subagent" in eff:
                ok = False
    check("effective ⊆ parent and no spawn, across all combos", ok)


# --- flows through _child_config and spawn_subagent ----------------------------

def test_child_config_applies_the_intersection():
    # A read-only coordinator + a proof-candidate role (declares write/edit/lean_check):
    # the child ends up with only what the parent also had.
    parent = _cfg(tools=_READONLY)
    prof = AgentProfile(name="proof-candidate", system_prompt="H",
                        tools=["read_file", "write_file", "edit_file", "lean_check",
                               "search_mathlib", "suggest_imports"])
    child = _child_config(parent, prof)
    check("the child cannot write beyond a write-less parent",
          "write_file" not in child.tools and "edit_file" not in child.tools)
    check("the child keeps the overlap with the parent",
          child.tools == ["read_file", "search_mathlib"])


def test_spawn_end_to_end_refuses_escalation(monkeypatch):
    calls: list = []
    monkeypatch.setattr(agent, "run_events", _fake_run_events(calls))
    parent = _cfg(tools=_READONLY)  # coordinator without write
    with tempfile.TemporaryDirectory() as d:
        wd = str(Path(d).resolve())
        with run_context(depth=0, config=parent, working_dir=wd, run_key="s"):
            # Generalist child would "want" all built-ins, but the parent has no write.
            spawn_subagent({"description": "try", "prompt": "attempt a proof"})
        child_cfg = calls[0]["config"] if calls else None
        check("the spawned child inherited no write tool from a write-less parent",
              child_cfg and "write_file" not in child_cfg.tools and "bash" not in child_cfg.tools)
        check("the spawned child kept the parent's read tools",
              child_cfg and set(child_cfg.tools) == {"read_file", "search_mathlib"})
        # Path rule tightens: the child's dir is strictly under the parent's.
        child_wd = calls[0]["working_dir"] if calls else ""
        check("the child working_dir is under the parent's (path tightens)",
              child_wd.startswith(wd) and "/.lea/tmp/" in child_wd.replace("\\", "/"))


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
    print("subagent permission-composition tests (v2.3 item 21):")
    test_declared_tools_the_parent_has_are_kept()
    test_declared_tools_the_parent_lacks_are_tightened_away()
    test_generalist_is_tightened_to_the_parent()
    test_spawn_subagent_is_stripped_even_when_the_parent_has_it()
    test_unknown_tool_name_is_dropped_and_reported()
    test_mcp_tools_never_reach_a_child(None)
    test_invariant_holds_over_a_matrix()
    test_child_config_applies_the_intersection()
    mp = _MonkeyPatch()
    try:
        test_spawn_end_to_end_refuses_escalation(mp)
    finally:
        mp.undo()

    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All subagent permission item-21 tests passed.")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
