"""Human correspondence decisions over immutable, currently checked inputs.

Canonical decisions use the same candidate/evidence rules without changing run
outcomes. Attempt review rechecks its retained draft and queues host application;
approval never starts another provider run.
"""
from __future__ import annotations

import hashlib
import json
import re
from uuid import uuid4

from fastapi import HTTPException
from lea.check_evidence import mutation

from . import db, store, project_coordination
from .config import load_config
from .artifacts import contains_sorry_marker, scan_lean_declarations, scrub_lean_source
from .read_projections import decode_evidence, read_projection
from .target_completion import checked_target, compiler_result

REVIEW_MESSAGE = 'Lean check passed. Your approval is needed to confirm this result against the LaTeX.'


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _error(message, status=409):
    raise HTTPException(status, detail={'error': 'review_conflict', 'message': message})


def _records(fid):
    with db.connect() as conn:
        events = [dict(row) for row in conn.execute(
            'select * from formalization_review_events where formalization_id=? order by rowid desc', (fid,))]
        attempt = conn.execute('''select id from runs where focus_formalization_id=?
            and (purpose in ('overleaf_solver','project_dependency')
                or (status != 'answered' and (result_kind is null or result_kind != 'answered')))
            order by created_at desc, rowid desc limit 1''', (fid,)).fetchone()
    return events, attempt['id'] if attempt else None


