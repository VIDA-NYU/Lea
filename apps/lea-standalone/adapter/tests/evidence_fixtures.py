"""Explicit synthetic compiler evidence for unit tests that already mock Lean.

These helpers never run a compiler. Real/compiler-integrity tests do not opt in.
They bind the unit mock's result to actual temporary input bytes and configuration.
"""
import json
from pathlib import Path
from uuid import uuid4

import pytest


def mock_execution(path):
    from lea.check_evidence import digest, lake_root, _configuration, source_identity
    path = Path(path).resolve()
    environment = {'configuration': _configuration(lake_root(path)) if lake_root(path) else {}, 'files': {}}
    from lea.check_evidence import environment_hash
    return dict(check_id=uuid4().hex, evidence_version=1, content_sha256=digest(path.read_bytes()),
        source_path=str(path), source_identity=source_identity(path), environment=environment, environment_sha256=environment_hash(environment))


@pytest.fixture
def certified_unit_inputs(monkeypatch):
    from app import db, store, proof_files, project_library
    from app.proof_evidence import record_check
    from lea import tools
    from lea.events import CheckResult, VerifyResult
    from lea.check_runtime import CheckText
    from app import proof_evidence
    monkeypatch.setattr(proof_evidence, 'canonical_path', lambda step, execution, *_: Path(execution['source_path']))
    original_init = CheckResult.__init__
    def checked_init(self, path, status, detail=None, execution=None, content=None):
        target = Path(path)
        if target.is_file() and (execution is None or execution.get('content_sha256') == mock_execution(target)['content_sha256']):
            execution = {**mock_execution(target), **(execution or {})}
            content = target.read_text() if content is None else content
        original_init(self, path, status, detail, execution, content)
    monkeypatch.setattr(CheckResult, '__init__', checked_init)

    original_add = store.add_code_step
    def checked_add(session_id, run_id, path, **kwargs):
        step = original_add(session_id, run_id, path, **kwargs)
        if kwargs.get('check_status') not in {'ok', 'error'} or kwargs.get('content_lost'):
            return step
        candidates = list(db.DB_PATH.parent.rglob(Path(path).name))
        absolute = next((p for p in candidates if p.is_file() and p.read_text(errors='replace') == kwargs['content']), None)
        if absolute is None:
            absolute = db.DB_PATH.parent / 'mock-compiler' / session_id / path
            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_text(kwargs['content'])
        result = CheckResult(str(absolute), kwargs['check_status'], kwargs.get('check_detail'))
        _, updated = record_check(session_id, path, absolute, result,
            run_id=run_id, formalization_id=kwargs.get('formalization_id'))
        # Preserve caller-supplied artifact classification in unit fixtures.
        if updated and result.status == 'ok':
            with db.connect() as conn:
                conn.execute('update timeline set artifact_kind=? where id=?', (step.get('artifact_kind'), int(updated['id'])))
            return store.latest_code_step_for_path(session_id, path)
        return updated or step
    monkeypatch.setattr(store, 'add_code_step', checked_add)

    original_set = store.set_code_step_check
    def checked_set(step_id, status, detail=None, artifact_kind=None):
        step = original_set(step_id, status, detail, artifact_kind)
        if not step:
            return None
        candidates = list(db.DB_PATH.parent.rglob(Path(step['path']).name))
        absolute = next((p for p in candidates if p.is_file() and p.read_text(errors='replace') == step['code']), None)
        if absolute is None:
            absolute = db.DB_PATH.parent / 'mock-compiler' / step['session_id'] / step['path']
            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_text(step['code'])
        result = CheckResult(str(absolute), status, detail)
        _, updated = record_check(step['session_id'], step['path'], absolute, result,
            run_id=step['run_id'], formalization_id=step.get('formalization_id'))
        return updated or step

    monkeypatch.setattr(store, 'set_code_step_check', checked_set)

    original_verify_event = store.record_verification_event
    def verified_event(**kwargs):
        if kwargs.get('execution') is None:
            step = store.current_proof_step(kwargs['session_id'], kwargs['path'])
            if step and step.get('check_evidence_id'):
                with db.connect() as conn:
                    row = conn.execute('select evidence_json from proof_check_evidence where id=?', (step['check_evidence_id'],)).fetchone()
                kwargs.update(execution=json.loads(row[0]), content=step['code'])
        return original_verify_event(**kwargs)
    monkeypatch.setattr(store, 'record_verification_event', verified_event)

    original_verify = proof_files.verify
    def verified(session_id, path, absolute, verifier, **kwargs):
        def invoke(actual):
            result = verifier(actual)
            if result.execution is None and Path(actual).is_file():
                return VerifyResult(result.status, result.detail, mock_execution(actual), Path(actual).read_text())
            return result
        return original_verify(session_id, path, absolute, invoke, **kwargs)
    monkeypatch.setattr(proof_files, 'verify', verified)

    original_audit = project_library.audit_module
    def audited(path, declarations):
        result = original_audit(path, declarations)
        if result.execution is None and Path(path).is_file():
            return VerifyResult(result.status, result.detail, mock_execution(path), Path(path).read_text())
        return result
    monkeypatch.setattr(project_library, 'audit_module', audited)

    generated = tools._generated_lean_check
    def checked_tool(path):
        result = generated(path)
        absolute = tools._run_relative_path(path)
        if isinstance(result, str) and not getattr(result, 'execution', None) and absolute.is_file():
            return CheckText(result, mock_execution(absolute), absolute.read_text())
        return result
    monkeypatch.setattr(tools, '_generated_lean_check', checked_tool)

    original_set_verify = store.set_session_safe_verify
    def set_verify(session_id, status, detail):
        original_set_verify(session_id, status, detail)
        step = store.latest_agent_code_step(session_id)
        if step:
            verified_event(session_id=session_id, formalization_id=step.get('formalization_id'),
                run_id=step.get('run_id'), path=step['path'], status=status, detail=detail,
                code_step_id=step['id'])
    monkeypatch.setattr(store, 'set_session_safe_verify', set_verify)
