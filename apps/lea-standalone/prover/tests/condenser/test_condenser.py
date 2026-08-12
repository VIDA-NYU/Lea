"""Context compaction — the condenser (G1). Prune → summarize.

These pin the condenser's contract and, critically, its message-shape safety:

  * prune masks superseded read-only tool results OUTSIDE the recent window, keeps the
    recent ones verbatim, leaves non-prunable tools + tiny content alone, and is
    copy-on-write (the caller's message dicts are never mutated);
  * summarize folds only COMPLETE turns — head = leading all-user context, tail starts
    at an assistant message — so no assistant tool_call is ever separated from its
    tool_result (the invariant that keeps LiteLLM from emitting an orphan `role:"tool"`);
  * the trigger honours the threshold and the 0-disable escape hatch;
  * condense takes the cheap prune path when it's enough and only pays for a summary
    when it isn't;
  * driven through the REAL coordinator loop, a turn whose provider reports a large
    input size makes the next turn emit a `Compacted` event and shrink `messages`.

Run:  uv run python -m tests.condenser.test_condenser
Exits 0 if every check passes, 1 otherwise.
"""

import tempfile
from pathlib import Path

import lea.agent as agent
import lea.condenser as condenser
from lea.condenser import (
    CondenseResult,
    compaction_trigger,
    condense,
    estimate_tokens,
    prune_superseded,
    referenced_files,
    should_compact,
    summarize_middle,
)
from lea.config import LeaConfig
from lea.events import Compacted, Finished
from lea.providers import TextDelta, ToolCall, Done, _ToolMeta, Usage

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def _cfg(**over):
    base = dict(model="m", max_turns=None)
    base.update(over)
    return LeaConfig(**base)


# --- history builders --------------------------------------------------------

def _tool_result_msg(tool_name: str, call_id: str, content: str) -> dict:
    return {"role": "user", "content": [{
        "type": "tool_result", "tool_name": tool_name,
        "tool_use_id": call_id, "tool_call_id": call_id, "content": content,
    }]}


def _assistant_call_msg(tool_name: str, call_id: str, text: str = "") -> dict:
    parts = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.append({"type": "tool_call", "name": tool_name, "args": {"path": "F.lean"}, "id": call_id})
    return {"role": "assistant", "content": parts}


def _history(n_turns: int, tool_name: str = "lean_check", body_chars: int = 4000) -> list:
    """A request + `n_turns` assistant(tool_call)+user(tool_result) pairs."""
    msgs: list = [{"role": "user", "content": "Prove that 2 + 2 = 4 in Lean 4."}]
    for i in range(n_turns):
        cid = f"c{i}"
        msgs.append(_assistant_call_msg(tool_name, cid, text=f"Attempt {i}."))
        msgs.append(_tool_result_msg(tool_name, cid, f"error {i}: " + "x" * body_chars))
    return msgs


# --- prune -------------------------------------------------------------------

def test_prune_masks_superseded_keeps_recent():
    msgs = _history(5, tool_name="lean_check")
    before = estimate_tokens(msgs)
    out, n = prune_superseded(msgs, keep_recent_results=2)
    # 5 results, keep 2 → 3 masked.
    check("prune masks all-but-recent", n == 3)
    # The last two results survive verbatim; the first three carry the placeholder.
    results = [p for m in out if isinstance(m["content"], list)
               for p in m["content"] if p.get("type") == "tool_result"]
    check("prune keeps recent verbatim", "x" * 4000 in results[-1]["content"])
    check("prune placeholder on old", "pruned to save context" in results[0]["content"])
    check("prune preserves tool_result id", results[0].get("tool_use_id") == "c0")
    check("prune shrinks the estimate", estimate_tokens(out) < before)


def test_prune_is_copy_on_write():
    msgs = _history(3)
    snapshot = msgs[2]["content"][0]["content"]  # a result body
    out, n = prune_superseded(msgs, keep_recent_results=0)
    check("prune left input dicts untouched", msgs[2]["content"][0]["content"] == snapshot)
    check("prune returned a different list object", out is not msgs)


def test_prune_ignores_non_prunable_and_tiny():
    msgs = _history(3, tool_name="bash")  # bash is not prunable
    out, n = prune_superseded(msgs, keep_recent_results=0)
    check("prune skips non-prunable tools", n == 0)
    tiny = [{"role": "user", "content": "hi"},
            _assistant_call_msg("lean_check", "c0"),
            _tool_result_msg("lean_check", "c0", "OK")]  # < min chars
    _out, n2 = prune_superseded(tiny, keep_recent_results=0)
    check("prune skips tiny results", n2 == 0)


