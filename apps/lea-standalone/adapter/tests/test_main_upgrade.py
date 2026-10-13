"""Exercise the deployed main schema and feature schema without touching live data."""
import hashlib
import sqlite3

import pytest
from alembic import command

from app import db, migrations, store


@pytest.mark.parametrize("revision", ["0019_custom_tools", "0031_usage_details"])
def test_upgrade_preserves_deployed_history_and_requires_new_evidence(tmp_path, monkeypatch, revision):
    path = tmp_path / "deployed.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", path)
    command.upgrade(migrations._config(), revision)
    source = "theorem preserved : True := by trivial\n"
    digest = hashlib.sha256(source.encode()).hexdigest()
    with sqlite3.connect(path) as conn:
        conn.execute("insert into projects(id,slug,title,namespace,repo_path,created_at,updated_at) "
                     "values ('p','old-main','Project','Lea.Project','proofs/Lea/Project','t','t')")
        conn.execute("insert into sessions(id,project_id,title,origin,created_at,updated_at) "
                     "values ('s','p','Session','overleaf','t','t')")
        conn.execute("insert into runs(id,session_id,project_id,status,model,result_kind,created_at,updated_at) "
                     "values ('r','s','p','done','test/model','proved','t','t')")
        conn.execute("insert into artifact_blobs(id,sha256,content,created_at) values ('b',?,?, 't')",
                     (digest, source))
        conn.execute("insert into timeline(session_id,run_id,kind,author,path,after_blob_id,check_status,"
                     "artifact_kind,created_at) values ('s','r','code','agent','Proof.lean','b','ok','proof','t')")
        tables = ["projects", "sessions", "runs", "artifact_blobs", "timeline"]
        snapshots = {}
        for table in tables:
            columns = [r[1] for r in conn.execute(f"pragma table_info({table})")]
            snapshots[table] = (columns, conn.execute(f"select * from {table} order by rowid").fetchall())

    db.init_db()
    assert migrations.current_revision() == "0033_formalization_reviews"
    with sqlite3.connect(path) as conn:
        assert conn.execute("pragma integrity_check").fetchone()[0] == "ok"
        for table, (columns, rows) in snapshots.items():
            assert conn.execute(f"select {','.join(columns)} from {table} order by rowid").fetchall() == rows
        assert conn.execute("select count(*) from proof_check_evidence").fetchone()[0] == 0
        assert conn.execute("select check_evidence_id from timeline").fetchone()[0] is None
        assert conn.execute("select result_kind from runs").fetchone()[0] == "proved"
    step = store.session_detail("s")["code_steps"][0]
    assert step["code"] == source
    assert step["check_status"] == "ok"
    assert step["check_current"] is False
    assert step["check_stale_reason"]
    db.init_db()  # An already-current database is unchanged, including history.
    assert migrations.current_revision() == "0033_formalization_reviews"
