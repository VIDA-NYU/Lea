"""D1·bridge: the in-process prover seam (bridge.run_lea).

Instead of normalizing an HTTP SSE stream, the bridge consumes the prover's typed
events directly. These tests drive `run_lea` with a *fake* `run_events` generator
(no model, no Lean) that writes a real `.lean` file into the session's git repo
and yields a realistic event sequence, then assert the bridge's side effects: a
git commit, a code_step pointing at it with the back-filled verdict, narration
flushed into messages, the run persisted with usage, and the SSE events emitted in
order — ending with `done`.
"""

import sqlite3
import time
from pathlib import Path
from queue import Queue
from threading import Event, Thread

import pytest

from lea.interface import (
    AssistantTextDelta,
    CheckResult,
    FileChanged,
    Finished,
    ToolApprovalRequested,
    ToolCalled,
    ToolResulted,
    TurnStarted,
    VerifyResult,
    UsageUpdated,
)
from lea.providers import Usage

from app import bridge, db, projects, runbroker, runregistry, store
from app.config import LeaConfig
from app.runregistry import RunRegistry


def _drain(q: Queue) -> list[dict]:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def _context(tmp_path, monkeypatch, task="Prove True", max_spend_usd=None):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session(task)
    run = store.create_run(session["id"], "gemini/test", None, 3)
    config = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path, max_spend_usd=max_spend_usd)
    queue: Queue = Queue()
    ctx = bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"], task=task, config=config, events=queue,
    )
    return ctx, queue


def _fake_run_events(events):
    """Build a fake run_events that writes a file into working_dir then replays
    `events` (a callable taking the absolute proof path, returning the sequence)."""

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None, gate=None):
        proof = Path(working_dir) / "Lea" / "Misc" / "proof.lean"
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_text("import Mathlib\n\ntheorem t : True := by trivial\n")
        yield from events(str(proof))

    return fake


def test_happy_path_commits_steps_and_persists_run(tmp_path, monkeypatch):
    ctx, queue = _context(tmp_path, monkeypatch)

    def script(proof_path):
        yield TurnStarted(1)
        yield AssistantTextDelta("Let me try ")
        yield AssistantTextDelta("trivial.")
        yield ToolCalled("write_file", {"path": proof_path})
        yield FileChanged(proof_path)
        yield UsageUpdated(10, 5, 0.01)
        yield ToolCalled("lean_check", {"path": proof_path})
        yield CheckResult(proof_path, "ok", None)
        yield Finished("completed", "Done — it compiles.", 1, ctx.session_id,
                       "gemini/test", Usage(input_tokens=10, output_tokens=5), 0.01, {})

    monkeypatch.setattr(bridge, "run_events", _fake_run_events(script))

    bridge.run_lea(ctx)

    detail = store.session_detail(ctx.session_id)

    # exactly one curated code step, holding the proof, verdict back-filled
    assert len(detail["code_steps"]) == 1
    step = detail["code_steps"][0]
    assert step["path"] == "Lea/Misc/proof.lean"
    assert step["author"] == "agent" and step["turn"] == 1
    assert "theorem t : True := by trivial" in step["code"]  # the file's after-state
    assert step["check_status"] == "ok"
    assert step["artifact_kind"] == "proof"

    # The step's content IS the file the fake wrote. This used to resolve the SHA
    # through git — the pointer and the bytes were two things that had to agree.
    # Now the row carries the bytes, so the read can't disagree with the write.
    on_disk = (tmp_path / "workspace" / "proofs" / ctx.session_id / step["path"]).read_text()
    assert step["code"] == on_disk

    # narration + terminal text landed as assistant messages
    contents = [m["content"] for m in detail["messages"] if m["role"] == "assistant"]
    assert "Let me try trivial." in contents
    assert "Done — it compiles." in contents

    # session status derives to the checked run outcome.
    assert detail["status"] == "proved"

    # run persisted: proved + usage + a per-turn breakdown row
    run = store.get_run(ctx.run_id)
    assert run["status"] == "proved"
    assert run["result_kind"] == "proved"
    assert run["input_tokens"] == 10 and run["output_tokens"] == 5
    assert abs(run["cost_usd"] - 0.01) < 1e-9
    assert [r["label"] for r in detail["usage_breakdown"]] == ["Turn 1"]

    # the SSE stream carried the live events and ended with done(proved)
    types = [item["type"] for item in _drain(queue)]
    assert "assistant_delta" in types
    assert types.count("code_step") == 2  # write, then verdict back-fill
    assert "message" in types
    assert types[-1] == "done"


def test_definition_artifact_persists_defined_result_kind(tmp_path, monkeypatch):
    ctx, queue = _context(tmp_path, monkeypatch, task="Define a subadditive predicate")

    def script(proof_path):
        Path(proof_path).write_text(
            "import Mathlib\n\n"
            "def Subadditive (a : Nat -> Int) : Prop := True\n"
        )
        yield TurnStarted(1)
        yield ToolCalled("write_file", {"path": proof_path})
        yield FileChanged(proof_path)
        yield ToolCalled("lean_check", {"path": proof_path})
        yield CheckResult(proof_path, "ok", None)
        yield Finished(
            "completed",
            "Defined Subadditive.",
            1,
            ctx.session_id,
            "gemini/test",
            Usage(input_tokens=10, output_tokens=5),
            0.01,
            {},
            result_kind="needs_review",
            result_detail="NEEDS_REVIEW",
        )

    monkeypatch.setattr(bridge, "run_events", _fake_run_events(script))

    bridge.run_lea(ctx)

    run = store.get_run(ctx.run_id)
    assert run["status"] == "proved"
    assert run["result_kind"] == "defined"
    assert run["result_detail"] is None

    done = _drain(queue)[-1]
    assert done["type"] == "done"
    assert done["payload"]["status"] == "proved"
    assert done["payload"]["result_kind"] == "defined"


def test_needs_review_proof_artifact_keeps_proved_session_status(tmp_path, monkeypatch):
    ctx, queue = _context(tmp_path, monkeypatch, task="Prove True")

    def script(proof_path):
        yield TurnStarted(1)
        yield ToolCalled("write_file", {"path": proof_path})
        yield FileChanged(proof_path)
        yield ToolCalled("lean_check", {"path": proof_path})
        yield CheckResult(proof_path, "ok", None)
        yield Finished(
            "completed",
            "The file compiles, but the classifier is cautious.",
            1,
            ctx.session_id,
            "gemini/test",
            Usage(input_tokens=10, output_tokens=5),
            0.01,
            {},
            result_kind="needs_review",
            result_detail="NEEDS_REVIEW",
        )

    monkeypatch.setattr(bridge, "run_events", _fake_run_events(script))

    bridge.run_lea(ctx)

    detail = store.session_detail(ctx.session_id)
    assert detail["status"] == "proved"
    assert detail["code_steps"][0]["artifact_kind"] == "proof"

    run = store.get_run(ctx.run_id)
    assert run["status"] == "needs_review"
    assert run["result_kind"] == "needs_review"

    done = _drain(queue)[-1]
    assert done["type"] == "done"
    assert done["payload"]["status"] == "needs_review"
    assert done["payload"]["result_kind"] == "needs_review"


def test_disproof_result_persists_and_streams_distinct_outcome(tmp_path, monkeypatch):
    ctx, queue = _context(tmp_path, monkeypatch, task="Find a counterexample")

    def script(proof_path):
        yield TurnStarted(1)
        yield ToolCalled("write_file", {"path": proof_path})
        yield FileChanged(proof_path)
        yield ToolCalled("lean_check", {"path": proof_path})
        yield CheckResult(proof_path, "ok", None)
        yield Finished(
            "completed",
            "Counterexample verified.",
            1,
            ctx.session_id,
            "gemini/test",
            Usage(input_tokens=10, output_tokens=5),
            0.01,
            {},
            result_kind="disproved",
            result_detail="DISPROVED",
        )

    monkeypatch.setattr(bridge, "run_events", _fake_run_events(script))

    bridge.run_lea(ctx)

    detail = store.session_detail(ctx.session_id)
    run = store.get_run(ctx.run_id)
    assert run["status"] == "disproved"
    assert run["result_kind"] == "disproved"
    assert run["result_detail"] == "DISPROVED"
    assert detail["status"] == "disproved"

    items = _drain(queue)
    assert items[-1]["type"] == "done"
    assert items[-1]["payload"]["status"] == "disproved"
    assert items[-1]["payload"]["result_kind"] == "disproved"


