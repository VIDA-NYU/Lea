"""C4 under concurrency: two rows must never share a timeline position.

`test_db_seq.py` covers the single-threaded contract. It passed even with the
original read-modify-write bug, because that bug needs two writers in flight at
once — exactly what concurrent runs (and subagents) introduce.

The failure this pins was SILENT: no exception, no "database is locked", just rows
sharing a seq. `ORDER BY seq` then returns duplicates in arbitrary order, so the
thread interleaves wrong and the derived session status can read the wrong "latest"
code step. Measured on `main` before any fix: 0 errors, 99-119 duplicate seqs per
200 concurrent inserts.

**These tests now pass for a different reason, and that's the point.** The first fix
was `db.write()` (BEGIN IMMEDIATE), which made the read-modify-write atomic —
correct, but it kept a hand-rolled counter that every future writer had to remember
to take the lock for. Merging the tables (v2.3) deletes the counter: `timeline.id`
is an autoincrement primary key, so the position is assigned by SQLite under the
write lock. There is no read-then-write left to race. A new writer cannot get this
wrong by forgetting something.

Gaplessness is deliberately not asserted — see `test_db_seq.py`. Uniqueness and
no-lost-rows are the contract.
"""

import threading

from app import db, store

THREADS = 8
PER_THREAD = 25
TOTAL = THREADS * PER_THREAD


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def _hammer(fn):
    """Run `fn(i)` from THREADS threads, PER_THREAD times each; collect exceptions."""
    errors = []
    lock = threading.Lock()

    def worker(t):
        for i in range(PER_THREAD):
            try:
                fn(t * PER_THREAD + i)
            except Exception as exc:  # noqa: BLE001 - the test reports whatever surfaced
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def _seqs(session_id):
    with db.connect() as conn:
        rows = conn.execute(
            "select id from timeline where session_id = ?", (session_id,)
        ).fetchall()
    return [r["id"] for r in rows]


def test_concurrent_messages_get_unique_seqs(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Concurrent messages")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)

    errors = _hammer(lambda i: store.add_message(session["id"], "assistant", f"m{i}", run["id"]))

    seqs = _seqs(session["id"])
    assert not errors, f"unexpected errors: {errors[:3]}"
    assert len(seqs) == TOTAL, f"lost rows: {len(seqs)}/{TOTAL}"
    assert len(set(seqs)) == TOTAL, f"{TOTAL - len(set(seqs))} duplicate seqs"


def test_concurrent_mixed_writers_never_collide(tmp_path, monkeypatch):
    """Messages and code steps are the same table now, so racing them against each
    other is both the real-world shape (a run narrates while it writes) and the
    worst case for the counter that used to sit between them."""
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Concurrent mixed")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)

    def either(i):
        if i % 2:
            store.add_message(session["id"], "assistant", f"m{i}", run["id"])
        else:
            store.add_code_step(session["id"], run["id"], "p.lean", content=f"proof-{i}")

    errors = _hammer(either)

    seqs = _seqs(session["id"])
    assert not errors, f"unexpected errors: {errors[:3]}"
    assert len(seqs) == TOTAL, f"lost rows: {len(seqs)}/{TOTAL}"
    assert len(set(seqs)) == TOTAL, f"{TOTAL - len(set(seqs))} rows shared a position"


def test_concurrent_writers_do_not_duplicate_a_blob(tmp_path, monkeypatch):
    """The one read-then-write left in the write path: `_put_blob` looks up a content
    hash before inserting. Racing identical content is what would exercise it.

    Measured, because the obvious guess was wrong: running `_put_blob` on a plain
    `connect()` (no BEGIN IMMEDIATE — the exact shape `_next_seq` had) does NOT
    silently duplicate. 8 threads x 25 identical writes gave **1 blob and 4
    IntegrityErrors** — `sha256 UNIQUE` turns the race into a loud failure. That is
    the structural difference from the old seq bug: the counter had no constraint
    behind it, so its race was invisible; this one cannot be.

    So the assertion with teeth here is `not errors` (the broken version fails it),
    not the blob count (true either way). `write()` is what keeps a racing run from
    erroring — the schema is what keeps it from lying."""
    _fresh_db(tmp_path, monkeypatch)
    session = store.create_session("Same bytes, many writers")
    run = store.create_run(session["id"], "gpt-4o", "openai", 3)
    code = "theorem t : True := by trivial\n"

    errors = _hammer(
        lambda i: store.add_code_step(session["id"], run["id"], "p.lean", content=code)
    )

    assert not errors, f"unexpected errors: {errors[:3]}"
    with db.connect() as conn:
        blobs = conn.execute("select count(*) from artifact_blobs").fetchone()[0]
    assert blobs == 1, f"identical content stored {blobs} times"
    steps = store.session_detail(session["id"])["code_steps"]
    assert len(steps) == TOTAL
    assert all(s["code"] == code for s in steps), "a step lost its content under load"


def test_sessions_do_not_interleave_under_load(tmp_path, monkeypatch):
    """Concurrent writes to *different* sessions must not bleed into each other.
    This used to assert each session's counter restarts at 1; ids are global now, so
    the real property is that each session reads back exactly its own rows, ordered."""
    _fresh_db(tmp_path, monkeypatch)
    a = store.create_session("A")
    b = store.create_session("B")
    run_a = store.create_run(a["id"], "gpt-4o", "openai", 3)
    run_b = store.create_run(b["id"], "gpt-4o", "openai", 3)

    errors = _hammer(
        lambda i: store.add_message(
            (a if i % 2 else b)["id"], "assistant", f"m{i}", (run_a if i % 2 else run_b)["id"]
        )
    )

    assert not errors, f"unexpected errors: {errors[:3]}"
    for session in (a, b):
        seqs = _seqs(session["id"])
        assert len(seqs) == TOTAL // 2, f"session {session['title']} lost or gained rows"
        assert len(set(seqs)) == len(seqs), f"session {session['title']} has duplicate positions"
    assert not set(_seqs(a["id"])) & set(_seqs(b["id"])), "sessions shared a position"