# --- summarize ---------------------------------------------------------------

def _patch_summary(mp, text="GOAL: 2+2=4\nNEXT: try `norm_num`."):
    mp.setattr(condenser, "_summarize", lambda model, log, config: (text, Usage(5, 3), 0.0002))


def _tool_ids(messages):
    calls, results = set(), []
    for m in messages:
        if m["role"] == "assistant" and isinstance(m["content"], list):
            for p in m["content"]:
                if p.get("type") == "tool_call":
                    calls.add(p["id"])
        elif m["role"] == "user" and isinstance(m["content"], list):
            for p in m["content"]:
                if p.get("type") == "tool_result":
                    results.append(p.get("tool_use_id"))
    return calls, results


def test_summarize_folds_middle_and_preserves_pairing(mp):
    _patch_summary(mp)
    msgs = _history(10)
    out, summarized, usage, cost = summarize_middle(msgs, keep_recent_turns=3, model="m", config=_cfg())
    check("summarize reports it folded", summarized == 1)
    check("summarize shrinks history", len(out) < len(msgs))
    # Head preserved: the original request still leads (with the summary folded onto it, so
    # no user/user seam is introduced before the assistant tail).
    check("summarize keeps the goal first",
          out[0]["role"] == "user" and out[0]["content"].startswith("Prove that 2 + 2 = 4 in Lean 4."))
    check("summarize folded in the summary block",
          any(isinstance(m["content"], str) and "SESSION SUMMARY" in m["content"] for m in out))
    # THE INVARIANT: no orphaned tool_result — every kept result's id has its call kept too.
    calls, results = _tool_ids(out)
    check("summarize orphans no tool_result", all(r in calls for r in results))
    # Tail starts at an assistant message (right after the folded head).
    check("summarize tail starts at assistant", out[1]["role"] == "assistant")


def test_summarize_noops_when_too_short(mp):
    _patch_summary(mp)
    msgs = _history(2)
    out, summarized, _u, _c = summarize_middle(msgs, keep_recent_turns=6, model="m", config=_cfg())
    check("summarize no-ops with too few turns", summarized == 0 and out is msgs)


def test_summarize_degrades_when_summary_empty(mp):
    mp.setattr(condenser, "_summarize", lambda model, log, config: ("", Usage(), 0.0))
    msgs = _history(10)
    out, summarized, _u, _c = summarize_middle(msgs, keep_recent_turns=3, model="m", config=_cfg())
    check("summarize degrades to no-op on empty summary", summarized == 0 and out is msgs)


# --- trigger -----------------------------------------------------------------

def test_referenced_files_are_distinct_and_in_order():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [
            {"type": "tool_call", "name": "read_file", "args": {"path": "A.lean"}, "id": "1"},
            {"type": "tool_call", "name": "lean_check", "args": {"path": "B.lean"}, "id": "2"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_name": "read_file",
                                      "tool_use_id": "1", "content": "x"}]},
        {"role": "assistant", "content": [
            {"type": "tool_call", "name": "lean_check", "args": {"path": "A.lean"}, "id": "3"}]},
    ]
    check("referenced_files distinct, first-seen order",
          referenced_files(msgs) == ["A.lean", "B.lean"])
    check("referenced_files empty when no tool calls",
          referenced_files([{"role": "user", "content": "hi"}]) == [])


def test_trigger_and_disable():
    c = _cfg(context_token_limit=100_000, compaction_threshold=0.75)
    check("trigger computed from limit×threshold", compaction_trigger(c) == 75_000)
    check("should_compact true at/over trigger", should_compact(80_000, c))
    check("should_compact false under trigger", not should_compact(10_000, c))
    off = _cfg(context_token_limit=0)
    check("limit 0 disables compaction", compaction_trigger(off) == 0 and not should_compact(10**9, off))


# --- condense orchestration --------------------------------------------------

