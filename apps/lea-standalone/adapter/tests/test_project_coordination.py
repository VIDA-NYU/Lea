"""Deterministic probes at the filesystem/metadata boundaries of a rename."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import db, projects, project_coordination as coordination, store
from app.config import LeaConfig
from app.routes import projects as routes, runs, sessions, formalizations
from app.routes.projects import FilePut, ProjectIdentityUpdate
from app.routes.runs import RunRequest
from app.routes.sessions import FileWriteRequest


def fixture_project(tmp_path, monkeypatch, title="Old Name"):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config = LeaConfig(model="m", max_turns=3, lea_root=tmp_path, max_spend_usd=None)
    for module in (routes, runs, sessions):
        monkeypatch.setattr(module, "load_config", lambda: config)
    proofs = tmp_path / "workspace" / "proofs"
    project = projects.provision_project(title, proofs)
    for name in ("alpha", "beta", "gamma"):
        session = store.create_session(name, project_id=project["id"])
        target = store.create_formalization(project_id=project["id"], loose_session_id=None,
                                           display_title=name, declaration_name=name, kind="theorem")
        store.link_session_formalization(session["id"], target["id"])
        store.link_formalization_file(target["id"], f"{name}.lean", "primary")
        run = store.create_run(session["id"], "m", None, 3, project_id=project["id"],
                               focus_formalization_id=target["id"])
        code = f"namespace {project['namespace']}\ntheorem {name} : True := by trivial\nend {project['namespace']}\n"
        (projects.project_repo_dir(project, proofs) / f"{name}.lean").write_text(code)
        store.add_code_step(session["id"], run["id"], f"{name}.lean", content=code,
                            check_status="ok", formalization_id=target["id"], artifact_kind="proof")
        store.update_run(run["id"], "proved", result_kind="proved")
        store.upsert_artifact(project_id=project["id"], session_id=session["id"], run_id=run["id"],
                              declaration_name=name, kind="proof", path=f"{name}.lean",
                              module_name=f"{project['namespace']}.{name}", formalization_id=target["id"])
    monkeypatch.setattr(routes, "interface_check", lambda _: SimpleNamespace(status="ok", detail=None))
    return project, proofs


def rename(project):
    return routes.update_project_identity_by_slug.worker(project["slug"], ProjectIdentityUpdate(
        project_name="New Name", mode="rename-namespace", namespace="Lea.NewName",
        expected_namespace=project["namespace"],
    ))


def assert_renaming(call):
    with pytest.raises(HTTPException) as error:
        call()
    assert error.value.status_code == 409
    assert error.value.detail["error"] == "project_renaming"
    assert error.value.headers["Retry-After"] == "1"


@pytest.mark.parametrize("boundary", ["move", "check"])
def test_reads_and_mutations_defer_for_entire_migration(tmp_path, monkeypatch, boundary):
    project, proofs = fixture_project(tmp_path, monkeypatch)
    other = projects.provision_project("Unrelated", proofs)
    revision = routes.get_project_identity_by_slug(project["slug"])["namespaceRevision"]
    entered, release = Event(), Event()
    if boundary == "move":
        move = projects.shutil.move
        def paused_move(*args, **kwargs):
            result = move(*args, **kwargs)
            entered.set()
            assert release.wait(5)
            return result
        monkeypatch.setattr(projects.shutil, "move", paused_move)
    else:
        def paused_check(_):
            entered.set()
            assert release.wait(5)
            return CheckResult(_, "ok")
        monkeypatch.setattr(routes, "interface_check", paused_check)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(rename, project)
        try:
            assert entered.wait(5)
            for reader in (
                lambda: routes.project_target_status_by_slug.worker(project["slug"], declarations="alpha,beta,gamma"),
                lambda: routes.get_graph_by_slug.worker(project["slug"]),
            ):
                assert_renaming(reader)
            assert len(routes.project_artifacts_by_slug(project["slug"])["artifacts"]) == 3
            assert routes.get_project_identity_by_slug(project["slug"])["projectId"] == project["id"]
            assert_renaming(lambda: rename(project))
            assert_renaming(lambda: runs.create_run(RunRequest(message="new run", project_slug=project["slug"])))
            assert_renaming(lambda: routes.write_project_file.worker(project["id"], FilePut(path="alpha.lean", content="x")))
            session = store.list_project_sessions(project["id"])[0]
            assert_renaming(lambda: sessions.session_detail.worker(session["id"]))
            assert_renaming(lambda: formalizations.project_formalizations.worker(project["id"]))
            artifact = store.list_artifacts_for_scope(project["id"])[0]
            assert_renaming(lambda: formalizations.formalization_current.worker(artifact["formalization_id"]))
            assert_renaming(lambda: sessions.write_file_session.worker(session["id"], FileWriteRequest(path="alpha.lean", content="x")))
            assert_renaming(lambda: routes.sync_project_formalizations_by_slug.worker(
                project["slug"], routes.FormalizationTargetSyncRequest(targets=[])))
            assert routes.get_project_identity_by_slug(other["slug"])["renameInProgress"] is False
        finally:
            release.set()
        result = pending.result(timeout=5)
    assert result["identity"]["namespaceRevision"] != revision
    assert result["identity"]["renameInProgress"] is False
    evidence = routes.project_target_status_by_slug.worker(project["slug"], declarations="alpha,beta,gamma")
    assert evidence["namespace_revision"] == result["identity"]["namespaceRevision"]
    assert evidence["project_namespace"] == "Lea.NewName"
    assert len(evidence["targets"]) == 3
    assert all(item["exists"] and item["check_current"] and item["validity_status"] == "proved"
               for item in evidence["targets"])


def test_read_spanning_a_complete_rename_cannot_publish(tmp_path, monkeypatch):
    project, _ = fixture_project(tmp_path, monkeypatch)
    entered, release = Event(), Event()
    backfill = routes._ensure_artifacts_backfilled
    def paused_backfill(p):
        backfill(p)
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(routes, "_ensure_artifacts_backfilled", paused_backfill)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(routes.project_target_status_by_slug.worker, project["slug"], "alpha,beta,gamma")
        try:
            assert entered.wait(5)
            rename(project)
        finally:
            release.set()
        assert_renaming(lambda: pending.result(timeout=5))


def test_migration_exception_releases_guard_and_rotates_revision(tmp_path, monkeypatch):
    project, _ = fixture_project(tmp_path, monkeypatch)
    revision = coordination.snapshot(project["id"])["namespaceRevision"]
    def fail_check(_):
        raise RuntimeError("checker failed")
    monkeypatch.setattr(routes, "interface_check", fail_check)
    with pytest.raises(RuntimeError, match="checker failed"):
        rename(project)
    assert coordination.snapshot(project["id"])["renameInProgress"] is False
    assert coordination.snapshot(project["id"])["namespaceRevision"] != revision
    assert routes.project_target_status_by_slug.worker(project["slug"], declarations="alpha")["targets"][0]["exists"]


def test_failed_migration_checks_remain_failures(tmp_path, monkeypatch):
    project, _ = fixture_project(tmp_path, monkeypatch)
    monkeypatch.setattr(routes, "interface_check", lambda _: SimpleNamespace(status="error", detail="type mismatch"))
    assert len(rename(project)["migration"]["failedFiles"]) == 3
    targets = routes.project_target_status_by_slug.worker(project["slug"], declarations="alpha,beta,gamma")["targets"]
    assert all(item["validity_status"] == "failing" and item["check_status"] == "error" for item in targets)


def test_admission_reservation_excludes_migration_and_reads_remain_concurrent(tmp_path, monkeypatch):
    project, _ = fixture_project(tmp_path, monkeypatch)
    with coordination.mutation(project["id"]):
        with pytest.raises(HTTPException) as error:
            rename(project)
        assert error.value.detail["error"] == "project_busy"
    barrier = Barrier(2)
    def read():
        with coordination.read(project["id"]):
            barrier.wait(timeout=5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(read), pool.submit(read)
        a.result(timeout=5)
        b.result(timeout=5)


def test_stale_project_record_is_refetched_before_migration(tmp_path, monkeypatch):
    project, proofs = fixture_project(tmp_path, monkeypatch)
    rename(project)
    result = projects.migrate_project_namespace(project, proofs, title="Final Name", namespace="Lea.FinalName",
                                               check_fn=lambda _: SimpleNamespace(status="ok", detail=None))
    assert result["migration"]["oldNamespace"] == "Lea.NewName"
    assert (proofs / "Lea" / "FinalName" / "alpha.lean").is_file()


def test_deferred_mirror_commit_waits_and_refetches_the_repo(tmp_path, monkeypatch):
    from app import uploads
    project, proofs = fixture_project(tmp_path, monkeypatch)
    commit_started, release = Event(), Event()
    def commit(_self, repo, *_args, **_kwargs):
        commit_started.set()
        assert repo == proofs / "Lea" / "NewName"
        return "commit"
    monkeypatch.setattr(uploads.GitStore, "commit_all", commit)
    mutation = coordination.mutation
    def waiting_mutation(*args, **kwargs):
        release.set()
        return mutation(*args, **kwargs)
    monkeypatch.setattr(coordination, "mutation", waiting_mutation)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with coordination.migration(project["id"]):
            store.update_project_identity(project["id"], title="New Name", namespace="Lea.NewName",
                                          repo_path="Lea/NewName")
            future = pool.submit(uploads.commit_mirror, project, proofs)
            assert release.wait(5)
            assert not commit_started.is_set()
        assert future.result(timeout=5) == "commit"


def test_retryable_read_contract_over_http(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    project, _ = fixture_project(tmp_path, monkeypatch)
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    base = f"/api/projects/by-slug/{project['slug']}"
    identity = client.get(f"{base}/identity").json()
    statuses = client.get(f"{base}/target-status?declarations=alpha,beta,gamma").json()
    assert statuses["namespace_revision"] == identity["namespaceRevision"]
    from app import peripheral_views
    peripheral_views.capture('get_graph_by_slug',{'slug':project['slug']})
    peripheral_views.capture('get_blueprint_by_slug',{'slug':project['slug']})
    with coordination.migration(project['id']):
        for endpoint in ('identity','target-status?declarations=alpha','artifacts','graph','blueprint'):
            assert client.get(f'{base}/{endpoint}').status_code == 200
    assert client.get(f'{base}/identity').json()['namespaceRevision'] == identity['namespaceRevision']
    rename(project)
    assert client.get(f'{base}/identity').json()['namespaceRevision'] > identity['namespaceRevision']



# This module mocks compiler behavior; bind those mock verdicts to their inputs.
pytestmark = pytest.mark.usefixtures("certified_unit_inputs")

from lea.events import CheckResult
