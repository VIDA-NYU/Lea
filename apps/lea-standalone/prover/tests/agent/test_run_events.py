"""Unit tests for the agent core: run_events() generator + run() wrapper.

Monkeypatches providers.stream with a fake two-turn generator and the tool
handlers, so the whole loop runs with no network and no disk side effects.

Run:  uv run python -m tests.agent.test_run_events
Exits 0 if every check passes, 1 otherwise.
"""

import dataclasses
import sys
import tempfile
from pathlib import Path

import lea.agent as agent
from lea.config import LeaConfig
from lea.providers import TextDelta, ToolCall, Done, _ToolMeta, Usage
from lea.events import (
    TurnStarted, AssistantTextDelta, ToolCalled, ToolResulted, UsageUpdated, Finished,
)

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def install_fakes():
    """Patch the agent's collaborators; returns a fresh two-turn fake stream."""
    calls = {"n": 0, "systems": [], "messages": [], "tmpdir": tempfile.TemporaryDirectory()}
    proof_path = str(Path(calls["tmpdir"].name) / "Basic.lean")

    def fake_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        if "classify the mathematical outcome" in system:
            yield TextDelta("PROVED")
            yield Done(Usage(2, 1), 0.0001)
            return
        calls["n"] += 1
        calls["systems"].append(system)
        calls["messages"].append(messages)
        if calls["n"] == 1:
            yield TextDelta("Let me check. ")
            yield ToolCall("write_file", {"path": proof_path, "content": "theorem basic : True := by trivial\n"})
            yield _ToolMeta("call_write")
            yield ToolCall("lean_check", {"path": proof_path})
            yield _ToolMeta("call_check")
            yield Done(Usage(100, 40), 0.003)
        else:
            yield TextDelta("All done.")
            yield Done(Usage(20, 10), 0.001)

    agent.stream = fake_stream
    agent._tools.lean_check = lambda path: "OK — no errors, no warnings."
    agent.load_system_prompt = lambda variant, skills=None, workspace=None, namespace=None: "SYS"
    return calls


def install_silent_tool_fake():
    calls = {"n": 0, "systems": [], "tmpdir": tempfile.TemporaryDirectory()}
    proof_path = str(Path(calls["tmpdir"].name) / "Silent.lean")

    def fake_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        if "classify the mathematical outcome" in system:
            yield TextDelta("PROVED")
            yield Done(Usage(2, 1), 0.0001)
            return
        calls["n"] += 1
        calls["systems"].append(system)
        if not tools:
            yield TextDelta("I will explain the proof move before using the tool.")
            yield Done(Usage(5, 7), 0.0001)
        elif calls["n"] == 1:
            yield ToolCall("write_file", {"path": proof_path, "content": "theorem silent : True := by trivial\n"})
            yield _ToolMeta("call_write")
            yield Done(Usage(100, 40), 0.003)
        else:
            yield TextDelta("All done.")
            yield Done(Usage(20, 10), 0.001)

    agent.stream = fake_stream
    agent._tools.lean_check = lambda path: "OK — no errors, no warnings."
    agent.load_system_prompt = lambda variant, skills=None, workspace=None, namespace=None: "SYS"
    return calls


def cfg(max_turns=None, tools=None, skills=None, narrate_tool_steps=False):
    return LeaConfig(model="gemini/test", model_kwargs={}, stream=True,
                     prompt_variant="default", max_turns=max_turns,
                     tools=tools, tool_modules=[], skills=skills or [],
                     narrate_tool_steps=narrate_tool_steps,
                     mcp_servers={})


def msgs(task):
    """A fresh single-turn transcript — run_events is messages-in (D16/A9)."""
    return [{"role": "user", "content": task}]


