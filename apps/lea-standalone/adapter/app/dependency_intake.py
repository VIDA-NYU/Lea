"""Shared SQL/admission functions. No workspace or executor dependency."""
from __future__ import annotations
import json
from .queries import core
from .db import connect, utc_now

def _decode(row):
    if row is None:
        return None
    result = dict(row)
    for column, key in (('specification_json', 'specification'), ('blocker_json', 'blocker'), ('result_json', 'result')):
        result[key] = json.loads(result.pop(column)) if result.get(column) else None
    return result

def get(dependency_id: str, *, reconcile: bool=True, validate: bool=True) -> dict | None:
    """Project readiness from current evidence; readers can defer compilation.

    Status projections use ``validate=False``: filesystem freshness remains
    authoritative, but rendering a status must not rebuild the project library.
    Explicit dependency coordination retains validation of changed modules.
    """
    with connect() as conn:
        row = conn.execute('select * from project_dependencies where id=?', (dependency_id,)).fetchone()
        if not row:
            return None
        attempts = [dict(r) for r in conn.execute('select a.*,r.session_id from project_dependency_attempts a\n            left join runs r on r.id=a.run_id where a.dependency_id=? order by generation', (dependency_id,))]
        consumers = [r[0] for r in conn.execute('select requester_id from project_dependency_edges where dependency_id=? order by requester_id', (dependency_id,))]
    result = _decode(row)
    result.update(attempts=attempts, requesting_targets=consumers)
    observation=core.fact('p:'+result['project_id'],'dependency',dependency_id)
    if observation:
        result.update(observation)
    else:
        result['ready']=False
        if result['state']=='ready':result['state']='stale'
    return result

def admit_worker(conn, dependency_id: str, project_id: str, run_id: str, resumes_run_id: str | None=None, initiating_run_id: str | None=None):
    if initiating_run_id:
        initiator = conn.execute('select project_id from runs where id=?', (initiating_run_id,)).fetchone()
        if not initiator or initiator[0] != project_id:
            raise ValueError('The initiating run must belong to this project.')
    dep = conn.execute('select * from project_dependencies where id=? and project_id=?', (dependency_id, project_id)).fetchone()
    continuation = bool(dep and resumes_run_id and (dep['state'] == 'waiting') and (dep['owner_run_id'] == resumes_run_id))
    if dep and resumes_run_id and (not continuation) and (dep['state'] in {'interrupted', 'failed'}) and (not dep['owner_run_id']):
        prior = conn.execute('select r.status,r.recoverable from project_dependency_attempts a\n            join runs r on r.id=a.run_id where a.dependency_id=? and a.generation=? and a.run_id=?', (dependency_id, dep['generation'], resumes_run_id)).fetchone()
        continuation = bool(prior and prior['recoverable'] and (prior['status'] in {'cancelled', 'max_turns'}))
    if not dep or (not continuation and (dep['state'] not in {'pending', 'interrupted'} or dep['owner_run_id'])):
        raise ValueError('Dependency is absent, blocked, ready, or already owned by another attempt.')
    conn.execute("update project_dependencies set state='running',owner_run_id=?,updated_at=? where id=?", (run_id, utc_now(), dependency_id))
    conn.execute("update project_dependency_attempts set state='running',run_id=?,initiating_run_id=coalesce(initiating_run_id,?),updated_at=? where dependency_id=? and generation=?", (run_id, initiating_run_id, utc_now(), dependency_id, dep['generation']))
