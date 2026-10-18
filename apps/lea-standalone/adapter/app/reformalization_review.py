"""Exact-draft correspondence review using the existing review event ledger."""
from __future__ import annotations
import json
from uuid import uuid4
from fastapi import HTTPException
from . import db, store, reformalizations as ops


def latest(fid):
    with db.connect() as conn:
        row = conn.execute("select id from reformalization_operations where formalization_id=? order by rowid desc limit 1", (fid,)).fetchone()
    return ops.get(row[0]) if row else None


def published_approval(form, steps):
    """A receipt binds a draft approval to its actual canonical application.

    The original classifier remains needs_review. Withdrawal or changed canonical
    inputs remove this certificate; preservation receipts never transfer it.
    """
    from pathlib import Path
    from lea.check_evidence import freshness
    from .read_projections import current_snapshot
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("""select a.manifest_json,o.manifest_json as baseline,
            e.* from reformalization_applications a join reformalization_operations o on o.id=a.operation_id
            join formalization_review_events e on e.run_id=o.root_run_id and e.action='approve'
            where o.formalization_id=? and a.state='published' and o.state='settled'
            and not exists(select 1 from formalization_review_events w where w.action='withdraw' and w.approval_id=e.id)
            order by e.created_at desc""", (form['id'],))]
    for row in rows:
        journal = json.loads(row['manifest_json'])
        baseline = json.loads(row['baseline'])
        if journal.get('approval_id') != row['id']:
            continue
        if journal.get('selection', {}).get('declaration') != row['candidate_declaration']:
            continue
        if any(form.get(k) != v for k, v in baseline.get('target_identity', {}).items()):
            continue
        by_path = {s['path']: s for s in steps}
        candidate = by_path.get(row['candidate_path'])
        if (not candidate or candidate.get('code') != journal['checks'][row['candidate_path']]['content']
                or candidate.get('check_status') != 'ok' or not candidate.get('check_current')):
            continue
        exact = True
        for binding in json.loads(row['evidence_json']):
            current = by_path.get(binding['path'])
            if current and (current.get('check_status') != 'ok' or not current.get('check_current')):
                exact = False
                break
            check = journal['checks'].get(binding['path'])
            if check and not freshness(check['execution'], Path(baseline['canonical_repo']) / binding['path'], snapshot=current_snapshot())[0]:
                exact = False
                break
        if exact:
            return {k: row[k] for k in ('id', 'outcome', 'created_at', 'revision_token')}
    return None


def binding(op):
    drafts = ops.drafts(op['id'])
    result = op.get('result') or {}
    files = [{'path': d['path'], 'blob_id': d['blob_id'],
              'environment_sha256': ((d.get('evidence') or {}).get('execution') or {}).get('environment_sha256')}
             for d in drafts]
    token = 'reformalization:' + op['id'] + ':' + ops.digest(ops.dumps({
        'source': op['manifest']['source_hash'], 'baseline': op['manifest'],
        'selection': result.get('selection'), 'files': files}))
    return token, files


def checked_draft(op):
    from .reformalization_apply import assess
    assessment = assess(op['id'], op['result']['selection'])
    expected = {path: store.blob_content(info['blob_id']) for path, info in op['manifest']['files'].items()}
    expected.update({draft['path']: draft['code'] for draft in ops.drafts(op['id'])})
    if any(expected.get(path) != content for path, content in assessment['after'].items()):
        raise ValueError('The checked candidate changed from the retained draft being reviewed')
    return assessment


def context(op, form, source=None):
    selection = (op.get('result') or {}).get('selection')
    eligible, reason = op['state'] == 'review', 'Lean checked this draft. Confirm its correspondence with the source.'
    try:
        if not selection:
            raise ValueError('No explicitly selected checked result')
        checked_draft(op)
    except ValueError as exc:
        eligible, reason = False, str(exc)
    token, files = binding(op)
    outcome = 'defined' if form.get('kind') == 'definition' else 'disproved' if (selection or {}).get('kind') == 'disproved' else 'proved'
    review = {'eligible': eligible, 'reason': reason, 'revision_token': token,
        'allowed_outcomes': [outcome], 'approval': None, 'run_id': op['root_run_id'],
        'candidate_path': (selection or {}).get('path'),
        'candidate_declaration': (selection or {}).get('declaration'),
        'evidence': files, 'artifact_scope': 'attempt', 'reformalization_id': op['id']}
    return {'formalization': {**form, 'review': review}, 'review': review,
        'source_snapshot': source or {'statement': form.get('statement') or ''},
        'artifacts': [{'path': d['path'], 'content': d['code'], 'blob_id': d['blob_id'], 'artifact_scope': 'attempt'} for d in ops.drafts(op['id'])]}


