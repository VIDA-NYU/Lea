"""Contention must not restart a live worker; failures must survive its exit."""
import importlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest
from app import db, process_state, runtime, worker_diagnostics


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'data/state.sqlite3')
    db.init_db()
    monkeypatch.setattr(worker_diagnostics.faulthandler, 'enable', lambda *a, **k:None)
    monkeypatch.setattr(worker_diagnostics.faulthandler, 'disable', lambda:None)
    yield tmp_path
    worker_diagnostics.close()


def test_real_sqlite_contention_keeps_runtime_and_can_publish_after_release(isolated, monkeypatch):
    entry = importlib.import_module('run_worker')
    monkeypatch.setenv('LEA_PROCESS_ROLE', 'test')
    monkeypatch.setattr(db, 'BUSY_TIMEOUT_MS', 30)
    monkeypatch.setattr(runtime, 'IDENTITY', 'same-lifetime')
    process_state.record_start(runtime.IDENTITY, os.getpid())
    worker_diagnostics.setup(runtime.IDENTITY)
    with db.read_snapshot() as reader:
        reader.execute('select * from worker_process').fetchall()
        # Publish from another thread/context, so it really contends with the
        # long reader instead of sharing that read-only connection.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(entry.publish_status, {}).result(timeout=2) is False
    assert entry.publish_status({}) is True
    with db.connect() as conn:
        row = conn.execute('select * from worker_process').fetchone()
    assert row['runtime_id'] == 'same-lifetime' and row['generation'] == 1
    assert any(e['event'] == 'worker.status_write_delayed' for e in json.loads(row['snapshot_json'])['diagnostics']['events'])


def test_obsolete_lifetime_and_non_contention_fail_closed(isolated, monkeypatch):
    entry = importlib.import_module('run_worker')
    process_state.record_start('replacement', 1)
    monkeypatch.setattr(runtime, 'IDENTITY', 'old')
    with pytest.raises(RuntimeError, match='fenced'):
        entry.publish_status({})
    def damaged(*_): raise sqlite3.OperationalError('disk I/O error')
    monkeypatch.setattr(process_state, 'publish', damaged)
    with pytest.raises(sqlite3.OperationalError, match='disk I/O'):
        entry.publish_status({})


def test_diagnostics_survive_lifetimes_and_redact_credentials(isolated, monkeypatch):
    secret = 'sk-test-secret-value-1234567890'
    monkeypatch.setenv('OPENAI_API_KEY', secret)
    worker_diagnostics.setup('first')
    try: raise ValueError('retained exception ' + secret)
    except ValueError as exc: worker_diagnostics.exception('worker.exception', exc)
    worker_diagnostics.event('worker.signal', signal='SIGTERM')
    worker_diagnostics.close()
    worker_diagnostics.setup('second')
    events = worker_diagnostics.snapshot()['events']
    assert any(e['event'] == 'worker.exception' and e['runtime_id'] == 'first' for e in events)
    assert any(e['event'] == 'worker.signal' for e in events)
    directory = db.DB_PATH.parent / 'worker-diagnostics'
    persisted = '\n'.join(p.read_text() for p in directory.iterdir() if p.is_file())
    assert secret not in persisted and '[redacted]' in persisted
    assert 'ValueError' in persisted and 'retained exception' in persisted
    assert (directory / 'worker.log').stat().st_mode & 0o077 == 0


