from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import HTTPException

from app import db, projects, project_coordination as coordination, runregistry, store
from app.config import LeaConfig
from app.routes import projects as routes


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "audit.sqlite3")
    db.init_db()
    monkeypatch.setattr(routes, "load_config", lambda: LeaConfig(model="test", max_turns=3, lea_root=tmp_path))
    value = projects.provision_project("Lifecycle audit", tmp_path / "workspace" / "proofs")
    session = store.create_session("Audit", project_id=value["id"])
    return value, session, tmp_path / "workspace" / "proofs"


@pytest.mark.parametrize("status", ["pending", "running"])
def test_delete_busy_run_preserves_database_and_repository(project, status):
    value, session, root = project
    run = store.create_run(session["id"], "test", None, 3, project_id=value["id"])
    store.update_run(run["id"], status)
    with pytest.raises(HTTPException) as error:
        routes.delete_project.worker(value["id"])
    assert error.value.status_code == 409
    assert error.value.detail["error"] == "project_busy"
    assert store.get_run(run["id"])
    assert projects.project_repo_dir(value, root).exists()


def test_delete_rejects_inflight_operation(project):
    value, _, _ = project
    with coordination.mutation(value["id"]):
        with pytest.raises(HTTPException) as error:
            routes.delete_project.worker(value["id"])
        assert error.value.status_code == 409
    assert store.get_project(value["id"])


def test_terminal_worker_lease_blocks_delete_until_cleanup(project):
    value, session, _ = project
    run = store.create_run(session["id"], "test", None, 3, project_id=value["id"])
    registry = runregistry.RunRegistry(max_concurrent=1)
    assert registry.try_admit(run["id"], session["id"], project_id=value["id"]).ok
    store.update_run(run["id"], "proved")
    try:
        with pytest.raises(HTTPException) as error:
            routes.delete_project.worker(value["id"])
        assert error.value.status_code == 409
    finally:
        registry.release(run["id"])
    assert routes.delete_project.worker(value["id"])["deleted"]


def test_delete_excludes_late_mutation_and_keeps_deleted_identity_closed(project, monkeypatch):
    value, session, _ = project
    entered, release = Event(), Event()
    rmtree = projects.shutil.rmtree
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return rmtree(*args, **kwargs)
    monkeypatch.setattr(projects.shutil, "rmtree", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(routes.delete_project.worker, value["id"])
        try:
            assert entered.wait(5)
            with pytest.raises(HTTPException):
                with coordination.mutation(value["id"]):
                    store.create_run(session["id"], "test", None, 3, project_id=value["id"])
        finally:
            release.set()
        assert future.result(timeout=5)["deleted"]
    with pytest.raises(HTTPException):
        with coordination.mutation(value["id"]):
            raise AssertionError("deleted project must not admit obsolete work")