def install_final_gate_fake(*, check_outputs, final_texts=None):
    calls = {"n": 0, "messages": [], "checks": [], "tmpdir": tempfile.TemporaryDirectory()}
    proof_path = str(Path(calls["tmpdir"].name) / "Gate.lean")
    final_texts = final_texts or ["All done.", "Fixed now."]

    def fake_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        if "classify the mathematical outcome" in system:
            yield TextDelta("PROVED")
            yield Done(Usage(2, 1), 0.0001)
            return
        calls["n"] += 1
        calls["messages"].append(messages)
        if calls["n"] == 1:
            yield TextDelta("Writing proof.")
            yield ToolCall("write_file", {"path": proof_path, "content": "theorem gate : True := by trivial\n"})
            yield _ToolMeta("call_write")
            yield Done(Usage(10, 5), 0.001)
            return
        index = min(calls["n"] - 2, len(final_texts) - 1)
        yield TextDelta(final_texts[index])
        yield Done(Usage(3, 2), 0.0002)

    def fake_lean_check(path):
        calls["checks"].append(path)
        index = min(len(calls["checks"]) - 1, len(check_outputs) - 1)
        return check_outputs[index]

    agent.stream = fake_stream
    agent._tools.lean_check = fake_lean_check
    agent.load_system_prompt = lambda variant, skills=None, workspace=None, namespace=None: "SYS"
    return calls, proof_path


def _res(path: str) -> str:
    """The resolved form of a path, which is what the loop now records.

    `_meaning_events` and `ProofVerificationState` report the path the TOOL resolved,
    not the string the model typed — without that, a relative name crossing into the
    adapter resolved against a different directory and a sub-agent's compiling proof
    was silently dropped. On macOS a tempdir's `/var/...` normalises to
    `/private/var/...`, so these comparisons have to resolve too."""
    return str(Path(path).resolve())


def install_final_gate_repair_fake():
    calls = {"n": 0, "messages": [], "checks": [], "tmpdir": tempfile.TemporaryDirectory()}
    proof_path = str(Path(calls["tmpdir"].name) / "Repair.lean")

    def fake_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        if "classify the mathematical outcome" in system:
            yield TextDelta("PROVED")
            yield Done(Usage(2, 1), 0.0001)
            return
        calls["n"] += 1
        calls["messages"].append(messages)
        if calls["n"] == 1:
            yield TextDelta("Writing proof.")
            yield ToolCall("write_file", {"path": proof_path, "content": "theorem repair : True := by trivial\n"})
            yield _ToolMeta("call_write")
            yield Done(Usage(10, 5), 0.001)
            return
        if calls["n"] == 2:
            yield TextDelta("All done.")
            yield Done(Usage(3, 2), 0.0002)
            return
        if calls["n"] == 3:
            yield TextDelta("Repairing.")
            yield ToolCall("edit_file", {
                "path": proof_path,
                "old_string": "trivial",
                "new_string": "trivial",
            })
            yield _ToolMeta("call_edit")
            yield Done(Usage(4, 2), 0.0002)
            return
        yield TextDelta("Fixed now.")
        yield Done(Usage(3, 2), 0.0002)

    def fake_lean_check(path):
        calls["checks"].append(path)
        if len(calls["checks"]) == 1:
            return "Repair.lean:2:2: error: No goals to be solved"
        return "OK — no errors, no warnings."

    agent.stream = fake_stream
    agent._tools.lean_check = fake_lean_check
    agent.load_system_prompt = lambda variant, skills=None, workspace=None, namespace=None: "SYS"
    return calls, proof_path


