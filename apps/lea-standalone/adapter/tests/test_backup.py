"""Pre-migration snapshots — the recovery path now that SQL owns proof content."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from app import backup, db, migrations


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    # Redirecting db.DB_PATH is enough: backup.py resolves the path AND its backup
    # dir through the module at call time. It used to `from .db import DB_PATH`,
    # which bound at import time — so this fixture couldn't move it and snapshots of
    # the developer's REAL database were written during test runs.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    return tmp_path


def test_backup_follows_a_redirected_db_and_never_touches_the_real_one(tmp_path, monkeypatch):
    """Regression: backup.py bound DB_PATH at import time, so it copied the real
    database no matter where the caller pointed it."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "elsewhere.sqlite3")
    db.init_db()
    with db.connect() as conn:
        conn.execute("insert into sessions (id,title,origin,created_at,updated_at)"
                     " values ('s1','x','ui','t','t')")
    assert backup.backup_dir() == tmp_path / "backups"
    path = backup.snapshot("t")
    assert path is not None and path.parent == tmp_path / "backups"


def test_no_snapshot_when_there_is_no_database(scratch):
    assert backup.snapshot("x") is None
    assert backup.list_snapshots() == []


def test_a_fresh_install_is_not_snapshotted(scratch):
    """`DB_PATH.exists()` is not a proxy for "there is a database": opening a
    connection creates the file, and current_revision() does exactly that before
    deciding to migrate. Without the table check, every fresh install would back up
    an empty file."""
    db.init_db()
    assert backup.list_snapshots() == [], "backed up an empty, brand-new database"


def test_a_legacy_unstamped_database_with_rows_IS_snapshotted(scratch):
    """The mirror of the above, and the case that actually matters: unstamped must
    not be the skip condition, or the riskiest database gets no backup."""
    db.init_db()
    with db.connect() as conn:
        conn.execute("insert into sessions (id,title,origin,created_at,updated_at)"
                     " values ('s1','legacy','ui','t','t')")
    with sqlite3.connect(scratch / "test.sqlite3") as conn:
        conn.execute("delete from alembic_version")  # pre-Alembic: schema + rows, no stamp
        conn.commit()

    db.init_db()

    snaps = backup.list_snapshots()
    assert len(snaps) == 1, "a legacy database with real rows was not snapshotted"
    assert "unstamped" in snaps[0].name
    with sqlite3.connect(snaps[0]) as conn:
        assert conn.execute("select title from sessions").fetchall() == [("legacy",)]


def test_migration_snapshots_first_and_the_copy_is_restorable(scratch):
    """The point of the whole module: if a revision eats the data, the state from
    just before it is still on disk."""
    db.init_db()
    with db.connect() as conn:
        conn.execute(
            "insert into sessions (id,title,origin,created_at,updated_at)"
            " values ('s1','precious','ui','t','t')"
        )
    # Rewind so a migration is pending again.
    with sqlite3.connect(scratch / "test.sqlite3") as conn:
        conn.execute("update alembic_version set version_num='0001_baseline'")
        conn.commit()

    db.init_db()

    snaps = backup.list_snapshots()
    assert len(snaps) == 1, "no snapshot was taken before migrating"
    assert "0001_baseline" in snaps[0].name, "snapshot isn't tagged with what it came from"
    with sqlite3.connect(snaps[0]) as conn:
        assert conn.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert conn.execute("select title from sessions").fetchall() == [("precious",)]


def test_no_snapshot_when_already_current(scratch):
    """Startup must not accumulate one backup per boot."""
    db.init_db()
    before = len(backup.list_snapshots())
    db.init_db()
    db.init_db()
    assert len(backup.list_snapshots()) == before


def test_a_failed_snapshot_stops_the_migration(scratch, monkeypatch):
    """Refusing to start is recoverable; migrating the only copy of the user's
    proofs without a fallback is not."""
    db.init_db()
    with sqlite3.connect(scratch / "test.sqlite3") as conn:
        conn.execute("update alembic_version set version_num='0001_baseline'")
        conn.commit()

    def boom(*_a, **_k):
        raise backup.BackupError("disk full")

    monkeypatch.setattr(backup, "snapshot", boom)
    with pytest.raises(backup.BackupError):
        migrations.upgrade_to_head()
    # Still where it was: the migration did not proceed.
    assert migrations.current_revision() == "0001_baseline"


def test_a_failed_snapshot_leaves_no_half_written_file(scratch, monkeypatch):
    db.init_db()

    real_connect = sqlite3.connect

    def flaky(path, *a, **k):
        conn = real_connect(path, *a, **k)
        if str(path).endswith(".sqlite3") and "backups" in str(path):
            conn.backup = lambda *_a, **_k: (_ for _ in ()).throw(sqlite3.OperationalError("boom"))
        return conn

    monkeypatch.setattr(sqlite3, "connect", flaky)
    with pytest.raises(backup.BackupError):
        backup.snapshot("t")
    # Deliberately NOT monkeypatch.undo() — that would also revert the fixture's
    # DB_PATH redirect and list the developer's real backups instead of this test's.
    leftovers = list((scratch / "backups").glob("*.sqlite3"))
    assert leftovers == [], "a corrupt file was left looking like a backup"


def test_old_snapshots_are_pruned(scratch):
    db.init_db()
    for i in range(backup.KEEP + 3):
        backup.snapshot(f"tag{i:02d}")
    assert len(backup.list_snapshots()) == backup.KEEP


def test_snapshot_is_consistent_under_a_concurrent_writer(scratch):
    """A copy taken while a run is mid-write must still be a usable database."""
    db.init_db()
    with db.connect() as conn:
        conn.execute("insert into sessions (id,title,origin,created_at,updated_at)"
                     " values ('s1','t','ui','t','t')")

    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            try:
                with db.write() as conn:
                    conn.execute(
                        "insert into timeline (session_id,kind,author,content,created_at)"
                        " values ('s1','message','agent','x','t')"
                    )
            except Exception:  # noqa: BLE001 - contention is not what's under test
                pass

    writer = threading.Thread(target=hammer)
    writer.start()
    try:
        path = backup.snapshot("under-load")
    finally:
        stop.set()
        writer.join()

    with sqlite3.connect(path) as conn:
        assert conn.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert conn.execute("select count(*) from sessions").fetchone()[0] == 1
