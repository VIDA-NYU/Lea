"""C1 tests: a code step holds proof *content* (D5/D7/D8, inverted in v2.3).

This file used to pin the opposite contract — "a code_step is no longer the proof
text; git owns content. A row is a pointer (`commit_sha` + `path`)". v2.3 inverts
it: SQL owns the content, a step points at a content-addressed `artifact_blobs`
row, and git is demoted to transport. The tests below pin the new shape on a fresh
DB: content round-trips, blobs dedupe, the row is reachable without a second store,
and a stale pointer-style call fails loudly.

The old file's core worry — "never silently store proof text where a commit_sha
belongs" — is now unrepresentable in the good direction: content is the only thing
a step stores.
"""

import sqlite3

import pytest

from app import db, store


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def _columns(db_path, table):
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}


def test_a_step_points_at_a_blob_not_a_commit(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    cols = _columns(tmp_path / "test.sqlite3", "timeline")
    assert {"after_blob_id", "author", "path", "check_status", "check_detail"} <= cols
    # the git pointer is gone from the write path entirely
    assert "commit_sha" not in cols


def test_a_code_row_must_have_content_or_admit_it_lost_it(tmp_path, monkeypatch):
    """The schema CHECK that replaces the old `NOT NULL commit_sha`. A code row with
    no blob and no `content_lost` flag is the exact state that let a step claim
    content it couldn't produce — so the DB rejects it."""
    _fresh_db(tmp_path, monkeypatch)
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as conn:
            conn.execute(
                "insert into timeline (session_id, kind, author, path, created_at) "
                "values ('s', 'code', 'agent', 'p.lean', 't')"
            )


def test_run_id_is_nullable_for_user_edits(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Edit outside a run")
    # a user canvas edit (D9) has no run — run_id=None must be accepted
    step = store.add_code_step(
        session["id"], None, "workspace/proofs/p.lean",
        content="theorem t : True := by trivial", author="user",
    )
    assert step["run_id"] is None
    assert step["author"] == "user"


def test_cascade_is_recorded_as_a_reason_not_an_author(tmp_path, monkeypatch):
    """`author` used to be free text ('agent' | 'user' | 'cascade') because the
    schema had nowhere else to put *why* a step happened. 'cascade' — the Overleaf
    lean pane's downstream re-verification — is a reason, not a person: the file is
    still the agent's work. The new schema CHECKs author, so the reason moves to
    `data` and the row stays attributable."""
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Cascade re-check")
    step = store.add_code_step(
        session["id"], None, "workspace/proofs/Lea/P/dependent.lean",
        content="import Lea.P.Base\ntheorem d : True := by trivial",
        author="cascade",
        summary="Re-checked after edit to compactness_criterion",
        check_status="error", check_detail="unknown identifier: compactness_criterion",
    )
    assert step["run_id"] is None
    assert step["author"] == "agent", "a cascade re-check is still the agent's file"
    assert step["check_status"] == "error"
    with sqlite3.connect(tmp_path / "test.sqlite3") as conn:
        data = conn.execute("select data from timeline where kind='code'").fetchone()[0]
    assert "cascade" in data, "the reason must survive somewhere"
    detail = store.session_detail(session["id"])
    assert detail["code_steps"][0]["check_status"] == "error"


def test_add_code_step_round_trips_content_and_verdict(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Round trip")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    code = "theorem p : 2 + 2 = 4 := by norm_num\n"
    step = store.add_code_step(
        session["id"], run["id"], "workspace/proofs/p.lean",
        content=code, author="agent", summary="wrote it", turn=4,
        check_status="error", check_detail="p.lean:2:0: error: unknown identifier",
    )
    assert step["code"] == code
    assert step["author"] == "agent"
    assert step["turn"] == 4
    assert step["check_status"] == "error"
    assert step["check_detail"].startswith("p.lean:2:0")
    # and it reads back with its content — no hydrate step, no second store
    detail = store.session_detail(session["id"])
    assert detail["code_steps"][0]["code"] == code


def test_identical_content_is_stored_once(tmp_path, monkeypatch):
    """Blobs are content-addressed, so a revert, a cascade re-check, or an unchanged
    save costs a row — not another copy of the proof. This is what makes storing full
    content per step affordable instead of a reason to keep a pointer store."""
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Dedup")
    code = "theorem t : True := by trivial\n"
    a = store.add_code_step(session["id"], None, "p.lean", content=code, author="agent")
    b = store.add_code_step(session["id"], None, "p.lean", content=code, author="user")
    assert a["code"] == b["code"] == code
    with sqlite3.connect(tmp_path / "test.sqlite3") as conn:
        assert conn.execute("select count(*) from artifact_blobs").fetchone()[0] == 1
        blobs = conn.execute(
            "select distinct after_blob_id from timeline where kind='code'"
        ).fetchall()
    assert len(blobs) == 1, "same bytes must resolve to the same blob"


def test_old_pointer_style_call_fails_loudly(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Loud failure")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    # A caller still passing a commit_sha must raise, never store a 40-char sha as
    # if it were a proof — the mirror of the v1-vs-v2 guard this file used to hold.
    with pytest.raises(TypeError):
        store.add_code_step(session["id"], run["id"], "p.lean", commit_sha="a" * 40)


def test_seq_orders_within_a_session_and_is_not_reused_across_them(tmp_path, monkeypatch):
    """`seq` used to be a per-session counter that restarted at 1 — a hand-rolled
    read-modify-write that silently issued duplicates under concurrent writers.
    It's now the timeline's autoincrement id: global, assigned under the write lock.

    So it no longer restarts per session, and that's the point — nothing reads it as
    a count. It is only ever used to *order within* a session and to compare against
    another row's position, both of which a global monotonic id satisfies. (The
    frontend already invented fractional seqs — `+0.5`, `+1e-4` in useProofStream —
    to slot live rows between persisted ones, so contiguity was never real.)"""
    _fresh_db(tmp_path, monkeypatch)
    s1 = store.create_session("S1")
    r1 = store.create_run(s1["id"], "gpt-4o", "openai", 3)
    a = store.add_code_step(s1["id"], r1["id"], "p.lean", content="a")
    b = store.add_code_step(s1["id"], r1["id"], "p.lean", content="b")
    assert a["seq"] < b["seq"], "steps must order within a session"

    s2 = store.create_session("S2")
    r2 = store.create_run(s2["id"], "gpt-4o", "openai", 3)
    c = store.add_code_step(s2["id"], r2["id"], "p.lean", content="c")
    assert c["seq"] > b["seq"], "ids are global; a new session does not restart them"
    # and each session still reads back only its own, in order
    assert [s["seq"] for s in store.session_detail(s1["id"])["code_steps"]] == [a["seq"], b["seq"]]
    assert [s["seq"] for s in store.session_detail(s2["id"])["code_steps"]] == [c["seq"]]