def test_project_run_uses_shared_repo_namespace_and_context(tmp_path, monkeypatch):
    # Q2 (D23/D24/D25/D32/D33): a project session writes the shared project repo, the
    # prompt gets the project namespace, the composed context message leads the
    # messages, and an asset write becomes a graph signal (not a canvas snapshot).
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    proofs_root = tmp_path / "workspace" / "proofs"
    project = projects.provision_project("Epsilon", proofs_root, description="ε–δ")
    session = store.create_session("prove foo", project_id=project["id"])
    run = store.create_run(session["id"], "gemini/test", None, 3, project_id=project["id"])
    config = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)
    queue: Queue = Queue()
    ctx = bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"], task="prove foo", config=config, events=queue,
    )
    captured: dict = {}

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None, gate=None):
        captured["namespace"] = namespace
        captured["working_dir"] = working_dir
        captured["messages"] = list(messages)
        # A proof written directly in the project dir (importable as Lea.Epsilon.Foo).
        proof = Path(working_dir) / "Foo.lean"
        proof.write_text("import Mathlib\nnamespace Lea.Epsilon\ntheorem foo : True := by trivial\nend Lea.Epsilon\n")
        yield TurnStarted(1)
        yield ToolCalled("write_file", {"path": str(proof)})
        yield FileChanged(str(proof))
        yield CheckResult(str(proof), "ok", None)
        # The agent also revises the blueprint — an asset write (D33).
        bp = Path(working_dir) / ".lea" / "blueprint.md"
        bp.write_text("# Blueprint — Epsilon\n\n## foo\n- kind: theorem\n- lean: `Lea.Epsilon.foo`\n")
        yield ToolCalled("edit_file", {"path": str(bp)})
        yield ToolResulted("edit_file", "ok", "ok")
        yield Finished("completed", "Proved.", 1, session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)

    # D32/D24: the prompt namespace + working dir point at the project repo.
    assert captured["namespace"] == "Lea.Epsilon"
    assert captured["working_dir"].replace("\\", "/").endswith("/proofs/Lea/Epsilon")
    # D25: the composed project-context message leads the messages.
    first = captured["messages"][0]
    assert first["content"].startswith(projects.CONTEXT_MARKER)
    assert "## Project Instructions" in first["content"] and "Lea.Epsilon" in first["content"]
    assert "project title is a human-facing display name" in first["content"]
    assert "namespace `Lea.Epsilon` is authoritative" in first["content"]
    assert "Do not derive a namespace from the display name" in first["content"]

    # D24: the proof committed to the SHARED project repo, not proofs/<session-id>.
    detail = store.session_detail(session["id"])
    steps = detail["code_steps"]
    assert len(steps) == 1 and steps[0]["path"] == "Foo.lean"
    assert not (proofs_root / session["id"]).exists()  # no loose per-session repo
    # The step holds the proof itself — this used to read it back out of the shared
    # repo via `gs.snapshot(..., commit_sha, ...)`, which is what D24 had to get
    # right. The step is still attributed to the shared repo's path; the content
    # just no longer depends on resolving that repo correctly to be readable.
    assert "theorem foo" in steps[0]["code"]
    assert (proofs_root / "Lea" / "Epsilon" / "Foo.lean").exists()  # still on disk (D3)

    # D33: the asset write emitted a project_updated signal and NO extra code_step.
    events = _drain(queue)
    updated = [e for e in events if e["type"] == "project_updated"]
    assert len(updated) == 1
    assert updated[0]["payload"]["path"] == ".lea/blueprint.md"
    assert updated[0]["payload"]["project_id"] == project["id"]
    assert sum(1 for e in events if e["type"] == "code_step") == 2  # proof write + verdict only


def _policy_recording_fake(received: dict):
    """A fake run_events that records the prompt_variant + gate it was handed,
    then finishes cleanly. Lets us assert the autonomous policy without a model."""

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None, gate=None):
        received["prompt_variant"] = config.prompt_variant
        received["gate"] = gate
        yield TurnStarted(1)
        yield Finished("assistant", "ok", 1, session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {"messages": []})

    return fake


def test_autonomous_run_disables_gate_and_uses_default_variant(tmp_path, monkeypatch):
    # Overleaf path: an autonomous run must reach the prover with NO gate (no
    # approval prompts) and the non-interactive `default` prompt variant (no
    # plan-then-pause) — so it formalizes with zero human interaction.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Prove A")
    run = store.create_run(session["id"], "gemini/test", None, 3, autonomous=True)
    config = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path,
                       prompt_variant="interactive")
    ctx = bridge.RunnerContext(session_id=session["id"], run_id=run["id"], task="Prove A",
                               config=config, events=Queue(), autonomous=True)

    received: dict = {}
    monkeypatch.setattr(bridge, "run_events", _policy_recording_fake(received))
    bridge.run_lea(ctx)

    assert received["gate"] is None
    assert received["prompt_variant"] == "default"
    # the run row persisted the flag (it must survive create → events HTTP hops)
    assert bool(store.get_run(run["id"])["autonomous"]) is True


def test_interactive_run_keeps_gate_and_config_variant(tmp_path, monkeypatch):
    # UI path (default): the gate is wired and the configured prompt variant is
    # left untouched — current behavior is preserved.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Prove A")
    run = store.create_run(session["id"], "gemini/test", None, 3)  # autonomous defaults False
    config = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path,
                       prompt_variant="interactive")
    ctx = bridge.RunnerContext(session_id=session["id"], run_id=run["id"], task="Prove A",
                               config=config, events=Queue())  # autonomous defaults False

    received: dict = {}
    monkeypatch.setattr(bridge, "run_events", _policy_recording_fake(received))
    bridge.run_lea(ctx)

    assert callable(received["gate"])
    assert received["prompt_variant"] == "interactive"
    assert bool(store.get_run(run["id"])["autonomous"]) is False


def test_done_emitted_and_run_failed_on_exception(tmp_path, monkeypatch):
    ctx, queue = _context(tmp_path, monkeypatch)

    def boom(
        config, messages, *, namespace=None, session_id=None, working_dir=None,
        should_stop=None, gate=None,
    ):
        yield TurnStarted(1)
        raise RuntimeError("model exploded")

    monkeypatch.setattr(bridge, "run_events", boom)

    bridge.run_lea(ctx)

    items = _drain(queue)
    types = [i["type"] for i in items]
    assert "run_error" in types
    assert types[-1] == "done"
    assert items[-1]["payload"]["status"] == "failed"
    persisted = store.get_run(ctx.run_id)
    assert persisted["status"] == "failed"
    assert persisted["result_kind"] == "failed"
    assert persisted["result_detail"] == "RuntimeError: model exploded"
    assert items[-1]["payload"]["result_detail"] == "RuntimeError: model exploded"


def test_exception_detail_is_redacted_before_streaming_or_persistence(tmp_path, monkeypatch):
    ctx, queue = _context(tmp_path, monkeypatch)
    secret = "sk-super-secret-provider-key"
    monkeypatch.setattr(bridge, "configured_provider_keys", lambda: {"OPENAI_API_KEY": secret})

    def boom(
        config, messages, *, namespace=None, session_id=None, working_dir=None,
        should_stop=None, gate=None,
    ):
        raise RuntimeError(f"provider rejected {secret}")
        yield  # pragma: no cover — keep this a generator like run_events

    monkeypatch.setattr(bridge, "run_events", boom)
    bridge.run_lea(ctx)

    items = _drain(queue)
    run_error = next(item for item in items if item["type"] == "run_error")
    detail = store.get_run(ctx.run_id)["result_detail"]
    assert secret not in run_error["payload"]["message"]
    assert secret not in detail
    assert "[redacted]" in detail


def _recording_fake(received: list, transcript_messages: list):
    """A fake run_events that records the `messages` it was handed, then Finishes
    with a given transcript (so the bridge persists it for the next activation)."""

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None, gate=None):
        received.append(messages)
        yield TurnStarted(1)
        yield Finished("completed", "done", 1, session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0,
                       {"messages": transcript_messages})

    return fake


def test_first_run_seeds_only_the_new_user_turn(tmp_path, monkeypatch):
    ctx, _ = _context(tmp_path, monkeypatch, task="Prove A")
    received: list = []
    monkeypatch.setattr(bridge, "run_events",
                        _recording_fake(received, [{"role": "user", "content": "Prove A"}]))

    bridge.run_lea(ctx)

    # a cold first run gets no prior transcript — just the new user turn
    assert received == [[{"role": "user", "content": "Prove A"}]]
    # ...and its transcript is persisted for next time
    assert store.latest_transcript_for_session(ctx.session_id) == [{"role": "user", "content": "Prove A"}]