def install_no_artifact_repair_fake():
    calls = {"n": 0, "messages": [], "checks": [], "tmpdir": tempfile.TemporaryDirectory()}
    proof_path = str(Path(calls["tmpdir"].name) / "Recovered.lean")

    def fake_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        if "classify the mathematical outcome" in system:
            yield TextDelta("PROVED")
            yield Done(Usage(2, 1), 0.0001)
            return
        calls["n"] += 1
        calls["messages"].append(messages)
        if calls["n"] == 1:
            yield TextDelta("Here is a proof in a Markdown code block.")
            yield Done(Usage(7, 4), 0.0002)
            return
        if calls["n"] == 2:
            yield TextDelta("Writing the proof file now.")
            yield ToolCall("write_file", {
                "path": proof_path,
                "content": "theorem recovered : True := by trivial\n",
            })
            yield _ToolMeta("call_write")
            yield ToolCall("lean_check", {"path": proof_path})
            yield _ToolMeta("call_check")
            yield Done(Usage(10, 5), 0.001)
            return
        yield TextDelta("Recovered.")
        yield Done(Usage(3, 2), 0.0002)

    def fake_lean_check(path):
        calls["checks"].append(path)
        return "OK — no errors, no warnings."

    agent.stream = fake_stream
    agent._tools.lean_check = fake_lean_check
    agent.load_system_prompt = lambda variant, skills=None, workspace=None, namespace=None: "SYS"
    return calls, proof_path


def install_explicit_check_fake(*, edit_after_check=False):
    calls = {"n": 0, "messages": [], "checks": [], "tmpdir": tempfile.TemporaryDirectory()}
    proof_path = str(Path(calls["tmpdir"].name) / "Explicit.lean")

    def fake_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        if "classify the mathematical outcome" in system:
            yield TextDelta("PROVED")
            yield Done(Usage(2, 1), 0.0001)
            return
        calls["n"] += 1
        calls["messages"].append(messages)
        if calls["n"] == 1:
            yield TextDelta("Writing and checking.")
            yield ToolCall("write_file", {"path": proof_path, "content": "theorem explicit : True := by trivial\n"})
            yield _ToolMeta("call_write")
            yield ToolCall("lean_check", {"path": proof_path})
            yield _ToolMeta("call_check")
            yield Done(Usage(10, 5), 0.001)
            return
        if edit_after_check and calls["n"] == 2:
            yield TextDelta("Polishing.")
            yield ToolCall("edit_file", {
                "path": proof_path,
                "old_string": "trivial",
                "new_string": "trivial",
            })
            yield _ToolMeta("call_edit")
            yield Done(Usage(4, 2), 0.0002)
            return
        yield TextDelta("All done.")
        yield Done(Usage(3, 2), 0.0002)

    def fake_lean_check(path):
        calls["checks"].append(path)
        return "OK — no errors, no warnings."

    agent.stream = fake_stream
    agent._tools.lean_check = fake_lean_check
    agent.load_system_prompt = lambda variant, skills=None, workspace=None, namespace=None: "SYS"
    return calls, proof_path


def test_run_events_sequence():
    install_fakes()
    events = list(agent.run_events(cfg(), msgs("prove it")))
    types = [type(e).__name__ for e in events]
    expected_types = [
        "TurnStarted", "AssistantTextDelta", "ToolCalled", "ToolCalled", "UsageUpdated",
        # each ToolResulted is immediately followed by its meaning-level event (A2):
        # write_file -> FileChanged, lean_check -> CheckResult.
        "ToolResulted", "FileChanged", "ToolResulted", "CheckResult",
        "TurnStarted", "AssistantTextDelta", "UsageUpdated", "UsageUpdated", "Finished",
    ]
    check("event order", types == expected_types)

    by = {t: [e for e in events if type(e).__name__ == t] for t in set(types)}
    check("turn-1 ToolCalled write_file", by["ToolCalled"][0].name == "write_file")
    check("turn-1 ToolCalled lean_check", by["ToolCalled"][1].name == "lean_check")
    check("turn-1 UsageUpdated cost", by["UsageUpdated"][0] == UsageUpdated(100, 40, 0.003))
    check("write_file result content", by["ToolResulted"][0].content.startswith("Wrote "))
    check("lean_check result content", by["ToolResulted"][1].content == "OK — no errors, no warnings.")

    fin = events[-1]
    check("Finished.reason completed", fin.reason == "completed")
    check("Finished.text", fin.text == "All done.")
    check("Finished.turns", fin.turns == 2)
    check("Finished cumulative usage", fin.usage == Usage(122, 51))
    check("Finished cumulative cost", abs(fin.cost - 0.0041) < 1e-9)
    check("Finished result kind proved", fin.result_kind == "proved")
    check("transcript turns", fin.transcript["turns"] == 2)
    check("transcript has messages", len(fin.transcript["messages"]) >= 3)


