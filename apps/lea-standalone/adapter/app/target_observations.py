"""Publish each target independently of unrelated project activity.

Compiler evidence, imports, toolchain/config files and SQL inputs are captured
by the worker. Query processes only replay the persisted SQL read set.
"""
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from . import db, read_projections
from .queries.target_observations import _dump, _digest, _matches, observation, publication_barriers

LIFETIME_SECONDS = 120
MAX_QUERIES = 2000
MAX_BYTES = 32 * 1024 * 1024


def _snapshot(inputs):
    result = read_projections.ReadSnapshot()
    result.files = inputs.get('files', {})
    result.topology = inputs.get('topology', {})
    result.artifact_sets = inputs.get('artifact_sets', {})
    return result


def publish_run(run_id):
    """Publish committed completion inputs before the run's terminal event.

    A concurrent edit may require a retry; the independent target observer
    handles that without making the solver's outcome depend on publication.
    """
    with db.connect() as conn:
        row = conn.execute('''select f.id,coalesce('p:'||f.project_id,'s:'||f.loose_session_id) scope
            from runs r join formalizations f on f.id=r.focus_formalization_id
            where r.id=? and r.status not in ('pending','running')''', (run_id,)).fetchone()
        if not row:
            return {'state': 'waiting'}
    with db.write() as conn:
        # Respect an explicitly disabled scope, and support newly created scopes
        # before the broad inventory maintenance has enrolled them.
        conn.execute('insert or ignore into core_projection_enrollment(scope,enabled) values (?,1)', (row['scope'],))
        enabled = conn.execute('select enabled from core_projection_enrollment where scope=?', (row['scope'],)).fetchone()[0]
    return capture(row['id']) if enabled else {'state': 'disabled'}


def capture(formalization_id):
    from . import core_observer, config, store
    from .queries.core import scope_for
    snapshot = read_projections.ReadSnapshot()
    token = read_projections._active.set(snapshot)
    artifacts = read_projections._artifacts.set({})
    try:
        snapshot.observation(config.ROOT / 'config/lea.local.toml')
        with db.read_snapshot() as conn:
            if not conn.execute('select 1 from formalizations where id=?', (formalization_id,)).fetchone():
                return {'state': 'deleted'}
            scope = scope_for(formalization_id=formalization_id)
            with db.capture_read_dependencies() as statements:
                payload = core_observer._capture('formalization', formalization_id, snapshot)
                steps = store.current_code_steps_for_formalization(formalization_id)
                payload['step'] = {str(s['id']): core_observer._capture('step', str(s['id']), snapshot)['step'] for s in steps}
                run_ids = {r['id'] for r in conn.execute('select id from runs where focus_formalization_id=?', (formalization_id,))}
                payload['run'] = {rid: core_observer._capture('run', rid, snapshot)['run'] for rid in run_ids}
                payload['dependency'] = {r[0]: core_observer._capture('dependency', r[0], snapshot)['dependency']
                    for r in conn.execute('select id from project_dependencies where formalization_id=?', (formalization_id,))}
                paths = {item['path'] for item in (payload.get('artifact_snapshot') or {}).get('files', [])}
                paths.update(item['path'] for item in steps)
                if publication_barriers(conn, scope, formalization_id, snapshot.files, paths):
                    return {'state': 'retry'}
            if len(statements) > MAX_QUERIES:
                raise ValueError('Target observation exceeds query budget')
            queries = [[sql, _digest(conn, sql)] for sql in sorted(statements)]
        inputs = {'files': snapshot.files, 'topology': snapshot.topology, 'artifact_sets': snapshot.artifact_sets}
        encoded = [_dump(value) for value in (payload, queries, inputs)]
        if sum(len(value.encode()) for value in encoded) > MAX_BYTES:
            raise ValueError('Target observation exceeds byte budget')
        if not snapshot.unchanged():
            return {'state': 'retry'}
        with db.write() as conn:
            if not _matches(conn, queries) or not snapshot.unchanged():
                return {'state': 'retry'}
            now = datetime.now(timezone.utc)
            conn.execute('''insert into core_target_observations values(?,?,?,'observed',?,?,?,?,?,?)
                on conflict(formalization_id) do update set scope=excluded.scope,observation_id=excluded.observation_id,
                state=excluded.state,payload_json=excluded.payload_json,queries_json=excluded.queries_json,
                inputs_json=excluded.inputs_json,observed_at=excluded.observed_at,expires_at=excluded.expires_at,last_scan_at=excluded.last_scan_at''',
                (formalization_id, scope, uuid4().hex, *encoded, now.isoformat(),
                 (now + timedelta(seconds=LIFETIME_SECONDS)).isoformat(), now.isoformat()))
        return {'state': 'observed', 'formalization_id': formalization_id}
    except OSError:
        # An active file can change during capture; it must not prevent other
        # targets from publishing independently on the next maintenance pass.
        return {'state': 'retry'}
    finally:
        read_projections._active.reset(token)
        read_projections._artifacts.reset(artifacts)


