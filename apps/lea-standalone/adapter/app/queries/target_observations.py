"""SQL-only target facts, fenced by the worker's exact dependency read set."""
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from .. import db

MAX_QUERIES = 2000


def publication_barriers(conn, scope, formalization_id, absolute_paths, relative_paths):
    """Keep publication/conflict fences for the target's actual files.

    Namespace/project operations still fence the whole scope. File edits and
    staged proof publication fence only their affected targets/imports.
    These SELECTs are also part of the persisted dependency read set.
    """
    absolute = _dump(sorted(absolute_paths))
    relative = _dump(sorted(relative_paths))
    owners = [r[0] for r in conn.execute('''select distinct f.id from formalizations f
        join formalization_files ff on ff.formalization_id=f.id
        where coalesce('p:'||f.project_id,'s:'||f.loose_session_id)=?
        and ff.path in (select value from json_each(?))''', (scope, relative))]
    owners = _dump(sorted(set(owners) | {formalization_id}))
    effects = conn.execute('''select e.id,e.state from workspace_effects e
        left join workspace_requests w on w.command_id=e.command_id
        where e.state in ('prepared','installed','conflict') and (
            (e.kind='file' and json_extract(e.manifest_json,'$.absolute') in (select value from json_each(?)))
            or (e.kind!='file' and coalesce('p:'||e.project_id,'s:'||e.session_id)=?
                and (w.formalization_id is null or w.formalization_id in (select value from json_each(?)))))''',
        (absolute, scope, owners)).fetchall()
    applications = conn.execute('''select a.id,a.state from reformalization_applications a
        join reformalization_operations o on o.id=a.operation_id join formalizations f on f.id=o.formalization_id
        where coalesce('p:'||f.project_id,'s:'||f.loose_session_id)=?
        and a.state in ('prepared','applying','restoring','conflict','conflicted')
        and (f.id in (select value from json_each(?)) or exists(
            select 1 from json_each(a.manifest_json,'$.files') j where j.key in (select value from json_each(?))))''',
        (scope, owners, relative)).fetchall()
    return bool(effects or applications)


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      default=lambda value: {'bytes': value.hex()} if isinstance(value, bytes) else str(value))


def _digest(conn, sql):
    # The recorded statements originate in worker reads. The connection is
    # query-only even when loading a corrupted/unsupported observation.
    cursor = conn.execute(sql)
    # Explicitly annotated presentation fields do not determine proof validity.
    # Conversation recency can change while a different target runs in it.
    columns = [i for i, column in enumerate(cursor.description) if not column[0].startswith('_display_')]
    rows = [tuple(row[i] for i in columns) for row in cursor]
    if len(columns) != len(cursor.description):
        rows.sort(key=_dump)
    return hashlib.sha256(_dump(rows).encode()).hexdigest()


def _matches(conn, queries):
    return len(queries) <= MAX_QUERIES and all(_digest(conn, sql) == digest for sql, digest in queries)


def observation(formalization_id):
    from . import core
    cache = core._cache.get()
    key = ('target', formalization_id)
    if cache is not None and key in cache:
        return cache[key]
    with db.read_snapshot() as conn:
        row = conn.execute('''select o.* from core_target_observations o join core_projection_enrollment e
            on e.scope=o.scope and e.enabled=1 where o.formalization_id=?''', (formalization_id,)).fetchone()
        value = dict(row) if row else None
        reason = None
        if value:
            reason = None if value['state'] == 'observed' else value['state']
            if datetime.fromisoformat(value['expires_at']) <= datetime.now(timezone.utc):
                reason = 'observation_expired'
            try:
                if not _matches(conn, json.loads(value['queries_json'])):
                    reason = 'inputs_changed'
            except (sqlite3.Error, ValueError, TypeError):
                reason = 'inputs_changed'
    result = (value, reason)
    if cache is not None:
        cache[key] = result
    return result


def metadata(formalization_id):
    value, reason = observation(formalization_id)
    if not value:
        return None
    return {'version': 1, 'state': reason or 'observed', 'observation_id': value['observation_id'],
            'observed_at': value['observed_at'], 'expires_at': value['expires_at'],
            'environment_revision': hashlib.sha256(value['inputs_json'].encode()).hexdigest()}


def fact(scope, kind, entity_id):
    """Return (handled, value); a stale target never falls back to project facts."""
    if kind in {'formalization', 'artifact_snapshot', 'review_context', 'target'}:
        fid = entity_id
    else:
        with db.connect() as conn:
            if kind == 'step':
                row = conn.execute('''select coalesce(t.formalization_id,ff.formalization_id) from timeline t
                    join sessions s on s.id=t.session_id left join formalization_files ff on ff.path=t.path
                    and ff.formalization_id in (select id from formalizations where project_id=s.project_id
                        or (project_id is null and loose_session_id=s.id)) where t.id=? limit 1''', (entity_id,)).fetchone()
            elif kind == 'run':
                row = conn.execute('select focus_formalization_id from runs where id=?', (entity_id,)).fetchone()
            elif kind == 'dependency':
                row = conn.execute('select formalization_id from project_dependencies where id=?', (entity_id,)).fetchone()
            else:
                return False, None
        fid = row[0] if row else None
    if not fid:
        return False, None
    observed, reason = observation(fid)
    if not observed:
        return False, None
    if reason or observed['scope'] != scope:
        return True, None
    payload = json.loads(observed['payload_json'])
    value = payload.get(kind)
    if kind in {'step', 'run', 'dependency'}:
        value = (value or {}).get(str(entity_id))
    return True, value


