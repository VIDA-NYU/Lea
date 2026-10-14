"""Isolated audit reproduction; temporary data, no paid model calls.

Run with the adapter virtualenv Python from the repository root.
"""

import hashlib
import json
from pathlib import Path
from queue import Queue
import shlex
import sys
import tempfile
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / 'apps/lea-standalone/adapter'))
from app import bridge, db, formalizations, store
from app.config import LeaConfig
from lea import agent, tools, lsp_daemon
from lea.providers import TextDelta, ToolCall, Done, Usage
from lea.lean_checks import check_file

GOOD = 'theorem audit_target : True := by trivial\n'
BAD = 'theorem audit_target : False := by trivial\n'
toolchain = REPO_ROOT / 'apps/lea-standalone/prover/workspace/lean-toolchain'
with tempfile.TemporaryDirectory(prefix='lea-audit-agent-') as tmp:
    root = Path(tmp)
    from app import config as app_config
    app_config.load_config = lambda: LeaConfig(model='test', max_turns=3, lea_root=root)
    workspace = root / 'workspace'
    workspace.mkdir()
    (workspace / 'lakefile.toml').write_text('name = "audit"\nversion = "0.1.0"\n[[lean_lib]]\nname = "Fixture"\nsrcDir = "proofs"\n')
    (workspace / 'lean-toolchain').write_text(toolchain.read_text())
    db.DB_PATH = root / 'audit.sqlite3'
    db.init_db()
    session = store.create_session('Audit real agent loop')
    target = store.create_formalization(project_id=None, loose_session_id=session['id'],
        display_title='Audit target', declaration_name='audit_target', kind='theorem')
    store.link_session_formalization(session['id'], target['id'])
    run = store.create_run(session['id'], 'gemini/test', None, 3, autonomous=True, focus_formalization_id=target['id'])
    proof = workspace / 'proofs' / session['id'] / 'Target.lean'
    ordinary_calls = 0
    check_hashes = []
    def fake_stream(model, system, messages, schemas, model_kwargs=None, streaming=True):
        global ordinary_calls
        if 'classify the mathematical outcome' in system:
            yield TextDelta('PROVED')
            yield Done(Usage(), 0)
            return
        ordinary_calls += 1
        if ordinary_calls == 1:
            yield TextDelta('Writing and checking the proof.')
            yield ToolCall('write_file', {'path': str(proof), 'content': GOOD})
            yield ToolCall('lean_check', {'path': str(proof)})
            yield ToolCall('bash', {'command': "printf '%s' " + shlex.quote(BAD) + ' > ' + shlex.quote(str(proof))})
        else:
            yield TextDelta('The proof is complete.')
        yield Done(Usage(), 0)
    original_check = tools.lean_check
    def cold_check(path, **kwargs):
        result = original_check(path, use_lsp=False)
        check_hashes.append(result.execution['content_sha256'])
        return result
    events = Queue()
    ctx = bridge.RunnerContext(session_id=session['id'], run_id=run['id'], task='Prove audit_target',
        config=LeaConfig(model='gemini/test', max_turns=3, lea_root=root, tools=['write_file', 'lean_check', 'bash']),
        events=events, autonomous=True)
    with patch.object(agent, 'stream', fake_stream), patch.object(tools, 'lean_check', cold_check):
        bridge.run_lea(ctx)
    step = store.latest_code_step_for_path(session['id'], 'Target.lean')
    actual = check_file(proof, str(workspace), use_lsp=False)
    print(json.dumps({'real_agent_revision': {
        'real_compiler_check_hashes': check_hashes,
        'stored_hash': hashlib.sha256(step['code'].encode()).hexdigest() if step else None,
        'stored_code_is_bad': step['code'] == BAD if step else None,
        'stored_check_status': step['check_status'] if step else None,
        'run_status': store.get_run(run['id'])['status'],
        'formalization_status': formalizations.get(target['id'])['validity_status'],
        'actual_final_check': actual.status,
        'actual_final_detail': actual.output,
    }}, indent=2))
    lsp_daemon._shutdown_all()