def test_max_turns():
    install_fakes()
    events = list(agent.run_events(cfg(max_turns=1), msgs("prove it")))
    fin = events[-1]
    check("max_turns Finished reason", fin.reason == "max_turns")
    check("max_turns Finished turns", fin.turns == 1)


def test_max_turns_hands_back_a_summary():
    # On hitting the turn budget the run spends ONE tool-less turn to summarize its
    # findings + next step, and returns THAT as the result — not a discarded "max turns"
    # error. This is what makes a capped sub-agent scout still useful to the coordinator.
    tmp = tempfile.TemporaryDirectory()
    proof_path = str(Path(tmp.name) / "Work.lean")
    n = {"real": 0}

    def fake_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        if not tools:  # the tool-less summary turn
            yield TextDelta("FINDINGS: Finset.sum_le_sum applies; next, try gcongr.")
            yield Done(Usage(9, 4), 0.001)
            return
        n["real"] += 1  # every real turn just calls a tool → never finishes on its own
        yield ToolCall("read_file", {"path": proof_path})
        yield _ToolMeta(f"c{n['real']}")
        yield Done(Usage(20, 8), 0.002)

    agent.stream = fake_stream
    agent.load_system_prompt = lambda variant, skills=None, workspace=None, namespace=None: "SYS"
    events = list(agent.run_events(cfg(max_turns=2, tools=["read_file"]), msgs("scout lemmas")))
    fin = events[-1]
    check("finished on max_turns", isinstance(fin, Finished) and fin.reason == "max_turns")
    check("result is the SUMMARY, not an error",
          "FINDINGS" in fin.text and "Error: max turns" not in fin.text)
    check("only the budgeted tool turns ran (2), then the summary", n["real"] == 2)
    check("summary streamed as assistant text",
          any(isinstance(e, AssistantTextDelta) and "FINDINGS" in e.text for e in events))
    check("summary folded into the returned transcript",
          any("FINDINGS" in str(m.get("content")) for m in fin.transcript["messages"]))


def test_should_stop_interrupts_before_first_turn():
    # Cooperative interrupt (D18): should_stop True from the start → a clean
    # terminal Finished("interrupted") with no turns run. (Real asserts so pytest
    # enforces it — the file's check() helper is soft-logged only.)
    install_fakes()
    events = list(agent.run_events(cfg(), msgs("prove it"), should_stop=lambda: True))
    fin = events[-1]
    assert isinstance(fin, Finished)
    assert fin.reason == "interrupted"
    assert fin.turns == 0
    assert not any(isinstance(e, TurnStarted) for e in events)


def test_should_stop_after_one_turn_stops_cleanly():
    # Stop requested while running: the current turn finishes (its write committed),
    # then the next turn-boundary check stops the loop. turns == 1.
    install_fakes()
    seen = {"n": 0}

    def stop():
        seen["n"] += 1
        return seen["n"] > 1  # allow turn 1, stop at the turn-2 boundary

    events = list(agent.run_events(cfg(), msgs("prove it"), should_stop=stop))
    fin = events[-1]
    assert fin.reason == "interrupted"
    assert fin.turns == 1
    assert any(isinstance(e, TurnStarted) for e in events)


def test_narrate_tool_steps_instruction():
    calls = install_fakes()
    list(agent.run_events(cfg(narrate_tool_steps=True), msgs("prove it")))
    check("narration instruction added", "first write a concise progress" in calls["systems"][0])

    calls = install_fakes()
    list(agent.run_events(cfg(narrate_tool_steps=False), msgs("prove it")))
    check("narration instruction omitted by default", "first write a concise progress" not in calls["systems"][0])


