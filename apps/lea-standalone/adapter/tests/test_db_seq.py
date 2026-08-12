"""C4 tests: one timeline table, one ordering key.

This file used to pin a *shared per-session `seq` counter* across two tables
(`messages` + `code_steps`), so a thread was an `ORDER BY seq` merge. That counter
was a read-modify-write across both tables and, under concurrent writers, silently
issued duplicate seqs (~110 collisions in 200; see `db.write()` and
`test_db_seq_concurrent.py`).

v2.3 merges the tables. `timeline.id` is an autoincrement primary key, so ordering
is assigned by SQLite under the write lock — the race is unrepresentable rather
than fixed, because there is no read-then-write left to lose. `seq` is that id.

Two assertions changed deliberately:
  * seq no longer restarts per session — it's global. Nothing reads it as a count;
    every read filters by session_id, so only *order within* a session was ever the
    contract.
  * seq is no longer contiguous. It never really was: the frontend already invents
    fractional seqs (`+0.5`, `+1e-4` in useProofStream) to slot live rows between
    persisted ones.
"""

import sqlite3

from app import db, store


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def _cols(db_path, table):
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}


def test_ordering_is_the_primary_key_not_a_hand_rolled_column(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    with sqlite3.connect(tmp_path / "test.sqlite3") as conn:
        sql = conn.execute("select sql from sqlite_master where name = 'timeline'").fetchone()[0]
    assert "autoincrement" in sql.lower(), "the position must be assigned by SQLite, not by us"
    # there is no second counter to keep in sync — keeping it in sync was the bug
    assert "seq" not in _cols(tmp_path / "test.sqlite3", "timeline")


def test_order_is_monotonic_across_messages_and_code(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Interleaved")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    # a realistic thread: user prompt, agent code, agent narration, agent code
    m1 = store.add_message(session["id"], "user", "prove it", run["id"])
    c1 = store.add_code_step(session["id"], run["id"], "p.lean", content="proof-1")
    m2 = store.add_message(session["id"], "assistant", "trying norm_num", run["id"])
    c2 = store.add_code_step(session["id"], run["id"], "p.lean", content="proof-2")
    seqs = [m1["seq"], c1["seq"], m2["seq"], c2["seq"]]
    assert seqs == sorted(seqs), "the thread must order in the sequence it happened"
    assert len(set(seqs)) == 4, "two rows must never share a position"


def test_timeline_merges_by_seq(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Merge")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.add_message(session["id"], "user", "prove it", run["id"])
    store.add_code_step(session["id"], run["id"], "p.lean", content="proof-a")
    store.add_message(session["id"], "assistant", "done", run["id"])

    detail = store.session_detail(session["id"])
    # the API still exposes two lists; they're now two views of one ordered table
    merged = sorted(
        [("message", m["seq"]) for m in detail["messages"]]
        + [("code", c["seq"]) for c in detail["code_steps"]],
        key=lambda t: t[1],
    )
    assert [kind for kind, _ in merged] == ["message", "code", "message"]


def test_each_session_reads_only_its_own_rows_in_order(tmp_path, monkeypatch):
    """The property that replaces "restarts at 1": ids are global, but a session's
    thread is still exactly its own rows, in order."""
    _fresh_db(tmp_path, monkeypatch)
    s1 = store.create_session("S1")
    s2 = store.create_session("S2")
    a = store.add_message(s1["id"], "user", "first in s1")
    b = store.add_message(s2["id"], "user", "first in s2")
    c = store.add_message(s1["id"], "user", "second in s1")
    assert a["seq"] < b["seq"] < c["seq"], "ids are global and monotonic"

    s1_msgs = store.session_detail(s1["id"])["messages"]
    assert [m["content"] for m in s1_msgs] == ["first in s1", "second in s1"]
    assert [m["seq"] for m in s1_msgs] == sorted(m["seq"] for m in s1_msgs)
    assert [m["content"] for m in store.session_detail(s2["id"])["messages"]] == ["first in s2"]
