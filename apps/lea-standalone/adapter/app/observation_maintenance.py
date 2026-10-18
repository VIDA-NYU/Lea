"""Worker-side bounded replay maintenance, independent of query traffic."""
import logging
from threading import Event, Thread
from . import db, observation

_stop = Event()
_worker = None


def sweep():
    result = observation.compact_run_events()
    from . import peripheral_views,core_observer
    # Stage 6 removes all read fallbacks. Enrollment is now worker maintenance,
    # including newly created scopes; it is never performed in an API query.
    with db.connect() as conn:
        scopes=[r[0] for r in conn.execute("select 'p:'||id from projects where not exists(select 1 from core_projection_enrollment e where e.scope='p:'||projects.id) union all select 's:'||id from sessions where project_id is null and not exists(select 1 from core_projection_enrollment e where e.scope='s:'||sessions.id) limit 10")]
    for scope in scopes:core_observer.enroll(scope)
    result['peripheral']=peripheral_views.maintain()
    from . import source_publications,companion_service
    result['source_retention']=source_publications.retain()
    result['companion_retention']=companion_service.retain()
    from . import editor_service,editor_worker
    result['editor_retention']=editor_service.retain()
    editor_worker.maintain()
    with db.connect() as conn:
        previous = conn.execute('select floor from change_retention where id=1').fetchone()[0]
    result['changes_compacted'] = observation.compact_changes() > previous
    from . import workspace_observer
    try:
        result['workspace'] = workspace_observer.maintain()
    except Exception:
        logging.getLogger(__name__).exception('Workspace observation failed')
    from . import core_observer
    try:
        result["core"] = core_observer.maintain()
    except Exception:
        logging.getLogger(__name__).exception("Core observation failed")
    return result


def start():
    global _worker
    if _worker and _worker.is_alive():
        return
    _stop.clear()
    def work():
        while not _stop.is_set():
            try:
                result = sweep()
            except Exception:
                logging.getLogger(__name__).exception('Observation maintenance failed')
                result = {}
            # Release the write lock between bounded batches, and drain a backlog
            # without a long DB transaction. Idle workers check once per minute.
            _stop.wait(1 if result.get('removed') or result.get('changes_compacted') or
                       result.get('workspace',{}).get('state') in {'capturing','retry'} or
                       result.get('core',{}).get('state') in {'capturing','retry'} else
                       1 if result.get('peripheral') or result.get('core') else 5)
    _worker = Thread(target=work, name='lea-observation-maintenance', daemon=True)
    _worker.start()


def close():
    global _worker
    _stop.set()
    if _worker:
        _worker.join(timeout=10)
        if _worker.is_alive():
            raise RuntimeError('Observation maintenance did not stop')
        _worker = None
