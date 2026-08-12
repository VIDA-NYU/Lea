"""Typed result envelope + transcript storage (v2.3 item 22).

A child returns a TYPED result, not free prose the parent must re-parse. The
`spawn_subagent` tool_result the model reads is a rendered string, but the parent
loop also gets a `SubagentResult` (drained from a per-activation collector) it turns
into a `SubagentFinished` event — so the adapter can store the child `transcript`
SEPARATELY (never a code_step) and link a promoted candidate back to it by
`result_id`.

These pin:
  * the typed struct carries id / candidate / verdict / stop reason / summary /
    transcript, and render()/to_event() expose the two views;
  * the transcript is captured from the child's Finished and kept OUT of the
    rendered prose (it is data, not display);
  * the collector records on spawn and drains once (the loop drains after the call);
  * result_id is unique per spawn and appears in the render (the audit handle);
  * an errored child still yields a typed result (status None, reason 'error');
  * with no open scope, recording is a harmless no-op.

Run:  uv run python -m tests.subagents.test_subagent_result_envelope
Exits 0 if every check passes, 1 otherwise.
"""

import tempfile
from pathlib import Path

import lea.agent as agent
from lea.config import LeaConfig
from lea.events import CheckResult, Finished, SubagentFinished
from lea.providers import Usage
from lea.runctx import run_context
from lea import subagents
from lea.subagents import SubagentResult, drain_results, spawn_subagent

_FAILURES: list[str] = []

_CHILD_MESSAGES = [
    {"role": "user", "content": "prove it"},
    {"role": "assistant", "content": "let me try trivial"},
    {"role": "assistant", "content": "SECRET_TRANSCRIPT_TOKEN done"},
]


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def _cfg(**over):
    base = dict(model="m", max_turns=None, tools=["read_file", "lean_check", "spawn_subagent"])
    base.update(over)
    return LeaConfig(**base)


def _fake_child(verdict: str | None, finished: bool, calls: list | None = None):
    """A stubbed child run_events. `verdict=None` → no CheckResult; `finished=False` →
    ends without a Finished (an errored child)."""
    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None, depth=0):
        if calls is not None:
            calls.append({"working_dir": working_dir})
        if verdict is not None:
            cand = Path(working_dir) / "C.lean"
            cand.write_text("theorem c : True := by trivial\n")
            yield CheckResult(str(cand), verdict, None if verdict == "ok" else "boom")
        if finished:
            yield Finished("completed", "SECRET_TRANSCRIPT_TOKEN done", 1, session_id or "c",
                           config.model, Usage(input_tokens=1, output_tokens=1), 0.0,
                           {"messages": list(_CHILD_MESSAGES)})
    return fake


# --- the typed struct ----------------------------------------------------------

def test_struct_render_and_to_event():
    r = SubagentResult(
        result_id="premise-search-abc123", subagent_type="premise-search", depth=1,
        candidate_path=".lea/tmp/s/a/C.lean", check_status="ok", check_detail=None,
        stop_reason="completed", summary="found Finset.sum_le_sum",
        transcript=[{"role": "user", "content": "x"}],
    )
    rendered = r.render()
    check("render names the result id (audit handle)", "premise-search-abc123" in rendered)
    check("render reports the candidate", ".lea/tmp/s/a/C.lean" in rendered)
    check("render reports the verdict", "lean_check: ok" in rendered)
    check("render carries the summary", "found Finset.sum_le_sum" in rendered)
    # The transcript is DATA, not display — it must not bleed into the prose.
    check("render does not embed the transcript", "role" not in rendered and "content" not in rendered)

    ev = r.to_event()
    check("to_event yields a SubagentFinished", isinstance(ev, SubagentFinished))
    check("the event carries every typed field",
          ev.result_id == r.result_id and ev.check_status == "ok"
          and ev.stop_reason == "completed" and ev.transcript == r.transcript)


# --- collector + spawn wiring --------------------------------------------------