def decide(fid, request):
    from . import formalization_reviews, overleaf_source
    op = latest(fid)
    raw = store.get_formalization(fid)
    digest = formalization_reviews._hash({k: v for k, v in request.items() if k != 'source_context_ref'})
    with db.connect() as conn:
        previous = conn.execute('select * from formalization_review_events where formalization_id=? and idempotency_key=?', (fid, request['idempotency_key'])).fetchone()
    if previous:
        if previous['request_digest'] != digest:
            raise HTTPException(409, 'Decision identity was reused')
        return {'decision': dict(previous), 'reformalization': ops.response(op)}
    if request['action'] == 'approve':
        info = context(op, raw)
        review = info['review']
        if not review['eligible'] or request.get('revision_token') != review['revision_token']:
            raise HTTPException(409, review['reason'] or 'The reviewed draft changed')
        if request.get('outcome') not in review['allowed_outcomes']:
            raise HTTPException(422, 'Outcome does not match the explicitly selected result')
        if raw['origin'] == 'overleaf':
            with db.connect() as conn:
                overleaf_source.resolve(conn, request.get('source_context_ref'), fid, bind_source=False)
        token, files = binding(op)
        selection = op['result']['selection']
        event = dict(id=str(uuid4()), formalization_id=fid, project_id=op['project_id'],
            session_id=op['session_id'], run_id=op['root_run_id'], action='approve', outcome=request['outcome'],
            revision_token=token, source_hash=op['manifest']['source_hash'],
            candidate_declaration=selection['declaration'], candidate_path=selection['path'],
            evidence_json=ops.dumps(files), approval_id=None, idempotency_key=request['idempotency_key'],
            request_digest=digest, actor='user', created_at=db.utc_now())
        with db.write() as conn:
            from .workspace_commands import authorize
            authorize(conn)
            current = conn.execute('select state from reformalization_operations where id=?', (op['id'],)).fetchone()
            if current[0] != 'review':
                raise HTTPException(409, 'The attempt is no longer awaiting review')
            conn.execute(f"insert into formalization_review_events ({','.join(event)}) values ({','.join('?' for _ in event)})", tuple(event.values()))
            result = {**op['result'], 'approval_id': event['id'], 'approval_token': token, 'outcome': request['outcome']}
            conn.execute("update reformalization_operations set state='queued',result_json=?,queued_at=?,updated_at=? where id=?", (ops.dumps(result), db.utc_now(), db.utc_now(), op['id']))
        ops.dispatch(op['project_id'])
    else:
        with db.connect() as conn:
            previous_approval = conn.execute("select run_id from formalization_review_events where id=? and formalization_id=? and action='approve'", (request.get('approval_id'), fid)).fetchone()
        if not previous_approval or not ops.for_run(previous_approval['run_id']):
            raise HTTPException(404, 'Approval not found')
        op = ops.for_run(previous_approval['run_id'])
        with db.write() as conn:
            from .workspace_commands import authorize
            authorize(conn)
            approved = conn.execute("select * from formalization_review_events where id=? and formalization_id=? and action='approve'", (request.get('approval_id'), fid)).fetchone()
            if not approved:
                raise HTTPException(404, 'Approval not found')
            event = {**dict(approved), 'id': str(uuid4()), 'action': 'withdraw', 'outcome': None,
                'approval_id': approved['id'], 'idempotency_key': request['idempotency_key'],
                'request_digest': digest, 'created_at': db.utc_now()}
            conn.execute(f"insert into formalization_review_events ({','.join(event)}) values ({','.join('?' for _ in event)})", tuple(event.values()))
            if op['state'] == 'queued':
                result = {k: v for k, v in op['result'].items() if k not in {'approval_id', 'approval_token'}}
                conn.execute("update reformalization_operations set state='review',result_json=? where id=?", (ops.dumps(result), op['id']))
    return {'decision': event, 'reformalization': ops.response(ops.get(op['id']))}


def apply_approved(op):
    from .reformalization_apply import apply
    try:
        with ops.acting(op['id']):
            token, _ = binding(op)
            if token != op['result']['approval_token']:
                raise ValueError('The approved draft changed')
            with db.connect() as conn:
                withdrawn = conn.execute("select 1 from formalization_review_events where action='withdraw' and approval_id=?", (op['result']['approval_id'],)).fetchone()
            if withdrawn:
                raise ValueError('The draft approval was withdrawn')
            assessment = checked_draft(op)
            apply(op['id'], op['result']['selection'], op['result']['outcome'], assessment=assessment)
    except Exception as exc:
        ops.update(op['id'], 'retained', result={**op['result'], 'detail': str(exc), 'outcome': 'conflict'})
    finally:
        ops.dispatch(op['project_id'])