def test_followup_replays_prior_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("S")
    config = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)

    # run 1 finishes with a structured transcript carrying a real tool exchange
    transcript1 = [
        {"role": "user", "content": "Prove A"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "trying"},
            {"type": "tool_call", "name": "write_file", "args": {"path": "p.lean"}, "id": "c1"},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_name": "write_file", "content": "ok", "tool_call_id": "c1"},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    run1 = store.create_run(session["id"], "gemini/test", None, 3)
    rec1: list = []
    monkeypatch.setattr(bridge, "run_events", _recording_fake(rec1, transcript1))
    bridge.run_lea(bridge.RunnerContext(session["id"], run1["id"], "Prove A", config, Queue()))
    assert rec1[0] == [{"role": "user", "content": "Prove A"}]

    # run 2 (a follow-up) must receive run 1's full transcript + the new user turn,
    # with the tool_call/tool_result parts intact (no orphaned tool results)
    run2 = store.create_run(session["id"], "gemini/test", None, 3)
    rec2: list = []
    monkeypatch.setattr(bridge, "run_events", _recording_fake(rec2, []))
    bridge.run_lea(bridge.RunnerContext(session["id"], run2["id"], "Now prove B", config, Queue()))

    assert rec2[0] == transcript1 + [{"role": "user", "content": "Now prove B"}]


def test_interrupt_maps_finished_interrupted_to_cancelled(tmp_path, monkeypatch):
    ctx, queue = _context(tmp_path, monkeypatch)

    def interrupted(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None, gate=None):
        yield TurnStarted(1)
        yield Finished("interrupted", "Run interrupted by the user.", 1, session_id,
                       "gemini/test", Usage(input_tokens=2, output_tokens=1), 0.0, {"messages": []})

    monkeypatch.setattr(bridge, "run_events", interrupted)
    bridge.run_lea(ctx)

    assert store.get_run(ctx.run_id)["status"] == "cancelled"
    items = _drain(queue)
    assert items[-1]["type"] == "done"
    assert items[-1]["payload"]["status"] == "cancelled"


def test_request_stop_flag_reaches_the_run(tmp_path, monkeypatch):
    ctx, queue = _context(tmp_path, monkeypatch)
    # Stop was hit before the run loop got going — the endpoint pre-set the flag.
    bridge.request_stop(ctx.run_id)

    def stops_when_asked(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None, gate=None):
        # the agent honors should_stop at its turn boundary
        if should_stop():
            yield Finished("interrupted", "stopped", 0, session_id, "gemini/test",
                           Usage(input_tokens=0, output_tokens=0), 0.0, {"messages": []})
            return
        yield Finished("completed", "done", 1, session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {"messages": []})

    monkeypatch.setattr(bridge, "run_events", stops_when_asked)
    bridge.run_lea(ctx)

    assert store.get_run(ctx.run_id)["status"] == "cancelled"
    # the flag is cleaned up once the run ends
    assert ctx.run_id not in bridge._stop_events


def test_gate_policy_gates_only_impactful_tools():
    bridge._session_allowlists.pop("sess-gate", None)
    gate = bridge._make_gate("sess-gate")
    # impactful tools are gated; read-only + lean_check are auto-allowed (D19)
    assert gate("bash", {}) and gate("write_file", {}) and gate("edit_file", {})
    assert not gate("read_file", {})
    assert not gate("search_mathlib", {})
    assert not gate("lean_check", {})
    # "always allow this session" exempts that one tool; others still prompt
    bridge._session_allowlists.setdefault("sess-gate", set()).add("bash")
    assert not gate("bash", {})
    assert gate("write_file", {})
    bridge._session_allowlists.pop("sess-gate", None)


def test_resolve_approval_matches_and_rejects_stale():
    bridge._pending_approvals["r1"] = {"approval_id": "a1", "event": Event(), "decision": None}
    try:
        assert not bridge.resolve_approval("r1", "WRONG", "allow")  # wrong approval id
        assert not bridge.resolve_approval("rX", "a1", "allow")     # unknown run
        assert bridge.resolve_approval("r1", "a1", "allow")
        pending = bridge._pending_approvals["r1"]
        assert pending["decision"] == "allow" and pending["event"].is_set()
    finally:
        bridge._pending_approvals.pop("r1", None)


def _gated_fake(received: dict, decision_sink: str = "decision"):
    """A fake run_events that gates one bash call, records the decision it gets
    back, then Finishes."""
    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None, gate=None):
        received[decision_sink] = yield ToolApprovalRequested("bash", {"command": "ls"})
        yield Finished("completed", "done", 1, session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {"messages": []})
    return fake


def _run_in_thread_and_resolve(ctx, decision):
    """Run run_lea in a thread, wait for its pending approval, resolve it, join."""
    t = Thread(target=bridge.run_lea, args=(ctx,))
    t.start()
    approval_id = None
    for _ in range(200):  # up to ~4s
        pending = bridge._pending_approvals.get(ctx.run_id)
        if pending:
            approval_id = pending["approval_id"]
            break
        time.sleep(0.02)
    assert approval_id, "run never raised a pending approval"
    assert bridge.resolve_approval(ctx.run_id, approval_id, decision)
    t.join(timeout=5)
    assert not t.is_alive()


def test_approval_relay_allow(tmp_path, monkeypatch):
    ctx, queue = _context(tmp_path, monkeypatch)
    received: dict = {}
    monkeypatch.setattr(bridge, "run_events", _gated_fake(received))

    _run_in_thread_and_resolve(ctx, "allow")

    assert received["decision"] == "allow"  # the decision reached the generator
    assert store.get_run(ctx.run_id)["status"] == "proved"
    types = [i["type"] for i in _drain(queue)]
    assert "approval_requested" in types and "approval_resolved" in types


def test_pending_approval_is_persisted_so_it_survives_a_reconnect(tmp_path, monkeypatch):
    # The gate must live on the run row too, not only in the one-shot SSE event:
    # session_detail re-surfaces active_run.pending_approval so a client that missed
    # the live `approval_requested` (reattached after it fired, or switched away and
    # back) still rebuilds the Allow/Deny card instead of waiting forever with none.
    ctx, queue = _context(tmp_path, monkeypatch)
    received: dict = {}
    monkeypatch.setattr(bridge, "run_events", _gated_fake(received))

    t = Thread(target=bridge.run_lea, args=(ctx,))
    t.start()
    approval_id = None
    for _ in range(200):  # up to ~4s for the gate to raise
        pending = bridge._pending_approvals.get(ctx.run_id)
        if pending:
            approval_id = pending["approval_id"]
            break
        time.sleep(0.02)
    assert approval_id, "run never raised a pending approval"

    # While the gate blocks, the run row carries the approval — exactly the bytes a
    # reconnecting client reads back through session_detail.
    persisted = store.get_run(ctx.run_id)["pending_approval"]
    assert persisted is not None, "pending approval was not persisted for reconnect"
    assert persisted["approval_id"] == approval_id
    assert persisted["tool_name"] == "bash"
    assert persisted["args"] == {"command": "ls"}

    assert bridge.resolve_approval(ctx.run_id, approval_id, "allow")
    t.join(timeout=5)
    assert not t.is_alive()

    # Resolved → cleared, so a later reconnect can't re-raise a consumed decision.
    assert store.get_run(ctx.run_id)["pending_approval"] is None


def test_approval_relay_always_session_updates_allowlist(tmp_path, monkeypatch):
    ctx, queue = _context(tmp_path, monkeypatch)
    bridge._session_allowlists.pop(ctx.session_id, None)
    received: dict = {}
    monkeypatch.setattr(bridge, "run_events", _gated_fake(received))

    _run_in_thread_and_resolve(ctx, "always_session")

    assert received["decision"] == "always_session"
    # the tool is now allow-listed for the rest of the session (D19)
    assert "bash" in bridge._session_allowlists.get(ctx.session_id, set())
    bridge._session_allowlists.pop(ctx.session_id, None)


def _seed_agent_then_user_edit(tmp_path, *, user_edit: bool):
    """A session where the agent wrote a proof; optionally the human then edited it
    out-of-run. Returns (session, run for a new activation)."""
    gs = bridge.GitStore(tmp_path / "workspace" / "proofs")
    session = store.create_session("Divergence")
    repo = gs.init_session(session["id"])
    proof = repo / "Lea" / "Misc" / "P.lean"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text("theorem p : True := by trivial\n")
    run0 = store.create_run(session["id"], "m", None, 3)
    store.add_code_step(session["id"], run0["id"], "Lea/Misc/P.lean",
                        content="theorem p : True := by trivial\n", author="agent", turn=1)
    if user_edit:
        # The human's edit: the file on disk moves on, the agent's stored step does
        # not. Divergence is exactly that gap — the agent's last known content vs.
        # what's on disk now — rather than a diff between two git revisions.
        proof.write_text("theorem p : True := by exact trivial\n")
        store.add_code_step(session["id"], None, "Lea/Misc/P.lean",
                            content="theorem p : True := by exact trivial\n", author="user")
        store.add_message(session["id"], "user", "used exact instead", None, kind="edit_note")
    return session, store.create_run(session["id"], "m", None, 3)


def test_run_start_injects_divergence_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, run = _seed_agent_then_user_edit(tmp_path, user_edit=True)
    config = LeaConfig(model="m", max_turns=3, lea_root=tmp_path)
    received: list = []
    monkeypatch.setattr(bridge, "run_events", _recording_fake(received, []))

    bridge.run_lea(bridge.RunnerContext(session["id"], run["id"], "keep going", config, Queue()))

    task_msg = received[0][-1]["content"]
    assert "human edited" in task_msg.lower()
    assert "exact trivial" in task_msg       # the diff (the human's added line)
    assert "used exact instead" in task_msg  # the linked edit note (D11)
    assert task_msg.endswith("keep going")   # the original task is preserved