def project(form, steps, primary, artifact, completion, active=None, *, requested=False):
    """Return review metadata, immutable bindings, candidate and technical verdict."""
    events, attempt_id = _records(form['id'])
    result = {'eligible': False, 'reason': '', 'revision_token': None, 'allowed_outcomes': [], 'approval': None}
    bindings = []
    candidate = None
    technical = None
    review_run = requested or bool(completion and (completion.get('result_kind') == 'needs_review'
        or (completion.get('result_kind') == 'defined' and form.get('kind') != 'definition')
        or (completion.get('result_kind') in {'proved', 'disproved'} and completion.get('result_detail')
            and completion['result_detail'].strip().upper() != completion['result_kind'].upper())))
    managed = review_run or bool(events)
    if not managed:
        return result, bindings, candidate, technical
    active = active or project_coordination.review_busy(form.get('project_id'), (completion or {}).get('id'))

    def blocked(status, reason):
        result['reason'] = reason
        return result, bindings, candidate, (status, reason, False, None)

    if not steps:
        return blocked('draft' if not form.get('declaration_name') else 'planned', 'No candidate artifact is available for approval.')
    source_hash = form.get('source_hash') or _hash(form.get('statement') or '')
    candidate_source = (artifact or {}).get('source_hash') or (completion or {}).get('focus_source_hash')
    if form.get('origin') == 'overleaf' and (not form.get('source_hash') or not candidate_source):
        return blocked('unchecked', 'The candidate has no recorded LaTeX source identity.')
    if form.get('source_hash') and candidate_source and source_hash != candidate_source:
        return blocked('stale', 'The LaTeX source changed. Re-formalize before approving this result.')
    match, reason = checked_target(form, steps)
    if 'ambiguous' in reason:
        return blocked('unchecked', reason)
    primary_path = (primary or {}).get('path') or (artifact or {}).get('path')
    # A differently named counterexample must be explicitly associated as primary.
    selected = [s for s in steps if s['path'] == primary_path] if primary_path else ([match] if match else [])
    if len(selected) != 1 or 'scratch' in selected[0]['path'].lower():
        return blocked('unchecked', reason or 'The candidate artifact is missing or ambiguous.')
    candidate = selected[0]
    code = candidate.get('code') or ''
    declarations = scan_lean_declarations(code)
    compiled = compiler_result(candidate, (artifact or {}).get('declaration_name') or form.get('declaration_name'), form.get('kind') or 'theorem')
    if compiled is False:
        return blocked('failing', 'The selected result has no passing compiler trust evidence.')
    if compiled is None and contains_sorry_marker(code):
        return blocked('failing', 'The candidate still contains sorry or admit.')
    # Compile evidence alone is not permission to accept proof shortcuts.
    if compiled is None and re.search(r'\b(?:axiom|native_decide|unsafe|partial)\b', scrub_lean_source(code)):
        return blocked('failing', 'The candidate contains a prohibited proof declaration or tactic.')
    if any(s.get('check_status') == 'error' and s.get('check_current') for s in steps):
        return blocked('failing', 'The current artifact failed its Lean check.')
    if not candidate.get('check_current') or candidate.get('check_status') != 'ok':
        return blocked('unchecked', 'The candidate needs a current passing Lean check.')
    primary_role = next((f.get('role') for f in form.get('files', []) if f['path'] == candidate['path']), None)
    if primary_role == 'support' and (not match or match['path'] != candidate['path']):
        return blocked('unchecked', 'A supporting helper cannot be approved as the target result.')
    outcomes = []
    if match is not None and match['path'] == candidate['path']:
        outcomes.append('defined' if form.get('kind') == 'definition' else 'proved')
    proof_declarations = [d for d in declarations if d.kind in {'theorem', 'lemma'}]
    if form.get('kind') != 'definition' and primary_path and proof_declarations:
        # The actual declaration must be unambiguous when it isn't the named target.
        if (match and match['path'] == candidate['path']) or len(proof_declarations) == 1:
            outcomes.append('disproved')
    if not outcomes:
        return blocked('unchecked', reason or 'No checked declaration can be approved for this target.')
    for step in sorted(steps, key=lambda s: s['path']):
        execution = decode_evidence(step.get('proof_evidence_json') or '{}')
        if not execution:
            with db.connect() as conn:
                row = conn.execute('select evidence_json from proof_check_evidence where id=?', (step.get('check_evidence_id'),)).fetchone()
            execution = json.loads(row[0]) if row else {}
        bindings.append({'path': step['path'], 'blob_id': step.get('blob_id'),
            'sha256': _hash(step.get('code') or ''), 'check_evidence_id': step.get('check_evidence_id'),
            'environment_sha256': execution.get('environment_sha256')})
    candidate_binding = next(b for b in bindings if b['path'] == candidate['path'])
    if not candidate.get('check_evidence_id') or not candidate_binding['blob_id'] or not candidate_binding['environment_sha256']:
        return blocked('unchecked', 'The candidate has no immutable check evidence.')
    from .target_completion import target_declaration
    named = target_declaration(code, form.get('declaration_name'), form.get('kind') or 'theorem')
    declaration = named.full_name if named else proof_declarations[0].full_name
    # Evidence IDs can change on an identical-input manual check. Content and
    # environment, rather than the check timestamp/ID, define the reviewed revision.
    token = _hash({'version': 1, 'formalization_id': form['id'], 'run_id': (completion or {}).get('id'),
        'attempt_id': attempt_id, 'source_hash': source_hash, 'declaration': declaration,
        'kind': form.get('kind'), 'candidate_path': candidate['path'],
        'files': [{k: b[k] for k in ('path', 'sha256', 'environment_sha256')} for b in bindings]})
    withdrawn = {e['approval_id'] for e in events if e['action'] == 'withdraw'}
    approval = next((e for e in events if e['action'] == 'approve' and e['id'] not in withdrawn
        and e['revision_token'] == token), None)
    result.update(revision_token=token, allowed_outcomes=outcomes,
        reason=(completion or {}).get('result_detail') or REVIEW_MESSAGE)
    if result['reason'].strip().upper() == 'NEEDS_REVIEW':
        result['reason'] = REVIEW_MESSAGE
    result['candidate_declaration'] = declaration
    result['candidate_path'] = candidate['path']
    result['run_id'] = (completion or {}).get('id')
    result['evidence'] = bindings
    if approval:
        result['approval'] = {k: approval[k] for k in ('id', 'outcome', 'created_at', 'revision_token')}
        technical = (approval['outcome'], 'Approved by user.', True,
            'definition' if approval['outcome'] == 'defined' else 'theorem')
    elif review_run:
        technical = ('needs_review', REVIEW_MESSAGE, True, 'definition' if form.get('kind') == 'definition' else 'theorem')
        result['eligible'] = not active
    if active:
        result['eligible'] = False
        result['reason'] = 'Wait for the current target operation to finish before approving.'
        # A pending attempt cannot retain an earlier user certificate.
        if approval:
            result['approval'] = None
            technical = ('needs_review', result['reason'], True, None)
    return result, bindings, candidate, technical


