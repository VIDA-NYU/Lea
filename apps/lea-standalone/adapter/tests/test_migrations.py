"""Migration framework tests — pin the properties the runner *claims*.

Every assertion here exists because the claim was wrong, or unverified, at some
point:

- `transaction_per_migration` is OFF by default in Alembic. With it off, a failure
  at revision N discards revisions 1..N-1 too. We turn it on in env.py; this file
  proves it, rather than trusting the setting.
- `executescript()` inside a transaction COMMITs early (measured), which is why the
  baseline applies statements one at a time via `op.execute`. If someone
  "simplifies" that back to executescript, `test_failed_revision_*` catches it.
- Several processes can start at once and all call `upgrade_to_head()`.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys

import pytest
from alembic.config import Config

from app import db, migrations
from app.migrations import ADAPTER_ROOT

BASELINE_TABLES = {
    "sessions", "projects", "project_files", "skills", "skill_projects",
    "runs", "messages", "code_steps", "status_events", "run_usage_breakdown",
}


def _cols(path, table: str) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {r[1] for r in conn.execute(f"pragma table_info({table})")}


def _tables(path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {
            r[0] for r in conn.execute(
                "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
            )
        }


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    return tmp_path / "test.sqlite3"


# ── the baseline ──────────────────────────────────────────────────────────────

def test_baseline_builds_the_whole_schema(tmp_path, monkeypatch):
    path = _fresh(tmp_path, monkeypatch)
    db.init_db()
    assert BASELINE_TABLES <= _tables(path)
    assert "alembic_version" in _tables(path)


def test_init_db_is_idempotent(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    db.init_db()
    rev = migrations.current_revision()
    db.init_db()  # a second startup must be a no-op, not an error
    assert migrations.current_revision() == rev


def test_baseline_is_a_noop_on_a_preexisting_database(tmp_path, monkeypatch):
    """The live DB already had these tables at user_version=0 with real rows. The
    baseline must adopt it without touching data — that's why it's all
    `create table if not exists` instead of an `alembic stamp`."""
    path = _fresh(tmp_path, monkeypatch)
    # Simulate the pre-Alembic database: schema present, data present, no stamp.
    db.init_db()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into sessions (id, title, origin, created_at, updated_at)"
            " values ('s1','keep me','ui','t','t')"
        )
        conn.execute("delete from alembic_version")  # unstamped, like the real DB
        conn.commit()

    db.init_db()  # re-adopt

    with sqlite3.connect(path) as conn:
        rows = conn.execute("select title from sessions").fetchall()
    assert rows == [("keep me",)], "baseline destroyed pre-existing data"
    assert migrations.current_revision() is not None


def test_reconcile_repairs_a_legacy_database_missing_artifact_kind(tmp_path, monkeypatch):
    """The real drift, reproduced: this repo's actual database was missing
    `code_steps.artifact_kind` (added in 98d69ef) because `create table if not
    exists` never alters an existing table. It looked healthy and threw
    `OperationalError: table code_steps has no column named artifact_kind` on the
    next write.

    The legacy DB is built by genuinely removing the column, so this fails without
    revision 0002 rather than passing vacuously."""
    path = _fresh(tmp_path, monkeypatch)
    db.init_db()

    # Rewind to the pre-98d69ef shape, keeping a row to prove data survives.
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into sessions (id, title, origin, created_at, updated_at)"
            " values ('s1','legacy','ui','t','t')"
        )
        conn.execute(
            "insert into code_steps (id, session_id, seq, path, commit_sha, author, created_at)"
            " values ('c1','s1',1,'p.lean','deadbeef','agent','t')"
        )
        conn.execute("alter table code_steps drop column artifact_kind")
        conn.execute("delete from alembic_version")  # unstamped, as it really was
        conn.commit()

    assert "artifact_kind" not in _cols(path, "code_steps"), "precondition: drift not reproduced"

    db.init_db()  # adopt + reconcile

    assert "artifact_kind" in _cols(path, "code_steps"), "0002 did not repair the drift"
    with sqlite3.connect(path) as conn:
        assert conn.execute("select path from code_steps").fetchall() == [("p.lean",)]

    # The actual symptom must be gone. This used to be an `add_code_step` call —
    # the live write that raised `OperationalError: table code_steps has no column
    # named artifact_kind`. Writes go to `timeline` now, so that call no longer
    # touches this table and would pass vacuously. The column still has to be
    # repaired, though: 0004 SELECTs `code_steps.artifact_kind` to backfill, so
    # without 0002 the migration chain itself breaks on a drifted database.
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into code_steps (id, session_id, seq, path, commit_sha, author,"
            " artifact_kind, created_at)"
            " values ('c2','s1',2,'q.lean','deadbeef','agent','proof','t')"
        )
        conn.commit()
        assert conn.execute(
            "select artifact_kind from code_steps where id='c2'"
        ).fetchone() == ("proof",)


def test_reconcile_is_a_noop_when_the_column_is_already_present(tmp_path, monkeypatch):
    """A database created *after* 98d69ef already has the column. An unconditional
    add_column would fail on it — which is why 0002 inspects first."""
    path = _fresh(tmp_path, monkeypatch)
    db.init_db()
    assert "artifact_kind" in _cols(path, "code_steps")
    db.init_db()  # must not raise "duplicate column name"
    assert "artifact_kind" in _cols(path, "code_steps")


def test_env_resolves_url_from_db_path_not_the_ini(tmp_path, monkeypatch):
    """A hardcoded URL in alembic.ini would migrate the developer's REAL database
    during tests. The ini's url must stay empty."""
    cfg = Config(str(migrations.ALEMBIC_INI))
    assert not (cfg.get_main_option("sqlalchemy.url") or "").strip()

    path = _fresh(tmp_path, monkeypatch)
    db.init_db()
    assert path.exists(), "migration did not follow the monkeypatched DB_PATH"


