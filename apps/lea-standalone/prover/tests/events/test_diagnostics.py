"""Unit tests for the v2.4 `Diagnostic` event + the per-activation report channel.

The gap this closes: a tool handler returns a `str`, and that string is read by the
MODEL. A tool that failed — or silently degraded, like `lean_check` falling back from
the LSP daemon to a ~440x slower cold compile — had no way to reach the HUMAN.
`lea.diagnostics.report()` is that channel; the agent loop drains it into events.

Run:  uv run python -m tests.events.test_diagnostics
Exits 0 if every check passes, 1 otherwise.
"""

import contextvars
import sys
import threading
import typing
from dataclasses import FrozenInstanceError, fields

from lea import diagnostics
from lea.events import AgentEvent, Diagnostic

_FAILURES = []


def check(label, ok):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}")
    if not ok:
        _FAILURES.append(label)


def test_shape():
    d = Diagnostic(severity="step_error", code="tool.raised", message="boom")
    check("defaults are prover/None/{}", d.source == "prover" and d.remedy is None and d.context == {})
    check("Diagnostic in AgentEvent union", Diagnostic in set(typing.get_args(AgentEvent)))
    names = {f.name for f in fields(Diagnostic)}
    check("carries severity/code/message/source/remedy/context",
          names == {"severity", "code", "message", "source", "remedy", "context"})
    try:
        d.code = "other"  # type: ignore[misc]
        check("Diagnostic is frozen", False)
    except FrozenInstanceError:
        check("Diagnostic is frozen", True)


def test_report_is_a_noop_without_a_scope():
    # interface.check(), the CLI and standalone tests all run with no activation.
    # report() must be inert there rather than needing conditional wiring.
    diagnostics.report("step_error", "tool.raised", "no scope open")
    check("no scope → drain is empty", diagnostics.drain() == [])


def test_report_and_drain():
    token = diagnostics.begin_scope()
    try:
        diagnostics.report("step_error", "tool.raised", "search_mathlib failed",
                           tool="search_mathlib", turn=3)
        drained = diagnostics.drain()
        check("one diagnostic drained", len(drained) == 1)
        check("severity/code preserved",
              drained[0].severity == "step_error" and drained[0].code == "tool.raised")
        check("kwargs become context",
              drained[0].context == {"tool": "search_mathlib", "turn": 3})
        check("drain clears", diagnostics.drain() == [])
    finally:
        diagnostics.end_scope(token)


def test_none_context_values_are_dropped():
    token = diagnostics.begin_scope()
    try:
        diagnostics.report("notice", "tool.raised", "x", tool="bash", path=None)
        # A None anchor is not an anchor; carrying it would make the adapter render
        # an empty "path" chip.
        check("None context entries dropped", diagnostics.drain()[0].context == {"tool": "bash"})
    finally:
        diagnostics.end_scope(token)


def test_once_reports_a_condition_a_single_time():
    token = diagnostics.begin_scope()
    try:
        for _ in range(40):
            diagnostics.report("degraded", "lean.lsp_cold_fallback", "daemon down", once=True)
        first = diagnostics.drain()
        # The daemon being down is ONE fact, not one per check — 40 identical rows
        # would bury the run's real output.
        check("once=True collapses repeats", len(first) == 1)
        diagnostics.report("degraded", "lean.lsp_cold_fallback", "daemon down", once=True)
        check("once=True holds across drains", diagnostics.drain() == [])
    finally:
        diagnostics.end_scope(token)


def test_bad_severity_is_clamped():
    token = diagnostics.begin_scope()
    try:
        diagnostics.report("catastrophic", "tool.raised", "x")
        check("unknown severity → notice", diagnostics.drain()[0].severity == "notice")
    finally:
        diagnostics.end_scope(token)


def test_worker_thread_reports_into_the_parent_scope():
    # E2/E3 run tools on worker threads via contextvars.copy_context(). copy_context
    # copies the MAPPING, not the values, so a child thread appends to the same list
    # the coordinator drains — this is what lets `_exec_tool` stay a plain function.
    token = diagnostics.begin_scope()
    try:
        def work():
            diagnostics.report("step_error", "tool.raised", "from a worker", tool="bash")

        threads = [threading.Thread(target=contextvars.copy_context().run, args=(work,))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("worker-thread reports reach the parent", len(diagnostics.drain()) == 4)
    finally:
        diagnostics.end_scope(token)


def test_scopes_do_not_leak_across_activations():
    outer = diagnostics.begin_scope()
    try:
        inner = diagnostics.begin_scope()
        diagnostics.report("notice", "tool.raised", "child-only")
        diagnostics.end_scope(inner)
        # A child activation's diagnostics belong to the child, not the coordinator.
        check("child scope does not leak to parent", diagnostics.drain() == [])
    finally:
        diagnostics.end_scope(outer)


def main():
    print("diagnostics (v2.4) tests:")
    test_shape()
    test_report_is_a_noop_without_a_scope()
    test_report_and_drain()
    test_none_context_values_are_dropped()
    test_once_reports_a_condition_a_single_time()
    test_bad_severity_is_clamped()
    test_worker_thread_reports_into_the_parent_scope()
    test_scopes_do_not_leak_across_activations()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        sys.exit(1)
    print("All diagnostics tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