def test_narrate_tool_steps_forces_text_before_silent_tool_call():
    install_silent_tool_fake()
    events = list(agent.run_events(cfg(narrate_tool_steps=True), msgs("prove it")))
    types = [type(e).__name__ for e in events]
    text_index = types.index("AssistantTextDelta")
    tool_index = types.index("ToolCalled")
    check("forced narration before tool call", text_index < tool_index)

    fin = events[-1]
    first_assistant = fin.transcript["messages"][1]
    check(
        "forced narration persisted before tool call",
        first_assistant["content"][0] == {
            "type": "text",
            "text": "I will explain the proof move before using the tool.",
        },
    )
    check("forced narration usage included", fin.usage == Usage(127, 58))


def test_final_gate_failed_check_resumes_loop():
    calls, proof_path = install_final_gate_repair_fake()
    events = list(agent.run_events(cfg(max_turns=4), msgs("prove it")))
    fin = events[-1]
    check("failed final gate eventually completes", isinstance(fin, Finished) and fin.reason == "completed")
    check("final gate checked before and after repair", calls["checks"] == [_res(proof_path), _res(proof_path)])
    check("failed gate resumed the model loop", calls["n"] == 4)
    saw_failure_prompt = any(
        isinstance(message.get("content"), str)
        and "final verification gate failed" in message["content"]
        and "No goals to be solved" in message["content"]
        for message in calls["messages"][2]
    )
    check("model received final gate failure", saw_failure_prompt)


def test_no_proof_artifact_resumes_loop():
    calls, proof_path = install_no_artifact_repair_fake()
    events = list(agent.run_events(cfg(max_turns=3), msgs("prove it")))
    fin = events[-1]
    check("no-artifact run eventually completes", isinstance(fin, Finished) and fin.reason == "completed")
    # model-invoked → the stub sees the raw args path
    check("no-artifact recovery used lean_check", calls["checks"] == [proof_path])
    check("no-artifact recovery resumed the model loop", calls["n"] == 3)
    saw_no_artifact_prompt = any(
        isinstance(message.get("content"), str)
        and "no proof artifact was produced" in message["content"]
        and "write_file" in message["content"]
        and "lean_check" in message["content"]
        for message in calls["messages"][1]
    )
    check("model received no-artifact correction", saw_no_artifact_prompt)


def test_failed_final_gate_respects_max_turns():
    calls, proof_path = install_final_gate_fake(
        check_outputs=["Gate.lean:2:2: error: No goals to be solved"],
        final_texts=["All done."],
    )
    events = list(agent.run_events(cfg(max_turns=2), msgs("prove it")))
    fin = events[-1]
    check("failed final gate hits max_turns next", isinstance(fin, Finished) and fin.reason == "max_turns")
    # The gate loop stops at max_turns (2 model turns); then exactly ONE tool-less summary
    # turn runs on the max_turns branch (item: summarize-on-cap). No runaway beyond that.
    check("gate loop stops at max_turns, plus one summary turn", calls["n"] == 3)
    check("failed final gate checked once with max_turns", calls["checks"] == [_res(proof_path)])


def test_final_gate_success_allows_completion():
    calls, proof_path = install_final_gate_fake(check_outputs=["OK — no errors, no warnings."])
    events = list(agent.run_events(cfg(), msgs("prove it")))
    fin = events[-1]
    check("passing final gate completes", isinstance(fin, Finished) and fin.reason == "completed")
    check("passing final gate checked latest proof", calls["checks"] == [_res(proof_path)])


def test_successful_explicit_check_skips_duplicate_final_gate():
    calls, proof_path = install_explicit_check_fake()
    events = list(agent.run_events(cfg(), msgs("prove it")))
    fin = events[-1]
    check("explicit check completes", isinstance(fin, Finished) and fin.reason == "completed")
    # model-invoked only; the gate must NOT add a second check
    check("explicit successful check not duplicated", calls["checks"] == [proof_path])


