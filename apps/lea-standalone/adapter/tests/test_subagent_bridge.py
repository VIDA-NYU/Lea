"""Bridge sub-agent handling (v2.3 item 24): enablement + SubagentFinished → child session.

These pin the adapter side of sub-agents, without a live prover run:

  * `_with_subagents` adds the opt-in `spawn_subagent` onto the coordinator's default
    toolset (and nothing else changes);
  * `_text_from_content` flattens both a plain string and a provider block list to prose;
  * `_materialize_subagent` creates a CHILD session (parent_id / role / spawned_at_turn),
    replays the child transcript into the child's own timeline, and stores the candidate
    as a code_step so the child's DERIVED status is its lean_check verdict;
  * a missing/unreadable candidate is tolerated (child still lands, just unchecked);
  * the child transcript never lands on the PARENT's timeline.
"""

from pathlib import Path
from queue import Queue

from lea.interface import (
    AssistantTextDelta,
    CheckResult,
    Compacted,
    FileChanged,
    Finished,
    SubagentFinished,
    SubagentProgress,
    SubagentStarted,
    ToolCalled,
    TurnStarted,
)
from lea.providers import Usage

from app import bridge, db, store
from app.config import LeaConfig


def _ok_recheck(monkeypatch):
    """Stub the canonical-path re-verification so promotion tests need no Lean env.

    BOTH gates, not just the compile. Promotion now also runs SafeVerify (AUDIT C5),
    and stubbing only `_lean_check_file` left the real auditor in the path: on a
    machine where SafeVerify is actually built, `_safeverify_file` shells out for two
    Lean compiles plus a kernel replay, and the suite appears to hang rather than
    fail. It passes wherever SafeVerify is absent, which is what hid it — a test whose
    isolation depends on a tool NOT being installed."""
    monkeypatch.setattr(bridge, "_lean_check_file", lambda path: CheckResult(path, "ok", None))
    monkeypatch.setattr(bridge, "_safeverify_file", lambda path: "ok")


def _drain(q: Queue) -> list[dict]:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


_OK_CANDIDATE = "import Mathlib\ntheorem t : True := by trivial\n"


def _finished(candidate_path=None, check_status="ok", check_detail=None,
              summary="parity / infinite descent works", transcript=None):
    return SubagentFinished(
        result_id="proof-candidate-abc123",
        subagent_type="proof-candidate",
        candidate_path=candidate_path,
        check_status=check_status,
        check_detail=check_detail,
        stop_reason="completed",
        summary=summary,
        transcript=transcript if transcript is not None else [
            {"role": "user", "content": "Prove it via infinite descent."},
            {"role": "assistant", "content": "Trying descent…"},
            {"role": "assistant", "content": [{"type": "text", "text": "It compiles."},
                                              {"type": "tool_call", "name": "lean_check"}]},
        ],
    )


# --- enablement ---------------------------------------------------------------

def test_with_subagents_adds_spawn_to_default_toolset():
    """v2.5 — DELIBERATELY changed from asserting a frozen allowlist.

    This used to assert `out.tools` contained the built-ins plus the two opt-ins, which is
    exactly what made every MCP and HTTP tool unreachable: the list was resolved in the
    adapter, before the run, so it could not contain tools that register once the run
    starts. The coordinator now asks for "the default set PLUS these", and the default is
    resolved inside the run where everything has registered.
    """
    from lea.registry import build_toolset

    cfg = LeaConfig(model="gemini/test", max_turns=3)
    # F2: also returns why the user's overrides didn't apply, if they didn't — a
    # readable overrides file (this case) means no error.
    out, override_error = bridge._with_subagents(cfg)
    assert override_error is None
    assert out.tools is None, "a frozen list cannot include run-time tools"
    assert out.extra_tools == ["spawn_subagent", "safe_verify"]

    # Resolved the way the run resolves it, the built-ins and both opt-ins are present.
    names = [s["name"] for s in build_toolset(out.tools, out.extra_tools)[0]]
    assert {
        "read_file", "write_file", "edit_file", "lean_check", "bash",
        "search_mathlib", "suggest_imports",
    } <= set(names)
    assert names[-2:] == ["spawn_subagent", "safe_verify"]
    # nothing else about the config changed
    assert out.model == cfg.model and out.max_turns == cfg.max_turns


# --- text flattening ----------------------------------------------------------

def test_text_from_content_handles_string_and_blocks():
    assert bridge._text_from_content("hello") == "hello"
    blocks = [{"type": "text", "text": "line one"}, {"type": "tool_call", "name": "x"},
              {"type": "text", "text": "line two"}]
    assert bridge._text_from_content(blocks) == "line one\nline two"
    assert bridge._text_from_content(None) == ""
    assert bridge._text_from_content([{"type": "tool_call", "name": "x"}]) == ""


