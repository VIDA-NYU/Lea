"""Unit tests for the tool registry (registry.py).

Covers registration, the @tool decorator, build_toolset selection/order, and the
typed ToolError paths (unknown tool, duplicate, bad tool_modules import). No
network or disk.

Run:  uv run python -m tests.registry.test_registry
Exits 0 if every check passes, 1 otherwise.
"""

import sys

import lea.tools  # noqa: F401 — registers the built-ins
from lea.registry import (
    REGISTRY,
    Tool,
    build_toolset,
    import_tool_modules,
    register,
    tool,
)
from lea.errors import ToolError

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def expect_raises(name: str, err_type: type, fn) -> None:
    try:
        fn()
    except err_type:
        print(f"  ok   {name}")
    except Exception as e:
        print(f"  FAIL {name} (raised {type(e).__name__}, expected {err_type.__name__})")
        _FAILURES.append(name)
    else:
        print(f"  FAIL {name} (no error raised, expected {err_type.__name__})")
        _FAILURES.append(name)


BUILTINS = [
    "read_file", "write_file", "edit_file", "lean_check", "bash",
    "search_mathlib", "suggest_imports",
]


def test_builtins_registered():
    for name in BUILTINS:
        check(f"built-in registered: {name}", name in REGISTRY)


def test_none_selects_all_in_order():
    schemas, handlers = build_toolset(None)
    names = [s["name"] for s in schemas]
    # The built-ins come first, in TOOLS_SCHEMA order.
    check("None: built-ins present in order", names[:len(BUILTINS)] == BUILTINS)
    check("None: handlers cover schemas", set(handlers) == set(names))
    check("None: a handler is callable", callable(handlers["bash"]))


def test_list_filters_and_orders():
    schemas, handlers = build_toolset(["search_mathlib", "bash"])
    names = [s["name"] for s in schemas]
    check("list: exactly the selected names, in order", names == ["search_mathlib", "bash"])
    check("list: handlers match selection", set(handlers) == {"search_mathlib", "bash"})


def test_unknown_tool_raises():
    expect_raises("unknown tool → ToolError", ToolError, lambda: build_toolset(["does_not_exist"]))


def test_duplicate_register_raises():
    register(Tool(name="dup_tool_x", schema={"name": "dup_tool_x"}, handler=lambda a: "ok"))
    expect_raises(
        "duplicate register → ToolError",
        ToolError,
        lambda: register(Tool(name="dup_tool_x", schema={"name": "dup_tool_x"}, handler=lambda a: "ok")),
    )


def test_decorator_registers():
    @tool(name="shout", description="upcase", input_schema={"type": "object"})
    def shout(args: dict) -> str:
        return str(args.get("s", "")).upper()

    check("decorator: registered", "shout" in REGISTRY)
    schemas, handlers = build_toolset(["shout"])
    check("decorator: schema assembled", schemas[0]["description"] == "upcase")
    check("decorator: handler works", handlers["shout"]({"s": "hi"}) == "HI")


def test_import_tool_modules():
    # An empty list is a no-op; a missing module is a typed ToolError.
    import_tool_modules([])
    check("import_tool_modules([]) no-op", True)
    expect_raises(
        "bad tool module → ToolError",
        ToolError,
        lambda: import_tool_modules(["lea._definitely_not_a_module_xyz"]),
    )


def main():
    print("tool registry tests:")
    test_builtins_registered()
    test_none_selects_all_in_order()
    test_list_filters_and_orders()
    test_unknown_tool_raises()
    test_duplicate_register_raises()
    test_decorator_registers()
    test_import_tool_modules()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        sys.exit(1)
    print("All registry tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