def test_spawn_records_a_typed_result_with_the_transcript(monkeypatch):
    monkeypatch.setattr(agent, "run_events", _fake_child("ok", True))
    with tempfile.TemporaryDirectory() as d:
        wd = str(Path(d).resolve())
        with run_context(depth=0, config=_cfg(), working_dir=wd, run_key="sess"):
            token = subagents.begin_results_scope()
            out = spawn_subagent({"description": "find", "prompt": "search", "subagent_type": "generalist"})
            drained = drain_results()
            check("drain returns the recorded result", len(drained) == 1)
            check("a second drain is empty (drain clears)", drain_results() == [])
            subagents.end_results_scope(token)

    r = drained[0]
    check("the transcript is captured from the child", [m["role"] for m in r.transcript] ==
          ["user", "assistant", "assistant"])
    # The transcript token appears in the STRUCT but NOT the model-facing string...
    check("the transcript content lives in the struct",
          any("SECRET_TRANSCRIPT_TOKEN" in m["content"] for m in r.transcript))
    check("the rendered tool_result does not dump the transcript",
          out.count("SECRET_TRANSCRIPT_TOKEN") == 0 or "role" not in out)
    check("result_id is the child's agent id and appears in the render", r.result_id in out)
    check("the verdict and candidate are typed", r.check_status == "ok" and r.candidate_path.endswith("C.lean"))


def test_result_ids_are_unique_across_spawns(monkeypatch):
    monkeypatch.setattr(agent, "run_events", _fake_child("ok", True))
    ids = []
    with tempfile.TemporaryDirectory() as d:
        with run_context(depth=0, config=_cfg(), working_dir=str(Path(d).resolve()), run_key="sess"):
            token = subagents.begin_results_scope()
            for _ in range(2):
                spawn_subagent({"description": "x", "prompt": "y"})
            for r in drain_results():
                ids.append(r.result_id)
            subagents.end_results_scope(token)
    check("two spawns recorded two results", len(ids) == 2)
    check("result ids are unique", len(set(ids)) == 2)


def test_errored_child_still_yields_a_typed_result(monkeypatch):
    # No CheckResult, no Finished — the child died. The parent still gets a typed
    # envelope (status None, reason 'error') instead of a crash or bare prose.
    monkeypatch.setattr(agent, "run_events", _fake_child(None, False))
    with tempfile.TemporaryDirectory() as d:
        with run_context(depth=0, config=_cfg(), working_dir=str(Path(d).resolve()), run_key="s"):
            token = subagents.begin_results_scope()
            spawn_subagent({"description": "x", "prompt": "y"})
            drained = drain_results()
            subagents.end_results_scope(token)
    r = drained[0]
    check("an errored child records a result", len(drained) == 1)
    check("errored result has no verdict", r.check_status is None and r.candidate_path is None)
    check("errored result reason is 'error'", r.stop_reason == "error")
    check("errored result has an empty transcript", r.transcript == [])


def test_no_open_scope_is_a_harmless_noop(monkeypatch):
    monkeypatch.setattr(agent, "run_events", _fake_child("ok", True))
    with tempfile.TemporaryDirectory() as d:
        with run_context(depth=0, config=_cfg(), working_dir=str(Path(d).resolve()), run_key="s"):
            # No begin_results_scope(): recording finds no collector.
            out = spawn_subagent({"description": "x", "prompt": "y"})
            check("spawn still returns a rendered result without a scope", out.startswith("[subagent:"))
            check("drain with no scope is empty", drain_results() == [])


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
    print("subagent typed-result envelope tests (v2.3 item 22):")
    test_struct_render_and_to_event()
    for fn in (
        test_spawn_records_a_typed_result_with_the_transcript,
        test_result_ids_are_unique_across_spawns,
        test_errored_child_still_yields_a_typed_result,
        test_no_open_scope_is_a_harmless_noop,
    ):
        mp = _MonkeyPatch()
        try:
            fn(mp)
        finally:
            mp.undo()

    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All subagent result-envelope item-22 tests passed.")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