def test_no_divergence_when_agent_state_is_current(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session, run = _seed_agent_then_user_edit(tmp_path, user_edit=False)
    config = LeaConfig(model="m", max_turns=3, lea_root=tmp_path)
    received: list = []
    monkeypatch.setattr(bridge, "run_events", _recording_fake(received, []))

    bridge.run_lea(bridge.RunnerContext(session["id"], run["id"], "keep going", config, Queue()))

    # no edits since the agent's last write → the task is passed through untouched
    assert received[0][-1]["content"] == "keep going"


def _skills_recording_fake(received: dict):
    """A fake run_events that records the `config.skills` paths it was handed and
    reads each file's content *while the temp dir still exists* (before the run's
    finally cleans it up), then finishes cleanly."""

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None, gate=None):
        received["skills"] = list(config.skills)
        received["bodies"] = {Path(p).name: Path(p).read_text() for p in config.skills}
        yield TurnStarted(1)
        yield Finished("assistant", "ok", 1, session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {"messages": []})

    return fake


def test_project_run_materializes_resolved_skills_into_cfg(tmp_path, monkeypatch):
    # W3/D48: a project run picks up the skills that resolve for it (global ∪
    # assigned) as per-run temp .md files on cfg.skills, and the temp dir is cleaned
    # up when the run ends.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    proofs_root = tmp_path / "workspace" / "proofs"
    project = projects.provision_project("Epsilon", proofs_root)
    glob = store.create_skill("Ring Tactics", "use `ring`")
    store.set_skill_assignment(glob["id"], is_global=True)
    scoped = store.create_skill("House Rules", "our conventions")
    store.set_skill_assignment(scoped["id"], is_global=False, project_ids=[project["id"]])

    session = store.create_session("prove foo", project_id=project["id"])
    run = store.create_run(session["id"], "gemini/test", None, 3, project_id=project["id"])
    config = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)
    ctx = bridge.RunnerContext(session["id"], run["id"], "prove foo", config, Queue())

    received: dict = {}
    monkeypatch.setattr(bridge, "run_events", _skills_recording_fake(received))
    bridge.run_lea(ctx)

    assert {Path(p).name for p in received["skills"]} == {"ring-tactics.md", "house-rules.md"}
    assert received["bodies"]["ring-tactics.md"] == "use `ring`"
    assert received["bodies"]["house-rules.md"] == "our conventions"
    # The materialized temp dir is removed in the run's finally (no leak).
    for path in received["skills"]:
        assert not Path(path).exists()
    # The original config object is untouched — cfg is rebuilt via replace() (frozen).
    assert config.skills == []


def test_loose_run_resolves_global_skills(tmp_path, monkeypatch):
    """v2.5 H — DELIBERATELY changed from D47's "a loose session resolves to no skills".

    "Global" has to mean global, or the word is a lie: a skill marked as applying to every
    project was silently absent from every project-less session, and there was no way to
    opt one in. E0e added the per-session opt-in; this makes the INHERITED half consistent,
    and matches how MCP servers already resolve for a loose session.

    A non-global, project-assigned skill is still absent — that part of D47 stands."""
    ctx, _ = _context(tmp_path, monkeypatch)
    glob = store.create_skill("Ring Tactics", "use `ring`")
    store.set_skill_assignment(glob["id"], is_global=True)
    scoped = store.create_skill("Project Only", "not for loose sessions")

    received: dict = {}
    monkeypatch.setattr(bridge, "run_events", _skills_recording_fake(received))
    bridge.run_lea(ctx)

    assert len(received["skills"]) == 1
    assert received["skills"][0].endswith("ring-tactics.md")


def test_run_lea_releases_its_admission_slot(tmp_path, monkeypatch):
    """Admission now happens at the endpoint; run_lea owns the admitted slot and must
    release it on the happy path so the next run can be admitted."""
    ctx, queue = _context(tmp_path, monkeypatch)
    reg = RunRegistry(max_concurrent=1)
    monkeypatch.setattr(runregistry, "registry", reg)
    reg.try_admit(ctx.run_id, ctx.session_id)  # endpoint would have done this

    monkeypatch.setattr(bridge, "run_events", _fake_run_events(
        lambda proof: [Finished(reason="assistant", text="ok", usage=Usage(0, 0), cost=0.0,
                                transcript={"messages": []})]
    ))
    bridge.run_lea(ctx)

    assert not reg.is_active(ctx.run_id), "run_lea must release its slot when it finishes"
    assert reg.try_admit("next", "next-sess").outcome == runregistry.ADMITTED


def test_setup_failure_still_releases_the_slot(tmp_path, monkeypatch):
    """Item 4, fixed by construction (item 9): run_lea's setup now runs INSIDE the
    try/finally, so a throw during setup releases the admitted slot instead of
    leaking it forever. On `main` the setup sat outside the try, so this exception
    would propagate out of run_lea and the slot would never be freed."""
    ctx, queue = _context(tmp_path, monkeypatch)
    reg = RunRegistry(max_concurrent=1)
    monkeypatch.setattr(runregistry, "registry", reg)
    reg.try_admit(ctx.run_id, ctx.session_id)

    def _boom(*a, **k):
        raise RuntimeError("setup blew up")
    # repo_for_session is in the setup block that used to sit outside the try.
    monkeypatch.setattr(bridge.projects, "repo_for_session", _boom)

    bridge.run_lea(ctx)  # must NOT raise — the failure is surfaced as events

    assert not reg.is_active(ctx.run_id), "a setup throw leaked the admission slot"
    items = _drain(queue)
    types = [i["type"] for i in items]
    assert types[-1] == "done" and items[-1]["payload"]["status"] == "failed"
    assert "run_error" in types
    # And the slot is genuinely free for the next run.
    assert reg.try_admit("next", "next-sess").outcome == runregistry.ADMITTED


def test_mid_run_spend_cap_requests_stop_and_labels_result(tmp_path, monkeypatch):
    # PLAN-system-hardening 0.1: a UsageUpdated that crosses the cap sets the
    # cooperative stop flag mid-run; the interrupted finish is labelled
    # result_kind="max_spend" (status stays "cancelled" — vocabulary unchanged).
    ctx, queue = _context(tmp_path, monkeypatch, max_spend_usd=0.02)

    def capped(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None, gate=None):
        yield TurnStarted(1)
        yield AssistantTextDelta("Working on it.")
        yield UsageUpdated(10, 5, 0.03)
        # The cost event above crossed the cap — the bridge must have set the
        # stop flag by the time the agent reaches its next boundary check.
        assert should_stop()
        yield Finished("interrupted", "stopped", 1, session_id, "gemini/test",
                       Usage(input_tokens=10, output_tokens=5), 0.03, {"messages": []})

    monkeypatch.setattr(bridge, "run_events", capped)
    bridge.run_lea(ctx)

    run = store.get_run(ctx.run_id)
    assert run["status"] == "cancelled"
    assert run["result_kind"] == "max_spend"

    items = _drain(queue)
    cap_status = [i for i in items if i["type"] == "status" and i["payload"].get("status") == "max_spend"]
    assert cap_status, "a max_spend status event should be streamed when the cap trips"
    done = items[-1]
    assert done["type"] == "done"
    assert done["payload"]["status"] == "cancelled"
    assert done["payload"]["result_kind"] == "max_spend"
    assert "spend" in done["payload"]["result_detail"].lower()


def test_spend_cap_baseline_includes_prior_runs(tmp_path, monkeypatch):
    # The cap is global (all-time), not per-run: prior persisted spend counts,
    # so a run whose own cost is small still trips a nearly-exhausted cap.
    ctx, queue = _context(tmp_path, monkeypatch, max_spend_usd=0.10)
    prior_run = store.create_run(ctx.session_id, "gemini/test", None, 3)
    store.update_run(prior_run["id"], "proved", input_tokens=100, output_tokens=50, cost_usd=0.095)

    def capped(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None, gate=None):
        yield TurnStarted(1)
        yield UsageUpdated(10, 5, 0.01)
        assert should_stop()
        yield Finished("interrupted", "stopped", 1, session_id, "gemini/test",
                       Usage(input_tokens=10, output_tokens=5), 0.01, {"messages": []})

    monkeypatch.setattr(bridge, "run_events", capped)
    bridge.run_lea(ctx)
    assert store.get_run(ctx.run_id)["result_kind"] == "max_spend"


def test_spend_cap_untripped_run_is_untouched(tmp_path, monkeypatch):
    # A configured cap that is never reached must not alter the outcome.
    ctx, queue = _context(tmp_path, monkeypatch, max_spend_usd=100.0)

    def script(proof_path):
        yield TurnStarted(1)
        yield ToolCalled("write_file", {"path": proof_path})
        yield FileChanged(proof_path)
        yield UsageUpdated(10, 5, 0.01)
        yield ToolCalled("lean_check", {"path": proof_path})
        yield CheckResult(proof_path, "ok", None)
        yield Finished("completed", "Done.", 1, ctx.session_id, "gemini/test",
                       Usage(input_tokens=10, output_tokens=5), 0.01, {"messages": []})

    monkeypatch.setattr(bridge, "run_events", _fake_run_events(script))
    bridge.run_lea(ctx)

    run = store.get_run(ctx.run_id)
    assert run["status"] == "proved"
    assert run["result_kind"] != "max_spend"
    items = _drain(queue)
    assert not [i for i in items if i["type"] == "status" and i["payload"].get("status") == "max_spend"]
    assert items[-1]["payload"]["status"] == "proved"