def test_child_title_prefers_task_over_error_summary():
    # A max-turns / errored child's summary is an error nudge — the title must come from
    # the delegated task instead (regression from the first live spawn).
    ev = _finished(
        summary="Error: max turns reached without completing the proof",
        transcript=[
            {"role": "user", "content": "Search block imprimitivity lemmas\n\nYou are a scout."},
            {"role": "assistant", "content": "searching Mathlib…"},
        ],
    )
    assert bridge._subagent_child_title(ev) == "Search block imprimitivity lemmas"


# --- materialization ----------------------------------------------------------

def test_materialize_creates_child_with_tree_fields(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    parent = store.create_session("Prove sqrt2 irrational")
    ev = _finished()
    child = bridge._materialize_subagent(
        ev, parent_session_id=parent["id"], project_id=None, turn=4, repo=tmp_path,
    )
    assert child["parent_id"] == parent["id"]
    assert child["role"] == "proof-candidate"
    assert child["spawned_at_turn"] == 4
    # title comes from the delegated task (first user message), not the summary
    assert child["title"] == "Prove it via infinite descent."


def test_materialize_replays_transcript_as_child_messages(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    parent = store.create_session("root")
    child = bridge._materialize_subagent(
        _finished(), parent_session_id=parent["id"], project_id=None, turn=1, repo=tmp_path,
    )
    detail = store.session_detail(child["id"])
    contents = [m["content"] for m in detail["messages"]]
    assert "Prove it via infinite descent." in contents   # the user task
    assert "Trying descent…" in contents                  # an assistant step
    assert "It compiles." in contents                     # text pulled from a block list
    # The PARENT timeline is untouched — the child transcript is the child's, not a
    # coordinator code_step.
    parent_detail = store.session_detail(parent["id"])
    assert parent_detail["messages"] == []
    assert parent_detail["code_steps"] == []


def test_materialize_stores_candidate_and_derives_status(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    parent = store.create_session("root")
    # the candidate lives in the child's scratch under the run's working tree
    scratch = tmp_path / ".lea" / "tmp" / "run" / "agent"
    scratch.mkdir(parents=True)
    (scratch / "Sqrt2.lean").write_text(_OK_CANDIDATE)
    ev = _finished(candidate_path=".lea/tmp/run/agent/Sqrt2.lean", check_status="ok")
    child = bridge._materialize_subagent(
        ev, parent_session_id=parent["id"], project_id=None, turn=2, repo=tmp_path,
    )
    detail = store.session_detail(child["id"])
    assert len(detail["code_steps"]) == 1
    step = detail["code_steps"][0]
    assert step["code"] == _OK_CANDIDATE
    assert step["check_status"] == "ok"
    # the child's derived session status reflects the candidate's verdict, not 'empty'
    assert detail["status"] in ("ok", "proved", "defined")


def test_materialize_tolerates_missing_candidate(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    parent = store.create_session("root")
    ev = _finished(candidate_path=".lea/tmp/run/agent/gone.lean", check_status="error",
                   check_detail="boom")
    child = bridge._materialize_subagent(
        ev, parent_session_id=parent["id"], project_id=None, turn=1, repo=tmp_path,
    )
    detail = store.session_detail(child["id"])
    assert detail["code_steps"] == []          # unreadable candidate → no code step
    assert len(detail["messages"]) >= 1        # but the child still landed with its transcript


# --- D1: spawn-started visibility ---------------------------------------------

def _started(result_id="proof-candidate-abc123", subagent_type="proof-candidate",
             description="Prove it via infinite descent"):
    return SubagentStarted(result_id=result_id, subagent_type=subagent_type,
                           description=description)


def test_start_subagent_creates_a_running_child(tmp_path, monkeypatch):
    # D1: at spawn, the child is a RUNNING session — parented, with an active run row so
    # its derived status is 'running' BEFORE any transcript or candidate exists.
    _fresh_db(tmp_path, monkeypatch)
    parent = store.create_session("Prove sqrt2 irrational")
    cfg = LeaConfig(model="gemini/test", max_turns=3)
    child, child_run_id = bridge._start_subagent(
        _started(), parent_session_id=parent["id"], project_id=None, turn=2, cfg=cfg,
    )
    assert child["parent_id"] == parent["id"]
    assert child["role"] == "proof-candidate"
    assert child["spawned_at_turn"] == 2
    assert child["title"] == "Prove it via infinite descent"  # from the delegated task
    kids = store.list_child_sessions(parent["id"])
    assert len(kids) == 1
    # no code yet + an active run → derived status 'running' (the 'exploring…' badge)
    assert kids[0]["status"] == "running"
    assert kids[0]["active_run_count"] == 1
    assert bool(child_run_id)


def test_finalize_started_fills_the_same_child_and_retires_the_run(tmp_path, monkeypatch):
    # The started child is FILLED IN — not duplicated — and its run retires so its
    # derived status flips from 'running' to the candidate's verdict.
    _fresh_db(tmp_path, monkeypatch)
    parent = store.create_session("root")
    cfg = LeaConfig(model="gemini/test", max_turns=3)
    child, child_run_id = bridge._start_subagent(
        _started(), parent_session_id=parent["id"], project_id=None, turn=1, cfg=cfg,
    )
    scratch = tmp_path / ".lea" / "tmp" / "run" / "agent"
    scratch.mkdir(parents=True)
    (scratch / "Sqrt2.lean").write_text(_OK_CANDIDATE)
    ev = _finished(candidate_path=".lea/tmp/run/agent/Sqrt2.lean", check_status="ok",
                   transcript=[{"role": "user", "content": "prove it"},
                               {"role": "assistant", "content": "done, it compiles"}])
    bridge._finalize_started_subagent(child["id"], child_run_id, ev, turn=1, repo=tmp_path)

    kids = store.list_child_sessions(parent["id"])
    assert len(kids) == 1                       # SAME child, not a second one
    assert kids[0]["active_run_count"] == 0     # run retired
    detail = store.session_detail(child["id"])
    assert detail["status"] in ("ok", "proved", "defined")   # verdict now rules
    assert len(detail["code_steps"]) == 1 and detail["code_steps"][0]["check_status"] == "ok"
    assert any("done, it compiles" in m["content"] for m in detail["messages"])


def test_run_lea_started_then_finished_is_one_child(tmp_path, monkeypatch):
    # The full loop: SubagentStarted materializes a running child; SubagentFinished
    # (same result_id) fills THAT child in — exactly one child, and the SSE stream
    # carries both a subagent_started and a subagent_finished for it.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Prove sqrt2 irrational")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    cfg = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)
    queue: Queue = Queue()
    ctx = bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"],
        task="Prove sqrt2 irrational", config=cfg, events=queue,
    )

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None):
        scratch = Path(working_dir) / ".lea" / "tmp" / "run" / "agent"
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "Sqrt2.lean").write_text(_OK_CANDIDATE)
        yield TurnStarted(1)
        yield SubagentStarted(result_id="pc-42", subagent_type="proof-candidate",
                              description="Prove sqrt2 irrational via descent")
        yield SubagentFinished(
            result_id="pc-42", subagent_type="proof-candidate",
            candidate_path=".lea/tmp/run/agent/Sqrt2.lean", check_status="ok", check_detail=None,
            stop_reason="completed", summary="descent compiles",
            transcript=[{"role": "user", "content": "prove it"},
                        {"role": "assistant", "content": "done, it compiles"}],
        )
        yield Finished("completed", "A candidate compiled.", 1, ctx.session_id,
                       "gemini/test", Usage(input_tokens=5, output_tokens=3), 0.0, {})

    _ok_recheck(monkeypatch)
    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)

    kids = store.list_child_sessions(session["id"])
    assert len(kids) == 1                        # started + finished collapsed to ONE child
    assert kids[0]["active_run_count"] == 0      # child run retired on finish
    cdetail = store.session_detail(kids[0]["id"])
    assert cdetail["code_steps"] and cdetail["code_steps"][0]["check_status"] == "ok"

    events = _drain(queue)
    started_evs = [e for e in events if e["type"] == "subagent_started"]
    finished_evs = [e for e in events if e["type"] == "subagent_finished"]
    assert len(started_evs) == 1 and len(finished_evs) == 1
    assert started_evs[0]["payload"]["child_id"] == kids[0]["id"]
    assert started_evs[0]["payload"]["result_id"] == "pc-42"
    assert finished_evs[0]["payload"]["child_id"] == kids[0]["id"]


