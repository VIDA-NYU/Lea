"""Repeated Overleaf inventories must let a bounded SQL projection finish."""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import commands, core_observer, db, store, workspace_commands
from app.api_routes import projects as public_projects
from app.routes import projects as worker_projects, workspace
from app.config import LeaConfig
from app.queries import core, proofs


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path/'data/db.sqlite3')
    db.init_db()
    config = LeaConfig(model='test',max_turns=1,lea_root=tmp_path/'prover')
    monkeypatch.setattr('app.config.load_config',lambda:config)
    monkeypatch.setattr(worker_projects,'load_config',lambda:config)
    project = store.create_project('inventory-stability')
    epoch = commands.start_worker('sync-test')
    api = FastAPI()
    api.include_router(public_projects.router)
    api.include_router(workspace.router)
    targets = [{'origin_key':f"{project['slug']}:theorem:target_{n}",
        'kind':'theorem','label':f'target_{n}','declaration_name':f'target_{n}',
        'display_title':f'Target {n}','statement':'True','source_hash':f'source-{n}'} for n in range(9)]
    return project, epoch, api, targets


def synchronize(client, project, epoch, targets):
    response = client.post(f"/api/projects/by-slug/{project['slug']}/formalizations/sync",json={'targets':targets})
    assert response.status_code == 202, response.text
    token = commands.claim('sync-test',epoch,'compute',kinds=workspace_commands.KINDS)
    assert token and token['id'] == response.json()['command']['id']
    assert workspace_commands.execute(token)
    result = workspace_commands.get(token['id'])['command']
    assert result['state'] == 'succeeded',result
    return result['result']


def test_refresh_between_every_capture_batch_preserves_progress_and_current_sql_facts(inventory, monkeypatch):
    project, epoch, api, targets = inventory
    scope = 'p:'+project['id']
    with TestClient(api) as client:
        original = synchronize(client,project,epoch,targets)
        assert len(original['targets']) == 9
        core_observer.enroll(scope)
        with db.connect() as conn:
            revision = core.revision(conn,scope)
            before = [tuple(r) for r in conn.execute('select id,updated_at from formalizations order by id')]
        # A one-entity budget deliberately stretches capture across ten refreshes.
        for _ in range(12):
            captured = core_observer.capture(scope,limit=1)
            synchronize(client,project,epoch,targets)
            with db.connect() as conn:
                assert core.revision(conn,scope) == revision
                assert conn.execute('select count(*) from workspace_effects').fetchone()[0] == 0
                assert [tuple(r) for r in conn.execute('select id,updated_at from formalizations order by id')] == before
            if captured['state'] == 'observed':
                break
        else:
            pytest.fail('Unchanged inventory refreshes starved the core projection')
        def denied(*args,**kwargs):
            pytest.fail('API observation reached the physical workspace')
        with monkeypatch.context() as sql_only:
            sql_only.setattr(Path,'read_bytes',denied)
            sql_only.setattr(Path,'read_text',denied)
            response = client.get(f"/api/projects/by-slug/{project['slug']}/target-status",
                params={'declarations':','.join(t['declaration_name'] for t in targets)})
        assert response.status_code == 200,response.text
        rows = response.json()['targets']
        assert len(rows) == 9
        assert all(r['projection']['state'] == 'observed' and r['validity_status'] == 'planned'
                   and not r['check_current'] and r['exists'] is False for r in rows)
        # A real source edit must still invalidate the old observation.
        revised = [{**t,'statement':'False','source_hash':'new-source'} if n==0 else t
                   for n,t in enumerate(targets)]
        synchronize(client,project,epoch,revised)
        assert proofs.for_project(project['id'])[0]['projection']['state'] == 'inputs_changed'


def test_normalized_target_noop_does_not_touch_timestamp_or_revision(inventory):
    project, _, _, _ = inventory
    target = store.create_formalization(project_id=project['id'],loose_session_id=None,
        display_title='Target',declaration_name='result',statement='True',source_hash='source')
    with db.connect() as conn:
        revision = core.revision(conn,'p:'+project['id'])
    updated = store.update_formalization(target['id'],display_title=' Target ',declaration_name=' result ',
        kind='proof',statement='True',source_hash=' source ')
    assert updated == target
    with db.connect() as conn:
        assert core.revision(conn,'p:'+project['id']) == revision


def test_binding_rejection_rolls_back_the_metadata_set_without_a_file_journal(inventory, monkeypatch):
    from app import overleaf_source
    project, epoch, api, targets = inventory
    with TestClient(api) as client:
        synchronize(client,project,epoch,targets)
        with db.connect() as conn:
            before = [tuple(r) for r in conn.execute('select id,statement,source_hash,updated_at from formalizations order by id')]
            revision = core.revision(conn,'p:'+project['id'])
        bind = overleaf_source.bind
        def reject_second(conn, form, source):
            if form['declaration_name'] == 'target_1':
                raise ValueError('Source target binding changed')
            return bind(conn,form,source)
        monkeypatch.setattr(overleaf_source,'bind',reject_second)
        revised = [{**t,'statement':'False','source_hash':'new-source'} for t in targets]
        response = client.post(f"/api/projects/by-slug/{project['slug']}/formalizations/sync",json={'targets':revised})
        token = commands.claim('sync-test',epoch,'compute',kinds=workspace_commands.KINDS)
        assert token['id'] == response.json()['command']['id']
        assert workspace_commands.execute(token)
        assert workspace_commands.get(token['id'])['command']['state'] == 'failed'
        with db.connect() as conn:
            assert [tuple(r) for r in conn.execute('select id,statement,source_hash,updated_at from formalizations order by id')] == before
            assert core.revision(conn,'p:'+project['id']) == revision
            assert conn.execute('select count(*) from workspace_effects').fetchone()[0] == 0


def test_sql_only_project_waits_for_materialization_and_missing_roots_fail_closed(inventory):
    from app import workspace_observer
    from app.config import load_config
    from app.projects import project_repo_dir
    from app.queries import files
    project, _, _, _ = inventory
    core_observer.enroll('p:'+project['id'])
    root = project_repo_dir(project,load_config().lea_root/'workspace/proofs')
    assert not root.exists()
    assert workspace_observer.maintain()['state'] == 'awaiting_materialization'
    assert not root.exists()
    with pytest.raises(files.ProjectionUnavailable):
        files.listing(project['id'])
    root.mkdir(parents=True)
    (root/'notes.txt').write_text('Retained source bytes')
    observed = workspace_observer.maintain()
    assert observed['state'] == 'observed'
    assert files.content(project['id'],'notes.txt')['content'] == 'Retained source bytes'
    moved = root.with_name(root.name+'-moved')
    root.rename(moved)
    assert workspace_observer.maintain()['state'] == 'unavailable'
    with pytest.raises(files.ProjectionUnavailable,match='observation_expired'):
        files.content(project['id'],'notes.txt')
    assert files.content(project['id'],'notes.txt',observed['observation_id'])['content'] == 'Retained source bytes'
    moved.rename(root)
    assert workspace_observer.maintain()['state'] == 'observed'
    assert files.content(project['id'],'notes.txt')['content'] == 'Retained source bytes'
