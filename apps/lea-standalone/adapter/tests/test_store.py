import sqlite3
import json

from app import db, store


def test_init_db_creates_the_authoritative_v2_schema(tmp_path, monkeypatch):
    # v2 clean rebuild: create-table is the single authoritative schema — there are
    # NO in-place ALTER migrations (a schema change means a fresh DB). So init_db on
    # an empty file must produce every column directly.
    db_path = tmp_path / "test.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    db.init_db()

    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute("pragma table_info(runs)").fetchall()]
    assert "cost_usd" in columns
    assert "api_run_id" in columns
    assert "pending_approval" in columns
    assert "transcript" in columns  # the multi-turn replay conversation (D16)
    with sqlite3.connect(db_path) as conn:
        timeline_columns = [row[1] for row in conn.execute("pragma table_info(timeline)").fetchall()]
    # a code row carries its content (via a blob) + verdict; the git pointer is gone
    assert "after_blob_id" in timeline_columns
    assert "author" in timeline_columns
    assert "check_status" in timeline_columns
    assert "check_detail" in timeline_columns
    assert "artifact_kind" in timeline_columns
    assert "commit_sha" not in timeline_columns
    assert "used_project_formalizations" not in timeline_columns
    with sqlite3.connect(db_path) as conn:
        usage_columns = [row[1] for row in conn.execute("pragma table_info(run_usage_breakdown)").fetchall()]
    assert "phase" in usage_columns
    assert "cost_usd" in usage_columns


def test_session_status_ignores_scratch_files(tmp_path, monkeypatch):
    """M14: a session is 'ok' only when a real proof compiles — a throwaway
    scratch/probe file (exact?/apply? scratchpad) that compiles must not mask
    the real proof's verdict, nor count as the session having a proof."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    session = store.create_session("Prove something")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    # The real proof errored…
    store.add_code_step(session["id"], run["id"], "Lea/Misc/Foo.lean",
                        content="proof-a", check_status="error")
    # …then a later scratch probe compiled cleanly.
    store.add_code_step(session["id"], run["id"], "Lea/Misc/scratch.lean",
                        content="proof-b", check_status="ok")

    detail = store.session_detail(session["id"])
    assert detail["status"] == "error", "scratch 'ok' must not mask the real proof's error"
    summary = next(s for s in store.list_sessions() if s["id"] == session["id"])
    assert summary["status"] == "error"
    assert len(detail["code_steps"]) == 2  # the canvas still shows both


def test_safe_verify_persists_on_latest_run(tmp_path, monkeypatch):
    """M24: a standalone /verify verdict is stored on the session's latest run and
    surfaced as session_detail.safe_verify, so it survives a reload."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Verify me")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.add_code_step(session["id"], run["id"], "Lea/Misc/Foo.lean",
                        content="proof-a", check_status="ok")

    assert store.session_detail(session["id"])["safe_verify"] is None
    store.set_session_safe_verify(session["id"], "ok", None)
    sv = store.session_detail(session["id"])["safe_verify"]
    assert sv["status"] == "ok" and sv["run_id"] == run["id"]


def test_session_status_scratch_only_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Probes only")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.add_code_step(session["id"], run["id"], "Lea/Misc/Scratch.lean",  # capital → case-insensitive
                        content="proof-c", check_status="ok")
    # While the run is active, an in-progress session reads 'running' — an active run
    # with no *real* proof yet still surfaces as in-progress (the 'running' feature).
    assert store.session_detail(session["id"])["status"] == "running"
    assert next(s for s in store.list_sessions() if s["id"] == session["id"])["status"] == "running"
    # Once the run ends, scratch-only means no real proof → 'empty' (M14).
    store.update_run(run["id"], "failed")
    detail = store.session_detail(session["id"])
    assert detail["status"] == "empty", "only scratch probes means no real proof yet"
    summary = next(s for s in store.list_sessions() if s["id"] == session["id"])
    assert summary["status"] == "empty"