def test_edit_after_successful_check_rechecks_final_gate():
    calls, proof_path = install_explicit_check_fake(edit_after_check=True)
    events = list(agent.run_events(cfg(), msgs("prove it")))
    fin = events[-1]
    check("edit after check completes", isinstance(fin, Finished) and fin.reason == "completed")
    # the model checked (raw path), then edited, so the gate re-checks — and the gate
    # passes `latest_proof_path`, which is resolved.
    check("edit after check rechecked", calls["checks"] == [proof_path, _res(proof_path)])


def install_result_classifier_fake(classifier_text):
    calls = {"n": 0, "tmpdir": tempfile.TemporaryDirectory()}
    proof_path = str(Path(calls["tmpdir"].name) / "Result.lean")

    def fake_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        if "classify the mathematical outcome" in system:
            yield TextDelta(classifier_text)
            yield Done(Usage(2, 1), 0.0001)
            return
        calls["n"] += 1
        if calls["n"] == 1:
            yield ToolCall("write_file", {"path": proof_path, "content": "theorem result : True := by trivial\n"})
            yield _ToolMeta("write")
            yield ToolCall("lean_check", {"path": proof_path})
            yield _ToolMeta("check")
            yield Done(Usage(10, 5), 0.001)
            return
        yield TextDelta("Final message.")
        yield Done(Usage(3, 2), 0.0002)

    agent.stream = fake_stream
    agent._tools.lean_check = lambda path: "OK — no errors, no warnings."
    agent.load_system_prompt = lambda variant, skills=None, workspace=None, namespace=None: "SYS"
    return calls


def test_final_result_classifier_marks_proved():
    install_result_classifier_fake("PROVED")
    fin = list(agent.run_events(cfg(), msgs("prove it")))[-1]
    check("result classifier proved", fin.result_kind == "proved")


def test_final_result_classifier_marks_disproved():
    install_result_classifier_fake("DISPROVED")
    fin = list(agent.run_events(cfg(), msgs("find a counterexample")))[-1]
    check("result classifier disproved", fin.result_kind == "disproved")


def test_final_result_classifier_defaults_ambiguous_to_needs_review():
    install_result_classifier_fake("I am not sure")
    fin = list(agent.run_events(cfg(), msgs("prove it")))[-1]
    check("result classifier ambiguous", fin.result_kind == "needs_review")


def cfg_interactive():
    # LeaConfig is frozen now; build with the interactive variant directly
    # (it's also the default, but cfg() pins "default", so override explicitly).
    return dataclasses.replace(cfg(), prompt_variant="interactive")


def install_interactive_fake(decision):
    """Fake stream for an interactive continuation turn.

    Routes two kinds of model call by their system prompt: the intent
    classifier and the main loop turn.
    """
    calls = {"loop_tool_names": None, "search_called": False}

    def fake_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        if "FORMALIZE or ASSISTANT" in system:  # intent classifier
            yield TextDelta(decision)
            yield Done(Usage(3, 1), 0.0001)
            return
        # main agentic loop turn
        calls["loop_tool_names"] = [t.get("name") for t in (tools or [])]
        if decision == "ASSISTANT" and not calls["search_called"]:
            calls["search_called"] = True
            yield TextDelta("Let me look that up. ")
            yield ToolCall("search_mathlib", {"query": "even"})
            yield _ToolMeta("call_s")
            yield Done(Usage(8, 4), 0.0002)
            return
        yield TextDelta("Here is the explanation, in plain terms.")
        yield Done(Usage(6, 3), 0.0002)

    agent.stream = fake_stream
    agent._tools.search_mathlib = lambda *a, **k: "Found: Nat.even_add in Mathlib/Algebra/Parity.lean"
    agent.load_system_prompt = lambda variant, skills=None, workspace=None, namespace=None: f"SYS[{variant}]"
    return calls