def test_condense_prune_only_path_no_llm(mp):
    # Make the summarizer explode: if condense reaches it, the test fails loudly.
    mp.setattr(condenser, "_summarize", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not summarize")))
    # A small history that, once pruned, sits under the trigger → summary must be skipped.
    msgs = _history(4, body_chars=4000)
    c = _cfg(context_token_limit=100_000, compaction_threshold=0.75,
             compaction_keep_recent_results=1)
    res = condense(msgs, c, model="m", last_input_tokens=80_000)
    check("condense returns a CondenseResult", isinstance(res, CondenseResult))
    check("condense pruned without summarizing", res.pruned >= 1 and res.summarized == 0)
    check("condense marks changed", res.changed)


def test_condense_summarizes_when_prune_insufficient(mp):
    _patch_summary(mp)
    # Low limit so even after pruning we stay over the trigger → summary fires.
    msgs = _history(12, body_chars=4000)
    c = _cfg(context_token_limit=1_000, compaction_threshold=0.75,
             compaction_keep_recent_turns=3, compaction_keep_recent_results=2)
    res = condense(msgs, c, model="m", last_input_tokens=50_000)
    check("condense summarized when prune not enough", res.summarized == 1)
    check("condense accounts summary usage", res.usage.input_tokens == 5 and res.cost > 0)


# --- loop integration --------------------------------------------------------

def _install_loop_fake():
    """A 3-turn fake: turn 1 reads two big files (prunable) and reports a LARGE input
    size; the next turn's top-of-loop must compact. Turn 2 writes+checks a proof; turn 3
    finishes."""
    tmp = tempfile.TemporaryDirectory()
    proof = str(Path(tmp.name) / "P.lean")
    state = {"n": 0, "tmp": tmp}

    def fake_stream(model, system, messages, tools, model_kwargs=None, streaming=True):
        if "classify the mathematical outcome" in system:
            yield TextDelta("PROVED")
            yield Done(Usage(2, 1), 0.0)
            return
        state["n"] += 1
        if state["n"] == 1:
            yield TextDelta("Reading context.")
            yield ToolCall("read_file", {"path": "a.txt"}); yield _ToolMeta("r1")
            yield ToolCall("read_file", {"path": "b.txt"}); yield _ToolMeta("r2")
            # A deliberately large input size to trip the trigger next turn.
            yield Done(Usage(500_000, 40), 0.01)
        elif state["n"] == 2:
            yield TextDelta("Writing the proof.")
            yield ToolCall("write_file", {"path": proof, "content": "theorem p : True := by trivial\n"})
            yield _ToolMeta("w1")
            yield ToolCall("lean_check", {"path": proof}); yield _ToolMeta("k1")
            yield Done(Usage(200, 40), 0.001)
        else:
            yield TextDelta("Done.")
            yield Done(Usage(50, 10), 0.001)

    agent.stream = fake_stream
    agent._tools.read_file = lambda path, *a, **k: "BIG " + "y" * 6000
    agent._tools.lean_check = lambda path: "OK — no errors, no warnings."
    agent.load_system_prompt = lambda variant, skills=None, workspace=None, namespace=None: "SYS"
    return state


def test_loop_emits_compacted_and_shrinks(mp):
    state = _install_loop_fake()
    # Trigger at 500k×0.75=375k < the 500k the fake reports → compaction fires turn 2.
    cfg = LeaConfig(model="gemini/test", max_turns=None, prompt_variant="default",
                    tools=["read_file", "write_file", "lean_check"],
                    context_token_limit=500_000, compaction_threshold=0.75,
                    compaction_keep_recent_results=1)
    events = list(agent.run_events(cfg, [{"role": "user", "content": "Prove p."}]))
    compacted = [e for e in events if isinstance(e, Compacted)]
    check("loop emitted a Compacted event", len(compacted) == 1)
    check("loop compaction pruned a superseded read_file", compacted and compacted[0].pruned >= 1)
    check("loop compaction reduced the estimate",
          compacted and compacted[0].after_tokens < compacted[0].before_tokens)
    check("loop still finished", any(isinstance(e, Finished) for e in events))


# --- tiny monkeypatch shim (mirrors the other prover test suites) ------------

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


def _run(fn, needs_mp):
    if needs_mp:
        mp = _MonkeyPatch()
        try:
            fn(mp)
        finally:
            mp.undo()
    else:
        fn()


def main():
    test_prune_masks_superseded_keeps_recent()
    test_prune_is_copy_on_write()
    test_prune_ignores_non_prunable_and_tiny()
    test_referenced_files_are_distinct_and_in_order()
    test_trigger_and_disable()
    for fn in (test_summarize_folds_middle_and_preserves_pairing,
               test_summarize_noops_when_too_short,
               test_summarize_degrades_when_summary_empty,
               test_condense_prune_only_path_no_llm,
               test_condense_summarizes_when_prune_insufficient,
               test_loop_emits_compacted_and_shrinks):
        _run(fn, needs_mp=True)

    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All condenser (G1) tests passed.")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
