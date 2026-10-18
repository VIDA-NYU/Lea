"""First Stage 2 enrollment: current/history file reads with workspace access denied."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import ast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, store, workspace_observer as worker
from app.queries import files
from app.routes import projects


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'files.sqlite')
    db.init_db()
    project=store.create_project('db-files')
    root=tmp_path/'workspace';root.mkdir()
    (root/'Proof.lean').write_bytes(b'-- preserved\r\ntheorem x : True := by trivial\r\n')
    (root/'README.md').write_text('Original')
    (root/'image.png').write_bytes(b'\x89PNG\xff')
    worker.enroll(project['id'])
    return project,root


def finish(project,root,limit=100):
    for _ in range(50):
        result=worker.scan(project['id'],root,limit=limit)
        if result['state']=='observed':
            return result['observation_id']
    pytest.fail('Backfill did not finish')


def test_resume_cutover_and_exact_historical_bytes_with_workspace_denied(fixture,monkeypatch):
    project,root=fixture
    with pytest.raises(files.ProjectionUnavailable,match='synchronizing'):
        files.tree(project['id'])
    assert worker.scan(project['id'],root,limit=1)['state']=='capturing'
    db.init_db()  # Resume persisted checkpoint after host restart.
    generation=finish(project,root,limit=1)
    def denied(*args,**kwargs): raise AssertionError('Query touched the workspace')
    monkeypatch.setattr(projects,'_proofs_root',denied)
    monkeypatch.setattr(Path,'read_bytes',denied)
    monkeypatch.setattr(Path,'read_text',denied)
    monkeypatch.setattr(projects.fs_service,'build_tree',denied)
    app=FastAPI();app.include_router(projects.router)
    with TestClient(app) as client:
        tree=client.get(f"/api/projects/{project['id']}/tree").json()
        assert {i['name'] for i in tree['tree']}=={'Proof.lean','README.md','image.png'}
        assert tree['observation']['id']==generation
        response=client.get(f"/api/projects/{project['id']}/file",params={'path':'Proof.lean'})
        assert response.status_code==200
        assert '\r\n' in response.json()['content']
        assert client.get(f"/api/projects/{project['id']}/file",params={'path':'image.png'}).status_code==415
        assert client.get(f"/api/projects/{project['id']}/file",params={'path':'../private'}).status_code==400


def test_external_write_deletion_environment_change_and_retained_history(fixture):
    project,root=fixture
    first=finish(project,root)
    (root/'README.md').write_text('External edit')
    (root/'Proof.lean').unlink()
    (root/'lean-toolchain').write_text('changed-toolchain')
    second=finish(project,root,limit=1)
    assert first!=second
    assert files.content(project['id'],'README.md')['content']=='External edit'
    assert files.content(project['id'],'README.md',first)['content']=='Original'
    assert 'theorem x' in files.content(project['id'],'Proof.lean',first)['content']
    tombstone=next(i for i in files.listing(project['id'])['files'] if i['path']=='Proof.lean')
    assert tombstone['state']=='deleted'
    with pytest.raises(files.ProjectionUnavailable,match='not_found'):
        files.content(project['id'],'Proof.lean')
    with db.connect() as conn:
        envs={r[0] for r in conn.execute('select environment_revision from workspace_observations')}
    assert len(envs)==2


def test_expiry_and_domain_updates_fail_closed_without_reindexing(fixture):
    project,root=fixture
    old=finish(project,root)
    with db.write() as conn:
        conn.execute("update projects set title='Changed' where id=?",(project['id'],))
    with pytest.raises(files.ProjectionUnavailable,match='inputs_changed'):
        files.tree(project['id'])
    assert files.content(project['id'],'README.md',old)['content']=='Original'
    latest=finish(project,root)
    with db.write() as conn:
        conn.execute('update workspace_observations set expires_at=? where id=?',
                     ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),latest))
    with pytest.raises(files.ProjectionUnavailable,match='observation_expired'):
        files.content(project['id'],'README.md')


def test_raced_scan_restarts_without_publishing_mixed_generation(fixture):
    project,root=fixture
    worker.scan(project['id'],root,limit=1)
    (root/'Proof.lean').write_text('Changed during scan')
    generation=finish(project,root,limit=1)
    assert files.content(project['id'],'Proof.lean')['content']=='Changed during scan'
    with db.connect() as conn:
        assert conn.execute("select count(*) from workspace_observations where state='superseded'").fetchone()[0]==1
        assert conn.execute('select observation_id from projection_enrollment').fetchone()[0]==generation


def test_missing_workspace_never_publishes_an_empty_generation(fixture):
    project,root=fixture
    generation=finish(project,root)
    root.rename(root.with_name('offline'))
    with pytest.raises(ValueError,match='unavailable'):
        worker.scan(project['id'],root)
    assert files.content(project['id'],'README.md')['observation']['id']==generation


def test_unrelated_project_deletion_preserves_indexed_blobs_and_scope(fixture):
    project,root=fixture
    generation=finish(project,root)
    other=store.create_project('other')
    store.create_session('Delete me',project_id=other['id'])
    store.delete_project_cascade(other['id'])
    assert files.content(project['id'],'README.md',generation)['content']=='Original'
    with pytest.raises(files.ProjectionUnavailable,match='observation_not_found'):
        files.content(other['id'],'README.md',generation)


def test_deletion_publication_is_bounded_and_resumes_after_restart(fixture):
    project,root=fixture
    for i in range(205):(root/f'Old{i:03}.txt').write_text('retained')
    old=finish(project,root)
    for path in root.glob('Old*.txt'):path.unlink()
    result=worker.scan(project['id'],root,limit=20)
    assert result['state']=='capturing'
    with pytest.raises(files.ProjectionUnavailable,match='synchronizing'):files.tree(project['id'])
    assert files.content(project['id'],'Old000.txt',old)['content']=='retained'
    db.init_db()
    with db.connect() as conn:before=conn.execute("select count(*) from current_file_bindings where state='deleted'").fetchone()[0]
    worker.scan(project['id'],root,limit=20)
    with db.connect() as conn:after=conn.execute("select count(*) from current_file_bindings where state='deleted'").fetchone()[0]
    assert 0<after-before<=20
    finish(project,root,limit=20)
    assert not any(i['name'].startswith('Old') for i in files.tree(project['id'])['tree'])


def test_symlinks_are_excluded_and_large_files_marked_unavailable(fixture,tmp_path,monkeypatch):
    project,root=fixture
    outside=tmp_path/'outside';outside.write_text('private')
    (root/'escape.txt').symlink_to(outside)
    monkeypatch.setattr(worker,'SOURCE_FILE_BYTES',16)
    finish(project,root)
    assert 'escape.txt' not in {i['path'] for i in files.listing(project['id'])['files']}
    with pytest.raises(files.ProjectionUnavailable,match='file_too_large'):
        files.content(project['id'],'Proof.lean')


def test_query_package_has_no_execution_or_workspace_service_imports():
    folder=Path(files.__file__).parent
    allowed_std={'__future__','datetime','contextlib','contextvars','functools','typing','collections','hashlib','json','re','base64'}
    pure={'db','observation','source_context','alignment_schemas'}
    for source in folder.glob('*.py'):
        tree=ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):
                if node.level==0: assert node.module in allowed_std
                elif node.level==1: assert node.module is None or (folder/(node.module+'.py')).exists()
                else: assert node.module in pure or (node.module is None and {a.name for a in node.names}<=pure)
            elif isinstance(node,ast.Import):
                assert all(a.name in allowed_std for a in node.names)