def _wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_enqueued_runs_execute_fifo(tmp_path, monkeypatch):
    """At capacity one, background admission preserves FIFO execution order."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    monkeypatch.setattr(
        bridge, "load_config",
        lambda: LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path),
    )
    monkeypatch.setattr(runregistry, "registry", RunRegistry(max_concurrent=1))
    runbroker._brokers.clear()
    executed = []

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None, gate=None):
        executed.append(messages[-1]["content"])
        yield TurnStarted(1)
        yield Finished("completed", "done", 1, session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {"messages": []})

    monkeypatch.setattr(bridge, "run_events", fake)

    run_ids = []
    for label in ("first", "second", "third"):
        session = store.create_session(label)
        run = store.create_run(session["id"], "gemini/test", None, 3)
        store.add_message(session["id"], "user", label, run["id"])
        run_ids.append(run["id"])
    for run_id in run_ids:
        bridge.enqueue_run(run_id)

    assert _wait_for(lambda: all(
        store.get_run(rid)["status"] not in {"pending", "running"} for rid in run_ids
    )), "all queued runs reach a terminal status"
    assert executed == ["first", "second", "third"], "FIFO order preserved"
    for run_id in run_ids:
        assert store.get_run(run_id)["status"] == "proved"


def test_queued_run_executes_its_snapshotted_model(tmp_path, monkeypatch):
    """A later global/default change cannot replace the model chosen for a run."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    monkeypatch.setattr(
        bridge, "load_config",
        lambda: LeaConfig(model="later-global-model", max_turns=3, lea_root=tmp_path),
    )
    monkeypatch.setattr(runregistry, "registry", RunRegistry(max_concurrent=1))
    runbroker._brokers.clear()
    executed_models = []

    def fake(
        config,
        messages,
        *,
        namespace=None,
        session_id=None,
        working_dir=None,
        should_stop=None,
        gate=None,
    ):
        executed_models.append(config.model)
        yield TurnStarted(1)
        yield Finished("completed", "done", 1, session_id, config.model,
                       Usage(input_tokens=1, output_tokens=1), 0.0, {"messages": []})

    monkeypatch.setattr(bridge, "run_events", fake)
    session = store.create_session("snapshot the picker")
    run = store.create_run(session["id"], "picker/model", None, 3)
    store.add_message(session["id"], "user", "snapshot the picker", run["id"])

    bridge.enqueue_run(run["id"])

    assert _wait_for(
        lambda: store.get_run(run["id"])["status"] not in {"pending", "running"}
    )
    assert executed_models == ["picker/model"]
    assert store.get_run(run["id"])["model"] == "picker/model"


def test_finished_broker_buffer_ends_in_done(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    monkeypatch.setattr(
        bridge, "load_config",
        lambda: LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path),
    )

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None, gate=None):
        yield TurnStarted(1)
        yield AssistantTextDelta("thinking…")
        yield Finished("completed", "done", 1, session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {"messages": []})

    monkeypatch.setattr(bridge, "run_events", fake)
    session = store.create_session("replay me")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    store.add_message(session["id"], "user", "replay me", run["id"])
    broker = runbroker.create(run["id"])
    bridge.enqueue_run(run["id"])
    assert _wait_for(lambda: broker.closed)

    replay = broker.events_after(0)
    types = [item["type"] for item in replay]
    assert "assistant_delta" in types
    assert types[-1] == "done"
    assert replay[-1]["payload"]["status"] == "proved"


def test_interrupted_queued_run_is_skipped_and_broker_sealed(tmp_path, monkeypatch):
    """A run interrupted while still queued must be skipped by the worker and
    its hub stream sealed with the terminal frame, so attached observers don't
    wait forever."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    monkeypatch.setattr(
        bridge, "load_config",
        lambda: LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path),
    )
    monkeypatch.setattr(bridge, "run_events", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a dequeued-but-finalized run must never execute")
    ))

    session = store.create_session("skip me")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    store.add_message(session["id"], "user", "skip me", run["id"])
    # Finalize BEFORE enqueueing so the worker sees a non-pending run.
    store.update_run(run["id"], "failed", result_kind="failed",
                     result_detail="Interrupted before the run started.")
    broker = runbroker.create(run["id"])
    bridge.enqueue_run(run["id"])

    assert _wait_for(lambda: broker.closed), "the dispatcher seals the skipped run's broker"
    replay = broker.events_after(0)
    assert replay[-1]["type"] == "done"
    assert replay[-1]["payload"]["status"] == "failed"


def test_run_records_structured_artifact_rows(tmp_path, monkeypatch):
    """PLAN-system-hardening 4.1: the finalizer writes one artifact row per
    checked file — declaration parsed server-side, kind from the step, module
    from the project namespace."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    proofs_root = tmp_path / "workspace" / "proofs"
    project = projects.provision_project("Epsilon", proofs_root)
    session = store.create_session("prove foo", project_id=project["id"])
    run = store.create_run(session["id"], "gemini/test", None, 3, project_id=project["id"])
    config = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)
    queue: Queue = Queue()
    ctx = bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"], task="prove foo", config=config, events=queue,
    )

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None, gate=None):
        proof = Path(working_dir) / "chapter" / "foo_theorem.lean"
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_text("import Mathlib\n\ntheorem foo_theorem : True := by trivial\n")
        broken = Path(working_dir) / "broken.lean"
        broken.write_text("theorem broken_one : False := by trivial\n")
        yield TurnStarted(1)
        yield ToolCalled("write_file", {"path": str(proof)})
        yield FileChanged(str(proof))
        yield CheckResult(str(proof), "ok", None)
        yield ToolCalled("write_file", {"path": str(broken)})
        yield FileChanged(str(broken))
        yield CheckResult(str(broken), "error", "type mismatch")
        yield Finished("completed", "Proved.", 1, session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)

    rows = store.list_artifacts_for_scope(project["id"])
    assert len(rows) == 1, "only checked-ok files become artifact rows"
    row = rows[0]
    assert row["declaration_name"] == "foo_theorem"
    assert row["kind"] == "proof"
    assert row["path"] == "chapter/foo_theorem.lean"
    assert row["module_name"] == "Lea.Epsilon.chapter.foo_theorem"
    assert row["run_id"] == run["id"]


def test_reformalize_updates_the_artifact_row_in_place(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    proofs_root = tmp_path / "workspace" / "proofs"
    project = projects.provision_project("Epsilon", proofs_root)
    session = store.create_session("prove foo", project_id=project["id"])

    def make_run(path_name):
        run = store.create_run(session["id"], "gemini/test", None, 3, project_id=project["id"])
        config = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)
        ctx = bridge.RunnerContext(
            session_id=session["id"], run_id=run["id"], task="prove foo",
            config=config, events=Queue(),
        )

        def fake(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None, gate=None):
            proof = Path(working_dir) / path_name
            proof.write_text("import Mathlib\n\ntheorem same_decl : True := by trivial\n")
            yield TurnStarted(1)
            yield ToolCalled("write_file", {"path": str(proof)})
            yield FileChanged(str(proof))
            yield CheckResult(str(proof), "ok", None)
            yield Finished("completed", "Proved.", 1, session_id, "gemini/test",
                           Usage(input_tokens=1, output_tokens=1), 0.0, {})

        monkeypatch.setattr(bridge, "run_events", fake)
        bridge.run_lea(ctx)
        return run

    make_run("first_home.lean")
    second = make_run("second_home.lean")

    rows = store.list_artifacts_for_scope(project["id"])
    assert len(rows) == 1, "same declaration re-recorded updates in place"
    assert rows[0]["path"] == "second_home.lean"
    assert rows[0]["run_id"] == second["id"]


# --- AUDIT-2026-07-24 P1/P2: cheaper stream, cheaper cap check -----------------

