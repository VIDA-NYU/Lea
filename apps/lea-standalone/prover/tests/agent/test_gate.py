"""Unit tests for A8: the per-tool approval gate in run_events (D19).

A fake stream writes a proof file (the real write_file handler runs, so an
executed write produces a FileChanged; a denied one produces none). We drive the
two-way generator: each ToolApprovalRequested is answered with gen.send(decision).

Run:  uv run python -m tests.agent.test_gate
"""

import sys
import tempfile
from pathlib import Path

import lea.agent as agent
from lea.config import LeaConfig
from lea.providers import TextDelta, ToolCall, Done, _ToolMeta, Usage
from lea.events import ToolApprovalRequested, ToolResulted, FileChanged

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def cfg(max_turns=3):
    return LeaConfig(model="gemini/test", model_kwargs={}, stream=True,
                     prompt_variant="default", max_turns=max_turns,
                     tools=None, tool_modules=[], skills=[],
                     narrate_tool_steps=False, mcp_servers={})


def msgs(task):
    return [{"role": "user", "content": task}]


def install_fake():
    """Turn 1 calls write_file; later turns just say done."""
    calls = {"n": 0, "tmpdir": tempfile.TemporaryDirectory()}
    proof = str(Path(calls["tmpdir"].name) / "Gate.lean")

    def fake_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        calls["n"] += 1
        if calls["n"] == 1:
            yield ToolCall("write_file", {"path": proof, "content": "theorem g : True := by trivial\n"})
            yield _ToolMeta("w")
            yield Done(Usage(10, 5), 0.001)
        else:
            yield TextDelta("All done.")
            yield Done(Usage(3, 2), 0.0002)

    agent.stream = fake_stream
    agent._tools.lean_check = lambda p: "OK — no errors, no warnings."
    agent.load_system_prompt = lambda v, skills=None, workspace=None, namespace=None: "SYS"
    return calls, proof


def drive(gen, decision):
    """Run `gen` to completion; answer each ToolApprovalRequested with `decision`."""
    events = []
    try:
        ev = next(gen)
        while True:
            events.append(ev)
            ev = gen.send(decision) if isinstance(ev, ToolApprovalRequested) else next(gen)
    except StopIteration:
        pass
    return events


_GATE_WRITE = lambda name, args: name == "write_file"


def test_gate_fires_then_allow_executes():
    install_fake()
    recorded = []
    events = drive(agent.run_events(cfg(), msgs("prove it"), gate=_GATE_WRITE,
        checkpoint_callback=lambda event, payload: recorded.append((event, payload))), "allow")
    approvals = [e for e in events if isinstance(e, ToolApprovalRequested)]
    check("gate fired once on write_file", len(approvals) == 1 and approvals[0].tool_name == "write_file")
    check("approval carries args", "path" in approvals[0].args)
    check("allowed -> write executed (FileChanged)", any(isinstance(e, FileChanged) for e in events))
    starts = [p for e, p in recorded if e == "invocation_start" and p["kind"] == "tool"
              and p["name"] == "write_file"]
    check("approval names the exact recorded tool invocation", len(starts) == 1
          and bool(approvals[0].invocation_id) and starts[0]["id"] == approvals[0].invocation_id)
    check("approved invocation retains its result identity", any(e == "invocation_result"
          and p["id"] == approvals[0].invocation_id for e, p in recorded))


def test_always_session_also_executes():
    install_fake()
    events = drive(agent.run_events(cfg(), msgs("prove it"), gate=_GATE_WRITE), "always_session")
    check("always_session -> executed (FileChanged)", any(isinstance(e, FileChanged) for e in events))


def test_deny_skips_tool():
    install_fake()
    recorded = []
    events = drive(agent.run_events(cfg(max_turns=2), msgs("prove it"), gate=_GATE_WRITE,
        checkpoint_callback=lambda event, payload: recorded.append((event, payload))), "deny")
    check("gate still fired on deny", any(isinstance(e, ToolApprovalRequested) for e in events))
    check("denied -> NOT executed (no FileChanged)", not any(isinstance(e, FileChanged) for e in events))
    declined = [e for e in events if isinstance(e, ToolResulted) and "declined" in e.content]
    check("denied -> tool result is the declined-error", len(declined) == 1)
    check("denied approval never records tool dispatch", not any(e == "invocation_start"
          and p["kind"] == "tool" for e, p in recorded))


def test_unknown_decision_is_treated_as_deny():
    install_fake()
    events = drive(agent.run_events(cfg(max_turns=2), msgs("prove it"), gate=_GATE_WRITE), "huh?")
    check("non-allow decision -> deny (no FileChanged)", not any(isinstance(e, FileChanged) for e in events))


def test_non_gated_tool_runs_without_approval():
    install_fake()
    # gate only bash; write_file is not gated -> no approval, runs straight through.
    gate = lambda name, args: name == "bash"
    events = drive(agent.run_events(cfg(), msgs("prove it"), gate=gate), "allow")
    check("non-gated tool: no approval event", not any(isinstance(e, ToolApprovalRequested) for e in events))
    check("non-gated tool: ran (FileChanged)", any(isinstance(e, FileChanged) for e in events))


def test_no_gate_means_no_gating():
    install_fake()
    events = list(agent.run_events(cfg(), msgs("prove it")))  # gate=None
    check("gate=None: no approval events", not any(isinstance(e, ToolApprovalRequested) for e in events))
    check("gate=None: tool ran (FileChanged)", any(isinstance(e, FileChanged) for e in events))


def main():
    print("per-tool gate (A8) tests:")
    test_gate_fires_then_allow_executes()
    test_always_session_also_executes()
    test_deny_skips_tool()
    test_unknown_decision_is_treated_as_deny()
    test_non_gated_tool_runs_without_approval()
    test_no_gate_means_no_gating()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        sys.exit(1)
    print("All gate tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
