"""C3 tests: session.status is the working-copy verdict, derived (D14/P2).

A session has no stored status column — its status is computed from the latest
code_step's check_status on every read, so it can never drift from the source of
truth. Run lifecycle (running/done/failed) is NOT here; it lives on runs.status.
These tests pin the vocabulary, the latest-wins rule, and that `list_sessions`
and `session_detail` agree.
"""

import sqlite3

from app import db, store


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def _list_status(session_id):
    return next(s for s in store.list_sessions() if s["id"] == session_id)["status"]


def test_no_stored_status_column(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    with sqlite3.connect(tmp_path / "test.sqlite3") as conn:
        cols = {row[1] for row in conn.execute("pragma table_info(sessions)").fetchall()}
    assert "status" not in cols  # derived on read, never stored


def test_session_with_no_code_is_empty(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Nothing yet")
    assert store.session_detail(session["id"])["status"] == "empty"
    assert _list_status(session["id"]) == "empty"


def test_status_follows_latest_code_step_verdict(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Verdict tracking")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)

    store.add_code_step(session["id"], run["id"], "p.lean", content="proof-1", check_status="error")
    assert store.session_detail(session["id"])["status"] == "error"
    assert _list_status(session["id"]) == "error"

    # a later step's verdict wins — the working copy moved on
    store.add_code_step(session["id"], run["id"], "p.lean", content="proof-2", check_status="ok")
    assert store.session_detail(session["id"])["status"] == "ok"
    assert _list_status(session["id"]) == "ok"


def test_checked_agent_step_uses_run_outcome_for_proof_or_disproof(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    proved = store.create_session("Proved")
    proved_run = store.create_run(proved["id"], "gpt-4o", "openai", 3)
    store.add_code_step(
        proved["id"], proved_run["id"], "p.lean",
        content="proof-1", check_status="ok", artifact_kind="proof",
    )
    store.update_run(proved_run["id"], "needs_review", result_kind="needs_review")
    assert store.session_detail(proved["id"])["status"] == "proved"
    assert _list_status(proved["id"]) == "proved"

    disproved = store.create_session("Disproved")
    disproved_run = store.create_run(disproved["id"], "gpt-4o", "openai", 3)
    store.add_code_step(
        disproved["id"], disproved_run["id"], "d.lean",
        content="proof-2", check_status="ok", artifact_kind="proof",
    )
    store.update_run(disproved_run["id"], "disproved", result_kind="disproved")
    assert store.session_detail(disproved["id"])["status"] == "disproved"
    assert _list_status(disproved["id"]) == "disproved"

    # A later human edit is a new working copy with no run outcome attached.
    store.add_code_step(
        disproved["id"], None, "d.lean",
        content="proof-3", author="user", check_status="ok", artifact_kind="proof",
    )
    assert store.session_detail(disproved["id"])["status"] == "proved"
    assert _list_status(disproved["id"]) == "proved"


def test_checked_artifact_kind_drives_primary_status_when_run_needs_review(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    definition = store.create_session("Definition")
    definition_run = store.create_run(definition["id"], "gpt-4o", "openai", 3)
    store.add_code_step(
        definition["id"], definition_run["id"], "d.lean",
        content="proof-1", check_status="ok", artifact_kind="definition",
    )
    store.update_run(definition_run["id"], "needs_review", result_kind="needs_review")
    assert store.session_detail(definition["id"])["status"] == "defined"
    assert _list_status(definition["id"]) == "defined"

    unknown = store.create_session("Unknown")
    unknown_run = store.create_run(unknown["id"], "gpt-4o", "openai", 3)
    store.add_code_step(
        unknown["id"], unknown_run["id"], "u.lean",
        content="proof-2", check_status="ok", artifact_kind="unknown",
    )
    store.update_run(unknown_run["id"], "needs_review", result_kind="needs_review")
    assert store.session_detail(unknown["id"])["status"] == "ok"
    assert _list_status(unknown["id"]) == "ok"


def test_checked_ok_without_artifact_kind_stays_generic_ok(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Legacy row")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.add_code_step(session["id"], run["id"], "p.lean", content="proof-1", check_status="ok")
    store.update_run(run["id"], "needs_review", result_kind="needs_review")
    assert store.session_detail(session["id"])["status"] == "ok"
    assert _list_status(session["id"]) == "ok"


def test_step_without_verdict_is_unchecked(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Pending check")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    # D6: a write is committed before lean_check returns -> verdict not yet known
    store.add_code_step(session["id"], run["id"], "p.lean", content="proof-3")
    assert store.session_detail(session["id"])["status"] == "unchecked"
    assert _list_status(session["id"]) == "unchecked"


def test_session_with_active_run_and_no_code_is_running(tmp_path, monkeypatch):
    # A freshly registered formalization (e.g. an Overleaf-driven run) has a
    # pending/running run but no code step yet -> it should read 'running' rather
    # than 'empty', so it surfaces as in-progress in the list the moment it starts.
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Just started")
    store.create_run(session["id"], "gpt-4o", "openai", 3)  # status defaults to 'pending'
    assert store.session_detail(session["id"])["status"] == "running"
    assert _list_status(session["id"]) == "running"


def test_active_run_does_not_override_an_existing_verdict(tmp_path, monkeypatch):
    # D14 still holds once code exists: the working-copy verdict wins even while a
    # run is active — run lifecycle does not leak into a session that has content.
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Has code, still running")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)  # stays 'pending'
    store.add_code_step(session["id"], run["id"], "p.lean", content="proof-9", check_status="ok")
    assert store.session_detail(session["id"])["status"] == "ok"
    assert _list_status(session["id"]) == "ok"


def test_list_sessions_surfaces_active_run_count_alongside_verdict(tmp_path, monkeypatch):
    # v2.3 item 13: because a session that already has code reads its verdict
    # ('ok'/'proved') even while re-running (D14), the sidebar can't learn about a
    # live background run from `status` alone. `list_sessions` carries a separate
    # `active_run_count` so a running dot can light regardless of the derived status.
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Proved, and re-running")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)  # stays 'pending'
    store.add_code_step(session["id"], run["id"], "p.lean", content="proof-9", check_status="ok")

    row = next(s for s in store.list_sessions() if s["id"] == session["id"])
    assert row["status"] == "ok"           # verdict unchanged (D14)
    assert row["active_run_count"] == 1    # ...but the live run is still visible

    store.update_run(run["id"], "proved")  # run leaves the active set
    row = next(s for s in store.list_sessions() if s["id"] == session["id"])
    assert row["active_run_count"] == 0     # count drops; status stays the verdict
    assert row["status"] == "ok"


def test_running_flips_to_verdict_when_run_finishes(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Lifecycle")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    assert _list_status(session["id"]) == "running"  # no code yet, active run
    store.update_run(run["id"], "proved")
    assert _list_status(session["id"]) == "empty"  # run no longer active, still no code


def test_sessions_digest_changes_on_create_and_run_state(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    empty = store.sessions_digest()
    session = store.create_session("D")
    after_create = store.sessions_digest()
    assert after_create != empty  # a new session moves the digest

    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    after_run = store.sessions_digest()
    assert after_run != after_create  # an active run moves it

    store.update_run(run["id"], "proved")
    after_finish = store.sessions_digest()
    assert after_finish != after_run  # leaving the active set moves it too


def test_user_edit_verdict_overrides_after_a_run(tmp_path, monkeypatch):
    # P2: a user edit after the run completes changes the working-copy verdict
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Human takes over")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.add_code_step(session["id"], run["id"], "p.lean", content="proof-a", check_status="ok")
    # user edits outside the run (run_id=None) and breaks it
    store.add_code_step(session["id"], None, "p.lean", content="proof-b", author="user", check_status="error")
    assert store.session_detail(session["id"])["status"] == "error"