# A prior conversation the adapter would pass in (D16/A9 — messages-in): a proved
# theorem, then a follow-up question. The prior assistant turn is what routes this
# to assistant/QA mode (not a fresh formalization).
_PRIOR_CONVERSATION = [
    {"role": "user", "content": "prove that the sum of two evens is even"},
    {"role": "assistant", "content": [{"type": "text", "text": "Proved it."}]},
    {"role": "user", "content": "explain this proof so a newbie gets it"},
]


def test_interactive_sorry_skeleton_is_not_proved():
    """A skeleton that compiles only because of `sorry` must NOT finish as
    'completed' (a false "Proved"). `sorry` is a warning, not an error, so the
    gate has to detect it; in interactive mode the run ends as a chat turn so the
    user can decide to fill the sorrys."""
    calls = {"n": 0, "tmpdir": tempfile.TemporaryDirectory()}
    proof_path = str(Path(calls["tmpdir"].name) / "Skel.lean")

    def fake_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        calls["n"] += 1
        if calls["n"] == 1:
            yield TextDelta("I'll write the skeleton.")
            yield ToolCall("write_file", {"path": proof_path, "content": "theorem t : True := by\n  sorry\n"})
            yield _ToolMeta("w")
            yield ToolCall("lean_check", {"path": proof_path})
            yield _ToolMeta("c")
            yield Done(Usage(10, 5), 0.001)
            return
        yield TextDelta("Would you like me to fill the sorry now?")
        yield Done(Usage(3, 2), 0.0002)

    agent.stream = fake_stream
    agent._tools.lean_check = lambda path: "Skel.lean:2:2: warning: declaration uses 'sorry'"
    agent.load_system_prompt = lambda variant, skills=None, workspace=None, namespace=None: f"SYS[{variant}]"
    events = list(agent.run_events(cfg_interactive(), msgs("prove t")))
    fin = events[-1]
    check("sorry skeleton is NOT 'completed'", isinstance(fin, Finished) and fin.reason != "completed")
    check("sorry skeleton ends as a chat turn (no false Proved)", fin.reason == "assistant")
    check("sorry skeleton hands back to the user", "fill the sorry" in fin.text)


def test_interactive_assistant_turn_routes_to_chat_and_keeps_tools():
    calls = install_interactive_fake("ASSISTANT")
    events = list(agent.run_events(cfg_interactive(), list(_PRIOR_CONVERSATION)))

    fin = events[-1]
    check("assistant turn: benign 'assistant' reason", isinstance(fin, Finished) and fin.reason == "assistant")
    check("assistant turn: answered in prose", "plain terms" in fin.text)
    # The user-requested case: a lemma-lookup question can use search_mathlib.
    check("assistant turn: full toolset available", "search_mathlib" in (calls["loop_tool_names"] or []))
    check("assistant turn: search_mathlib actually called",
          any(isinstance(e, ToolCalled) and e.name == "search_mathlib" for e in events))


def test_text_only_history_serializes_for_provider():
    """Regression: assistant turns must round-trip through _to_openai_messages.

    A bare-string assistant content made _to_openai_messages iterate the string
    char-by-char and call .get on a char ('str' object has no attribute 'get').
    """
    from lea.providers import _to_openai_messages

    resumed_messages = [
        {"role": "user", "content": "prove that 2 + 2 = 4"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "I'll write the proof."},
            {"type": "tool_call", "name": "write_file", "args": {"path": "p.lean"}, "id": "c1"},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_name": "write_file", "content": "ok", "tool_call_id": "c1"},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "It compiles."}]},
        {"role": "user", "content": "explain it for a newbie"},
    ]
    history = agent._text_only_history(resumed_messages)
    try:
        oai = _to_openai_messages("SYS", history)
        ok = True
    except Exception as exc:  # noqa: BLE001 - the regression we are guarding against
        ok = False
        print(f"    _to_openai_messages raised: {type(exc).__name__}: {exc}")
    check("text-only history serializes without error", ok)
    check("latest user message preserved", history[-1] == {"role": "user", "content": "explain it for a newbie"})
    check(
        "assistant turns use parts format",
        all(m["role"] != "assistant" or isinstance(m["content"], list) for m in history),
    )