def test_fatal_worker_error_is_persisted_in_a_real_process(isolated):
    adapter = Path(__file__).resolve().parents[1]
    script = '''
from app import worker,process_state
import run_worker
worker.startup=lambda:None
worker.shutdown_lean_checks=lambda:None
worker.health=lambda:{'capabilities':{}}
def fail(*args):raise ValueError('qualification fatal publication')
process_state.publish=fail
try:run_worker.main()
except ValueError:pass
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=adapter,
        env={**os.environ, 'LEA_DATABASE_FILE':str(db.DB_PATH)}, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    log = (db.DB_PATH.parent / 'worker-diagnostics/worker.log').read_text()
    assert 'qualification fatal publication' in log and 'Traceback' in log
    assert 'worker.exiting' in (db.DB_PATH.parent / 'worker-diagnostics/worker-events.jsonl').read_text()


def test_execution_heartbeat_renews_live_claims_without_reviving_expired_owners(isolated, monkeypatch):
    from app import commands, store
    p = store.create_project('heartbeat')
    s = store.create_session('Heartbeat', project_id=p['id'])
    credential = commands.register_producer('fixture', project_id=p['id'])
    commands.admit(credential, {'kind':'synthetic.echo','idempotency_key':'one',
        'session_id':s['id'],'payload':{'text':'hello'}})
    epoch = commands.start_worker('worker')
    token = commands.claim('worker', epoch, 'compute')
    with db.write() as conn:
        conn.execute("update commands set kind='run.execute' where id=?", (token['id'],))
    future = datetime.now(timezone.utc) + timedelta(seconds=3)
    monkeypatch.setattr(commands, '_now', lambda:future)
    assert commands.heartbeat_execution('worker', epoch)
    with db.connect() as conn:
        renewed = dict(conn.execute('select * from commands where id=?',(token['id'],)).fetchone())
    assert renewed['lease_until'] > token['lease_until']
    expired = (future - timedelta(seconds=1)).isoformat()
    with db.write() as conn:
        conn.execute('update commands set lease_until=? where id=?',(expired, token['id']))
    assert commands.heartbeat_execution('worker', epoch)
    with db.connect() as conn:
        assert conn.execute('select lease_until from commands where id=?',(token['id'],)).fetchone()[0] == expired
    with db.write() as conn:
        conn.execute('update command_workers set lease_until=? where id=?',(expired, 'worker'))
    assert commands.heartbeat_execution('worker', epoch) is False


def test_stop_gate_retries_only_contention_and_rechecks_fencing(monkeypatch):
    from app import bridge
    busy = sqlite3.OperationalError('database is locked')
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    calls = []
    def poll():
        calls.append(1)
        if len(calls) == 1: raise busy
        return True  # Ownership was lost while waiting: stop, never resume work.
    assert bridge._poll_stop_with_contention(poll, Event()) is True
    assert len(calls) == 2
    calls.clear()
    def damaged(): raise sqlite3.OperationalError('disk I/O error')
    with pytest.raises(sqlite3.OperationalError, match='disk I/O'):
        bridge._poll_stop_with_contention(damaged, Event())
    stop = Event(); stop.set()
    assert bridge._poll_stop_with_contention(damaged, stop) is True


def test_stop_gate_bounds_outage_without_allowing_another_effect(monkeypatch):
    from app import bridge
    busy = sqlite3.OperationalError('database is locked')
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    stamps = iter([0, 11])
    monkeypatch.setattr(bridge.time, 'monotonic', lambda:next(stamps))
    def blocked(): raise busy
    with pytest.raises(bridge._StopObservationUnavailable):
        bridge._poll_stop_with_contention(blocked, Event())


def test_stop_status_reads_current_stop_and_fencing_without_decoding_run_history(isolated,monkeypatch):
    from app import bridge,commands,store
    session=store.create_session('Stop gate')
    run=store.create_run(session['id'],'offline',None,1)
    credential=commands.register_producer('fixture')
    commands.admit(credential,{'kind':'synthetic.echo','session_id':session['id'],
        'idempotency_key':'stop-gate','payload':{'text':'hello'}})
    epoch=commands.start_worker('stop-fixture')
    token=commands.claim('stop-fixture',epoch,'compute')
    monkeypatch.setattr(store,'get_run_status',lambda *_:pytest.fail('Stop gate decoded the full status/history row'))
    row,owned=bridge._run_stop_status(run['id'],token)
    assert owned and row=={'deadline_at':None,'stop_requested_reason':None}
    with db.write() as conn:
        conn.execute("update runs set stop_requested_reason='user_stop' where id=?",(run['id'],))
        conn.execute('update command_producers set enabled=0 where id=?',(token['producer_id'],))
    row,owned=bridge._run_stop_status(run['id'],token)
    assert not owned and row['stop_requested_reason']=='user_stop'