@read_projection
def context(fid, source_context_ref=None):
    from . import formalizations, overleaf_source, reformalizations
    form = formalizations.get(fid)
    if not form:
        _error('Formalization not found.', 404)
    source = None
    if source_context_ref:
        with db.connect() as conn:
            try:
                source = overleaf_source.resolve(conn, source_context_ref, fid, bind_source=False)
            except ValueError as exc:
                _error(str(exc))
    from . import reformalization_review
    attempt = reformalization_review.latest(fid)
    if attempt and attempt['state'] != 'settled' and reformalizations.drafts(attempt['id']):
        return reformalization_review.context(attempt, form, source)
    steps = store.current_code_steps_for_formalization(fid)
    # Include all full immutable artifacts, never the capped target-status preview.
    if not steps and form.get('completion_run_id'):
        with db.connect() as conn:
            rows = conn.execute(store.TIMELINE_WITH_BLOB + ' where t.kind=\'code\' and t.run_id=? order by t.id desc',
                (form['completion_run_id'],)).fetchall()
        seen = set()
        for row in rows:
            if row['path'] not in seen:
                seen.add(row['path'])
                steps.append(store._code_step_from_row(row))
    if source is None and form.get('completion_run_id'):
        with db.connect() as conn:
            row = conn.execute('select source_bundle_json from lea_status_run_contexts where run_id=?', (form['completion_run_id'],)).fetchone()
        source = json.loads(row[0]) if row and row[0] else None
    bindings = form['review'].get('evidence')
    snapshots = ([{'path': binding['path'], 'content': store.blob_content(binding['blob_id']) or '',
        'blob_id': binding['blob_id']} for binding in bindings] if bindings else
        [{'path': s['path'], 'content': s.get('code') or '', 'blob_id': s.get('blob_id')} for s in steps])
    return {'formalization': form, 'review': form['review'], 'source_snapshot': source or {'statement': form.get('statement') or ''},
        'artifacts': snapshots}