def test_reasoning_items_are_persisted_with_the_tool_turn():
    """Responses continuation state survives agent execution and transcript storage."""
    temp = tempfile.TemporaryDirectory()
    source_path = Path(temp.name) / "Source.txt"
    source_path.write_text("context")
    calls = {"n": 0}
    reasoning_item = {
        "id": "rs_agent_1",
        "type": "reasoning",
        "encrypted_content": "encrypted-agent-state",
        "summary": [],
    }

    def fake_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        calls["n"] += 1
        if calls["n"] == 1:
            yield ToolCall("read_file", {"path": str(source_path)})
            yield _ToolMeta("call_read")
            yield Done(Usage(5, 2), 0.0001, [reasoning_item])
            return
        yield TextDelta("The requested context is available.")
        yield Done(Usage(4, 2), 0.0001)

    agent.stream = fake_stream
    agent.load_system_prompt = lambda variant, skills=None, workspace=None, namespace=None: "SYS"
    config = dataclasses.replace(cfg(tools=["read_file"]), prompt_variant="interactive")
    events = list(agent.run_events(config, msgs("inspect this context")))
    finished = events[-1]
    first_assistant = next(
        message
        for message in finished.transcript["messages"]
        if message.get("role") == "assistant"
        and any(part.get("type") == "tool_call" for part in message.get("content", []))
    )
    check(
        "reasoning item persisted beside originating tool call",
        {"type": "reasoning", "items": [reasoning_item]} in first_assistant["content"],
    )
    check(
        "reasoning item remains internal",
        not any(
            isinstance(event, AssistantTextDelta) and "encrypted-agent-state" in event.text
            for event in events
        ),
    )
    temp.cleanup()


def test_run_events_uses_caller_messages_verbatim():
    # A9/D16: run_events is messages-in. It does NOT build or inject anything
    # (project context, etc.) — the caller (adapter) assembles the transcript and
    # run_events runs over exactly what it's given.
    calls = install_fakes()
    given = [
        {"role": "user", "content": "Existing project facts:\n## Theorem: helper `workspace/proofs/helper.lean`"},
        {"role": "user", "content": "prove it"},
    ]
    events = list(agent.run_events(cfg(), given))
    check("run completes with caller-provided messages", isinstance(events[-1], Finished))
    first_messages = calls["messages"][0]
    # No prepend: the caller's context stays at [0] and the task at [1]. (A prepend
    # would push them down.) `messages` grows in place during the run, so we check
    # the head, not the length.
    check("caller context stays first (not prepended)", "Existing project facts" in first_messages[0]["content"])
    check("task stays second", first_messages[1]["content"] == "prove it")


def main():
    print("agent (run_events + run) tests:")
    test_run_events_sequence()
    test_max_turns()
    test_max_turns_hands_back_a_summary()
    test_narrate_tool_steps_instruction()
    test_narrate_tool_steps_forces_text_before_silent_tool_call()
    test_final_gate_failed_check_resumes_loop()
    test_no_proof_artifact_resumes_loop()
    test_failed_final_gate_respects_max_turns()
    test_final_gate_success_allows_completion()
    test_successful_explicit_check_skips_duplicate_final_gate()
    test_edit_after_successful_check_rechecks_final_gate()
    test_final_result_classifier_marks_proved()
    test_final_result_classifier_marks_disproved()
    test_final_result_classifier_defaults_ambiguous_to_needs_review()
    test_interactive_sorry_skeleton_is_not_proved()
    test_interactive_assistant_turn_routes_to_chat_and_keeps_tools()
    test_text_only_history_serializes_for_provider()
    test_reasoning_items_are_persisted_with_the_tool_turn()
    test_run_events_uses_caller_messages_verbatim()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        sys.exit(1)
    print("All agent tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
