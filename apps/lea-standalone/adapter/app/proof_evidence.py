"""Immutable check records and the single projection of current proof freshness."""
from __future__ import annotations
import json
from pathlib import Path
from uuid import uuid4

from lea.check_evidence import digest, freshness, coordinator, lake_root
from . import db
from .read_projections import current_snapshot, decode_evidence


def canonical_path(step, execution, namespace=None, parent_id=None):
    from .config import load_config
    root = load_config().lea_root / 'workspace/proofs'
    captured = Path(execution['source_path'])
    if parent_id and captured.is_relative_to(root.resolve()):
        return captured
    base = root / namespace.replace('.', '/') if namespace else root / step['session_id']
    return base / step['path']


def project_step(step, record=None):
    """Historical verdicts remain intact; current status requires their inputs."""
    evidence_id = step.get('check_evidence_id')
    if not evidence_id:
        return {**step, 'check_current': False, 'check_stale_reason': 'missing_evidence'}
    if record is None:
        with db.connect() as conn:
            row = conn.execute('''select e.evidence_json,p.namespace,s.parent_id from proof_check_evidence e
                join sessions s on s.id=e.session_id left join projects p on p.id=s.project_id
                where e.id=? and e.blob_id=? and e.session_id=? and e.path=? and e.project_id is s.project_id''',
                (evidence_id, step.get('blob_id'), step['session_id'], step['path'])).fetchone()
        record = dict(row) if row else {}
    try:
        execution = decode_evidence(record.get('evidence_json') or '{}')
        if execution.get('content_sha256') != digest(step['code'].encode()):
            return {**step, 'check_current': False, 'check_stale_reason': 'source_changed'}
        absolute = canonical_path(step, execution, record.get('namespace'), record.get('parent_id'))
        current, reason = freshness(execution, absolute, snapshot=current_snapshot())
    except (KeyError, ValueError, TypeError, OSError):
        current, reason = False, 'missing_evidence'
    selected = execution.get('selected_result') if 'execution' in locals() else None
    return {**step, 'check_current': current, 'check_stale_reason': reason,
            **({'selected_result': {'declaration': selected['user_name'], 'kind': selected['kind'],
                'axioms': selected['axioms']}} if selected else {})}


def effective_status(step):
    return step.get('check_status') if step and step.get('check_current') else None


def record_check(session_id, path, absolute, result, *, expected=None, require_current=False,
                 run_id=None, formalization_id=None, author=None, summary=None, content=None):
    """Keep stale evidence in history, but never backfill a different source blob."""
    from . import store
    absolute = Path(absolute)
    execution = getattr(result, 'execution', None) or {}
    captured = getattr(result, 'content', None)
    captured = content if captured is None else captured
    if captured is None or execution.get('content_sha256') != digest(captured.encode()):
        return False, None
    if execution.get('source_path') and Path(execution['source_path']).resolve() != absolute.resolve():
        return False, None
    execution = {**execution, 'source_path': str(absolute.resolve())}
    lock = coordinator(lake_root(absolute) or absolute.parent)
    with lock.lease():
        valid, _ = freshness(execution, absolute)
        try:
            disk = absolute.read_bytes().decode('utf-8')
        except (OSError, UnicodeError):
            disk = None
        with db.write() as conn:
            from .workspace_commands import authorize
            authorize(conn)
            session = conn.execute('select * from sessions where id=?', (session_id,)).fetchone()
            if not session:
                return False, None
            current = store.current_proof_step(session_id, path, conn=conn)
            pinned = not require_current or ((expected is None and current is None) or (
                expected and current and expected['id'] == current['id'] and
                expected['blob_id'] == current['blob_id']))
            blob_id = store._put_blob(conn, captured)
            step_id = current['id'] if current and current['blob_id'] == blob_id else None
            create = disk == captured and pinned and (current is None or
                (run_id and current.get('run_id') != run_id) or author or
                current['session_id'] != session_id or (formalization_id and current.get('formalization_id') != formalization_id))
            if create:
                kind = 'agent' if author not in {None, 'agent', 'user', 'environment'} else author or 'agent'
                cursor = conn.execute('''insert into timeline(session_id,run_id,kind,author,path,
                    after_blob_id,summary,formalization_id,created_at,content_lost,data)
                    values (?,?,'code',?,?,?,?,?,?,0,?)''',
                    (session_id, run_id, kind, path, blob_id, summary, formalization_id,
                     db.utc_now(), json.dumps({'reason': author}) if author else None))
                step_id = str(cursor.lastrowid)
            elif step_id is None:
                historical = conn.execute('''select id from timeline where kind='code' and
                    session_id=? and path=? and after_blob_id=? order by id desc limit 1''',
                    (session_id, path, blob_id)).fetchone()
                step_id = str(historical['id']) if historical else None
            evidence_id = execution.get('check_id') or str(uuid4())
            conn.execute('''insert or ignore into proof_check_evidence(id,session_id,project_id,
                run_id,code_step_id,blob_id,path,status,detail,evidence_json,created_at)
                values (?,?,?,?,?,?,?,?,?,?,?)''', (evidence_id, session_id, session['project_id'],
                run_id, int(step_id) if step_id else None, blob_id, path, result.status,
                result.detail, json.dumps(execution, sort_keys=True), db.utc_now()))
            stored = conn.execute('select * from proof_check_evidence where id=?', (evidence_id,)).fetchone()
            original = json.loads(stored['evidence_json'])
            if (stored['blob_id'] != blob_id or stored['status'] != result.status
                    or stored['session_id'] != session_id or stored['project_id'] != session['project_id']
                    or stored['path'] != path or stored['run_id'] != run_id or any(
                        original.get(key) != execution.get(key) for key in
                        ('content_sha256', 'environment_sha256', 'source_identity'))):
                return False, None
            if step_id and pinned:
                from .artifacts import classify_lean_artifact
                artifact = classify_lean_artifact(captured) if result.status == 'ok' else None
                conn.execute('''update timeline set check_status=?,check_detail=?,artifact_kind=?,
                    check_evidence_id=? where id=? and after_blob_id=?''',
                    (result.status, result.detail, artifact, evidence_id, int(step_id), blob_id))
            row = conn.execute(f'{store.TIMELINE_WITH_BLOB} where t.id=?', (int(step_id),)).fetchone() if step_id else None
        step = store._code_step_from_row(row) if row else None
    store._bump_sessions_changed()
    return bool(pinned and disk == captured and (valid or result.status == 'error')), step


def verification_current(event, step):
    if not event or not step or event.get('blob_id') != (step.get('blob_id') or step.get('after_blob_id')):
        return False
    try:
        execution = decode_evidence(event.get('evidence_json') or '{}')
        with db.connect() as conn:
            owner = conn.execute('select p.namespace,s.parent_id from sessions s left join projects p on p.id=s.project_id where s.id=?', (step['session_id'],)).fetchone()
        return bool(owner and freshness(execution, canonical_path(step, execution, owner['namespace'], owner['parent_id']), snapshot=current_snapshot())[0])
    except (KeyError, ValueError, TypeError):
        return False