def test_session_messages_and_code_steps_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    session = store.create_session("Prove 2 + 2 = 4")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.set_run_api_run_id(run["id"], "api-run-1")
    store.set_run_pending_approval(
        run["id"],
        {
            "type": "approval_requested",
            "approval_id": "ap-1",
            "tier": "theorem_translation",
            "candidate": 1,
            "lean_code": "theorem t : True := by sorry",
        },
    )
    message = store.add_message(session["id"], "user", "Prove 2 + 2 = 4", run["id"])
    step = store.add_code_step(
        session["id"],
        run["id"],
        "workspace/proofs/test.lean",
        content="theorem t : True := by trivial",
        summary="Turn 2: wrote the proof skeleton.",
        turn=2,
        check_status="ok",
    )
    status_event = store.add_status_event(
        session["id"],
        run["id"],
        "Captured Lean file update: workspace/proofs/test.lean",
        status="code_step",
        step_number=step["seq"],
    )

    detail = store.session_detail(session["id"])

    assert detail is not None
    assert detail["messages"][0]["id"] == message["id"]
    assert store.get_run(run["id"])["api_run_id"] == "api-run-1"
    assert detail["active_run"]["id"] == run["id"]
    assert detail["active_run"]["pending_approval"]["approval_id"] == "ap-1"
    assert detail["code_steps"][0]["id"] == step["id"]
    # one timeline (C4): the message came first, the code step after it. This used
    # to assert seq == 1 and 2 — a shared per-session counter. It's now the table's
    # autoincrement id, so the *order* is the contract and the values are not.
    assert detail["messages"][0]["seq"] < detail["code_steps"][0]["seq"]
    assert detail["code_steps"][0]["author"] == "agent"
    assert detail["code_steps"][0]["code"] == "theorem t : True := by trivial"
    assert detail["code_steps"][0]["check_status"] == "ok"
    assert detail["code_steps"][0]["summary"].startswith("Turn 2")
    assert detail["code_steps"][0]["turn"] == 2
    assert detail["status_events"][0]["id"] == status_event["id"]
    assert detail["status_events"][0]["step_number"] == step["seq"]
    assert detail["status_events"][0]["status"] == "code_step"
    assert detail["usage_breakdown"] == []


