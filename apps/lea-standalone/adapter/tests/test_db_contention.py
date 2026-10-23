"""Real rollback-journal contention must not replay workflow/tool side effects."""
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from app import db, commands, store


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path/'contention.sqlite')
    db.init_db()
    project = store.create_project('contention', namespace='Lea.Contention')
    monkeypatch.setattr(db, 'BUSY_TIMEOUT_MS', 2)
    return project


@pytest.mark.parametrize('held', ['writer', 'reader'])
def test_transaction_waits_at_begin_or_commit_without_replaying_body(database, held):
    holder = sqlite3.connect(db.DB_PATH, isolation_level=None, check_same_thread=False)
    holder.execute('begin immediate' if held == 'writer' else 'begin')
    holder.execute('select * from projects').fetchall()  # actually take the read lock
    thread = threading.Thread(target=lambda: (time.sleep(.2), holder.commit()))
    thread.start()
    effects = []
    try:
        with db.write() as conn:
            effects.append('provider/tool invoked once')
            conn.execute('update projects set title=? where id=?', ('Completed', database['id']))
        assert effects == ['provider/tool invoked once']
        assert store.get_project(database['id'])['title'] == 'Completed'
    finally:
        thread.join(timeout=5)
        holder.close()


def test_waiting_writer_rechecks_worker_fence_before_body(database):
    epoch = commands.start_worker('lea-command-host')
    holder = sqlite3.connect(db.DB_PATH, isolation_level=None, check_same_thread=False)
    holder.create_function('lea_change_batch', 0, lambda: 'held-worker-fence')
    holder.execute('begin immediate')
    holder.execute('update command_workers set lease_until=?',
                   ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
    thread = threading.Thread(target=lambda: (time.sleep(.2), holder.commit()))
    thread.start()
    token = db.caller_worker_epoch.set(epoch)
    effects = []
    try:
        with pytest.raises(HTTPException, match='lost ownership'):
            with db.write():effects.append('must not execute')
        assert effects == []
    finally:
        db.caller_worker_epoch.reset(token)
        thread.join(timeout=5)
        holder.close()


def test_new_reader_waits_for_pending_commit_instead_of_failing_a_tool(database):
    holder = sqlite3.connect(db.DB_PATH, isolation_level=None, check_same_thread=False)
    holder.execute('begin exclusive')
    thread = threading.Thread(target=lambda: (time.sleep(.2), holder.commit()))
    thread.start()
    try:
        assert store.get_project(database['id'])['id'] == database['id']
    finally:
        thread.join(timeout=5)
        holder.close()


def test_independent_insert_waits_without_replaying_its_caller(database):
    holder = sqlite3.connect(db.DB_PATH, isolation_level=None, check_same_thread=False)
    holder.execute('begin immediate')
    thread = threading.Thread(target=lambda: (time.sleep(.2), holder.commit()))
    thread.start()
    try:
        session = store.create_session('Once', project_id=database['id'])
        assert [s['id'] for s in store.list_project_sessions(database['id'])] == [session['id']]
    finally:
        thread.join(timeout=5)
        holder.close()


def test_non_contention_failure_rolls_back_and_propagates(database):
    with pytest.raises(sqlite3.OperationalError, match='no such table'):
        with db.write() as conn:
            conn.execute('update projects set title=? where id=?', ('Uncommitted', database['id']))
            conn.execute('insert into missing_table values (1)')
    assert store.get_project(database['id'])['title'] != 'Uncommitted'