def test_streamed_text_is_batched_but_arrives_whole_and_in_order(tmp_path, monkeypatch):
    """One SSE frame per model token was invisible to the browser (it polls every
    80 ms) and cost a dict + list slot retained in the broker for the run's life. The
    text must still arrive complete, and never after the step it preceded."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Batching")
    run = store.create_run(session["id"], "m", None, 3)

    def fake(config, messages, **kwargs):
        yield TurnStarted(1)
        for word in ("Proving ", "the ", "theorem ", "now."):
            yield AssistantTextDelta(word)
        yield ToolCalled("write_file", {"path": "p.lean"})
        for word in ("Then ", "checking ", "it."):
            yield AssistantTextDelta(word)
        yield Finished("completed", "done", 1, session["id"], "m",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {"messages": []})

    monkeypatch.setattr(bridge, "run_events", fake)
    monkeypatch.setattr(bridge, "load_config",
                        lambda: LeaConfig(model="m", max_turns=3, lea_root=tmp_path))
    events = Queue()
    bridge.run_lea(bridge.RunnerContext(session["id"], run["id"], "prove", 
                                        LeaConfig(model="m", max_turns=3, lea_root=tmp_path),
                                        events))
    frames = _drain(events)

    deltas = [f for f in frames if f["type"] == "assistant_delta"]
    # 7 tokens in, far fewer frames out — but not a single character lost.
    assert len(deltas) < 7
    assert "".join(f["payload"]["text"] for f in deltas) == (
        "Proving the theorem now.Then checking it."
    )
    # Ordering: everything narrated before the tool call is published before it.
    order = [f["type"] for f in frames]
    tool = next(i for i, f in enumerate(frames)
                if f["type"] == "status" and f["payload"].get("status") == "tool_call")
    before = "".join(f["payload"]["text"] for f in frames[:tool] if f["type"] == "assistant_delta")
    assert before == "Proving the theorem now."
    assert "done" in order


def test_a_trailing_batch_is_published_before_done(tmp_path, monkeypatch):
    """A run that ends mid-batch must not swallow its last words."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Trailing")
    run = store.create_run(session["id"], "m", None, 3)

    def fake(config, messages, **kwargs):
        yield TurnStarted(1)
        yield AssistantTextDelta("last words")
        raise RuntimeError("provider died mid-stream")

    monkeypatch.setattr(bridge, "run_events", fake)
    events = Queue()
    bridge.run_lea(bridge.RunnerContext(session["id"], run["id"], "prove",
                                        LeaConfig(model="m", max_turns=3, lea_root=tmp_path),
                                        events))
    frames = _drain(events)

    types = [f["type"] for f in frames]
    text = "".join(f["payload"]["text"] for f in frames if f["type"] == "assistant_delta")
    assert text == "last words"
    assert types.index("assistant_delta") < types.index("done")


def test_persisted_spend_is_read_once_per_window_not_once_per_event(tmp_path, monkeypatch):
    """The cap is re-checked on every UsageUpdated and every turn. Reading the DB each
    time meant several aggregates per second per active run, against the same
    single-writer SQLite the runs are writing to."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    bridge.reset_persisted_spend_cache()
    reads = []
    monkeypatch.setattr(store, "total_spend_usd", lambda: reads.append(1) or 0.0)

    for _ in range(50):
        assert bridge._persisted_spend_usd() == 0.0

    assert len(reads) == 1, f"{len(reads)} DB reads for 50 cap checks"


def test_a_run_reads_persisted_spend_once_not_per_usage_event(tmp_path, monkeypatch):
    """The end-to-end claim: a capped run emitting many usage events hits the database
    for the persisted total once, not once per event."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    bridge.reset_persisted_spend_cache()
    session = store.create_session("Capped")
    run = store.create_run(session["id"], "m", None, 3)
    reads = []
    monkeypatch.setattr(store, "total_spend_usd", lambda: reads.append(1) or 0.0)

    def fake(config, messages, **kwargs):
        for turn in range(1, 4):
            yield TurnStarted(turn)
            for _ in range(20):
                yield UsageUpdated(1, 1, 0.0001)
        yield Finished("completed", "done", 3, session["id"], "m",
                       Usage(input_tokens=60, output_tokens=60), 0.006, {"messages": []})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(bridge.RunnerContext(
        session["id"], run["id"], "prove",
        LeaConfig(model="m", max_turns=3, lea_root=tmp_path, max_spend_usd=100.0),
        Queue(),
    ))

    # 63 cap checks (3 turns + 60 usage events) against one database read.
    assert len(reads) == 1, f"{len(reads)} DB reads across 63 cap checks"


def test_the_spend_cache_is_scoped_to_its_database(tmp_path, monkeypatch):
    """The total is a property of ONE database. Production never repoints DB_PATH but
    tests do, and a cache that ignored which database it measured would hand one
    test's total to the next."""
    bridge.reset_persisted_spend_cache()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "first.sqlite3")
    monkeypatch.setattr(store, "total_spend_usd", lambda: 7.0)
    assert bridge._persisted_spend_usd() == 7.0

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "second.sqlite3")
    monkeypatch.setattr(store, "total_spend_usd", lambda: 3.0)
    assert bridge._persisted_spend_usd() == 3.0


# --- AUDIT-2026-07-24 X1: a blocked run must not hold up the queue -------------

def _seed_run(session_title, task, *, session_id=None):
    session = store.get_session(session_id) if session_id else store.create_session(session_title)
    run = store.create_run(session["id"], "gemini/test", None, 3)
    store.add_message(session["id"], "user", task, run["id"])
    return session, run["id"]


def test_a_session_busy_run_does_not_block_a_different_session(tmp_path, monkeypatch):
    """The dispatcher used to spin on the head of the queue until it was admissible.
    For SESSION_BUSY that means waiting on a DIFFERENT session's incumbent to wind
    down — a whole model call, or up to the 900s approval timeout — while unrelated
    runs sat behind it with slots free.
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    monkeypatch.setattr(
        bridge, "load_config",
        lambda: LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path),
    )
    monkeypatch.setattr(runregistry, "registry", RunRegistry(max_concurrent=4))
    runbroker._brokers.clear()

    release = Event()
    started: list[str] = []

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None):
        task = messages[-1]["content"]
        started.append(task)
        yield TurnStarted(1)
        if task == "incumbent":
            # Hold the session's slot until the test lets go — the stall the queue
            # used to inherit.
            release.wait(timeout=10)
        yield Finished("completed", "done", 1, session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {"messages": []})

    monkeypatch.setattr(bridge, "run_events", fake)

    session_a, incumbent = _seed_run("A", "incumbent")
    bridge.enqueue_run(incumbent)
    assert _wait_for(lambda: "incumbent" in started), "the incumbent takes its slot"

    # A follow-up in the SAME session: cannot be admitted while the incumbent holds it.
    _, blocked = _seed_run(None, "same-session follow-up", session_id=session_a["id"])
    bridge.enqueue_run(blocked)
    # ...and behind it, a run for an unrelated session, which has nothing to wait for.
    _, independent = _seed_run("B", "unrelated session")
    bridge.enqueue_run(independent)

    try:
        assert _wait_for(lambda: "unrelated session" in started, timeout=5.0), (
            "a run for a different session must start while another session is busy"
        )
        # The blocked one is still waiting, exactly as it should be.
        assert store.get_run(blocked)["status"] == "pending"
        assert "same-session follow-up" not in started
    finally:
        release.set()

    assert _wait_for(lambda: all(
        store.get_run(rid)["status"] not in {"pending", "running"}
        for rid in (incumbent, blocked, independent)
    )), "every run finishes once the incumbent releases"
    assert "same-session follow-up" in started, "the deferred run is retried, not dropped"


def test_deferred_runs_keep_their_relative_order(tmp_path, monkeypatch):
    """Setting a blocked run aside must not cost it its place relative to the other
    blocked runs — deferred entries are retried before newly arrived ones."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    monkeypatch.setattr(
        bridge, "load_config",
        lambda: LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path),
    )
    monkeypatch.setattr(runregistry, "registry", RunRegistry(max_concurrent=1))
    runbroker._brokers.clear()

    release = Event()
    started: list[str] = []

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None):
        task = messages[-1]["content"]
        started.append(task)
        yield TurnStarted(1)
        if task == "holder":
            release.wait(timeout=10)
        yield Finished("completed", "done", 1, session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {"messages": []})

    monkeypatch.setattr(bridge, "run_events", fake)

    _, holder = _seed_run("holder", "holder")
    bridge.enqueue_run(holder)
    assert _wait_for(lambda: "holder" in started)

    # At capacity 1 these all defer behind the holder; order among them must survive.
    waiting = []
    for label in ("q1", "q2", "q3"):
        _, run_id = _seed_run(label, label)
        bridge.enqueue_run(run_id)
        waiting.append(run_id)
    assert _wait_for(lambda: all(store.get_run(r)["status"] == "pending" for r in waiting))

    release.set()
    assert _wait_for(lambda: all(
        store.get_run(r)["status"] not in {"pending", "running"} for r in waiting
    ), timeout=15.0)
    assert [t for t in started if t.startswith("q")] == ["q1", "q2", "q3"]


