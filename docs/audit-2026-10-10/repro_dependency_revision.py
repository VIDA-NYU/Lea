"""Isolated audit reproduction; temporary data, no paid model calls.

Run with the adapter virtualenv Python from the repository root.
"""

import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / 'apps/lea-standalone/adapter'))
from app import db, formalizations, projects, store
from app.config import LeaConfig
from app.routes import projects as routes
from lea import interface, lsp_daemon

toolchain = REPO_ROOT / 'apps/lea-standalone/prover/workspace/lean-toolchain'
with tempfile.TemporaryDirectory(prefix='lea-audit-dependency-') as tmp:
    root = Path(tmp)
    from app import config as app_config
    app_config.load_config = lambda: LeaConfig(model='test', max_turns=3, lea_root=root)
    workspace = root / 'workspace'
    workspace.mkdir()
    (workspace / 'lakefile.toml').write_text('name = "audit"\nversion = "0.1.0"\n[[lean_lib]]\nname = "Lea"\nsrcDir = "proofs"\n')
    (workspace / 'lean-toolchain').write_text(toolchain.read_text())
    db.DB_PATH = root / 'audit.sqlite3'
    db.init_db()
    project = projects.provision_project('Dependency audit', workspace / 'proofs')
    repo = projects.project_repo_dir(project, workspace / 'proofs')
    namespace = project['namespace']
    up = f'namespace {namespace}\ntheorem base : True := by trivial\nend {namespace}\n'
    down = f'import {namespace}.Upstream\nnamespace {namespace}\ntheorem dependent : True := base\nend {namespace}\n'
    (repo / 'Upstream.lean').write_text(up)
    (repo / 'Downstream.lean').write_text(down)
    build = interface.rebuild(str(repo / 'Upstream.lean'))
    assert build.status == 'ok', build
    checked = interface.check(str(repo / 'Downstream.lean'))
    assert checked.status == 'ok', checked
    targets = {}
    for filename, declaration, content in [('Upstream.lean', 'base', up), ('Downstream.lean', 'dependent', down)]:
        session = store.create_session(declaration, project_id=project['id'])
        target = store.create_formalization(project_id=project['id'], loose_session_id=None,
            display_title=declaration, declaration_name=f'{namespace}.{declaration}', kind='theorem')
        store.link_session_formalization(session['id'], target['id'])
        store.link_formalization_file(target['id'], filename, 'primary')
        run = store.create_run(session['id'], 'test', None, 3, project_id=project['id'], focus_formalization_id=target['id'])
        store.add_code_step(session['id'], run['id'], filename, content=content, check_status='ok',
                           artifact_kind='proof', formalization_id=target['id'])
        store.update_run(run['id'], 'proved', result_kind='proved')
        store.upsert_artifact(project_id=project['id'], session_id=session['id'], run_id=run['id'],
            declaration_name=f'{namespace}.{declaration}', kind='proof', path=filename,
            module_name=f'{namespace}.{Path(filename).stem}', formalization_id=target['id'])
        from app.proof_evidence import record_check
        result = interface.check(str(repo / filename))
        accepted, _ = record_check(session['id'], filename, repo / filename, result,
            run_id=run['id'], formalization_id=target['id'])
        assert accepted
        targets[declaration] = target
        assert formalizations.get(target['id'])['validity_status'] == 'proved' 
    cfg = LeaConfig(model='test', max_turns=3, lea_root=root)
    with patch.object(routes, 'load_config', lambda: cfg):
        saved = routes.write_project_file.worker(project['id'], routes.FilePut(
            path='Upstream.lean', content=up.replace('theorem base', 'theorem renamed')))
        old = routes.project_target_status_by_slug(project['slug'],
            declarations=f'{namespace}.dependent', formalization_ids=targets['dependent']['id'])['targets'][0]
        rebuilt = interface.rebuild(str(repo / 'Upstream.lean'))
        assert rebuilt.status == 'ok', rebuilt
        actual = interface.check(str(repo / 'Downstream.lean'))
        now = routes.project_target_status_by_slug(project['slug'],
            declarations=f'{namespace}.dependent', formalization_ids=targets['dependent']['id'])['targets'][0]
    print(json.dumps({'dependency_edit': {
        'upstream_save_check': saved['check']['status'],
        'downstream_status_after_save': old['validity_status'],
        'downstream_status_after_rebuild': now['validity_status'],
        'downstream_check_current': now['check_current'],
        'actual_downstream_check': actual.status,
        'actual_downstream_detail': actual.detail,
        'formalization_status': formalizations.get(targets['dependent']['id'])['validity_status'],
    }}, indent=2))
    lsp_daemon._shutdown_all()