def decide(fid, request):
    from . import formalizations, overleaf_source
    if (request.get('revision_token') or '').startswith('reformalization:'):
        from .reformalization_review import decide as decide_draft
        return decide_draft(fid, request)
    if request.get('approval_id'):
        with db.connect() as conn:
            prior = conn.execute('select revision_token from formalization_review_events where id=?', (request['approval_id'],)).fetchone()
        if prior and prior[0].startswith('reformalization:'):
            from .reformalization_review import decide as decide_draft
            return decide_draft(fid, request)
    raw = store.get_formalization(fid)
    if not raw:
        _error('Formalization not found.', 404)
    digest = _hash({k: v for k, v in request.items() if k != 'source_context_ref'})
    root = load_config().lea_root
    with project_coordination.review_decision(raw.get('project_id'), formalizations.get(fid).get('completion_run_id')), mutation(root / 'workspace' if root else db.DB_PATH.parent):
        with db.write() as conn:
            from .workspace_commands import authorize
            authorize(conn)
            duplicate = conn.execute('''select e.*, q.request_digest as submitted_digest
                from formalization_review_requests q join formalization_review_events e on e.id=q.event_id
                where q.formalization_id=? and q.idempotency_key=?''',
                (fid, request['idempotency_key'])).fetchone()
            if duplicate:
                if duplicate['submitted_digest'] != digest:
                    _error('This idempotency key was used for a different decision.')
                event = dict(duplicate)
            else:
                form = formalizations.get(fid)
                if form['activity']['status'] != 'idle':
                    _error('Wait for the current target operation to finish before changing its approval.')
                review = form['review']
                action = request['action']
                if action == 'approve':
                    if not request.get('revision_token'):
                        _error('A reviewed revision token is required.', 422)
                    if form.get('origin') == 'overleaf':
                        if not request.get('source_context_ref'):
                            _error('Refresh the current Overleaf source before approving.')
                        try:
                            overleaf_source.resolve(conn, request['source_context_ref'], fid, bind_source=False)
                        except ValueError as exc:
                            _error(str(exc))
                    if request.get('outcome') not in {'proved', 'defined', 'disproved'}:
                        _error('This outcome is not supported by the checked candidate.', 422)
                    if request.get('revision_token') != review['revision_token']:
                        _error('The reviewed inputs changed. Refresh and review the current result.')
                    if review['allowed_outcomes'] and request['outcome'] not in review['allowed_outcomes']:
                        _error('This outcome is not supported by the checked candidate.', 422)
                    existing = review['approval']
                    if existing:
                        if existing['outcome'] != request['outcome']:
                            _error('Withdraw the current approval before choosing another outcome.')
                        conn.execute('insert into formalization_review_requests values (?,?,?,?)',
                            (fid, request['idempotency_key'], digest, existing['id']))
                        return {'decision': existing, 'formalization': form}
                    if not review['eligible']:
                        _error(review['reason'] or 'This result is not ready for approval.')
                    if request['outcome'] not in review['allowed_outcomes']:
                        _error('This outcome is not supported by the checked candidate.', 422)
                    evidence = review['evidence']
                    approval_id = None
                    outcome = request['outcome']
                    token = review['revision_token']
                    run_id = review['run_id']
                else:
                    if not request.get('approval_id'):
                        _error('The approval to withdraw is required.', 422)
                    previous = conn.execute('select * from formalization_review_events where id=? and formalization_id=? and action=\'approve\'',
                        (request.get('approval_id'), fid)).fetchone()
                    if not previous:
                        _error('Approval not found.', 404)
                    already = conn.execute('select * from formalization_review_events where approval_id=? and action=\'withdraw\'', (previous['id'],)).fetchone()
                    if already:
                        conn.execute('insert into formalization_review_requests values (?,?,?,?)',
                            (fid, request['idempotency_key'], digest, already['id']))
                        return {'decision': dict(already), 'formalization': form}
                    evidence = json.loads(previous['evidence_json'])
                    approval_id, outcome, token, run_id = previous['id'], None, previous['revision_token'], previous['run_id']
                session_id = (store.get_run(run_id) or {}).get('session_id') if run_id else raw.get('loose_session_id')
                event = dict(id=str(uuid4()), formalization_id=fid, project_id=raw.get('project_id'), session_id=session_id,
                    run_id=run_id, action=action, outcome=outcome, revision_token=token,
                    source_hash=(form.get('source_hash') or _hash(form.get('statement') or '')) if action == 'approve' else previous['source_hash'],
                    candidate_declaration=review.get('candidate_declaration') if action == 'approve' else previous['candidate_declaration'],
                    candidate_path=review.get('candidate_path') if action == 'approve' else previous['candidate_path'],
                    evidence_json=json.dumps(evidence, sort_keys=True), approval_id=approval_id,
                    idempotency_key=request['idempotency_key'], request_digest=digest, actor='user', created_at=db.utc_now())
                conn.execute(f"insert into formalization_review_events ({','.join(event)}) values ({','.join('?' for _ in event)})", tuple(event.values()))
                conn.execute('insert into formalization_review_requests values (?,?,?,?)',
                    (fid, request['idempotency_key'], digest, event['id']))
        return {'decision': event, 'formalization': formalizations.get(fid)}
