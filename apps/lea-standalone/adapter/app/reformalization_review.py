"""Recovery of drafts approved before human correspondence review was retired."""
from __future__ import annotations
from . import db, store, reformalizations as ops


def latest(fid):
    with db.connect() as conn:
        row = conn.execute("select id from reformalization_operations where formalization_id=? order by rowid desc limit 1", (fid,)).fetchone()
    return ops.get(row[0]) if row else None


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
    from .formalization_reviews import retired
    retired()


def decide(fid, request):
    from .formalization_reviews import retired
    retired()


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
