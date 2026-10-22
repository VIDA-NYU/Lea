"""Busy inventory observation cannot starve runtime publication."""
import inspect
import time
from datetime import datetime, timedelta, timezone
from threading import Event

from app import db, observation_maintenance, peripheral_views, store, target_observations


def register_runtime(monkeypatch, captured):
    def runtime(session_id):
        captured.append(session_id)
        return {'state': 'ready'}
    monkeypatch.setattr(peripheral_views, '_queries', {
        'lean_check_runtime': (runtime, inspect.signature(runtime), True, 120),
    })


def test_active_runtime_is_enrolled_even_beyond_the_history_page(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'state.sqlite3')
    db.init_db()
    for i in range(105):
        store.create_session(f'history {i}')
    active = store.create_session('active')
    store.create_run(active['id'], 'test', None, 3)
    captured = []
    register_runtime(monkeypatch, captured)
    assert peripheral_views.maintain(limit=1, kinds={'lean_check_runtime'}) == 1
    assert captured == [active['id']]


def test_runtime_renews_while_broad_observation_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'state.sqlite3')
    db.init_db()
    session = store.create_session('active')
    captured = []
    register_runtime(monkeypatch, captured)
    blocked, release = Event(), Event()
    def slow_sweep():
        blocked.set()
        release.wait(5)
        return {}
    monkeypatch.setattr(observation_maintenance, 'sweep', slow_sweep)
    monkeypatch.setattr(target_observations, 'maintain', lambda: {'state': 'idle'})
    observation_maintenance.start()
    try:
        assert blocked.wait(1)
        deadline = time.monotonic() + 4
        while len(captured) < 2 and time.monotonic() < deadline:
            time.sleep(.02)
        assert len(captured) >= 2
        observed = peripheral_views.get('lean_check_runtime', {'session_id': session['id']})
        assert observed['state'] == 'ready'
        assert observed['projection']['state'] == 'observed'
    finally:
        release.set()
        observation_maintenance.close()


def test_older_project_graph_is_not_starved_by_global_catalogs(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'state.sqlite3')
    db.init_db()
    project = store.create_project('graph')
    captured = []
    def catalog():
        captured.append('catalog')
        return {}
    def graph(project_id):
        captured.append(project_id)
        return {}
    monkeypatch.setattr(peripheral_views, '_queries', {
        **{f'catalog-{i}': (catalog, inspect.signature(catalog), True, 120) for i in range(3)},
        'graph': (graph, inspect.signature(graph), True, 120),
    })
    for kind in peripheral_views._queries:
        peripheral_views.capture(kind, {'project_id': project['id']} if kind == 'graph' else {})
    now = datetime.now(timezone.utc)
    with db.write() as conn:
        conn.execute('update peripheral_views set observed_at=?', ((now - timedelta(seconds=25)).isoformat(),))
        conn.execute("update peripheral_views set observed_at=? where kind='graph'",
                     ((now - timedelta(seconds=60)).isoformat(),))
    captured.clear()
    assert peripheral_views.maintain() == 3
    assert captured[0] == project['id']
