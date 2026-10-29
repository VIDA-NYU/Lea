"""Responsiveness across the real SQL boundary, without a provider or compiler."""
import json
from pathlib import Path
import subprocess
from threading import Event, Thread

import pytest

from app import commands, companion_service, db, recovery, run_commands, store


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'responsiveness.sqlite3')
    db.init_db()
    session = store.create_session('Responsiveness fixture')
    run = store.create_run(session['id'], 'fixture', None, 3)
    store.claim_pending_run(run['id'])
    return session, run


def test_checkpoint_fingerprinting_leaves_writer_available_and_still_fences_owner(workspace):
    _, run = workspace
    attempt = recovery.start_attempt(run['id'])
    preparing, release, stopped = Event(), Event(), Event()
    errors = []

    def manifest():
        preparing.set()
        assert release.wait(5)
        return {'files': {'Target.lean': 'hash'}}

    def checkpoint():
        try:
            recovery.Recorder(run['id'], attempt, manifest)('checkpoint', {
                'stage': 'turn_boundary', 'messages': [{'role': 'user', 'content': 'Task'}],
                'turn': 1, 'cost_usd': 0, 'max_cost_usd': 1})
        except BaseException as exc:
            errors.append(exc)

    def cancel():
        try:
            run_commands.stop(run['id'])
            stopped.set()
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=checkpoint)
    writer = Thread(target=cancel)
    thread.start()
    try:
        assert preparing.wait(3)
        writer.start()
        assert stopped.wait(1), 'Checkpoint file hashing held the SQL writer and blocked Stop'
        assert store.get_run(run['id'])['stop_requested_reason'] == 'user_stop'
    finally:
        release.set()
        thread.join(5)
        if writer.ident is not None:
            writer.join(5)
    assert not errors
    assert recovery.load_checkpoint(run['id'])[1]['files'] == {'Target.lean': 'hash'}

    def supersede():
        with db.write() as conn:
            conn.execute("update runs set attempt_id='new-owner' where id=?", (run['id'],))
        return {'files': {}}

    with pytest.raises(recovery.CheckpointWriteError, match='Obsolete run attempt'):
        recovery.Recorder(run['id'], attempt, supersede)('checkpoint', {'turn': 2})
    assert recovery.load_checkpoint(run['id'])[0]['turn'] == 1


@pytest.mark.parametrize('shared_child', [None, False, True])
def test_batch_stop_records_run_intent_at_sql_admission_without_a_worker(workspace, shared_child):
    session, run = workspace
    payload = {'apiRunId': run['id'], 'status': 'in_progress', 'targetKind': 'theorem', 'targetLabel': 'target'}
    selected = run
    child = None
    if shared_child is not None:
        child = store.create_run(session['id'], 'fixture', None, 3)
        store.claim_pending_run(child['id'])
        payload.update(activeDependencyOwned=True, activeDependencyRunId=child['id'])
        if not shared_child:
            selected = child
    batch = {'overleafProjectId': 'document', 'items': [
        {'targetKind': 'theorem', 'targetLabel': 'target', 'state': 'running', 'runJobId': 'active-job'}]}
    with db.write() as conn:
        conn.execute("insert into companion_documents values('jobs.json.batches.json',?,1,?)",
                     (json.dumps({'batch': batch}), db.utc_now()))
        conn.execute('insert into companion_jobs values(?,?,?,?,?,?,?,?,?)',
                     ('active-job', 'document', 'document:theorem:target', None, session['id'], run['id'], None,
                      json.dumps(payload), db.utc_now()))
        if shared_child:
            conn.execute('insert into companion_jobs values(?,?,?,?,?,?,?,?,?)',
                         ('other-job', 'other-document', None, None, session['id'], None, None,
                          json.dumps({'status': 'in_progress', 'dependencies': [{'owner_run_id': child['id']}]}), db.utc_now()))
    first = companion_service.admit('/lean-pane/repair/all/cancel', {'batchId': 'batch'}, 'stop')
    assert first['command']['state'] == 'queued'
    assert store.get_run(selected['id'])['stop_requested_reason'] == 'user_stop'
    if child:
        untouched = child if shared_child else run
        assert store.get_run(untouched['id'])['stop_requested_reason'] is None
    assert companion_service.admit('/lean-pane/repair/all/cancel', {'batchId': 'batch'}, 'stop')['command']['id'] == first['command']['id']
    with db.connect() as conn:
        assert conn.execute("select count(*) from commands where kind='run.stop'").fetchone()[0] == 1
        saved = json.loads(conn.execute("select payload from companion_documents where name='jobs.json.batches.json'").fetchone()[0])
        assert saved['batch']['cancelRequested']


def test_worker_sql_contention_yields_to_event_loop(workspace):
    epoch = commands.start_worker('responsiveness')
    module = Path(__file__).resolve().parents[3] / 'overleaf-extension/companion/databaseState.mjs'
    script = '''
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { openWorkerDatabase } from %s;
const [filename, epoch] = process.argv.slice(1);
const holder = new DatabaseSync(filename);
const database = openWorkerDatabase({filename, owner:'responsiveness', epoch});
try {
  holder.exec('begin immediate');
  let unlocked = false;
  const start = performance.now();
  setTimeout(() => { holder.exec('commit'); unlocked = true; }, 50);
  await database.persist('responsiveness.json', {saved:true});
  assert.ok(unlocked);
  assert.ok(performance.now() - start < 300, 'SQLite blocked the business event loop');
  assert.deepEqual(database.get('responsiveness.json'), {saved:true});
} finally { database.close(); holder.close(); }
''' % json.dumps(module.as_uri())
    result = subprocess.run(['node', '--input-type=module', '-e', script, str(db.DB_PATH), epoch],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