def test_run_lea_retires_a_child_that_started_but_never_finished(tmp_path, monkeypatch):
    # If the coordinator ends between a child's start and finish (interrupt/crash), the
    # child run must not linger 'running' forever — the finally retires it.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("root")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    cfg = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)
    ctx = bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"], task="root", config=cfg, events=Queue(),
    )

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None):
        yield TurnStarted(1)
        yield SubagentStarted(result_id="pc-orphan", subagent_type="proof-candidate",
                              description="a child that never returns")
        # coordinator finishes WITHOUT the child's SubagentFinished
        yield Finished("completed", "done", 1, ctx.session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)

    kids = store.list_child_sessions(session["id"])
    assert len(kids) == 1
    assert kids[0]["active_run_count"] == 0       # orphan run retired, not eternally 'running'


# --- sub-agent FAILURE surfacing ----------------------------------------------

def _errored(summary="error — litellm.AuthenticationError: azure_ai key not configured"):
    return SubagentFinished(
        result_id="pc-err", subagent_type="proof-candidate",
        candidate_path=None, check_status=None, check_detail=None,
        stop_reason="error", summary=summary, transcript=[],
    )


def test_subagent_error_only_for_a_child_that_could_not_run():
    # stop_reason 'error' (the child raised) → surface the real message…
    assert "AuthenticationError" in (bridge._subagent_error(_errored()) or "")
    # …but a child whose CANDIDATE merely has Lean errors is a normal outcome, not this.
    assert bridge._subagent_error(_finished(check_status="error", check_detail="unknown id")) is None
    assert bridge._subagent_error(_finished()) is None   # a clean child