def maintain(limit=20):
    with db.connect() as conn:
        rows = conn.execute('''select f.id,o.* from formalizations f join core_projection_enrollment e
            on e.scope=coalesce('p:'||f.project_id,'s:'||f.loose_session_id) and e.enabled=1
            left join core_target_observations o on o.formalization_id=f.id
            left join runs r on r.id=(select id from runs where focus_formalization_id=f.id
                order by created_at desc,rowid desc limit 1)
            order by case when r.status not in ('pending','running')
                and (o.last_scan_at is null or julianday(r.updated_at)>julianday(o.last_scan_at))
                then 0 else 1 end,coalesce(o.last_scan_at,''),f.id limit ?''', (limit,)).fetchall()
    for raw in rows:
        row = dict(raw); fid = row['id']
        with db.write() as conn:
            # Scanning is internal bookkeeping, not a status notification.
            if not row['formalization_id']:
                from .queries.core import scope_for
                now = db.utc_now()
                conn.execute("insert or ignore into core_target_observations values(?,?,?,'capturing','{}','[]','{}',?,?,?)",
                             (fid, scope_for(formalization_id=fid), uuid4().hex, now, now, now))
            conn.execute('update core_target_observations set last_scan_at=? where formalization_id=?', (db.utc_now(), fid))
        needs_capture = not row['formalization_id']
        if not needs_capture:
            with db.read_snapshot():
                _, reason = observation(fid)
            snapshot = _snapshot(json.loads(row['inputs_json']))
            unchanged = snapshot.unchanged()
            if unchanged and row['state'] == 'observed' and reason in {None, 'observation_expired'} and (
                datetime.fromisoformat(row['observed_at']) < datetime.now(timezone.utc) - timedelta(seconds=30)):
                queries = json.loads(row['queries_json'])
                with db.write() as conn:
                    # Revalidate after acquiring the writer. A concurrent edit,
                    # scope disable or replacement publication cannot be renewed.
                    current = conn.execute('''select o.observation_id from core_target_observations o
                        join core_projection_enrollment e on e.scope=o.scope and e.enabled=1
                        where o.formalization_id=? and o.state='observed' ''', (fid,)).fetchone()
                    if not current or current[0] != row['observation_id'] or not _matches(conn, queries) or not snapshot.unchanged():
                        return {'state': 'retry'}
                    now = datetime.now(timezone.utc)
                    conn.execute('update core_target_observations set observed_at=?,expires_at=? where formalization_id=?',
                        (now.isoformat(), (now + timedelta(seconds=LIFETIME_SECONDS)).isoformat(), fid))
                return {'state': 'observed', 'formalization_id': fid, 'renewed': True}
            needs_capture = bool(reason or not unchanged or
                datetime.fromisoformat(row['observed_at']) < datetime.now(timezone.utc) - timedelta(seconds=30))
            if not unchanged:
                with db.write() as conn:
                    conn.execute("update core_target_observations set state='reconciling' where formalization_id=?", (fid,))
        if needs_capture:
            return capture(fid)
    return {'state': 'waiting'}
