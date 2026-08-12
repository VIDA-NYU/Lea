"""Unit tests for A2: mapping a finished tool call to meaning-level events.

`_meaning_events(tool_name, args, result)` is the pure helper the run loop calls
right after each ToolResulted. Testing it directly needs no model or network —
the run loop just `yield`s whatever this returns.

Run:  uv run python -m tests.events.test_meaning_emit
Exits 0 if every check passes, 1 otherwise.
"""

import sys
from pathlib import Path

from lea.agent import _meaning_events
from lea.events import FileChanged, CheckResult

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}")
        _FAILURES.append(name)


def test_write_lean_ok():
    evs = _meaning_events("write_file", {"path": "proofs/s1/sqrt_two.lean"}, "Wrote 12 lines.")
    check("write .lean ok -> one FileChanged", len(evs) == 1 and isinstance(evs[0], FileChanged))
    # The event carries the path the TOOL used, not the string the model typed: these
    # events cross into the adapter, which reads the file back, and a bare relative name
    # there resolves against a different directory entirely. With no activation open the
    # base is the process cwd, exactly as the tools use.
    check("FileChanged carries the resolved path",
          evs and evs[0].path == str(Path("proofs/s1/sqrt_two.lean").resolve()))
    check("FileChanged path is absolute", evs and Path(evs[0].path).is_absolute())


def test_edit_lean_ok():
    evs = _meaning_events("edit_file", {"path": "a.lean"}, "Replaced 1 occurrence.")
    check("edit .lean ok -> FileChanged", len(evs) == 1 and isinstance(evs[0], FileChanged))


def test_write_error_result():
    evs = _meaning_events("write_file", {"path": "a.lean"}, "Error: permission denied")
    check("write with 'Error:' result -> no event", evs == [])


def test_write_non_lean():
    evs = _meaning_events("write_file", {"path": "notes.txt"}, "Wrote 3 lines.")
    check("write non-.lean -> no event", evs == [])


def test_write_no_path():
    evs = _meaning_events("write_file", {}, "Wrote 3 lines.")
    check("write without path -> no event", evs == [])


def test_lean_check_clean():
    evs = _meaning_events("lean_check", {"path": "a.lean"}, "No errors. 1 warning: sorry.")
    check("clean check -> one CheckResult", len(evs) == 1 and isinstance(evs[0], CheckResult))
    check("clean check status ok", evs and evs[0].status == "ok")
    check("clean check detail None", evs and evs[0].detail is None)


def test_lean_check_error():
    out = "a.lean:3:1: error: unknown identifier 'foo'\nsome trailing context"
    evs = _meaning_events("lean_check", {"path": "a.lean"}, out)
    check("error check -> CheckResult", len(evs) == 1 and isinstance(evs[0], CheckResult))
    check("error check status error", evs and evs[0].status == "error")
    check("error check detail = first error line",
          evs and evs[0].detail == "a.lean:3:1: error: unknown identifier 'foo'")
    check("error check carries the resolved path",
          evs and evs[0].path == str(Path("a.lean").resolve()))


def test_unrelated_tool():
    evs = _meaning_events("search_mathlib", {"query": "Nat.gcd"}, "Found 4 results.")
    check("search_mathlib -> no event", evs == [])
    evs2 = _meaning_events("bash", {"cmd": "ls"}, "a.lean\nb.lean")
    check("bash -> no event", evs2 == [])


def main():
    print("meaning-event emission (A2) tests:")
    test_write_lean_ok()
    test_edit_lean_ok()
    test_write_error_result()
    test_write_non_lean()
    test_write_no_path()
    test_lean_check_clean()
    test_lean_check_error()
    test_unrelated_tool()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        sys.exit(1)
    print("All meaning-emit tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