def test_run_lea_surfaces_a_failed_child(tmp_path, monkeypatch):
    # A child that could not run: its run is marked failed, its error is persisted as the
    # child's message (so its own session shows it), and the subagent_finished SSE carries
    # the error so the spawn card can render a red "failed" child.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Prove it")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    cfg = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)
    queue: Queue = Queue()
    ctx = bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"], task="Prove it", config=cfg, events=queue,
    )

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None):
        yield TurnStarted(1)
        yield SubagentStarted(result_id="pc-err", subagent_type="proof-candidate", description="prove L1")
        yield _errored()
        yield Finished("completed", "I'll search directly instead.", 1, ctx.session_id,
                       "gemini/test", Usage(input_tokens=1, output_tokens=1), 0.0, {})

    _ok_recheck(monkeypatch)
    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)

    kids = store.list_child_sessions(session["id"])
    assert len(kids) == 1
    child = kids[0]
    # the child's own session carries the error (not empty), and its run is failed
    detail = store.session_detail(child["id"])
    assert any("AuthenticationError" in m["content"] for m in detail["messages"])
    child_run = next(r for r in detail["runs"])
    assert child_run["status"] == "error"
    # the SSE carries the error for the spawn card
    events = _drain(queue)
    fin = [e for e in events if e["type"] == "subagent_finished"]
    assert len(fin) == 1
    assert "AuthenticationError" in (fin[0]["payload"]["error"] or "")


# --- E1: live child streaming + per-child stop --------------------------------

def test_subagent_progress_payload_compacts_inner_events():
    p = bridge._subagent_progress_payload("cid", "rid", AssistantTextDelta("hi"))
    assert p == {"child_id": "cid", "result_id": "rid", "kind": "text", "text": "hi"}
    assert bridge._subagent_progress_payload("cid", "rid", ToolCalled("lean_check", {}))["kind"] == "tool"
    assert bridge._subagent_progress_payload("cid", "rid", CheckResult("f", "ok", None))["kind"] == "check"
    assert bridge._subagent_progress_payload("cid", "rid", TurnStarted(3))["turn"] == 3
    # a FileChanged has nothing to show live → dropped
    assert bridge._subagent_progress_payload("cid", "rid", FileChanged("x.lean")) is None


def test_run_lea_streams_child_progress_and_registers_stop(tmp_path, monkeypatch):
    # A child that emits its OWN events (E1): the bridge streams each as a
    # subagent_progress SSE, registers the child for stop while running, and clears it
    # on finish.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Prove it")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    cfg = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)
    queue: Queue = Queue()
    ctx = bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"], task="Prove it", config=cfg, events=queue,
    )
    seen_registered = {}

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None):
        scratch = Path(working_dir) / ".lea" / "tmp" / "run" / "agent"
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "C.lean").write_text(_OK_CANDIDATE)
        yield TurnStarted(1)
        yield SubagentStarted(result_id="pc-9", subagent_type="proof-candidate",
                              description="prove a lemma")
        # while the child runs, its session must be addressable for a stop
        yield SubagentProgress("pc-9", AssistantTextDelta("thinking"))
        yield SubagentProgress("pc-9", ToolCalled("lean_check", {"path": "C.lean"}))
        yield SubagentProgress("pc-9", CheckResult(str(scratch / "C.lean"), "ok", None))
        # snapshot the stop registry mid-child
        seen_registered["during"] = dict(bridge._child_session_to_result)
        yield SubagentFinished(
            result_id="pc-9", subagent_type="proof-candidate",
            candidate_path=".lea/tmp/run/agent/C.lean", check_status="ok", check_detail=None,
            stop_reason="completed", summary="done", transcript=[{"role": "user", "content": "go"}],
        )
        yield Finished("completed", "done", 1, ctx.session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})

    _ok_recheck(monkeypatch)
    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)

    events = _drain(queue)
    progress = [e for e in events if e["type"] == "subagent_progress"]
    kinds = [e["payload"]["kind"] for e in progress]
    assert "text" in kinds and "tool" in kinds and "check" in kinds
    assert all(e["payload"]["result_id"] == "pc-9" for e in progress)
    # the child was registered for stop while running...
    kids = store.list_child_sessions(session["id"])
    assert len(kids) == 1
    assert seen_registered["during"].get(kids[0]["id"]) == "pc-9"
    # ...and cleared once it finished + the run ended
    assert kids[0]["id"] not in bridge._child_session_to_result