def test_run_transcript_round_trip_and_latest_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    session = store.create_session("Multi-turn")
    # the runs table carries a transcript column (model-replay conversation, D16)
    with sqlite3.connect(tmp_path / "test.sqlite3") as conn:
        cols = {row[1] for row in conn.execute("pragma table_info(runs)").fetchall()}
    assert "transcript" in cols

    first = store.create_run(session["id"], "gpt-4o", "openai", 3)
    second = store.create_run(session["id"], "gpt-4o", "openai", 3)

    # nothing stored yet
    assert store.latest_transcript_for_session(session["id"]) is None

    msgs1 = [{"role": "user", "content": "A"}]
    store.set_run_transcript(first["id"], msgs1)
    assert store.latest_transcript_for_session(session["id"]) == msgs1
    # the current run is excluded so a run never replays its own (absent) transcript
    assert store.latest_transcript_for_session(session["id"], exclude_run_id=first["id"]) is None

    # a later run's transcript wins as the replay base
    msgs2 = msgs1 + [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
    store.set_run_transcript(second["id"], msgs2)
    assert store.latest_transcript_for_session(session["id"]) == msgs2


def test_run_usage_breakdown_persists_in_session_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    session = store.create_session("Usage rows")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.replace_run_usage_breakdown(
        run["id"],
        [
            {
                "phase": "theorem_translation",
                "label": "Theorem translation preflight candidate 1",
                "candidate": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "cost_usd": 0.001,
                "event_count": 1,
            },
            {
                "phase": "proof_turn",
                "label": "Turn 1",
                "turn": 1,
                "input_tokens": 100,
                "output_tokens": 25,
                "cost_usd": 0.01,
                "event_count": 2,
            },
        ],
    )

    detail = store.session_detail(session["id"])

    assert [row["label"] for row in detail["usage_breakdown"]] == [
        "Theorem translation preflight candidate 1",
        "Turn 1",
    ]
    assert detail["usage_breakdown"][0]["total_tokens"] == 15
    assert detail["usage_breakdown"][1]["turn"] == 1


def test_usage_breakdown_falls_back_to_raw_event_log(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(store, "RAW_EVENT_LOG_DIR", tmp_path / "logs")
    db.init_db()

    session = store.create_session("Raw log usage")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.RAW_EVENT_LOG_DIR.mkdir()
    log_path = store.RAW_EVENT_LOG_DIR / f"{run['id']}.jsonl"
    frames = [
        {"type": "usage_updated", "payload": {"type": "usage_updated", "input_tokens": 10, "output_tokens": 5, "cost": 0.001}},
        {"type": "approval_requested", "payload": {"type": "approval_requested", "candidate": 1}},
        {"type": "turn_started", "payload": {"type": "turn_started", "turn": 1}},
        {"type": "usage_updated", "payload": {"type": "usage_updated", "input_tokens": 100, "output_tokens": 25, "cost": 0.01}},
        {"type": "usage_updated", "payload": {"type": "usage_updated", "input_tokens": 20, "output_tokens": 10, "cost": 0.002}},
        {
            "type": "finished",
            "payload": {
                "type": "finished",
                "usage": {"input_tokens": 140, "output_tokens": 45},
                "cost": 0.015,
            },
        },
    ]
    log_path.write_text("\n".join(json.dumps(frame) for frame in frames))

    detail = store.session_detail(session["id"])

    assert [row["label"] for row in detail["usage_breakdown"]] == [
        "Theorem translation preflight candidate 1",
        "Turn 1",
        "Unattributed usage",
    ]
    assert detail["usage_breakdown"][1]["input_tokens"] == 120
    assert detail["usage_breakdown"][1]["event_count"] == 2
    assert detail["usage_breakdown"][2]["input_tokens"] == 10


def test_session_detail_includes_approval_events_from_raw_log(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(store, "RAW_EVENT_LOG_DIR", tmp_path / "logs")
    db.init_db()

    session = store.create_session("Approval history")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.RAW_EVENT_LOG_DIR.mkdir()
    log_path = store.RAW_EVENT_LOG_DIR / f"{run['id']}.jsonl"
    frames = [
        {
            "type": "approval_requested",
            "payload": {
                "type": "approval_requested",
                "approval_id": "ap-1",
                "tier": "theorem_translation",
                "candidate": 1,
                "lean_code": "theorem demo : True := by\n  trivial",
                "theorem_name": "demo",
                "check_result": "warning",
            },
        },
        {
            "type": "approval_resolved",
            "payload": {
                "type": "approval_resolved",
                "approval_id": "ap-1",
                "decision": "reject",
                "feedback": "Use a stronger statement.",
            },
        },
    ]
    log_path.write_text("\n".join(json.dumps(frame) for frame in frames))

    detail = store.session_detail(session["id"])

    assert len(detail["approval_events"]) == 1
    approval = detail["approval_events"][0]
    assert approval["approval_id"] == "ap-1"
    assert approval["candidate"] == 1
    assert approval["lean_code"].startswith("theorem demo")
    assert approval["decision"] == "reject"
    assert approval["feedback"] == "Use a stronger statement."


def test_session_usage_rollups_include_multiple_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    session = store.create_session("Aggregate usage")
    first = store.create_run(session["id"], "gpt-4o", "openai", 3)
    second = store.create_run(session["id"], "claude-sonnet", "anthropic", 3)
    store.add_message(session["id"], "user", "first", first["id"])
    store.add_message(session["id"], "assistant", "done", first["id"])
    store.add_message(session["id"], "user", "second", second["id"])
    store.update_run(first["id"], "proved", input_tokens=10, output_tokens=5, cost_usd=0.02)
    store.update_run(second["id"], "proved", input_tokens=20, output_tokens=15, cost_usd=0.07)

    summary = next(item for item in store.list_sessions() if item["id"] == session["id"])
    detail = store.session_detail(session["id"])

    assert summary["input_tokens"] == 30
    assert summary["output_tokens"] == 20
    assert summary["total_tokens"] == 50
    assert abs(summary["cost_usd"] - 0.09) < 1e-9
    assert summary["message_count"] == 3
    assert summary["run_count"] == 2
    assert summary["primary_model"] == "claude-sonnet"
    assert set(summary["models"]) == {"gpt-4o", "claude-sonnet"}
    assert detail["total_tokens"] == 50


def test_usage_stats_global_daily_and_model_rollups(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    session = store.create_session("Stats")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.add_message(session["id"], "user", "prove it", run["id"])
    store.update_run(run["id"], "proved", input_tokens=100, output_tokens=25, cost_usd=0.125)

    stats = store.usage_stats()

    assert stats["global"]["session_count"] == 1
    assert stats["global"]["message_count"] == 1
    assert stats["global"]["total_tokens"] == 125
    assert abs(stats["global"]["cost_usd"] - 0.125) < 1e-9
    assert stats["daily"][0]["total_tokens"] == 125
    assert stats["models"][0]["model"] == "gpt-4o"
    assert stats["models"][0]["session_count"] == 1


def test_get_or_create_project_is_idempotent_by_slug(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    first = store.get_or_create_project("doc-a", title="Doc A")
    again = store.get_or_create_project("doc-a", title="ignored second title")

    assert first["id"] == again["id"]
    assert first["slug"] == "doc-a"
    assert again["title"] == "Doc A"  # not overwritten on the second call
    assert len(store.list_projects()) == 1
    assert store.get_project_by_slug("doc-a")["id"] == first["id"]
    assert store.get_project_by_slug("missing") is None


def test_create_project_derives_namespace_and_repo_path_from_slug(tmp_path, monkeypatch):
    # D22/D30: namespace + repo_path are NOT NULL and derived from the slug when not
    # given explicitly (the Overleaf tag-only path supplies neither).
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    p = store.create_project("eps-delta", title="Epsilon Delta")
    assert p["namespace"] == "Lea.EpsDelta"          # slug -> UpperCamel module segment
    assert p["repo_path"] == "proofs/Lea/EpsDelta"   # namespace -> shared dir/repo
    assert p["description"] is None
    assert p["remote_url"] is None


def test_create_project_accepts_explicit_namespace_repo_and_description(tmp_path, monkeypatch):
    # P2's project service passes computed values explicitly.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    p = store.create_project(
        "my-proj",
        title="My Proj",
        description="a test project",
        namespace="Lea.MyProj",
        repo_path="proofs/Lea/MyProj",
    )
    assert p["namespace"] == "Lea.MyProj"
    assert p["repo_path"] == "proofs/Lea/MyProj"
    assert p["description"] == "a test project"


def test_project_namespace_lookup_and_identity_update(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    p = store.create_project("doc-a", title="Doc A")
    assert store.get_project_by_namespace(p["namespace"])["id"] == p["id"]

    updated = store.update_project_identity(
        p["id"],
        title="Fourier Notes",
        namespace="Lea.FourierNotes",
        repo_path="proofs/Lea/FourierNotes",
    )
    assert updated["slug"] == "doc-a"
    assert updated["title"] == "Fourier Notes"
    assert updated["namespace"] == "Lea.FourierNotes"
    assert updated["repo_path"] == "proofs/Lea/FourierNotes"
    assert store.get_project_by_namespace("Lea.FourierNotes")["id"] == p["id"]


def test_project_namespace_derivation_handles_digit_initial_slug(tmp_path, monkeypatch):
    # A Lean module segment can't start with a digit — guard with a prefix.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    assert store.project_namespace_for_slug("2cat") == "Lea.P2cat"
    assert store.repo_path_for_namespace("Lea.P2cat") == "proofs/Lea/P2cat"


def test_update_project_edits_metadata_only(tmp_path, monkeypatch):
    # D31: title/description/remote_url are editable; the slug/namespace/repo_path
    # chain is immutable (D22) and must survive an update untouched.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    p = store.create_project("proj-x", title="Proj X")
    updated = store.update_project(
        p["id"], title="Renamed", description="now described",
        remote_url="https://github.com/me/proj-x.git",
    )
    assert updated["title"] == "Renamed"
    assert updated["description"] == "now described"
    assert updated["remote_url"] == "https://github.com/me/proj-x.git"
    assert updated["slug"] == "proj-x"                 # immutable
    assert updated["namespace"] == p["namespace"]      # immutable
    assert updated["repo_path"] == p["repo_path"]      # immutable
    # Passing None leaves a field untouched.
    again = store.update_project(p["id"], title="Renamed Twice")
    assert again["description"] == "now described"
    assert store.update_project("no-such-id") is None


def test_project_files_crud(tmp_path, monkeypatch):
    # D27: project_files is an index over bytes that live in the project repo.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    p = store.create_project("files-proj", title="Files Proj")
    assert store.list_project_files(p["id"]) == []

    f = store.create_project_file(
        p["id"], filename="paper.pdf", stored_path=".lea/files/paper.pdf",
        mime="application/pdf", kind="upload", extracted_path=".lea/files/paper.pdf.txt",
    )
    assert f["filename"] == "paper.pdf"
    assert f["kind"] == "upload"
    assert f["extracted_path"] == ".lea/files/paper.pdf.txt"

    listed = store.list_project_files(p["id"])
    assert len(listed) == 1 and listed[0]["id"] == f["id"]
    assert store.get_project_file(f["id"])["filename"] == "paper.pdf"

    assert store.delete_project_file(f["id"]) is True
    assert store.delete_project_file(f["id"]) is False
    assert store.list_project_files(p["id"]) == []


def test_session_listing_splits_loose_vs_in_project(tmp_path, monkeypatch):
    # D30: loose (project_id IS NULL) = the sidebar Chats group; in-project sessions
    # are reached through the project window, not the sidebar.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    proj = store.create_project("grp", title="Group")
    loose = store.create_session("loose one")
    in_proj = store.create_session("in project", project_id=proj["id"])

    loose_ids = {s["id"] for s in store.list_loose_sessions()}
    proj_ids = {s["id"] for s in store.list_project_sessions(proj["id"])}
    all_ids = {s["id"] for s in store.list_sessions()}

    assert loose["id"] in loose_ids and in_proj["id"] not in loose_ids
    assert in_proj["id"] in proj_ids and loose["id"] not in proj_ids
    assert {loose["id"], in_proj["id"]} <= all_ids


def test_usage_stats_sessions_carry_project_slug_for_per_document_totals(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    project = store.get_or_create_project("doc-a", title="Doc A")

    # Two sessions tagged to the same Overleaf document (two formalized theorems).
    s1 = store.create_session("thm_one", project_id=project["id"])
    r1 = store.create_run(s1["id"], "gpt-4o", "openai", 3, project_id=project["id"])
    store.update_run(r1["id"], "proved", input_tokens=200, output_tokens=50, cost_usd=0.10)

    s2 = store.create_session("thm_two", project_id=project["id"])
    r2 = store.create_run(s2["id"], "gpt-4o", "openai", 3, project_id=project["id"])
    store.update_run(r2["id"], "proved", input_tokens=300, output_tokens=75, cost_usd=0.20)

    # An untagged session (e.g. interactive UI run) must not count toward the doc.
    s3 = store.create_session("loose")
    r3 = store.create_run(s3["id"], "gpt-4o", "openai", 3)
    store.update_run(r3["id"], "proved", input_tokens=400, output_tokens=100, cost_usd=0.30)

    stats = store.usage_stats()

    doc_sessions = [s for s in stats["sessions"] if s["project_slug"] == "doc-a"]
    assert len(doc_sessions) == 2
    doc_input = sum(s["input_tokens"] for s in doc_sessions)
    doc_output = sum(s["output_tokens"] for s in doc_sessions)
    doc_cost = sum(s["cost_usd"] for s in doc_sessions)
    assert doc_input == 500
    assert doc_output == 125
    assert abs(doc_cost - 0.30) < 1e-9

    # All-time still includes every run (the untagged one too).
    assert stats["global"]["input_tokens"] == 900
    assert stats["global"]["output_tokens"] == 225
    assert abs(stats["global"]["cost_usd"] - 0.60) < 1e-9


def test_latest_agent_code_step_and_edit_notes_since(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Divergence helpers")
    run = store.create_run(session["id"], "m", None, 3)
    agent_step = store.add_code_step(session["id"], run["id"], "p.lean",
                                     content="proof-a", author="agent", turn=1)
    # a user edit + note land after the agent's step
    store.add_code_step(session["id"], None, "p.lean", content="proof-b", author="user")
    store.add_message(session["id"], "user", "swapped a lemma", None, kind="edit_note")

    latest_agent = store.latest_agent_code_step(session["id"])
    assert latest_agent["code"] == "proof-a"  # the agent step, not the later user one
    # notes recorded after the agent's timeline position
    assert store.edit_notes_since(session["id"], agent_step["seq"]) == ["swapped a lemma"]
    # nothing after a later position
    assert store.edit_notes_since(session["id"], 9999) == []


def test_fail_stale_active_runs_reaps_running_but_keeps_queued(tmp_path, monkeypatch):
    """Startup crash recovery (Phase 2 semantics): a `running` row has no live
    worker after a restart → failed. A `pending` row is an honest queue entry
    that recovery re-enqueues — it must NOT be reaped. Terminal rows untouched."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Reap me")
    queued_pending = store.create_run(session["id"], "m", None, 3)
    interrupted_running = store.create_run(session["id"], "m", None, 3)
    store.update_run(interrupted_running["id"], "running")
    finished = store.create_run(session["id"], "m", None, 3)
    store.update_run(finished["id"], "needs_review", result_kind="needs_review",
                     result_detail="NEEDS_REVIEW")

    assert store.fail_stale_active_runs() == 1

    assert store.get_run(queued_pending["id"])["status"] == "pending", \
        "queued work survives a restart (recovery re-enqueues it)"
    reaped_running = store.get_run(interrupted_running["id"])
    assert reaped_running["status"] == "failed"
    assert "restarted" in reaped_running["result_detail"]
    # the finished run keeps its real outcome
    survivor = store.get_run(finished["id"])
    assert survivor["status"] == "needs_review"
    assert survivor["result_detail"] == "NEEDS_REVIEW"

    # idempotent: nothing left to reap
    assert store.fail_stale_active_runs() == 0


def test_queue_position_counts_earlier_pending_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Queue me")
    first = store.create_run(session["id"], "m", None, 3)
    second = store.create_run(session["id"], "m", None, 3)
    assert store.queue_position(first["id"]) == 0
    assert store.queue_position(second["id"]) == 1
    store.update_run(first["id"], "running")
    assert store.queue_position(first["id"]) is None
    assert store.queue_position(second["id"]) == 0


def test_session_detail_run_rows_carry_usage(tmp_path, monkeypatch):
    """The Overleaf companion reads a run's tokens/cost off the session-detail
    run row (fetchApiRunUsage). These columns were missing from the per-run
    select, so every companion job recorded $0 — caught by the Phase 1
    integration harness (PLAN-system-hardening)."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Usage on run rows")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.update_run(run["id"], "proved", input_tokens=11, output_tokens=7, cost_usd=0.002)

    row = store.session_detail(session["id"])["runs"][0]
    assert row["id"] == run["id"]
    assert row["input_tokens"] == 11
    assert row["output_tokens"] == 7
    assert abs(row["cost_usd"] - 0.002) < 1e-9


def test_upsert_artifact_scopes_by_project_then_session(tmp_path, monkeypatch):
    """PLAN-system-hardening 4.1: one row per (scope, declaration). Project
    runs share a scope across sessions; loose sessions scope to themselves."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session_a = store.create_session("a")
    session_b = store.create_session("b")
    run_a = store.create_run(session_a["id"], "m", None, 3)
    run_b = store.create_run(session_b["id"], "m", None, 3)

    # Loose sessions: same declaration name in two sessions = two rows.
    store.upsert_artifact(project_id=None, session_id=session_a["id"], run_id=run_a["id"],
                          declaration_name="foo", kind="proof", path="foo.lean", module_name=None)
    store.upsert_artifact(project_id=None, session_id=session_b["id"], run_id=run_b["id"],
                          declaration_name="foo", kind="proof", path="foo.lean", module_name=None)
    assert len(store.list_artifacts_for_scope(session_a["id"])) == 1
    assert len(store.list_artifacts_for_scope(session_b["id"])) == 1

    # Project scope: re-recording the same declaration updates in place, even
    # from a different session of the same project.
    store.upsert_artifact(project_id="proj-1", session_id=session_a["id"], run_id=run_a["id"],
                          declaration_name="bar", kind="proof", path="old.lean", module_name="Lea.P.old")
    updated = store.upsert_artifact(project_id="proj-1", session_id=session_b["id"], run_id=run_b["id"],
                                    declaration_name="bar", kind="definition", path="new.lean", module_name="Lea.P.new")
    rows = store.list_artifacts_for_scope("proj-1")
    assert len(rows) == 1
    assert rows[0]["path"] == "new.lean"
    assert rows[0]["kind"] == "definition"
    assert rows[0]["session_id"] == session_b["id"]
    assert updated["created_at"] != updated["updated_at"] or rows[0]["path"] == "new.lean"


# --- AUDIT-2026-07-24 C1: global totals must not be a page of sessions ---------

def _seed_sessions_with_spend(count, cost_each, tokens_each=10):
    """`count` sessions, each with one finished run costing `cost_each`."""
    for i in range(count):
        session = store.create_session(f"S{i}")
        run = store.create_run(session["id"], "gpt-4o", "openai", 3)
        store.add_message(session["id"], "user", "prove it", run["id"])
        store.update_run(
            run["id"], "proved",
            input_tokens=tokens_each, output_tokens=tokens_each, cost_usd=cost_each,
        )


def test_global_usage_counts_every_session_past_the_list_page(tmp_path, monkeypatch):
    """`usage_stats()["global"]` summed `list_sessions()`, which ends in `limit 100`.
    So beyond 100 sessions the reported spend *fell* as older ones aged out of the
    window — and `max_spend_usd` is enforced against that number."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    _seed_sessions_with_spend(120, cost_each=1.0)

    stats = store.usage_stats()

    assert stats["global"]["session_count"] == 120
    assert stats["global"]["message_count"] == 120
    assert abs(stats["global"]["cost_usd"] - 120.0) < 1e-9
    assert stats["global"]["total_tokens"] == 120 * 20
    # The rendered session table is still a page — that part is intentional.
    assert len(stats["sessions"]) == 100
    # ...and the global block must agree with the daily/model rollups beside it,
    # which were already full-table aggregates and so silently disagreed.
    assert abs(sum(d["cost_usd"] for d in stats["daily"]) - stats["global"]["cost_usd"]) < 1e-9
    assert abs(sum(m["cost_usd"] for m in stats["models"]) - stats["global"]["cost_usd"]) < 1e-9


def test_total_spend_usd_sees_every_run(tmp_path, monkeypatch):
    """The scalar the spend cap reads. Same bug, and the one that actually let a
    capped workspace keep spending."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    _seed_sessions_with_spend(150, cost_each=0.5)

    assert abs(store.total_spend_usd() - 75.0) < 1e-9


def test_spend_limit_is_reached_past_the_list_page(tmp_path, monkeypatch):
    """The end-to-end consequence: with 150 sessions at $0.50 and a $100 cap, the
    old path summed the newest 100 ($50) and reported 'under the limit' forever."""
    from app import settings as settings_service

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    _seed_sessions_with_spend(150, cost_each=0.5)

    assert settings_service.current_spend_usd() > 50.0
    assert settings_service.spend_limit_reached(100.0) is False   # $75 < $100
    assert settings_service.spend_limit_reached(70.0) is True     # $75 >= $70


def test_origin_rollup_counts_every_session_and_agrees_with_global(tmp_path, monkeypatch):
    """The rollup's contract is that it agrees with `global`. Both were folded from
    the same truncated page, so they agreed while both were wrong."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    for i in range(60):
        for origin in ("ui", "overleaf"):
            session = store.create_session(f"{origin}-{i}", origin=origin)
            run = store.create_run(session["id"], "gpt-4o", "openai", 3)
            store.update_run(run["id"], "proved", input_tokens=5, output_tokens=5, cost_usd=0.25)

    stats = store.usage_stats()
    by_origin = {row["origin"]: row for row in stats["origins"]}

    assert by_origin["ui"]["session_count"] == 60
    assert by_origin["overleaf"]["session_count"] == 60
    total = sum(row["cost_usd"] for row in stats["origins"])
    assert abs(total - stats["global"]["cost_usd"]) < 1e-9
    assert abs(total - 30.0) < 1e-9


# --- AUDIT-2026-07-24 P3: code steps arrive with their bytes, not N+1 -----------
# Asserted as CONNECTIONS OPENED rather than elapsed time: the defect was structural
# (one extra connection + one extra query per step, from building the rows after the
# connection had closed), so counting the structure is both deterministic and the
# thing that actually regresses.

def _counting_open(monkeypatch):
    calls = []
    real = db._open

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "_open", counted)
    return calls


def _session_with_steps(count, *, project_id=None, path="Lea/Misc/p.lean"):
    session = store.create_session("Big session", project_id=project_id)
    run = store.create_run(session["id"], "m", None, 3, project_id=project_id)
    for i in range(count):
        store.add_code_step(
            session["id"], run["id"], path,
            content=f"theorem t{i} : True := by trivial\n", author="agent", turn=i,
            check_status="ok",
        )
    return session


def test_session_detail_does_not_open_a_connection_per_code_step(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = _session_with_steps(40)

    opened = _counting_open(monkeypatch)
    detail = store.session_detail(session["id"])

    assert len(detail["code_steps"]) == 40
    # Content still arrives in full — the point is how, not whether.
    assert detail["code_steps"][0]["code"] == "theorem t0 : True := by trivial\n"
    assert detail["code_steps"][-1]["code"] == "theorem t39 : True := by trivial\n"
    # Before the fix this was ~40 connections for the blobs alone, on top of the
    # handful session_detail legitimately makes.
    assert len(opened) < 15, f"session_detail opened {len(opened)} connections for 40 steps"


def test_code_step_reads_carry_their_content_from_one_query(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = _session_with_steps(3)

    for read in (
        lambda: store.latest_code_step_for_path(session["id"], "Lea/Misc/p.lean"),
        lambda: store.latest_agent_code_step(session["id"]),
        lambda: store.latest_agent_code_step_for_path(session["id"], "Lea/Misc/p.lean"),
    ):
        opened = _counting_open(monkeypatch)
        step = read()
        assert step["code"] == "theorem t2 : True := by trivial\n"
        assert len(opened) == 1, f"{len(opened)} connections for one code-step read"


def test_graph_reads_skip_blob_hydration_but_keep_the_verdict(tmp_path, monkeypatch):
    """`include_content=False` is what makes /graph cheap: it reads only the verdict
    and the session attribution, across every revision of every file."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    project = store.create_project("proj", title="Proj")
    _session_with_steps(25, project_id=project["id"])

    with_content = store.code_steps_for_project_path(project["id"], "Lea/Misc/p.lean")
    opened = _counting_open(monkeypatch)
    without = store.code_steps_for_project_path(
        project["id"], "Lea/Misc/p.lean", include_content=False
    )

    assert len(opened) == 1
    assert len(without) == len(with_content) == 25
    # Everything the graph actually reads is identical...
    for lean, full in zip(without, with_content):
        assert lean["check_status"] == full["check_status"]
        assert lean["session_id"] == full["session_id"]
        assert lean["created_at"] == full["created_at"]
    # ...and only the bytes it never looks at are withheld.
    assert without[0]["code"] == ""
    assert with_content[0]["code"] == "theorem t24 : True := by trivial\n"


def test_set_code_step_check_returns_the_updated_row_with_its_content(tmp_path, monkeypatch):
    """The back-fill path re-reads the row it just updated; that read goes through the
    same join, so a verdict landing on a step must not blank the canvas."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("S")
    run = store.create_run(session["id"], "m", None, 3)
    step = store.add_code_step(session["id"], run["id"], "p.lean", content="proof\n", author="agent")

    updated = store.set_code_step_check(step["id"], "ok", None, artifact_kind="proof")

    assert updated["code"] == "proof\n"
    assert updated["check_status"] == "ok"
    assert "blob_content" not in updated


# --- AUDIT-2026-07-24 C4: WITHDRAWN — this always worked ----------------------
# The audit claimed search was truncated to the 100 most-recently-updated sessions,
# so an older match was unreachable. That was wrong: the LIKE is part of the WHERE
# clause, and SQL applies WHERE before LIMIT, so the cap has always bounded the number
# of MATCHES, not the window searched. These tests pin the behaviour that was already
# correct — they pass against the pre-"fix" code too, which is how the error surfaced.


def test_search_finds_a_session_older_than_the_default_page(tmp_path, monkeypatch):
    """Search reaches an old session regardless of how many newer ones exist. Search is
    the ONLY path to an in-project session (the sidebar hides them), so this is worth
    pinning even though it was never broken."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    needle = store.create_session("Cauchy completeness")
    for i in range(140):  # every one of these is newer than the needle
        store.create_session(f"unrelated {i}")

    hits = store.search_sessions("cauchy")

    assert [h["id"] for h in hits] == [needle["id"]]


def test_search_still_honours_its_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    for i in range(40):
        store.create_session(f"matching {i}")

    assert len(store.search_sessions("matching", limit=5)) == 5
    assert len(store.search_sessions("matching")) == 30  # the default


# --- AUDIT-2026-07-24 C9: the cascade must take the artifact index with it -----

def test_deleting_a_project_removes_its_artifact_rows(tmp_path, monkeypatch):
    """A stale row survives a re-created slug and makes `_ensure_artifacts_backfilled`
    think the fresh project is already indexed, so its real proofs never get imported."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    project = store.create_project("doomed", title="Doomed")
    session = store.create_session("s", project_id=project["id"])
    store.upsert_artifact(
        project_id=project["id"], session_id=session["id"], run_id=None,
        declaration_name="Lea.Doomed.thm", kind="proof", path="p.lean",
        module_name="Lea.Doomed.p",
    )
    # ...and one scoped to the SESSION rather than the project (a loose-session artifact).
    store.upsert_artifact(
        project_id=None, session_id=session["id"], run_id=None,
        declaration_name="Lea.Misc.loose", kind="proof", path="q.lean", module_name=None,
    )
    assert store.list_artifacts_for_scope(project["id"])
    assert store.list_artifacts_for_scope(session["id"])

    assert store.delete_project_cascade(project["id"]) is True

    assert store.list_artifacts_for_scope(project["id"]) == []
    assert store.list_artifacts_for_scope(session["id"]) == []


# --- AUDIT-2026-07-24 C7: the pending row is claimed atomically ----------------

def test_only_one_caller_can_claim_a_pending_run(tmp_path, monkeypatch):
    """`fail_pending_run` and `claim_pending_run` are the same conditional UPDATE from
    opposite sides — the interrupt endpoint and the run driver. Exactly one wins."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("race")

    run = store.create_run(session["id"], "m", None, 3)
    assert store.claim_pending_run(run["id"]) is True
    assert store.claim_pending_run(run["id"]) is False, "a second claim must lose"
    assert store.fail_pending_run(run["id"], "too late") is False, "the row is no longer pending"
    assert store.get_run(run["id"])["status"] == "running"

    other = store.create_run(session["id"], "m", None, 3)
    assert store.fail_pending_run(other["id"], "interrupted before start") is True
    assert store.claim_pending_run(other["id"]) is False, "the driver must decline"
    assert store.get_run(other["id"])["status"] == "failed"
    assert store.get_run(other["id"])["result_detail"] == "interrupted before start"


def test_concurrent_claims_produce_exactly_one_winner(tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("race")
    run = store.create_run(session["id"], "m", None, 3)

    results = []
    start = threading.Barrier(8)

    def contend(n):
        start.wait(timeout=10)
        if n % 2:
            results.append(("claim", store.claim_pending_run(run["id"])))
        else:
            results.append(("fail", store.fail_pending_run(run["id"], "interrupted")))

    threads = [threading.Thread(target=contend, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sum(1 for _kind, won in results if won) == 1, results


# --- AUDIT-2026-07-24 P4: an idle client costs no query ------------------------

def test_the_change_token_moves_only_on_a_real_write(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()

    before = store.sessions_change_token()
    assert store.sessions_change_token() == before, "reading must not move the token"

    session = store.create_session("moves it")
    after_create = store.sessions_change_token()
    assert after_create > before

    run = store.create_run(session["id"], "m", None, 3)
    assert store.sessions_change_token() > after_create
    after_run = store.sessions_change_token()

    store.update_run(run["id"], "proved")
    assert store.sessions_change_token() > after_run
