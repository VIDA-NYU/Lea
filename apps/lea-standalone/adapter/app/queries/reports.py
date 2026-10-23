"""Shared SQL/admission functions. No workspace or executor dependency."""
from __future__ import annotations
import base64
import json
from . import usage as usage_metadata
from ..db import connect

def _public(row, *, detail=True):
    value = usage_metadata.decode(dict(row))
    value['canceled'] = bool(value['canceled'])
    value['facts'] = json.loads(value.pop('facts_json')) if detail else None
    value.pop('request_hash', None)
    return value

def _project(slug):
    with connect() as conn:
        row=conn.execute('select id from projects where slug=?',(slug,)).fetchone()
    project=dict(row) if row else None
    if not project:
        raise LookupError('Project not found')
    return project

def get(slug, batch_id):
    project = _project(slug)
    with connect() as conn:
        row = conn.execute('select * from formalize_batch_reports where project_id = ? and batch_id = ?', (project['id'], batch_id)).fetchone()
    if not row:
        raise LookupError('Report not found')
    return _public(row)

def list_reports(slug, limit=20, before=None):
    project = _project(slug)
    if not 1 <= limit <= 50:
        raise ValueError('limit must be between 1 and 50')
    cursor = None
    if before:
        try:
            cursor = json.loads(base64.urlsafe_b64decode(before.encode()))
            if not isinstance(cursor, list) or len(cursor) != 2 or (not all((isinstance(value, str) for value in cursor))):
                raise ValueError()
        except Exception as exc:
            raise ValueError('Invalid report cursor') from exc
    with connect() as conn:
        rows = conn.execute('select * from formalize_batch_reports where project_id = ?\n            and (? is null or finished_at < ? or (finished_at = ? and batch_id < ?))\n            order by finished_at desc, batch_id desc limit ?', (project['id'], None if cursor is None else 1, cursor[0] if cursor else None, cursor[0] if cursor else None, cursor[1] if cursor else None, limit + 1)).fetchall()
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([last['finished_at'], last['batch_id']]).encode()).decode()
    return {'reports': [_public(row, detail=False) for row in rows[:limit]], 'nextCursor': next_cursor}
