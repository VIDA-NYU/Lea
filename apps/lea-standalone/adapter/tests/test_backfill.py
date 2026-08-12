"""Revision 0004: backfilling timeline/artifact_blobs from messages/code_steps + git.

Builds a real legacy database and a real git repo, so these exercise the actual
`git show` path rather than a mock of it.
"""

from __future__ import annotations

import subprocess
import sqlite3
import types

import pytest

from app import db


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _make_repo(root, name, files: dict[str, str]) -> str:
    """A repo with one commit containing `files`; returns the sha."""
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for path, content in files.items():
        (repo / path).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """A pre-0004 database: schema present, old rows populated, timeline empty."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    proofs = tmp_path / "prover" / "workspace" / "proofs"
    proofs.mkdir(parents=True)

    # The migration resolves the proofs dir through the app's config, so redirect it
    # rather than depending on the developer's real workspace.
    from app import config as app_config
    monkeypatch.setattr(
        app_config, "load_config",
        lambda *a, **k: types.SimpleNamespace(lea_root=tmp_path / "prover"),
    )
    return tmp_path, proofs


def _seed(path, rows_sql):
    with sqlite3.connect(path) as conn:
        conn.executescript(rows_sql)
        conn.execute("delete from timeline")  # ensure the backfill has work to do
        conn.execute("delete from alembic_version")
        conn.commit()


def test_backfill_carries_every_row_and_preserves_order(legacy):
    tmp_path, proofs = legacy
    db.init_db()  # build schema (0004 no-ops: nothing to carry)
    sha = _make_repo(proofs, "s1", {"p.lean": "theorem t : True := trivial\n"})

    _seed(tmp_path / "test.sqlite3", f"""
        insert into sessions (id,title,origin,created_at,updated_at) values ('s1','t','ui','t','t');
        insert into runs (id,session_id,model,status,created_at,updated_at)
            values ('r1','s1','m','done','t','t');
        insert into messages (id,session_id,run_id,role,content,kind,seq,created_at)
            values ('m1','s1','r1','user','prove it','assistant',1,'t');
        insert into code_steps (id,session_id,run_id,seq,turn,author,path,commit_sha,created_at)
            values ('c1','s1','r1',2,1,'agent','p.lean','{sha}','t');
        insert into messages (id,session_id,run_id,role,content,kind,seq,created_at)
            values ('m2','s1','r1','assistant','done',​'assistant',3,'t');
    """.replace("​", ""))

    db.init_db()

    with db.connect() as conn:
        rows = conn.execute(
            "select kind, author, content, path, after_blob_id, content_lost"
            " from timeline order by id"
        ).fetchall()
    assert len(rows) == 3, "not every legacy row was carried forward"
    # old seq order (1,2,3) == new id order
    assert [r["kind"] for r in rows] == ["message", "code", "message"]
    # role='user' + kind='assistant' (the incoherent default) -> message/user
    assert rows[0]["author"] == "user"
    assert rows[2]["author"] == "agent"
    assert rows[1]["after_blob_id"] and rows[1]["content_lost"] == 0

    with db.connect() as conn:
        blob = conn.execute(
            "select content from artifact_blobs where id = ?", (rows[1]["after_blob_id"],)
        ).fetchone()
    assert blob["content"] == "theorem t : True := trivial\n", "wrong bytes recovered from git"


def test_unreachable_content_is_recorded_not_dropped(legacy):
    """A code_step whose sha does not contain its path — the real failure on this
    repo's DB, caused by commit_write's `if not staged: return HEAD` early return.
    The row must survive, flagged, not vanish."""
    tmp_path, proofs = legacy
    db.init_db()
    # Repo exists, but the commit does NOT contain the path the row claims.
    sha = _make_repo(proofs, "s1", {"other.lean": "x\n"})

    _seed(tmp_path / "test.sqlite3", f"""
        insert into sessions (id,title,origin,created_at,updated_at) values ('s1','t','ui','t','t');
        insert into code_steps (id,session_id,seq,author,path,commit_sha,created_at)
            values ('c1','s1',1,'agent','ghost.lean','{sha}','t');
    """)

    db.init_db()

    with db.connect() as conn:
        row = conn.execute("select * from timeline").fetchone()
    assert row is not None, "the row was dropped instead of being recorded as lost"
    assert row["content_lost"] == 1
    assert row["after_blob_id"] is None
    assert row["path"] == "ghost.lean"


def test_missing_repo_is_recorded_not_dropped(legacy):
    tmp_path, _proofs = legacy
    db.init_db()
    _seed(tmp_path / "test.sqlite3", """
        insert into sessions (id,title,origin,created_at,updated_at) values ('s1','t','ui','t','t');
        insert into code_steps (id,session_id,seq,author,path,commit_sha,created_at)
            values ('c1','s1',1,'agent','p.lean','deadbeef','t');
    """)
    db.init_db()
    with db.connect() as conn:
        row = conn.execute("select content_lost, after_blob_id from timeline").fetchone()
    assert row["content_lost"] == 1 and row["after_blob_id"] is None


def test_project_sessions_resolve_the_shared_repo(legacy):
    """D24: a project session's content lives in `proofs/Lea/<Project>`, NOT
    `proofs/<session-id>`. Getting this wrong doesn't fail loudly — it silently
    reports content as lost (it fooled the first measurement: 20 of 32 'unrecoverable'
    when the true number was 1)."""
    tmp_path, proofs = legacy
    db.init_db()
    sha = _make_repo(proofs / "Lea", "RealAnalysis", {"q.lean": "-- shared\n"})

    _seed(tmp_path / "test.sqlite3", f"""
        insert into projects (id,slug,title,namespace,repo_path,created_at,updated_at)
            values ('p1','real-analysis','RA','Lea.RealAnalysis','proofs/Lea/RealAnalysis','t','t');
        insert into sessions (id,project_id,title,origin,created_at,updated_at)
            values ('s1','p1','t','ui','t','t');
        insert into code_steps (id,session_id,seq,author,path,commit_sha,created_at)
            values ('c1','s1',1,'agent','q.lean','{sha}','t');
    """)

    db.init_db()

    with db.connect() as conn:
        row = conn.execute("select content_lost, after_blob_id from timeline").fetchone()
        assert row["content_lost"] == 0, "project session's shared repo was not resolved"
        blob = conn.execute("select content from artifact_blobs where id=?",
                            (row["after_blob_id"],)).fetchone()
    assert blob["content"] == "-- shared\n"


def test_backfill_is_idempotent(legacy):
    """A re-run after a manual restore must not double every row."""
    tmp_path, proofs = legacy
    db.init_db()
    sha = _make_repo(proofs, "s1", {"p.lean": "a\n"})
    _seed(tmp_path / "test.sqlite3", f"""
        insert into sessions (id,title,origin,created_at,updated_at) values ('s1','t','ui','t','t');
        insert into code_steps (id,session_id,seq,author,path,commit_sha,created_at)
            values ('c1','s1',1,'agent','p.lean','{sha}','t');
    """)
    db.init_db()
    with db.connect() as conn:
        first = conn.execute("select count(*) from timeline").fetchone()[0]

    # Re-stamp back to 0003 and re-run: the guard must hold.
    with sqlite3.connect(tmp_path / "test.sqlite3") as conn:
        conn.execute("update alembic_version set version_num='0003_timeline_and_blobs'")
        conn.commit()
    db.init_db()

    with db.connect() as conn:
        assert conn.execute("select count(*) from timeline").fetchone()[0] == first


def test_identical_content_dedupes_to_one_blob(legacy):
    """Two steps with the same bytes (a cascade re-verify) share a blob — sha256 is
    UNIQUE, so dedup is the schema's job."""
    tmp_path, proofs = legacy
    db.init_db()
    sha = _make_repo(proofs, "s1", {"p.lean": "same\n"})
    _seed(tmp_path / "test.sqlite3", f"""
        insert into sessions (id,title,origin,created_at,updated_at) values ('s1','t','ui','t','t');
        insert into code_steps (id,session_id,seq,author,path,commit_sha,created_at)
            values ('c1','s1',1,'agent','p.lean','{sha}','t');
        insert into code_steps (id,session_id,seq,author,path,commit_sha,created_at)
            values ('c2','s1',2,'agent','p.lean','{sha}','t');
    """)
    db.init_db()
    with db.connect() as conn:
        assert conn.execute("select count(*) from timeline where kind='code'").fetchone()[0] == 2
        assert conn.execute("select count(*) from artifact_blobs").fetchone()[0] == 1
        ids = [r[0] for r in conn.execute("select after_blob_id from timeline where kind='code'")]
    assert ids[0] == ids[1], "identical content produced two blobs"