def test_a_supersede_is_requested_once_per_incumbent(tmp_path, monkeypatch):
    """SESSION_BUSY asks the incumbent to stop. The old loop re-issued that on every
    100 ms poll for as long as the incumbent took to wind down."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    monkeypatch.setattr(runregistry, "registry", RunRegistry(max_concurrent=4))
    stops = []
    monkeypatch.setattr(bridge, "request_stop", lambda run_id: stops.append(run_id))

    session = store.create_session("S")
    incumbent = store.create_run(session["id"], "gemini/test", None, 3)
    runregistry.registry.try_admit(incumbent["id"], session["id"])

    follow_up = store.create_run(session["id"], "gemini/test", None, 3)
    store.add_message(session["id"], "user", "follow up", follow_up["id"])

    superseded: dict[str, str] = {}
    for _ in range(25):
        assert bridge._try_dispatch(follow_up["id"], superseded) == bridge._DEFER

    assert stops == [incumbent["id"]], f"asked {len(stops)} times, expected once"


# --- AUDIT-2026-07-24 C3: a failed promotion must not destroy the file it replaced --

class _FakeFinished:
    """The shape `collation.candidate_from_event` reads off a SubagentFinished."""

    def __init__(self, result_id, candidate_path, check_status="ok"):
        self.result_id = result_id
        self.candidate_path = candidate_path
        self.check_status = check_status


def _promotion_fixture(tmp_path, monkeypatch, *, existing=None):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    repo = tmp_path / "repo"
    (repo / "Lea" / "Misc").mkdir(parents=True)
    canonical = repo / "Lea" / "Misc" / "P.lean"
    if existing is not None:
        canonical.write_text(existing)

    scratch = repo / "scratch"
    scratch.mkdir()
    candidate = scratch / "P.lean"
    candidate.write_text("theorem t : True := by trivial\n")

    session = store.create_session("promote")
    run = store.create_run(session["id"], "m", None, 3)
    return repo, canonical, session, run, [_FakeFinished("child-1", str(candidate))]


def test_a_failed_reverification_restores_the_previous_proof(tmp_path, monkeypatch):
    """`promote` overwrites the canonical path, and the re-check that decides whether
    the candidate is any good runs afterwards — so a candidate that fails used to
    destroy a verified proof from an earlier run and leave itself in its place, with
    no code_step recording that the file had changed at all."""
    proven = "import Mathlib\n\ntheorem t : True := by trivial  -- the good proof\n"
    repo, canonical, session, run, results = _promotion_fixture(
        tmp_path, monkeypatch, existing=proven
    )
    monkeypatch.setattr(
        bridge, "_lean_check_file",
        lambda path: CheckResult(path, "error", "does not compile here"),
    )

    promoted = bridge._promote_winner(
        results, session_id=session["id"], run_id=run["id"], repo=repo,
        namespace=None, turn=1, events=Queue(),
    )

    assert promoted is None, "a candidate that fails re-verification is not promoted"
    assert canonical.read_text() == proven, "the previous proof must survive on disk"
    assert store.session_detail(session["id"])["code_steps"] == []


def test_a_failed_reverification_leaves_no_file_where_there_was_none(tmp_path, monkeypatch):
    """The other half: when nothing stood at the canonical path, a failed promotion
    must not leave the broken candidate behind either."""
    repo, canonical, session, run, results = _promotion_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        bridge, "_lean_check_file",
        lambda path: CheckResult(path, "error", "nope"),
    )

    assert bridge._promote_winner(
        results, session_id=session["id"], run_id=run["id"], repo=repo,
        namespace=None, turn=1, events=Queue(),
    ) is None
    assert not canonical.exists(), "the failed candidate must not be left in place"


def test_a_successful_promotion_still_replaces_the_file_and_records_it(tmp_path, monkeypatch):
    """The guard must not block the case it exists to protect: a candidate that DOES
    re-verify is promoted, overwrites what was there, and lands as a code_step."""
    repo, canonical, session, run, results = _promotion_fixture(
        tmp_path, monkeypatch, existing="old and worse\n"
    )
    monkeypatch.setattr(bridge, "_lean_check_file", lambda path: CheckResult(path, "ok", None))
    events = Queue()

    step = bridge._promote_winner(
        results, session_id=session["id"], run_id=run["id"], repo=repo,
        namespace=None, turn=1, events=events,
    )

    assert step is not None
    assert canonical.read_text() == "theorem t : True := by trivial\n"
    assert step["check_status"] == "ok"
    assert step["path"] == "Lea/Misc/P.lean"
    assert "promoted" in [f["payload"].get("status") for f in _drain(events)
                          if f["type"] == "status"]


def test_an_unreadable_previous_file_is_left_alone_rather_than_deleted(tmp_path, monkeypatch):
    """If the snapshot could not be taken there is nothing to put back. Deleting would
    turn a bad overwrite into outright data loss, so the file stays as promoted."""
    repo, canonical, session, run, results = _promotion_fixture(
        tmp_path, monkeypatch, existing="previous\n"
    )
    monkeypatch.setattr(bridge, "_lean_check_file", lambda path: CheckResult(path, "error", "no"))
    monkeypatch.setattr(bridge, "_snapshot_file", lambda path: None)  # snapshot failed

    assert bridge._promote_winner(
        results, session_id=session["id"], run_id=run["id"], repo=repo,
        namespace=None, turn=1, events=Queue(),
    ) is None
    assert canonical.exists(), "an unrecoverable file must not be deleted"


# --- AUDIT-2026-07-24 C6: bookkeeping must not rewrite a finished outcome ------

def _finishing_run(tmp_path, monkeypatch, *, result="proved"):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("C6")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    config = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None):
        proof = Path(working_dir) / "Lea" / "Misc" / "p.lean"
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_text("import Mathlib\n\ntheorem t : True := by trivial\n")
        yield TurnStarted(1)
        yield ToolCalled("write_file", {"path": str(proof)})
        yield FileChanged(str(proof))
        yield CheckResult(str(proof), "ok", None)
        yield Finished("completed", "It compiles.", 1, session["id"], "gemini/test",
                       Usage(input_tokens=10, output_tokens=5), 0.01, {"messages": []})

    monkeypatch.setattr(bridge, "run_events", fake)
    queue: Queue = Queue()
    ctx = bridge.RunnerContext(session["id"], run["id"], "prove", config, queue)
    return ctx, queue


@pytest.mark.parametrize("failing", ["replace_run_usage_breakdown", "set_run_transcript"])
def test_a_bookkeeping_failure_does_not_rewrite_a_proved_run(tmp_path, monkeypatch, failing):
    """The Finished handler persists the outcome, THEN records usage, the transcript,
    and the artifact index. A failure in any of those used to fall through to the
    outer handler, which marked the run 'failed' — discarding a real proof, and
    (because the derived session status reads the latest code step's run) making the
    whole session look broken."""
    ctx, queue = _finishing_run(tmp_path, monkeypatch)

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, failing, explode)

    bridge.run_lea(ctx)

    run = store.get_run(ctx.run_id)
    assert run["status"] == "proved", f"{failing} failing must not undo the outcome"
    assert run["result_kind"] == "proved"
    assert store.session_detail(ctx.session_id)["status"] == "proved"
    done = [f for f in _drain(queue) if f["type"] == "done"][-1]
    assert done["payload"]["status"] == "proved", "the client must be told the truth too"


def test_a_failure_before_the_outcome_is_persisted_still_fails_the_run(tmp_path, monkeypatch):
    """The guard must not swallow real failures: a run that dies before Finished has
    no terminal row, so it is still marked failed."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("C6-negative")
    run = store.create_run(session["id"], "gemini/test", None, 3)

    def fake(config, messages, **kwargs):
        yield TurnStarted(1)
        raise RuntimeError("the provider died")

    monkeypatch.setattr(bridge, "run_events", fake)
    queue: Queue = Queue()
    bridge.run_lea(bridge.RunnerContext(
        session["id"], run["id"], "prove",
        LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path), queue,
    ))

    assert store.get_run(run["id"])["status"] == "failed"
    assert [f for f in _drain(queue) if f["type"] == "done"][-1]["payload"]["status"] == "failed"


