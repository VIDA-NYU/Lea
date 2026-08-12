"""Integration tests for MCP: real stdio round-trip + warn-and-continue.

Launches the stdio stub server (tests/mcp/_server.py) through MCPManager and
checks its tools register into the shared registry, are callable, and unregister
on stop. Also checks a server that fails to start is skipped (no raise). No
network, no node — just a Python subprocess speaking MCP over stdio.

Run:  uv run python -m tests.mcp.test_mcp
Exits 0 if every check passes, 1 otherwise.
"""

import sys
from pathlib import Path

import lea.tools  # noqa: F401 — register built-ins so the collision test is meaningful
from lea.mcp import MCPManager, summarize_stderr
from lea.registry import REGISTRY, build_toolset

_FAILURES: list[str] = []

SERVER = str(Path(__file__).resolve().parent / "_server.py")


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def test_stdio_roundtrip():
    mgr = MCPManager({"stub": {"command": sys.executable, "args": [SERVER]}})
    mgr.start()
    try:
        schemas, handlers = build_toolset(None)
        names = [s["name"] for s in schemas]
        # Non-colliding tools register under their bare names.
        check("secret registered by bare name", "secret" in names)
        check("echo registered by bare name", "echo" in names)
        check("secret tool call returns server value", handlers["secret"]({}) == "MCP-SECRET-9")
        check("echo tool call passes args", handlers["echo"]({"text": "hi"}) == "stub-echo: hi")
        echo_schema = next(s for s in schemas if s["name"] == "echo")
        check("echo schema carries input_schema", "text" in echo_schema["input_schema"].get("properties", {}))
        check("echo schema carries description", bool(echo_schema["description"]))
        # `bash` collides with the built-in, so it is exposed prefixed; the
        # built-in `bash` keeps its name, the MCP one is reachable via stub__bash.
        check("colliding tool prefixed to stub__bash", "stub__bash" in names)
        check("built-in bash still bare", "bash" in names)
        check("prefixed tool calls the MCP server", handlers["stub__bash"]({"command": "x"}) == "stub-bash: x")
    finally:
        mgr.stop()
    check("bare MCP tools unregistered after stop", "secret" not in REGISTRY and "echo" not in REGISTRY)
    check("prefixed MCP tool unregistered after stop", "stub__bash" not in REGISTRY)
    check("built-in bash survives MCP stop", "bash" in REGISTRY)


def test_warn_and_continue_on_bad_server():
    mgr = MCPManager({"bad": {"command": "definitely-not-a-real-binary-xyz", "args": []}})
    try:
        mgr.start()  # must not raise — warn and continue
        check("start() did not raise on unstartable server", True)
        check("no tools from failed server", not any(n.startswith("bad__") for n in REGISTRY))
    finally:
        mgr.stop()


def test_no_servers_is_noop():
    mgr = MCPManager({})
    mgr.start()
    mgr.stop()
    check("empty manager start/stop is a no-op", True)


def test_summarize_stderr_picks_the_actionable_line():
    """A stdio crash arrives as a traceback whose LAST line is box-drawing, so the raw
    tail tells the user nothing. The useful line is the innermost error (v2.5 E0b)."""
    exception_group = """
  + Exception Group Traceback (most recent call last):
  |   File "/x/bin/lean-lsp-mcp", line 12, in <module>
  |     sys.exit(main())
  | ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)
    |     raise ValueError(
    | ValueError: Lean project path '/Users/x' must contain `lean-toolchain`.
    +------------------------------------
"""
    picked = summarize_stderr(exception_group)
    check("picks the innermost ValueError, not the ExceptionGroup",
          picked.startswith("Lean project path"))
    check("strips the exception class name", not picked.startswith("ValueError"))
    check("drops the trailing box-drawing line", "+---" not in picked)
    check("empty stderr yields empty summary", summarize_stderr("") == "")
    check("no error line falls back to the last prose line",
          summarize_stderr("starting up\nsomething odd happened") == "something odd happened")


def main():
    print("mcp integration tests:")
    test_summarize_stderr_picks_the_actionable_line()
    test_no_servers_is_noop()
    test_warn_and_continue_on_bad_server()
    test_stdio_roundtrip()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        sys.exit(1)
    print("All mcp tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
