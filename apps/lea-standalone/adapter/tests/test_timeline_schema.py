"""Revision 0003: the `timeline` + `artifact_blobs` schema.

The point of this table is that guarantees are *enforced* rather than promised, so
these tests assert the database actually rejects bad rows. A CHECK nobody tests is
a docstring again — which is the bug that started this whole workstream.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from app import db


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    with db.connect() as conn:
        conn.execute(
            "insert into sessions (id, title, origin, created_at, updated_at)"
            " values ('s1','t','ui','t','t')"
        )
    return tmp_path / "test.sqlite3"


def _blob(conn, content="x", sha="a" * 64, bid="b1"):
    conn.execute(
        "insert into artifact_blobs (id, sha256, content, created_at) values (?,?,?,'t')",
        (bid, sha, content),
    )
    return bid


def _code(conn, **over):
    row = dict(session_id="s1", kind="code", author="agent", path="p.lean",
               after_blob_id=None, content=None, content_lost=0, check_status=None,
               artifact_kind=None)
    row.update(over)
    conn.execute(
        "insert into timeline (session_id, kind, author, path, after_blob_id, content,"
        " content_lost, check_status, artifact_kind, created_at)"
        " values (:session_id,:kind,:author,:path,:after_blob_id,:content,"
        ":content_lost,:check_status,:artifact_kind,'t')",
        row,
    )


# ── the ordering key ──────────────────────────────────────────────────────────

def test_id_is_allocated_by_the_db_not_the_caller(tmp_path, monkeypatch):
    """No seq column: the read-modify-write that produced duplicate seqs has
    nowhere to live."""
    _fresh(tmp_path, monkeypatch)
    with db.connect() as conn:
        cols = {r[1] for r in conn.execute("pragma table_info(timeline)")}
    assert "seq" not in cols, "a seq column reintroduces the max()+1 race"
    assert "id" in cols


def test_concurrent_inserts_get_unique_ids_without_any_transaction(tmp_path, monkeypatch):
    """The whole justification for the merge: uniqueness is a primary key, so it
    holds with no BEGIN IMMEDIATE and no discipline to forget."""
    _fresh(tmp_path, monkeypatch)
    ids: list[int] = []
    errors: list[str] = []
    lock = threading.Lock()

    def writer(t: int):
        for i in range(25):
            try:
                with db.connect() as conn:  # NOT db.write()
                    cur = conn.execute(
                        "insert into timeline (session_id, kind, author, content, created_at)"
                        " values ('s1','message','agent',?,'t')",
                        (f"t{t}-i{i}",),
                    )
                    with lock:
                        ids.append(cur.lastrowid)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"unexpected errors: {errors[:2]}"
    assert len(ids) == 200
    assert len(set(ids)) == 200, f"{200 - len(set(ids))} duplicate ids"


def test_ids_are_not_reused_after_delete(tmp_path, monkeypatch):
    """Why AUTOINCREMENT and not a bare `integer primary key`: a reused id would let
    a new row inherit a deleted row's position in the thread."""
    _fresh(tmp_path, monkeypatch)
    with db.connect() as conn:
        conn.execute("insert into timeline (session_id,kind,author,content,created_at)"
                     " values ('s1','message','agent','a','t')")
        top = conn.execute("select max(id) from timeline").fetchone()[0]
        conn.execute("delete from timeline where id = ?", (top,))
        cur = conn.execute("insert into timeline (session_id,kind,author,content,created_at)"
                           " values ('s1','message','agent','b','t')")
    assert cur.lastrowid > top, f"id {cur.lastrowid} reused a deleted row's position"


# ── the CHECKs actually reject ────────────────────────────────────────────────

def test_blob_sha256_is_unique(tmp_path, monkeypatch):
    """Dedup is the schema's job, so put_blob can be a dumb insert-or-find."""
    _fresh(tmp_path, monkeypatch)
    with db.connect() as conn:
        _blob(conn, bid="b1", sha="f" * 64)
        with pytest.raises(sqlite3.IntegrityError):
            _blob(conn, bid="b2", sha="f" * 64)


def test_code_row_without_content_pointer_is_rejected(tmp_path, monkeypatch):
    """The guarantee that matters: after_blob_id is the ONLY pointer to the only
    copy of a proof, so a code row may not silently lack one."""
    _fresh(tmp_path, monkeypatch)
    with db.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _code(conn, after_blob_id=None, content_lost=0)


def test_code_row_may_lack_a_pointer_only_when_content_is_declared_lost(tmp_path, monkeypatch):
    """The 20 orphaned legacy rows whose git repos were deleted. The loss is
    recorded, not hidden, and not resolved by deleting the user's history."""
    _fresh(tmp_path, monkeypatch)
    with db.connect() as conn:
        _code(conn, after_blob_id=None, content_lost=1)
        assert conn.execute("select count(*) from timeline").fetchone()[0] == 1


def test_unknown_kind_and_author_are_rejected(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    with db.connect() as conn:
        bid = _blob(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _code(conn, kind="banana", after_blob_id=bid)
        with pytest.raises(sqlite3.IntegrityError):
            _code(conn, author="cascade", after_blob_id=bid)  # dropped: a trigger, not an identity


def test_prose_rows_must_have_prose(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    with db.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("insert into timeline (session_id,kind,author,content,created_at)"
                         " values ('s1','message','agent',NULL,'t')")


def test_artifact_kind_requires_a_passing_check(tmp_path, monkeypatch):
    """Was enforced in Python (`artifact_kind if check_status == 'ok' else None`) and
    could therefore drift. Now the DB refuses."""
    _fresh(tmp_path, monkeypatch)
    with db.connect() as conn:
        bid = _blob(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _code(conn, after_blob_id=bid, check_status="error", artifact_kind="proof")
        _code(conn, after_blob_id=bid, check_status="ok", artifact_kind="proof")


def test_writes_go_to_timeline_and_the_old_tables_still_exist(tmp_path, monkeypatch):
    """Was `test_expand_step_leaves_the_old_tables_alone`, when 0003 was additive and
    "nothing reads or writes timeline yet". The switch landed, so this now pins the
    other half of expand→migrate→**contract**: new writes go to `timeline`, and the
    old tables are still present, holding whatever 0004 backfilled.

    Dropping them is a separate revision on purpose — until it runs, a restored
    snapshot can still be inspected against the rows it came from."""
    path = _fresh(tmp_path, monkeypatch)
    with sqlite3.connect(path) as conn:
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"messages", "code_steps", "timeline", "artifact_blobs"} <= tables

    from app import store
    session = store.create_session("still works")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    store.add_message(session["id"], "user", "hi", run["id"])
    store.add_code_step(session["id"], run["id"], "p.lean", content="theorem t : True := by trivial")
    with db.connect() as conn:
        assert conn.execute("select count(*) from timeline").fetchone()[0] == 2
        # nothing writes the old tables any more
        assert conn.execute("select count(*) from code_steps").fetchone()[0] == 0
        assert conn.execute("select count(*) from messages").fetchone()[0] == 0