def test_forward_to_child_broker_translates_events():
    # E1 first-class: a child's inner events are re-emitted onto its own run broker in the
    # normal SSE vocab, so its session view renders them with the same listeners as any run.
    from app import runbroker
    b = runbroker.RunBroker("child-x")
    started = {"child_id": "cs-1", "run_id": "child-x"}
    bridge._forward_to_child_broker(b, AssistantTextDelta("hi from child"), started)
    bridge._forward_to_child_broker(b, ToolCalled("lean_check", {"path": "C.lean"}), started)
    bridge._forward_to_child_broker(b, CheckResult("C.lean", "ok", None), started)
    items = [(e["type"], e["payload"]) for e in b.events_after(0)]
    assert any(t == "assistant_delta" and p.get("text") == "hi from child" for t, p in items)
    assert any(t == "status" and p.get("status") == "tool_call" for t, p in items)
    assert any(t == "status" and p.get("status") == "lean_check" and p.get("check_status") == "ok"
               for t, p in items)


def test_child_narration_commits_a_message_per_turn():
    # The readability fix: each turn's narration is COMMITTED as a discrete message (on the
    # next turn / a tool call), so turns don't run together into one blob.
    from app import runbroker
    b = runbroker.RunBroker("child-y")
    started = {"child_id": "cs-2", "run_id": "child-y"}
    # turn 1 narration, then a tool call flushes it as a message
    bridge._forward_to_child_broker(b, AssistantTextDelta("First I examine the files."), started)
    bridge._forward_to_child_broker(b, ToolCalled("read_file", {}), started)
    # turn 2 narration, then a new turn flushes it as a SECOND message
    bridge._forward_to_child_broker(b, AssistantTextDelta("Now I write the proof."), started)
    bridge._forward_to_child_broker(b, TurnStarted(2), started)
    msgs = [p for t, p in ((e["type"], e["payload"]) for e in b.events_after(0)) if t == "message"]
    assert len(msgs) == 2, "each turn committed its own message"
    assert msgs[0]["content"] == "First I examine the files."
    assert msgs[1]["content"] == "Now I write the proof."
    assert msgs[0]["id"] != msgs[1]["id"]           # distinct ids so the frontend keeps both
    assert all(m["role"] == "assistant" and m["session_id"] == "cs-2" for m in msgs)


def test_run_lea_gives_child_a_first_class_broker_stream(tmp_path, monkeypatch):
    # The child's OWN run stream: a broker keyed by its run_id is created at spawn, fed the
    # child's forwarded events live, then closed (done) + dropped on finish — so a
    # sub-agent's session view attaches to /api/runs/<child_run_id>/events and streams live.
    import sqlite3
    from app import runbroker
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Prove it")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    cfg = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)
    ctx = bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"], task="Prove it", config=cfg, events=Queue(),
    )
    captured = {}

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None):
        scratch = Path(working_dir) / ".lea" / "tmp" / "run" / "agent"
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "C.lean").write_text(_OK_CANDIDATE)
        yield TurnStarted(1)
        yield SubagentStarted(result_id="pc-1", subagent_type="proof-candidate", description="prove L1")
        yield SubagentProgress("pc-1", AssistantTextDelta("child is thinking"))
        yield SubagentProgress("pc-1", ToolCalled("lean_check", {"path": "C.lean"}))
        # mid-run: the child's broker exists and holds the forwarded events
        with sqlite3.connect(str(db.DB_PATH)) as conn:
            row = conn.execute(
                "select r.id from runs r join sessions s on s.id = r.session_id where s.parent_id = ?",
                (session["id"],),
            ).fetchone()
        crid = row[0] if row else None
        b = runbroker.get(crid) if crid else None
        captured["run_id"] = crid
        captured["events"] = [(e["type"], e["payload"]) for e in b.events_after(0)] if b else None
        yield SubagentFinished(
            result_id="pc-1", subagent_type="proof-candidate",
            candidate_path=".lea/tmp/run/agent/C.lean", check_status="ok", check_detail=None,
            stop_reason="completed", summary="done", transcript=[{"role": "user", "content": "go"}],
        )
        yield Finished("completed", "done", 1, ctx.session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})

    _ok_recheck(monkeypatch)
    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)

    assert captured["run_id"], "a child run was created"
    assert captured["events"] is not None, "the child's broker existed while it ran"
    types = captured["events"]
    assert any(t == "assistant_delta" and p.get("text") == "child is thinking" for t, p in types)
    assert any(t == "status" and p.get("status") == "tool_call" for t, p in types)
    # after finish the broker is closed + dropped (no leak, no phantom stream)
    assert runbroker.get(captured["run_id"]) is None