# ── transaction_per_migration ─────────────────────────────────────────────────

def _scratch_env(tmp_path, revisions: list[tuple[str, str | None, str]]):
    """Build a throwaway migration tree. `revisions` = (rev, down_rev, upgrade_body)."""
    root = tmp_path / "scratch"
    (root / "versions").mkdir(parents=True)
    # Mirrors the real env.py exactly — including _force_real_begin. If this drifts
    # from production the test stops meaning anything, so keep them in step.
    (root / "env.py").write_text(
        "from alembic import context\n"
        "from sqlalchemy import engine_from_config, event, pool\n"
        "from app import db as app_db\n"
        "config = context.config\n"
        "section = config.get_section(config.config_ini_section, {})\n"
        "section['sqlalchemy.url'] = f'sqlite:///{app_db.DB_PATH}'\n"
        "connectable = engine_from_config(section, prefix='sqlalchemy.', poolclass=pool.NullPool)\n"
        "@event.listens_for(connectable, 'connect')\n"
        "def _no_implicit_txn(dbapi_connection, _record):\n"
        "    dbapi_connection.isolation_level = None\n"
        "@event.listens_for(connectable, 'begin')\n"
        "def _explicit_begin(conn):\n"
        "    conn.exec_driver_sql('BEGIN IMMEDIATE')\n"
        "with connectable.connect() as connection:\n"
        "    context.configure(connection=connection, transactional_ddl=True,\n"
        "                      transaction_per_migration=True)\n"
        "    with context.begin_transaction():\n"
        "        context.run_migrations()\n"
    )
    (root / "script.py.mako").write_text(
        '"""${message}"""\n'
        "revision = ${repr(up_revision)}\n"
        "down_revision = ${repr(down_revision)}\n"
    )
    for rev, down, body in revisions:
        (root / "versions" / f"{rev}.py").write_text(
            f'"""{rev}"""\n'
            "from alembic import op\n"
            f"revision = {rev!r}\n"
            f"down_revision = {down!r}\n"
            "branch_labels = None\ndepends_on = None\n"
            f"def upgrade():\n{body}\n"
            "def downgrade():\n    pass\n"
        )
    cfg = Config()
    cfg.set_main_option("script_location", str(root))
    cfg.set_main_option("sqlalchemy.url", "")
    return cfg


def test_failed_revision_keeps_earlier_revisions(tmp_path, monkeypatch):
    """transaction_per_migration=True: r1+r2 survive a failure in r3.

    With Alembic's DEFAULT (off) the whole upgrade shares one transaction and r1/r2
    would be rolled back too — this test is what stops that default sneaking back."""
    from alembic import command

    path = _fresh(tmp_path, monkeypatch)
    cfg = _scratch_env(tmp_path, [
        ("r1", None, "    op.execute('create table a(x)')"),
        ("r2", "r1", "    op.execute('create table b(x)')"),
        ("r3", "r2", "    op.execute('create table c(x)')\n    op.execute('this is not sql')"),
    ])

    with pytest.raises(Exception):
        command.upgrade(cfg, "head")

    tables = _tables(path)
    assert "a" in tables and "b" in tables, "earlier revisions were rolled back"
    assert "c" not in tables, "the failed revision left a partial schema"

    with sqlite3.connect(path) as conn:
        stamped = conn.execute("select version_num from alembic_version").fetchone()
    assert stamped == ("r2",), f"stamped at {stamped}, expected r2"


# ── concurrency ───────────────────────────────────────────────────────────────

