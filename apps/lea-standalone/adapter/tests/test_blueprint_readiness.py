"""Proof identity, current SQL views and compiler-derived graph references."""
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import blueprint_seed, db, peripheral_views, projects, store
from app.api_routes import projects as public


def test_missing_project_is_not_a_missing_worker_observation(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'state.sqlite3')
    db.init_db()
    with pytest.raises(HTTPException) as missing:
        public.get_graph_by_slug('missing')
    assert missing.value.status_code == 404
    project = store.create_project('known')
    with pytest.raises(HTTPException) as pending:
        public.get_graph_by_slug(project['slug'])
    assert pending.value.status_code == 503
    assert pending.value.detail['error'] == 'projection_unavailable'


def test_compiler_edges_resolve_binders_and_qualified_constants(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'state.sqlite3')
    db.init_db()
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'lean-toolchain').write_text('leanprover/lean4:v4.29.0\n')
    (root / 'lakefile.toml').write_text('name="blueprint_test"\n[[lean_lib]]\nname="Lea"\nsrcDir="proofs"\n')
    project = projects.provision_project('Semantic', root / 'proofs')
    repo = projects.project_repo_dir(project, root / 'proofs')
    code = ('namespace Lea.Semantic\n'
            'def base : Nat := 0\n'
            'theorem independent (base : Nat) : base = base := rfl\n'
            'theorem uses_base : base = 0 := rfl\n'
            'namespace Other\ndef base : Nat := 1\nend Other\n'
            'theorem qualified : Other.base = 1 := rfl\n'
            'end Lea.Semantic\n')
    (repo / 'Results.lean').write_text(code)
    session = store.create_session('compiler references', project_id=project['id'])
    run = store.create_run(session['id'], 'test', None, 1, project_id=project['id'])
    for name, kind in [('base', 'definition'), ('independent', 'proof'),
                       ('uses_base', 'proof'), ('Other.base', 'definition'), ('qualified', 'proof')]:
        store.upsert_artifact(project_id=project['id'], session_id=session['id'], run_id=run['id'],
            declaration_name=(name if name == 'independent' else 'Lea.Semantic.' + name),
            kind=kind, path='Results.lean', module_name=None)
    result = blueprint_seed.generate(project, root / 'proofs')
    assert result['warnings'] == []
    nodes = {n['lean']: n for n in result['graph']['nodes']}
    assert nodes['Lea.Semantic.independent']['uses'] == []
    assert nodes['Lea.Semantic.uses_base']['uses'] == [nodes['Lea.Semantic.base']['key']]
    assert nodes['Lea.Semantic.qualified']['uses'] == [nodes['Lea.Semantic.Other.base']['key']]
    again = blueprint_seed.generate(project, root / 'proofs')
    assert again['added'] == 0 and again['warnings'] == []


def test_failed_inspection_does_not_persist_partial_edges(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'state.sqlite3')
    db.init_db()
    project = projects.provision_project('Retry', tmp_path / 'proofs')
    repo = projects.project_repo_dir(project, tmp_path / 'proofs')
    (repo / 'Result.lean').write_text('namespace Lea.Retry\ntheorem result : True := trivial\nend Lea.Retry\n')
    session = store.create_session('retry', project_id=project['id'])
    run = store.create_run(session['id'], 'test', None, 1, project_id=project['id'])
    store.upsert_artifact(project_id=project['id'], session_id=session['id'], run_id=run['id'],
        declaration_name='Lea.Retry.result', kind='proof', path='Result.lean', module_name=None)
    before = (repo / '.lea/blueprint.md').read_text()
    result = blueprint_seed.generate(project, tmp_path / 'proofs')
    assert result['added'] == 0 and result['warnings']
    assert (repo / '.lea/blueprint.md').read_text() == before
    monkeypatch.setattr(blueprint_seed, '_compiler_references', lambda *a, **k: {'Lea.Retry.result': []})
    result = blueprint_seed.generate(project, tmp_path / 'proofs')
    assert result['added'] == 1