def test_request_subagent_stop_translates_session_to_child(monkeypatch):
    calls = {}

    def _fake_stop(rid):
        calls["rid"] = rid
        return True

    monkeypatch.setattr(bridge, "request_child_stop", _fake_stop)
    bridge._child_session_to_result.clear()
    # unknown session → False, no translation
    assert bridge.request_subagent_stop("nope") is False
    assert "rid" not in calls
    # a mapped child session → forwards its result_id to the prover
    bridge._child_session_to_result["sess-1"] = "pc-42"
    assert bridge.request_subagent_stop("sess-1") is True
    assert calls["rid"] == "pc-42"
    bridge._child_session_to_result.clear()


# --- end-to-end through the real bridge loop ----------------------------------

def test_run_lea_materializes_child_and_emits_sse(tmp_path, monkeypatch):
    # Drive the real run_lea event loop with a fake prover that delegates to a child.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Prove sqrt2 irrational")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    cfg = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)
    queue: Queue = Queue()
    ctx = bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"],
        task="Prove sqrt2 irrational", config=cfg, events=queue,
    )

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None):
        # the child writes its candidate into its scratch under the run's working tree
        scratch = Path(working_dir) / ".lea" / "tmp" / "run" / "agent"
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "Sqrt2.lean").write_text(_OK_CANDIDATE)
        yield TurnStarted(1)
        yield AssistantTextDelta("I'll delegate a candidate.")
        yield SubagentFinished(
            result_id="proof-candidate-xyz", subagent_type="proof-candidate",
            candidate_path=".lea/tmp/run/agent/Sqrt2.lean", check_status="ok", check_detail=None,
            stop_reason="completed", summary="parity approach compiles",
            transcript=[{"role": "user", "content": "prove it"},
                        {"role": "assistant", "content": "done, it compiles"}],
        )
        yield Finished("completed", "A candidate compiled.", 1, ctx.session_id,
                       "gemini/test", Usage(input_tokens=5, output_tokens=3), 0.0, {})

    _ok_recheck(monkeypatch)  # the coordinator delegated + wrote nothing → promotion fires
    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)

    # a child session was created under the coordinator, with its verdict-derived status
    kids = store.list_child_sessions(session["id"])
    assert len(kids) == 1
    child = kids[0]
    assert child["parent_id"] == session["id"] and child["role"] == "proof-candidate"
    cdetail = store.session_detail(child["id"])
    assert cdetail["code_steps"] and cdetail["code_steps"][0]["check_status"] == "ok"
    assert any("done, it compiles" in m["content"] for m in cdetail["messages"])

    # collation promoted the winning candidate onto the PARENT session (item 25): the
    # coordinator delegated and wrote no proof itself, so the winner fills the gap.
    pdetail = store.session_detail(session["id"])
    promoted = [s for s in pdetail["code_steps"] if s["path"] == "Lea/Misc/Sqrt2.lean"]
    assert len(promoted) == 1 and promoted[0]["check_status"] == "ok"
    assert pdetail["status"] == "proved"                       # derived from the promoted proof

    # the SSE stream carried a subagent_finished event + a promoted status
    events = _drain(queue)
    subagent_events = [e for e in events if e["type"] == "subagent_finished"]
    assert len(subagent_events) == 1
    assert subagent_events[0]["payload"]["child_id"] == child["id"]
    assert subagent_events[0]["payload"]["parent_id"] == session["id"]
    assert subagent_events[0]["payload"]["check_status"] == "ok"
    assert any(e["type"] == "status" and e["payload"].get("status") == "promoted" for e in events)


