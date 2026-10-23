import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi import HTTPException

from app import db, formalizations, projects, store
from app.config import LeaConfig
from app.routes import projects as pr, sessions as sr
from lea.interface import CheckResult

GOOD = "theorem audit_target : True := by trivial\n"
BAD = "theorem audit_target : False := by bad_tactic\n"


def checked(path, status="ok"):
    return CheckResult(str(path), status, None if status == "ok" else "bad tactic",
                       execution={"content_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()})


@pytest.fixture
def proof(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "audit.sqlite3")
    db.init_db()
    cfg = LeaConfig(model="test", max_turns=3, lea_root=tmp_path)
    monkeypatch.setattr(sr, "load_config", lambda: cfg)
    monkeypatch.setattr(pr, "load_config", lambda: cfg)
    monkeypatch.setattr(sr, "lean_check_runtime", lambda sid: {"state": "healthy"})
    monkeypatch.setattr(sr, "interface_check", lambda path, **kw: checked(path))
    monkeypatch.setattr(pr, "interface_check", lambda path, **kw: checked(path, "error"))
    project = projects.provision_project("Proof audit", tmp_path / "workspace" / "proofs")
    session = store.create_session("Audit", project_id=project["id"])
    target = store.create_formalization(project_id=project["id"], loose_session_id=None,
        display_title="Audit", declaration_name="audit_target")
    store.link_session_formalization(session["id"], target["id"])
    store.link_formalization_file(target["id"], "Target.lean", "primary")
    run = store.create_run(session["id"], "test", None, 3, project_id=project["id"],
                           focus_formalization_id=target["id"])
    path = projects.project_repo_dir(project, tmp_path / "workspace" / "proofs") / "Target.lean"
    path.write_text(GOOD)
    step = store.add_code_step(session["id"], run["id"], "Target.lean", content=GOOD,
        check_status="ok", artifact_kind="proof", formalization_id=target["id"])
    store.update_run(run["id"], "proved", result_kind="proved")
    return project, session, target, path, step


@pytest.mark.parametrize("author", [None, "cascade"])
@pytest.mark.parametrize("status", ["ok", "error"])
@pytest.mark.parametrize("coalesce", [False, True])
def test_completed_check_never_attaches_to_a_new_revision(proof, monkeypatch, author, status, coalesce):
    _, session, target, path, _ = proof
    if coalesce:
        store.upsert_user_code_step(session["id"], "Target.lean", content=GOOD, formalization_id=target["id"])
    entered, release = Event(), Event()
    def delayed(file, **kw):
        result = checked(file, status)  # The compiler result is already complete.
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(sr, "interface_check", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(sr.lean_check_session.worker, session["id"],
                             sr.PathRequest(path="Target.lean", formalization_id=target["id"], author=author))
        try:
            assert entered.wait(5)
            sr.write_file_session.worker(session["id"], sr.FileWriteRequest(
                path="Target.lean", content=BAD, formalization_id=target["id"]))
        finally:
            release.set()
        with pytest.raises(HTTPException) as error:
            result.result(timeout=5)
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "proof_revision_changed"
    latest = store.current_code_steps_for_formalization(target["id"])[0]
    assert latest["code"] == BAD
    assert latest["check_status"] is None


def test_filesystem_edit_updates_shared_snapshot_and_invalidates_proof(proof):
    project, session, target, path, _ = proof
    result = pr.write_project_file.worker(project["id"], pr.FilePut(path="Target.lean", content=BAD))
    assert result["check"]["status"] == "error"
    current = formalizations.current_snapshot(target["id"])
    assert current["files"][0]["code"] == BAD == path.read_text()
    assert current["validity_status"] != "proved"
    assert store.latest_code_step_for_path(session["id"], "Target.lean")["author"] == "user"


def test_save_repairs_drift_instead_of_reporting_unchanged(proof):
    _, session, target, path, _ = proof
    path.write_text(BAD)
    result = sr.write_file_session.worker(session["id"], sr.FileWriteRequest(
        path="Target.lean", content=GOOD, formalization_id=target["id"]))
    assert result["unchanged"] is False
    assert path.read_text() == GOOD
    assert result["code_step"]["check_status"] is None


def test_edit_does_not_coalesce_a_verified_user_row(proof):
    _, session, target, _, _ = proof
    step = store.upsert_user_code_step(session["id"], "Target.lean", content=GOOD, formalization_id=target["id"])
    store.record_verification_event(session_id=session["id"], formalization_id=target["id"],
        path="Target.lean", status="ok", detail=None, code_step_id=step["id"])
    updated = sr.write_file_session.worker(session["id"], sr.FileWriteRequest(
        path="Target.lean", content=BAD, formalization_id=target["id"]))
    assert updated["code_step"]["id"] != step["id"]
    assert formalizations.get(target["id"])["safe_verify"]["current"] is False


def test_filesystem_edit_blocks_a_run_in_another_project_session(proof):
    project, _, _, path, _ = proof
    other = store.create_session("Other", project_id=project["id"])
    store.create_run(other["id"], "test", None, 3, project_id=project["id"])
    with pytest.raises(HTTPException) as error:
        pr.write_project_file.worker(project["id"], pr.FilePut(path="Target.lean", content=BAD))
    assert error.value.status_code == 409
    assert path.read_text() == GOOD


def test_check_from_another_session_pins_the_shared_head(proof, monkeypatch):
    project, session, target, _, _ = proof
    other = store.create_session('Other', project_id=project['id'])
    store.link_session_formalization(other['id'], target['id'])
    entered, release = Event(), Event()
    def delayed(file, **kw):
        result = checked(file)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(sr, 'interface_check', delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(sr.lean_check_session.worker, session['id'], sr.PathRequest(path='Target.lean'))
        try:
            assert entered.wait(5)
            sr.write_file_session.worker(other['id'], sr.FileWriteRequest(path='Target.lean', content=BAD, formalization_id=target['id']))
        finally:
            release.set()
        with pytest.raises(HTTPException):
            pending.result(timeout=5)
    assert store.current_proof_step(session['id'], 'Target.lean')['check_status'] is None


@pytest.mark.parametrize('author', [None, 'cascade'])
def test_unchanged_check_persists_only_the_pinned_bytes(proof, author):
    _, session, _, _, step = proof
    result = sr.lean_check_session.worker(session['id'], sr.PathRequest(path='Target.lean', author=author))
    current = store.current_proof_step(session['id'], 'Target.lean')
    assert result['status'] == current['check_status'] == 'ok'
    assert current['code'] == GOOD
    assert (current['id'] != step['id']) == bool(author)


@pytest.mark.parametrize('execution', [{}, {'content_sha256': 'wrong'}])
def test_missing_or_mismatched_compiler_evidence_is_conservative(proof, monkeypatch, execution):
    _, session, _, _, _ = proof
    sr.write_file_session.worker(session['id'], sr.FileWriteRequest(path='Target.lean', content=BAD))
    monkeypatch.setattr(sr, 'interface_check', lambda file: CheckResult(file, 'ok', execution=execution))
    with pytest.raises(HTTPException) as error:
        sr.lean_check_session.worker(session['id'], sr.PathRequest(path='Target.lean'))
    assert error.value.status_code == 409
    assert store.current_proof_step(session['id'], 'Target.lean')['check_status'] is None


def test_shared_path_updates_both_formalizations_and_preserves_verification(proof):
    project, session, target, path, step = proof
    second = store.create_formalization(project_id=project['id'], loose_session_id=None,
        display_title='Second', declaration_name='second_target')
    store.link_session_formalization(session['id'], second['id'])
    store.link_formalization_file(second['id'], 'Target.lean', 'primary')
    for fid in [target['id'], second['id']]:
        store.record_verification_event(session_id=session['id'], formalization_id=fid,
            path='Target.lean', status='ok', detail=None, code_step_id=step['id'])
    pr.write_project_file.worker(project['id'], pr.FilePut(path='Target.lean', content=BAD))
    for fid in [target['id'], second['id']]:
        current = formalizations.current_snapshot(fid)
        assert current['files'][0]['code'] == BAD
        assert current['safe_verify']['status'] == 'ok'
        assert current['safe_verify']['current'] is False
        assert current['validity_status'] != 'proved'
    import io, zipfile
    from app.filesystem import export_zip
    with zipfile.ZipFile(io.BytesIO(export_zip(path.parent))) as archive:
        assert archive.read(f'{path.parent.name}/Target.lean').decode() == BAD


def test_materialization_failure_keeps_unchecked_history_and_can_retry(proof, monkeypatch):
    from app import proof_files
    _, session, _, path, _ = proof
    materialize = proof_files._materialize
    def fail(*args):
        raise OSError('simulated write failure')
    monkeypatch.setattr(proof_files, '_materialize', fail)
    with pytest.raises(HTTPException) as error:
        sr.write_file_session.worker(session['id'], sr.FileWriteRequest(path='Target.lean', content=BAD))
    assert error.value.detail['code'] == 'content_saved_materialization_failed'
    assert path.read_text() == GOOD
    assert store.current_proof_step(session['id'], 'Target.lean')['code'] == BAD
    assert store.current_proof_step(session['id'], 'Target.lean')['check_status'] is None
    monkeypatch.setattr(proof_files, '_materialize', materialize)
    assert sr.write_file_session.worker(session['id'], sr.FileWriteRequest(path='Target.lean', content=BAD))['unchanged'] is False
    assert path.read_text() == BAD


def test_git_failure_keeps_saved_revision_unchecked(proof, monkeypatch):
    from app.gitstore import GitStore, GitStoreError
    project, session, _, path, _ = proof
    def fail(*args, **kw):
        raise GitStoreError('simulated Git failure')
    monkeypatch.setattr(GitStore, 'commit_all', fail)
    with pytest.raises(HTTPException) as error:
        pr.write_project_file.worker(project['id'], pr.FilePut(path='Target.lean', content=BAD))
    assert error.value.detail['code'] == 'content_saved_commit_failed'
    current = store.current_proof_step(session['id'], 'Target.lean')
    assert current['code'] == path.read_text() == BAD
    assert current['check_status'] is None


def test_noop_requires_disk_and_ledger_agreement(proof):
    _, session, _, path, step = proof
    request = sr.FileWriteRequest(path='Target.lean', content=GOOD)
    assert sr.write_file_session.worker(session['id'], request)['unchanged'] is True
    assert store.current_proof_step(session['id'], 'Target.lean')['id'] == step['id']
    path.unlink()
    assert sr.write_file_session.worker(session['id'], request)['unchanged'] is False
    assert path.read_text() == GOOD


def test_checked_user_edit_is_history_but_unchecked_autosaves_coalesce(proof):
    _, session, _, _, _ = proof
    first = sr.write_file_session.worker(session['id'], sr.FileWriteRequest(path='Target.lean', content=BAD))['code_step']
    next_step = sr.write_file_session.worker(session['id'], sr.FileWriteRequest(path='Target.lean', content=BAD+'-- changed\n'))['code_step']
    assert first['id'] == next_step['id']
    sr.lean_check_session.worker(session['id'], sr.PathRequest(path='Target.lean'))
    last = sr.write_file_session.worker(session['id'], sr.FileWriteRequest(path='Target.lean', content=GOOD))['code_step']
    assert last['id'] != first['id']


def test_safe_verify_cannot_certify_a_coalesced_replacement(proof, monkeypatch):
    from lea.interface import VerifyResult
    _, session, target, _, _ = proof
    store.upsert_user_code_step(session['id'], 'Target.lean', content=GOOD, formalization_id=target['id'])
    entered, release = Event(), Event()
    def delayed(file):
        entered.set()
        assert release.wait(5)
        return VerifyResult('ok', None)
    monkeypatch.setattr(sr, 'interface_verify', delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(sr.verify_session.worker, session['id'], sr.PathRequest(path='Target.lean'))
        try:
            assert entered.wait(5)
            sr.write_file_session.worker(session['id'], sr.FileWriteRequest(path='Target.lean', content=BAD, formalization_id=target['id']))
        finally:
            release.set()
        with pytest.raises(HTTPException) as error:
            pending.result(timeout=5)
        assert error.value.status_code == 409
    assert formalizations.current_snapshot(target['id'])['safe_verify'] is None
    assert store.session_detail(session['id'])['safe_verify'] is None


# This module mocks compiler behavior; bind those mock verdicts to their inputs.
pytestmark = pytest.mark.usefixtures("certified_unit_inputs")
