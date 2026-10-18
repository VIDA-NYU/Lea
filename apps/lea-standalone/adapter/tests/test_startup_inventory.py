"""Startup inventories saved projects without running Lean for every module."""
import json

import pytest

from app import db, project_library as library, store
from evidence_fixtures import mock_execution
from lea.check_evidence import digest, environment_hash, file_version


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data/library.sqlite3")
    db.init_db()
    project = store.get_or_create_project("startup", "Startup")
    repo = tmp_path / "workspace/proofs/Lea/Startup"
    repo.mkdir(parents=True)
    (tmp_path / "workspace/lakefile.toml").write_text('name = "startup"\n')
    monkeypatch.setattr(library, "project_repo", lambda _: repo)
    return project, repo


def forbid_resolution(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Startup inventory must not resolve or compile Lean dependencies")
    monkeypatch.setattr(library, "environment_hash", unexpected)


def test_startup_inventory_indexes_large_library_without_compiler(project, monkeypatch):
    project, repo = project
    for index in range(130):
        (repo / f"Module{index:03}.lean").write_text(
            f"theorem result{index} : True := by trivial\n")
    forbid_resolution(monkeypatch)
    records = library.refresh(project, resolve_environment=False)
    assert len(records) == 130
    assert all(row["build_status"] == "unchecked" and not row["certified"] for row in records)
    assert library.refresh(project, resolve_environment=False) == records


def seed_certification(project, repo):
    target = repo / "Target.lean"
    dependency = repo / "Dependency.lean"
    target.write_text("theorem target : True := by trivial\n")
    dependency.write_text("theorem dependency : True := by trivial\n")
    execution = mock_execution(target)
    execution["environment"]["files"] = {
        "workspace:proofs/Lea/Startup/Dependency.lean": {
            "path": str(dependency), "sha256": digest(dependency.read_bytes()),
            "kind": "source", "version": file_version(dependency),
        }
    }
    execution["environment_sha256"] = environment_hash(execution["environment"])
    declaration = {**library._lexical(target.read_text())[0], "axioms": [], "type_expr": "True"}
    with db.write() as conn:
        conn.execute("""insert into project_library_modules
            (project_id,path,source_hash,environment_hash,declarations_json,
             build_status,verify_status,check_evidence_json,updated_at)
            values (?,?,?,?,?,'ok','ok',?,?)""", (
                project["id"], "Target.lean", digest(target.read_bytes()),
                "recorded-environment", json.dumps([declaration]),
                json.dumps(execution), db.utc_now()))
    return target, dependency


@pytest.mark.parametrize("change", ["source", "compiled", "deleted", "configuration", "unrelated"])
def test_inventory_preserves_evidence_and_checks_dependency_freshness(project, monkeypatch, change):
    project, repo = project
    _, dependency = seed_certification(project, repo)
    compiled = repo / "Dependency.olean"
    compiled.write_bytes(b"recorded compiled artifact")
    with db.write() as conn:
        row = conn.execute("select check_evidence_json from project_library_modules where project_id=?",
                           (project["id"],)).fetchone()
        execution = json.loads(row[0])
        execution["environment"]["files"]["workspace:proofs/Lea/Startup/Dependency.olean"] = {
            "path": str(compiled), "sha256": digest(compiled.read_bytes()),
            "kind": "compiled", "version": file_version(compiled),
        }
        execution["environment_sha256"] = environment_hash(execution["environment"])
        conn.execute("update project_library_modules set check_evidence_json=? where project_id=?",
                     (json.dumps(execution), project["id"]))
    forbid_resolution(monkeypatch)
    records = library.refresh(project, resolve_environment=False)
    target = next(row for row in records if row["path"] == "Target.lean")
    assert target["certified"] and target["reusable"]
    if change == "source":
        dependency.write_text("theorem dependency : False := by trivial\n")
    elif change == "compiled":
        compiled.write_bytes(b"changed compiled artifact")
    elif change == "deleted":
        dependency.unlink()
    elif change == "configuration":
        (repo.parents[2] / "lakefile.toml").write_text('name = "changed"\n')
    else:
        (repo / "Unrelated.lean").write_text("theorem unrelated : True := by trivial\n")
    records = library.refresh(project, resolve_environment=False)
    target = next(row for row in records if row["path"] == "Target.lean")
    assert target["build_status"] == "ok"  # historical result remains
    assert target["certified"] is (change == "unrelated")
    assert target["reusable"] is (change == "unrelated")


def test_inventory_updates_changed_source_and_removes_deleted_modules(project, monkeypatch):
    project, repo = project
    target, dependency = seed_certification(project, repo)
    forbid_resolution(monkeypatch)
    target.write_text("theorem replacement : False := by sorry\n")
    dependency.unlink()
    records = library.refresh(project, resolve_environment=False)
    assert len(records) == 1
    assert records[0]["user_name"] == "replacement"
    assert records[0]["build_status"] == "unchecked"
    assert not records[0]["certified"]
    target.unlink()
    assert library.refresh(project, resolve_environment=False) == []


def test_application_startup_uses_inventory_without_compiler(project, monkeypatch, tmp_path):
    from app import worker as main, runtime
    from app.config import LeaConfig
    from lea import process_ownership

    _, repo = project
    (repo / "Target.lean").write_text("theorem target : True := by trivial\n")
    forbid_resolution(monkeypatch)
    monkeypatch.setattr(runtime, "acquire_data_lock", lambda: None)
    monkeypatch.setattr(runtime,"acquire_workspace_lock",lambda _:None)
    monkeypatch.setattr(runtime, "owned_process_dir", lambda: tmp_path / "owned-processes")
    monkeypatch.setenv("LEA_OWNED_PROCESS_DIR", str(tmp_path / "owned-processes"))
    monkeypatch.setattr(process_ownership, "reap_previous", lambda: None)
    monkeypatch.setattr(main, "load_config", lambda: LeaConfig(model="test", max_turns=1, lea_root=tmp_path))
    # No pending runs/imports exist in this isolated database.
    try:
        main.startup()
        assert runtime.accepting_work()
        assert len(library.entries(project[0])) == 1
    finally:
        runtime.set_state("ready")
