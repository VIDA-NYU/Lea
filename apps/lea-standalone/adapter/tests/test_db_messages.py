"""C2 tests: the edit-note link (D11), now that messages live on `timeline`.

A user's canvas-edit explanation is a *normal* transcript row — no bespoke channel
— distinguished only by `kind='edit_note'`. This file used to also pin a
`commit_sha` column linking the note to the edit's git commit, plus a CHECK that an
edit_note must carry one.

**Both are gone, and the link is weaker now — deliberately.** The note's tie to the
edit it explains is positional: the note is written immediately after the step, and
`edit_notes_since(session_id, seq)` reads it back by position. That was already the
real mechanism — the reader has always been positional, and the sha was never
consulted to find a note. The CHECK was enforcing that a *decorative* field was
non-null. What it genuinely bought was "an edit_note is never orphaned", and
`test_an_edit_note_lands_after_the_step_it_explains` is what now covers that.

`kind` and `author` also stop being the same concept spelled twice: `kind` is what a
row is, `author` is who made it (the split OpenHands' `SourceType` draws and opencode
conflates). The old `kind` column defaulted to 'assistant' — a *role* value used as a
kind default — so it lied for every row nobody set explicitly.
"""

import sqlite3

from app import db, store


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def test_kind_and_author_are_separate_constrained_columns(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    with sqlite3.connect(tmp_path / "test.sqlite3") as conn:
        cols = {row[1] for row in conn.execute("pragma table_info(timeline)").fetchall()}
        sql = conn.execute("select sql from sqlite_master where name='timeline'").fetchone()[0]
    assert {"kind", "author"} <= cols
    assert "commit_sha" not in cols, "the note no longer points into a second store"
    # both are CHECKed enums now, not free text that drifts ('compaction' added in 0006
    # for the durable G1/G3 context-compaction marker; 'diagnostic' in 0007 for the v2.4
    # persisted failure rows)
    assert "kind in ('message', 'code', 'edit_note', 'compaction', 'diagnostic')" in sql
    assert "author in ('user', 'agent', 'environment')" in sql


def test_edit_note_round_trips(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Edit note")
    note = store.add_message(
        session["id"], "user", "Swapped the import to Mathlib.Tactic.", kind="edit_note",
    )
    assert note["kind"] == "edit_note"
    assert note["role"] == "user"
    # it surfaces through the normal transcript read — no special plumbing (D11)
    surfaced = store.session_detail(session["id"])["messages"][0]
    assert surfaced["kind"] == "edit_note"
    assert surfaced["content"] == "Swapped the import to Mathlib.Tactic."


def test_an_edit_note_lands_after_the_step_it_explains(tmp_path, monkeypatch):
    """What the dropped `commit_sha` CHECK was really protecting: a note that
    explains an edit must be findable *from* that edit. The reader has always been
    positional (`edit_notes_since`), so this is the property that matters — and it's
    now pinned directly instead of via a column nothing read."""
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Note link")
    run = store.create_run(session["id"], "m", None, 3)
    agent_step = store.add_code_step(
        session["id"], run["id"], "p.lean", content="theorem t : True := by sorry", author="agent"
    )
    store.add_code_step(session["id"], None, "p.lean", content="theorem t : True := by trivial",
                        author="user")
    store.add_message(session["id"], "user", "filled in the sorry", kind="edit_note")

    assert store.edit_notes_since(session["id"], agent_step["seq"]) == ["filled in the sorry"]
    # and a note is not visible from a position after it
    latest = store.session_detail(session["id"])["messages"][-1]["seq"]
    assert store.edit_notes_since(session["id"], latest) == []


def test_ordinary_message_defaults_are_unchanged(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Plain")
    msg = store.add_message(session["id"], "assistant", "Working on it.")
    assert msg["kind"] == "assistant"
    assert msg["role"] == "assistant"


def test_role_survives_the_round_trip(tmp_path, monkeypatch):
    """`role` is reconstructed from `author` on the way out, so it has to still be
    the value the caller passed — the API shape predates the merge and the frontend
    reads `role`."""
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Roles")
    store.add_message(session["id"], "user", "prove it")
    store.add_message(session["id"], "assistant", "on it")
    assert [m["role"] for m in store.session_detail(session["id"])["messages"]] == [
        "user", "assistant",
    ]
