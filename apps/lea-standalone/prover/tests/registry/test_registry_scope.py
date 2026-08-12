"""ContextVar-scoped tool registry (v2.3 item 27).

`REGISTRY`/`_ORDER` are the process-global base (built-ins, user tool_modules). Tools
registered *during* a run and scoped to it — today only MCP tools — go into a per-activation
overlay pushed by `run_events`, so two concurrent MCP-enabled runs can't corrupt each other's
toolsets. The old bug: one shared dict, so run A's `stop()` unregistered run B's tools by name.

These pin:
  * with no scope open, register/unregister/build_toolset behave exactly as before (global);
  * `scoped=True` inside a scope lands the tool in the overlay, visible to build_toolset,
    and gone the moment the scope is popped — no explicit unregister needed;
  * import-time registration (`scoped=False`) always hits the global, even inside a scope
    (so import-cached user tool_modules don't vanish after the first run);
  * a scoped tool can't shadow a global name (dup-check is against the effective registry);
  * THE FIX: two overlays on two threads are isolated — thread A popping its scope (or
    unregistering) never removes thread B's identically-named tool.

Run:  uv run python -m tests.registry.test_registry_scope
Exits 0 if every check passes, 1 otherwise.
"""

import sys
import threading

import lea.tools  # noqa: F401 — registers the built-ins into the global
from lea.registry import (
    REGISTRY,
    Tool,
    build_toolset,
    get_tool,
    is_registered,
    pop_scope,
    push_scope,
    register,
    unregister,
)

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def _mk(name: str) -> Tool:
    return Tool(name=name, schema={"name": name, "input_schema": {"type": "object"}}, handler=lambda a: name)


# --- back-compat: no scope → global, exactly as before -------------------------

def test_no_scope_registration_is_global():
    register(_mk("sc_global_x"))
    check("no-scope register lands in the global", "sc_global_x" in REGISTRY)
    check("and resolves", is_registered("sc_global_x"))
    unregister("sc_global_x")
    check("no-scope unregister removes from the global", "sc_global_x" not in REGISTRY)


# --- the overlay ---------------------------------------------------------------

def test_scoped_tool_lives_in_overlay_and_vanishes_on_pop():
    token = push_scope()
    register(_mk("sc_overlay_tool"), scoped=True)
    check("scoped tool resolves while the scope is open", is_registered("sc_overlay_tool"))
    check("scoped tool did NOT touch the global", "sc_overlay_tool" not in REGISTRY)
    names = [s["name"] for s in build_toolset(None)[0]]
    check("build_toolset(None) includes the overlay tool", "sc_overlay_tool" in names)
    check("built-ins still present alongside the overlay", "lean_check" in names)
    pop_scope(token)
    check("overlay tool is gone after pop (no explicit unregister)", not is_registered("sc_overlay_tool"))


def test_import_time_registration_stays_global_even_inside_a_scope():
    # scoped=False inside an open scope must still hit the global — this is what keeps
    # import-cached user tool_modules alive across runs.
    token = push_scope()
    register(_mk("sc_static_tool"))  # scoped defaults False
    check("scoped=False lands in the global even inside a scope", "sc_static_tool" in REGISTRY)
    pop_scope(token)
    check("and survives the scope pop", "sc_static_tool" in REGISTRY)
    unregister("sc_static_tool")


def test_scoped_cannot_shadow_a_global_name():
    from lea.errors import ToolError
    token = push_scope()
    raised = False
    try:
        register(_mk("lean_check"), scoped=True)  # a built-in name
    except ToolError:
        raised = True
    pop_scope(token)
    check("registering a scoped tool over a built-in raises", raised)


def test_overlay_ordering_appends_after_builtins():
    token = push_scope()
    register(_mk("sc_zzz"), scoped=True)
    names = [s["name"] for s in build_toolset(None)[0]]
    check("overlay tool comes after the built-ins", names.index("sc_zzz") > names.index("search_mathlib"))
    pop_scope(token)


def test_get_tool_prefers_overlay_then_global():
    check("global built-in resolves with no scope", get_tool("bash") is not None)
    token = push_scope()
    register(_mk("sc_only"), scoped=True)
    check("overlay-only tool resolves via get_tool", get_tool("sc_only") is not None)
    check("global still resolves through the scope", get_tool("bash") is not None)
    pop_scope(token)


# --- THE FIX: concurrent overlays are isolated ---------------------------------

def test_two_threads_overlays_are_isolated():
    """The item-27 guarantee: two runs registering a same-named dynamic tool on two
    threads don't corrupt each other. Thread A tears its scope down (the old bug's
    trigger — A's stop() by-name unregister) while B is still live; B must keep its tool.
    """
    barrier = threading.Barrier(2)
    a_saw_own = {}
    b_saw_after_a_tore_down = {}

    def thread_a():
        token = push_scope()
        register(_mk("mcp__shared"), scoped=True)
        a_saw_own["v"] = is_registered("mcp__shared")
        barrier.wait()          # both threads now have a scoped "mcp__shared"
        barrier.wait()          # wait for B to confirm it sees its own
        pop_scope(token)        # A tears down (also try an explicit unregister-by-name)
        unregister("mcp__shared")

    def thread_b():
        token = push_scope()
        register(_mk("mcp__shared"), scoped=True)
        barrier.wait()
        # B confirms it sees ITS tool, then lets A tear down, then checks it survived.
        b_before = is_registered("mcp__shared")
        barrier.wait()
        # give A time to pop + unregister
        ta.join()
        b_saw_after_a_tore_down["before"] = b_before
        b_saw_after_a_tore_down["after"] = is_registered("mcp__shared")
        pop_scope(token)

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start(); tb.start()
    tb.join()

    check("thread A saw its own overlay tool", a_saw_own.get("v") is True)
    check("thread B saw its own overlay tool", b_saw_after_a_tore_down.get("before") is True)
    check("thread B's tool SURVIVED thread A's teardown (the fix)",
          b_saw_after_a_tore_down.get("after") is True)
    check("neither overlay leaked into the global", "mcp__shared" not in REGISTRY)


def main():
    print("ContextVar-scoped registry tests (v2.3 item 27):")
    test_no_scope_registration_is_global()
    test_scoped_tool_lives_in_overlay_and_vanishes_on_pop()
    test_import_time_registration_stays_global_even_inside_a_scope()
    test_scoped_cannot_shadow_a_global_name()
    test_overlay_ordering_appends_after_builtins()
    test_get_tool_prefers_overlay_then_global()
    test_two_threads_overlays_are_isolated()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        sys.exit(1)
    print("All registry-scope item-27 tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
