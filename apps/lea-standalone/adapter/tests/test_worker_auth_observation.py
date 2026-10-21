"""A delayed runtime observation is availability, not an invalid credential."""
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from threading import Thread

from fastapi.testclient import TestClient
from app import db, commands, process_state
from test_core_projections import fixture


def test_private_auth_distinguishes_credentials_ownership_and_runtime_observation(fixture):
    from app.main import app
    epoch=commands.start_worker('lea-command-host')
    process_state.record_start('auth-runtime',os.getpid())
    process_state.publish('auth-runtime',{'lifecycle_state':'ready'}, {'database_boundary':process_state.BOUNDARY})
    proof='fixture-private-proof'
    with db.write() as conn:conn.execute('update worker_process set bridge_hash=?',(commands._hash(proof),))
    headers={'X-Lea-Worker-Epoch':epoch,'X-Lea-Worker-Proof':proof}
    client=TestClient(app,base_url='http://127.0.0.1')
    assert client.get('/api/health',headers=headers).status_code==200
    with db.write() as conn:conn.execute("update worker_process set expires_at='2000-01-01T00:00:00+00:00'")
    delayed=client.get('/api/health',headers=headers)
    assert delayed.status_code==503 and delayed.json()['detail']['error']=='worker_unavailable'
    assert delayed.headers['retry-after']=='1'
    denied=client.get('/api/health',headers={**headers,'X-Lea-Worker-Proof':'wrong'})
    assert denied.status_code==403 and denied.json()['detail']['error']=='worker_credential_required'
    with db.write() as conn:conn.execute("update command_workers set lease_until='2000-01-01T00:00:00+00:00'")
    fenced=client.get('/api/health',headers=headers)
    assert fenced.status_code==409 and fenced.json()['detail']['error']=='worker_fenced'
    assert client.get('/api/health').status_code==200  # Public SQL observations remain readable.
    client.close()


def test_heartbeat_lifetime_starts_after_waiting_for_the_writer(fixture,monkeypatch):
    process_state.record_start('queued-runtime',os.getpid())
    monkeypatch.setattr(db,'BUSY_TIMEOUT_MS',1)
    holder=sqlite3.connect(db.DB_PATH,isolation_level=None,check_same_thread=False)
    holder.execute('begin immediate');released=[]
    def release():time.sleep(.2);released.append(datetime.now(timezone.utc));holder.commit()
    thread=Thread(target=release);thread.start()
    try:
        process_state.publish('queued-runtime',{'lifecycle_state':'ready'},{})
        with db.connect() as conn:row=conn.execute('select heartbeat_at,expires_at from worker_process').fetchone()
        assert datetime.fromisoformat(row['heartbeat_at'])>=released[0]
        assert datetime.fromisoformat(row['expires_at'])-datetime.fromisoformat(row['heartbeat_at'])==timedelta(seconds=15)
    finally:thread.join(timeout=5);holder.close()