def test_concurrent_startup_migrates_exactly_once(tmp_path):
    """Several workers booting at once must not corrupt each other's migration.

    PROCESSES, not threads, and that distinction is real rather than cosmetic:
    `alembic.context` is a module-level global proxy, so two threads calling
    `command.upgrade()` in one interpreter tear down each other's state (observed:
    `KeyError: 'config'` / `KeyError: 'script'`). Alembic simply isn't thread-safe
    that way — and it doesn't need to be, because the deployment shape is N worker
    *processes*, each with its own interpreter. Testing it with threads would fail
    for a reason the real system never hits.

    What serializes them is `migrations._migration_lock`, NOT SQLite's write lock —
    this docstring used to say the latter, and that was the bug (AUDIT-2026-07-24 X6):
    Alembic plans the upgrade before it writes, so the database lock orders the writes
    but not plan-then-apply. With the branch at `0005` and the merge at `0007`, two
    workers could sit on different heads and the merge would fail. This test
    reproduced it 4 times in 10 parallel runs before the fix, and 0 in 30 after —
    which is also why the worker count is 8: the failure is a race, so the test is
    only as good as the contention it creates.
    """
    db_path = tmp_path / "test.sqlite3"
    runner = tmp_path / "boot.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(ADAPTER_ROOT)!r})\n"
        "from pathlib import Path\n"
        "from app import db\n"
        "db.DB_PATH = Path(sys.argv[1])\n"
        "db.init_db()\n"
    )

    procs = [
        subprocess.Popen(
            [sys.executable, str(runner), str(db_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(8)
    ]
    results = [(p.wait(timeout=60), p.communicate()[1]) for p in procs]

    failed = [err for code, err in results if code != 0]
    assert not failed, f"concurrent startup failed:\n{failed[0][-800:]}"
    assert BASELINE_TABLES <= _tables(db_path)
    with sqlite3.connect(db_path) as conn:
        stamps = conn.execute("select count(*) from alembic_version").fetchone()[0]
    assert stamps == 1, f"alembic_version has {stamps} rows — migrated more than once"


def test_migration_lock_is_mutually_exclusive_across_processes(tmp_path):
    """The primitive itself: two processes are never inside the lock at once.

    Tested directly rather than only through `init_db`, because the upgrade race it
    prevents is probabilistic — a test that only exercises it end-to-end passes by
    luck when the timing happens not to collide. Each worker records enter/exit
    around a deliberate pause; if the lock is real, the log is perfectly nested.
    """
    db_path = tmp_path / "test.sqlite3"
    log = tmp_path / "lock.log"
    runner = tmp_path / "locker.py"
    runner.write_text(
        "import os, sys, time\n"
        f"sys.path.insert(0, {str(ADAPTER_ROOT)!r})\n"
        "from pathlib import Path\n"
        "from app import db, migrations\n"
        "db.DB_PATH = Path(sys.argv[1])\n"
        "log = Path(sys.argv[2])\n"
        "pid = os.getpid()\n"
        "with migrations._migration_lock():\n"
        "    with log.open('a') as fh:\n"
        "        fh.write(f'enter {pid}\\n')\n"
        "    time.sleep(0.25)\n"
        "    with log.open('a') as fh:\n"
        "        fh.write(f'exit {pid}\\n')\n"
    )

    procs = [
        subprocess.Popen(
            [sys.executable, str(runner), str(db_path), str(log)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(6)
    ]
    results = [(p.wait(timeout=60), p.communicate()[1]) for p in procs]
    assert not [err for code, err in results if code != 0]

    lines = log.read_text().split()
    events = list(zip(lines[::2], lines[1::2]))
    assert len(events) == 12, events

    holder = None
    for action, pid in events:
        if action == "enter":
            assert holder is None, f"{pid} entered while {holder} held the lock"
            holder = pid
        else:
            assert holder == pid, f"{pid} exited but {holder} held the lock"
            holder = None
    assert holder is None


def test_migration_lock_lives_beside_the_database_it_guards(tmp_path, monkeypatch):
    """Resolved through `db.DB_PATH` at call time, so a test redirecting the database
    cannot end up locking (or littering) beside the developer's real one."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "nested" / "test.sqlite3")

    assert migrations._lock_path() == tmp_path / "nested" / "test.sqlite3.migrate.lock"

    with migrations._migration_lock():
        pass
    assert migrations._lock_path().exists()


def test_a_pending_migration_snapshots_once_across_concurrent_workers(tmp_path):
    """The snapshot decision is inside the lock, so N workers racing a pending
    migration take one backup between them, not one each."""
    db_path = tmp_path / "test.sqlite3"
    seed = sqlite3.connect(db_path)
    seed.execute("create table legacy_rows (id integer primary key)")
    seed.commit()
    seed.close()

    runner = tmp_path / "boot.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(ADAPTER_ROOT)!r})\n"
        "from pathlib import Path\n"
        "from app import db\n"
        "db.DB_PATH = Path(sys.argv[1])\n"
        "db.init_db()\n"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, str(runner), str(db_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(6)
    ]
    results = [(p.wait(timeout=60), p.communicate()[1]) for p in procs]
    assert not [err for code, err in results if code != 0], results

    snapshots = list((tmp_path / "backups").glob("test-*.sqlite3"))
    assert len(snapshots) == 1, [p.name for p in snapshots]