def test_promote_winner_writes_canonical_reverifies_and_links_provenance(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    _ok_recheck(monkeypatch)
    session = store.create_session("root")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    repo = tmp_path / "repo"
    scratch = repo / ".lea" / "tmp" / "run" / "agent"
    scratch.mkdir(parents=True)
    (scratch / "Win.lean").write_text(_OK_CANDIDATE)
    ev = _finished(candidate_path=".lea/tmp/run/agent/Win.lean", check_status="ok")

    step = bridge._promote_winner(
        [ev], session_id=session["id"], run_id=run["id"],
        repo=repo, namespace=None, turn=2, events=Queue(),
    )
    assert step is not None
    # winner bytes written to the canonical proofs dir (loose → Lea/Misc)
    assert (repo / "Lea" / "Misc" / "Win.lean").read_text() == _OK_CANDIDATE
    assert step["path"] == "Lea/Misc/Win.lean" and step["check_status"] == "ok"
    # recorded on the PARENT session, deriving it 'proved'
    detail = store.session_detail(session["id"])
    assert detail["status"] == "proved"
    # provenance links the promoted step back to the child result (item 25)
    with sqlite3.connect(tmp_path / "test.sqlite3") as conn:
        data = conn.execute(
            "select data from timeline where path = 'Lea/Misc/Win.lean'"
        ).fetchone()[0]
    assert ev.result_id in data and "promoted_from" in data


def test_promote_winner_skips_when_no_clean_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    _ok_recheck(monkeypatch)  # recheck would pass, but an error candidate isn't promotable
    session = store.create_session("root")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    ev = _finished(candidate_path=".lea/tmp/run/agent/Bad.lean", check_status="error",
                   check_detail="boom")
    step = bridge._promote_winner(
        [ev], session_id=session["id"], run_id=run["id"],
        repo=tmp_path / "repo", namespace=None, turn=1, events=Queue(),
    )
    assert step is None
    assert store.session_detail(session["id"])["code_steps"] == []


def test_promote_winner_skips_when_reverify_fails(tmp_path, monkeypatch):
    # the child's candidate compiled in scratch, but it does NOT re-verify at the
    # canonical path → nothing is promoted (never record an unchecked 'ok').
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    monkeypatch.setattr(bridge, "_lean_check_file", lambda p: CheckResult(p, "error", "moved-module boom"))
    session = store.create_session("root")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    repo = tmp_path / "repo"
    scratch = repo / ".lea" / "tmp" / "run" / "agent"
    scratch.mkdir(parents=True)
    (scratch / "Win.lean").write_text(_OK_CANDIDATE)
    ev = _finished(candidate_path=".lea/tmp/run/agent/Win.lean", check_status="ok")
    events: Queue = Queue()
    step = bridge._promote_winner(
        [ev], session_id=session["id"], run_id=run["id"],
        repo=repo, namespace=None, turn=1, events=events,
    )
    assert step is None
    assert store.session_detail(session["id"])["code_steps"] == []
    # D1 (v2.4): refusing to promote is correct — recording an unchecked "ok" would be
    # worse — but it used to be a `logger.warning`, so the user watched N children run
    # for minutes and was shown no proof, no promotion, and no reason. The refusal has
    # to SAY so, and say which candidate and why.
    diags = store.session_detail(session["id"])["diagnostics"]
    assert [d["code"] for d in diags] == ["subagent.promotion_rejected"]
    assert ev.result_id in diags[0]["message"]
    assert "moved-module boom" in diags[0]["message"]
    assert any(e["type"] == "diagnostic" for e in _drain(events))


def test_delegated_task_is_readable_while_the_child_is_still_running(tmp_path, monkeypatch):
    """The task has to be there at SPAWN, not at finish.

    A child's transcript only replays on SubagentFinished, so a running child's session
    was completely empty — you could watch it work for minutes with no way to see what
    it had been asked to do, and therefore no way to judge whether the coordinator had
    delegated the right thing while there was still time to stop it. Its title is three
    words; the task is the only judgeable content it has.
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    parent = store.create_session("Prove sqrt2 irrational")
    cfg = LeaConfig(model="gemini/test", max_turns=3)
    task = ("Prove sqrt2 is irrational\n\nUse infinite descent. State the theorem exactly "
            "as `theorem sqrt2_irrational : Irrational (Real.sqrt 2)`.")
    ev = SubagentStarted(result_id="pc-9", subagent_type="proof-candidate",
                         description="Prove sqrt2 is irrational", task=task)

    child, _run_id = bridge._start_subagent(
        ev, parent_session_id=parent["id"], project_id=None, turn=1, cfg=cfg)

    # Readable immediately — before the child has produced anything at all.
    detail = store.session_detail(child["id"])
    assert [m["content"] for m in detail["messages"]] == [task]
    assert detail["messages"][0]["role"] == "user"
    # And on the session LIST, so the coordinator's spawn box can show it without a
    # second fetch — including for a child that is still running.
    row = next(s for s in store.list_child_sessions(parent["id"]) if s["id"] == child["id"])
    assert row["task"] == task
    assert row["final_summary"] is None  # nothing finished yet


def test_the_delegated_task_is_not_stored_twice_on_finish(tmp_path, monkeypatch):
    # The transcript's first user message IS the task we already recorded at spawn.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    parent = store.create_session("root")
    cfg = LeaConfig(model="gemini/test", max_turns=3)
    task = "Prove it\n\nvia descent"
    child, child_run_id = bridge._start_subagent(
        SubagentStarted(result_id="pc-1", subagent_type="proof-candidate",
                        description="Prove it", task=task),
        parent_session_id=parent["id"], project_id=None, turn=1, cfg=cfg)

    ev = _finished(transcript=[{"role": "user", "content": task},
                               {"role": "assistant", "content": "done, it compiles"}])
    bridge._finalize_started_subagent(child["id"], child_run_id, ev, turn=1, repo=tmp_path)

    contents = [m["content"] for m in store.session_detail(child["id"])["messages"]]
    assert contents.count(task) == 1, "the delegated prompt must not open the thread twice"
    assert "done, it compiles" in contents


def test_promote_winner_reports_a_write_failure(tmp_path, monkeypatch):
    # D2: `collation.promote` raising was a bare `return None` with no log at all, so
    # "could not write the winner" was indistinguishable from "no child produced one".
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    monkeypatch.setattr(
        bridge.collation, "promote",
        lambda winner, canonical: (_ for _ in ()).throw(ValueError("destination is a directory")),
    )
    session = store.create_session("root")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    repo = tmp_path / "repo"
    scratch = repo / ".lea" / "tmp" / "run" / "agent"
    scratch.mkdir(parents=True)
    (scratch / "Win.lean").write_text(_OK_CANDIDATE)
    ev = _finished(candidate_path=".lea/tmp/run/agent/Win.lean", check_status="ok")
    step = bridge._promote_winner(
        [ev], session_id=session["id"], run_id=run["id"],
        repo=repo, namespace=None, turn=1, events=Queue(),
    )
    assert step is None
    diags = store.session_detail(session["id"])["diagnostics"]
    assert [d["code"] for d in diags] == ["subagent.promotion_failed"]
    assert "destination is a directory" in diags[0]["message"]


def test_run_lea_does_not_promote_over_a_clean_coordinator_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    _ok_recheck(monkeypatch)
    session = store.create_session("Prove it")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    cfg = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)
    ctx = bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"], task="Prove it", config=cfg, events=Queue(),
    )

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None):
        proof = Path(working_dir) / "Lea" / "Misc" / "Main.lean"
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_text(_OK_CANDIDATE)
        scratch = Path(working_dir) / ".lea" / "tmp" / "run" / "agent"
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "Cand.lean").write_text(_OK_CANDIDATE)
        yield TurnStarted(1)
        yield ToolCalled("write_file", {"path": str(proof)})
        yield FileChanged(str(proof))
        yield ToolCalled("lean_check", {"path": str(proof)})
        yield CheckResult(str(proof), "ok", None)   # coordinator produced a clean proof
        yield SubagentFinished(
            result_id="pc-1", subagent_type="proof-candidate",
            candidate_path=".lea/tmp/run/agent/Cand.lean", check_status="ok", check_detail=None,
            stop_reason="completed", summary="also compiles", transcript=[],
        )
        yield Finished("completed", "Proved it.", 1, ctx.session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)

    # only the coordinator's own proof — the child candidate is NOT promoted over it
    detail = store.session_detail(session["id"])
    assert [s["path"] for s in detail["code_steps"]] == ["Lea/Misc/Main.lean"]


def test_autonomous_run_does_not_enable_subagents(tmp_path, monkeypatch):
    # Overleaf/autonomous runs stay single-agent — spawn_subagent is not added.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("auto")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    cfg = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)
    seen_tools = {}
    ctx = bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"], task="auto",
        config=cfg, events=Queue(), autonomous=True,
    )

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None):
        seen_tools["tools"] = config.tools
        yield Finished("completed", "done", 1, ctx.session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)
    # autonomous keeps the prover default (None) — no spawn_subagent forced on
    assert seen_tools["tools"] is None


def test_run_lea_persists_context_compaction_as_a_durable_marker(tmp_path, monkeypatch):
    # G1: a Compacted event from the prover is persisted as a durable `compaction`
    # timeline message (not an ephemeral SSE) — so the marker survives a reload, and the
    # live stream carries the SAME row. Its JSON content holds the before/after size +
    # prune/summary counts.
    import json as _json
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Prove something long")
    run = store.create_run(session["id"], "gemini/test", None, None)
    cfg = LeaConfig(model="gemini/test", max_turns=None, lea_root=tmp_path)
    queue: Queue = Queue()
    ctx = bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"],
        task="Prove something long", config=cfg, events=queue,
    )

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None):
        yield TurnStarted(1)
        yield Compacted(before_tokens=152_000, after_tokens=41_000, pruned=5, summarized=1)
        yield Finished("completed", "done", 2, ctx.session_id, "gemini/test",
                       Usage(input_tokens=5, output_tokens=3), 0.0, {})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)

    # Live: emitted on the `message` channel with kind='compaction'.
    events = _drain(queue)
    live = [e for e in events if e["type"] == "message"
            and (e["payload"] or {}).get("kind") == "compaction"]
    assert len(live) == 1
    p = _json.loads(live[0]["payload"]["content"])
    assert p["before_tokens"] == 152_000 and p["after_tokens"] == 41_000
    assert p["pruned"] == 5 and p["summarized"] is True and p["manual"] is False

    # Durable: it's a real timeline message, so a reload (session_detail) still has it.
    detail = store.session_detail(session["id"])
    markers = [m for m in detail["messages"] if m.get("kind") == "compaction"]
    assert len(markers) == 1