def test_each_bookkeeping_step_is_independent(tmp_path, monkeypatch):
    """One failing piece must not cost the others — they are separate records of the
    same finished run, not a transaction."""
    ctx, _ = _finishing_run(tmp_path, monkeypatch)
    monkeypatch.setattr(store, "replace_run_usage_breakdown",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    bridge.run_lea(ctx)

    # The transcript, which runs after the failing step, still landed.
    assert store.latest_transcript_for_session(ctx.session_id) is not None
    assert store.get_run(ctx.run_id)["status"] == "proved"


# --- AUDIT-2026-07-24 C10: a turn that left no transcript must not vanish ------

def _run_with_transcript(session_id, task, messages):
    run = store.create_run(session_id, "gemini/test", None, 3)
    store.add_message(session_id, "user", task, run["id"])
    store.update_run(run["id"], "proved")
    store.set_run_transcript(run["id"], messages)
    return run["id"]


def _crashed_run(session_id, task, detail="LiteLLM connection reset"):
    """A run that died mid-turn: terminal, but never reached Finished, so no transcript."""
    run = store.create_run(session_id, "gemini/test", None, 3)
    store.add_message(session_id, "user", task, run["id"])
    store.update_run(run["id"], "failed", result_kind="failed", result_detail=detail)
    return run["id"]


def test_a_crashed_run_is_reported_as_a_gap_in_the_replayed_history(tmp_path, monkeypatch):
    """`latest_transcript_for_session` silently falls back to the newest run that HAS a
    transcript. A run that crashed mid-turn stores none and simply disappears — the
    user watched that turn happen, and the next one replays a conversation in which it
    never did."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("gap")
    _run_with_transcript(session["id"], "first", [{"role": "user", "content": "first"}])
    _crashed_run(session["id"], "second")

    current = store.create_run(session["id"], "gemini/test", None, 3)
    gap = store.transcript_gap_for_session(session["id"], exclude_run_id=current["id"])

    assert len(gap) == 1
    note = bridge._transcript_gap_context(session["id"], current["id"])
    assert note is not None
    assert "1 earlier attempt" in note
    assert "LiteLLM connection reset" in note


def test_a_continuous_history_produces_no_note(tmp_path, monkeypatch):
    """The note must appear only when something is actually missing — otherwise it is
    noise in every follow-up."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("continuous")
    _run_with_transcript(session["id"], "first", [{"role": "user", "content": "first"}])

    current = store.create_run(session["id"], "gemini/test", None, 3)

    assert store.transcript_gap_for_session(session["id"], exclude_run_id=current["id"]) == []
    assert bridge._transcript_gap_context(session["id"], current["id"]) is None


def test_a_crash_before_the_newest_transcript_is_not_a_gap(tmp_path, monkeypatch):
    """A run that failed and was then superseded by a run that DID store a transcript
    is already represented — the later transcript covers the conversation."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("recovered")
    _crashed_run(session["id"], "died")
    time.sleep(0.01)  # distinct created_at
    _run_with_transcript(session["id"], "recovered", [{"role": "user", "content": "recovered"}])

    current = store.create_run(session["id"], "gemini/test", None, 3)

    assert store.transcript_gap_for_session(session["id"], exclude_run_id=current["id"]) == []


def test_an_active_run_is_not_a_gap(tmp_path, monkeypatch):
    """A pending or running run has not lost anything — it has not finished."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("live")
    _run_with_transcript(session["id"], "first", [{"role": "user", "content": "first"}])
    store.create_run(session["id"], "gemini/test", None, 3)  # left pending

    current = store.create_run(session["id"], "gemini/test", None, 3)

    assert store.transcript_gap_for_session(session["id"], exclude_run_id=current["id"]) == []


def test_the_gap_note_reaches_the_task_the_agent_is_given(tmp_path, monkeypatch):
    """End to end: the note must actually be prepended to the run's task, not just
    computable."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("gap-e2e")
    _run_with_transcript(session["id"], "first", [{"role": "user", "content": "first"}])
    _crashed_run(session["id"], "second")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    store.add_message(session["id"], "user", "third", run["id"])

    seen = {}

    def fake(config, messages, **kwargs):
        seen["task"] = messages[-1]["content"]
        yield TurnStarted(1)
        yield Finished("completed", "done", 1, session["id"], "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {"messages": []})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(bridge.RunnerContext(
        session["id"], run["id"], "third",
        LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path), Queue(),
    ))

    assert "earlier attempt" in seen["task"]
    assert seen["task"].rstrip().endswith("third"), "the user's actual request stays last"


# --- AUDIT-2026-07-24 C5: the SafeVerify gate is enforced at promotion ---------

def test_a_safeverify_rejected_candidate_is_never_promoted(tmp_path, monkeypatch):
    """`collation` documents that a SafeVerify-REJECTED candidate must never become the
    proof of record — "promoting a cheat to the canonical file is exactly the failure
    SafeVerify exists to catch" — but `_TIER_SV_REJECTED` was unreachable because
    nothing populated `safeverify_status`. The guarantee was documented, not enforced."""
    proven = "theorem t : True := by trivial  -- the good proof\n"
    repo, canonical, session, run, results = _promotion_fixture(
        tmp_path, monkeypatch, existing=proven
    )
    monkeypatch.setattr(bridge, "_lean_check_file", lambda p: CheckResult(p, "ok", None))
    monkeypatch.setattr(bridge, "_safe_verify_file",
                        lambda p: VerifyResult("rejected", "sorry reachable through an import"))

    promoted = bridge._promote_winner(
        results, session_id=session["id"], run_id=run["id"], repo=repo,
        namespace=None, turn=1, events=Queue(),
    )

    assert promoted is None, "a cheat must not become the proof of record"
    assert canonical.read_text() == proven, "and the good proof it replaced must survive"


@pytest.mark.parametrize("status", ["unavailable", "error"])
def test_safeverify_being_unavailable_does_not_block_promotion(tmp_path, monkeypatch, status):
    """collation's stated degradation: a missing SafeVerify must never *mis*-rank, only
    fail to catch a cheat. Blocking here would silently disable collation on any
    install that skipped the SafeVerify build (`npm run setup -- --skip-verify`)."""
    repo, canonical, session, run, results = _promotion_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "_lean_check_file", lambda p: CheckResult(p, "ok", None))
    monkeypatch.setattr(bridge, "_safe_verify_file", lambda p: VerifyResult(status, None))

    step = bridge._promote_winner(
        results, session_id=session["id"], run_id=run["id"], repo=repo,
        namespace=None, turn=1, events=Queue(),
    )

    assert step is not None
    assert canonical.read_text() == "theorem t : True := by trivial\n"


def test_a_crashing_safeverify_does_not_block_promotion(tmp_path, monkeypatch):
    """An audit that raises is 'not run', not 'rejected'."""
    repo, canonical, session, run, results = _promotion_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "_lean_check_file", lambda p: CheckResult(p, "ok", None))
    monkeypatch.setattr(bridge, "_safe_verify_file",
                        lambda p: (_ for _ in ()).throw(RuntimeError("binary missing")))

    assert bridge._promote_winner(
        results, session_id=session["id"], run_id=run["id"], repo=repo,
        namespace=None, turn=1, events=Queue(),
    ) is not None


def test_promotion_falls_through_to_the_next_candidate(tmp_path, monkeypatch):
    """Ranking is best-first, so a rejected winner must not end the attempt — the next
    promotable candidate gets its turn."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    repo = tmp_path / "repo"
    (repo / "Lea" / "Misc").mkdir(parents=True)
    scratch = repo / "scratch"
    scratch.mkdir()
    # The shorter proof ranks first; make it the cheat.
    (scratch / "A.lean").write_text("theorem t : True := by trivial\n")
    (scratch / "B.lean").write_text("theorem t : True := by\n  exact trivial\n")
    session = store.create_session("fallthrough")
    run = store.create_run(session["id"], "m", None, 3)
    results = [
        _FakeFinished("cheat", str(scratch / "A.lean")),
        _FakeFinished("honest", str(scratch / "B.lean")),
    ]

    monkeypatch.setattr(bridge, "_lean_check_file", lambda p: CheckResult(p, "ok", None))
    monkeypatch.setattr(
        bridge, "_safe_verify_file",
        lambda p: VerifyResult("rejected" if p.endswith("A.lean") else "ok", None),
    )

    step = bridge._promote_winner(
        results, session_id=session["id"], run_id=run["id"], repo=repo,
        namespace=None, turn=1, events=Queue(),
    )

    assert step is not None
    assert "promoted_from" not in step or step["path"].endswith("B.lean")
    assert (repo / "Lea" / "Misc" / "B.lean").exists()
    assert not (repo / "Lea" / "Misc" / "A.lean").exists(), "the cheat must leave no trace"


def test_coordinator_toolset_does_not_freeze_out_dynamic_tools(tmp_path, monkeypatch):
    """v2.5 regression — the bug that made every MCP and HTTP tool unreachable from the UI.

    `_with_subagents` used to resolve `build_toolset(None)` in the ADAPTER and pass the
    result as an explicit allowlist. That snapshot is taken before the run starts, so it
    contained only the built-ins — and an explicit list then excluded every tool that
    registers once the run begins (MCP servers, declarative HTTP tools). The server would
    start, warm up and report 23 tools, and not one of them could ever be called.

    The invariant: the coordinator must ask for "the default set PLUS these opt-ins",
    never for a frozen list of names.
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    cfg, _ = bridge._with_subagents(
        LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path))

    assert cfg.tools is None, "a frozen allowlist cannot contain run-time tools"
    assert "spawn_subagent" in cfg.extra_tools
    assert "safe_verify" in cfg.extra_tools

    # And a tool registered AFTER this config was built must still reach the model.
    import lea.tools  # noqa: F401
    from lea.http_tools import register_http_tools
    from lea.registry import build_toolset, pop_scope, push_scope

    scope = push_scope()
    try:
        register_http_tools([{"name": "late_tool", "description": "d",
                              "url": "https://api.github.com/x"}])
        names = [s["name"] for s in build_toolset(cfg.tools, cfg.extra_tools)[0]]
    finally:
        pop_scope(scope)

    assert "late_tool" in names, "a tool registered during the run was excluded"
    assert "spawn_subagent" in names
