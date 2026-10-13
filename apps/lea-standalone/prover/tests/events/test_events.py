"""Unit tests for the meaning-level events added in A1 (events.py, D17).

These four events — FileChanged, CheckResult, VerifyResult, Error — are what the
LeaUI adapter reacts to directly, so it never has to decode tool names. This suite
pins their shape: fields, immutability, optional defaults, and union membership.

Run:  uv run python -m tests.events.test_events
Exits 0 if every check passes, 1 otherwise.
"""

import sys
import typing
from dataclasses import FrozenInstanceError, fields

from lea.events import (
    AgentEvent,
    FileChanged,
    CheckResult,
    VerifyResult,
    Error,
)

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}")
        _FAILURES.append(name)


def test_constructs_ok():
    fc = FileChanged(path="proofs/s1/sqrt_two.lean")
    check("FileChanged.path", fc.path == "proofs/s1/sqrt_two.lean")

    cr_ok = CheckResult(path="a.lean", status="ok")
    check("CheckResult ok status", cr_ok.status == "ok")
    check("CheckResult detail defaults None", cr_ok.detail is None)
    cr_err = CheckResult(path="a.lean", status="error", detail="a.lean:3:1: unknown id")
    check("CheckResult error detail", cr_err.detail.startswith("a.lean:3:1"))

    vr_ok = VerifyResult(status="ok")
    check("VerifyResult ok status", vr_ok.status == "ok")
    check("VerifyResult detail defaults None", vr_ok.detail is None)
    vr_rej = VerifyResult(status="rejected", detail="uses sorry in import")
    check("VerifyResult rejected detail", vr_rej.detail == "uses sorry in import")

    err = Error(message="provider stream closed")
    check("Error.message", err.message == "provider stream closed")


def test_is_frozen():
    for ev, attr, val in [
        (FileChanged("a.lean"), "path", "b.lean"),
        (CheckResult("a.lean", "ok"), "status", "error"),
        (VerifyResult("ok"), "status", "rejected"),
        (Error("x"), "message", "y"),
    ]:
        raised = False
        try:
            setattr(ev, attr, val)
        except FrozenInstanceError:
            raised = True
        check(f"{type(ev).__name__} is frozen", raised)


def test_in_union():
    members = set(typing.get_args(AgentEvent))
    for cls in (FileChanged, CheckResult, VerifyResult, Error):
        check(f"{cls.__name__} in AgentEvent union", cls in members)


def test_file_changed_has_no_content():
    # D3/D8: path-only — content is read from disk / git, never carried in the event.
    names = {f.name for f in fields(FileChanged)}
    check("FileChanged keeps compatible path and optional snapshot", names == {"path", "content"})
    check("legacy FileChanged omits snapshot", FileChanged("a.lean").content is None)
    check("FileChanged captures immutable content", FileChanged("a.lean", "source").content == "source")


def main():
    print("events (A1) tests:")
    test_constructs_ok()
    test_is_frozen()
    test_in_union()
    test_file_changed_has_no_content()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        sys.exit(1)
    print("All events tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
