"""Idle observation renewal keeps SQL readers independent of worker publication."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
from threading import Event, Thread

import pytest

from app import bootstrap, commands, core_observer, db, observation, process_state, store, target_observations, workspace_observer
from app.queries import proofs
from test_core_projections import fixture, finish


def test_supervised_bootstrap_enables_persistent_wal(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path/'data/db.sqlite3')
    config = tmp_path/'config.toml'
    config.write_text('lea_root=' + json.dumps(str(tmp_path/'prover')) + '\n')
    monkeypatch.setenv('LEA_CONFIG_FILE', str(config))
    bootstrap.migrate()
    bootstrap.require_schema()
    with sqlite3.connect(db.DB_PATH) as conn:
        assert conn.execute('pragma journal_mode').fetchone()[0] == 'wal'
        assert conn.execute('pragma synchronous').fetchone()[0] == 2
    with db.read_snapshot(), pytest.raises(RuntimeError, match='application transaction'):
        db.configure_concurrency()


def test_idle_reader_does_not_block_heartbeat_or_private_worker_lookup(fixture):
    x = fixture
    db.configure_concurrency()
    process_state.record_start('idle-runtime', 1)
    epoch = commands.start_worker('lea-command-host')
    errors, done = [], Event()
    def publish():
        try:
            for _ in range(3):
                process_state.publish('idle-runtime', {'lifecycle_state': 'ready'}, {})
                assert commands.heartbeat_execution('lea-command-host', epoch)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()
    with db.read_snapshot() as reader:
        reader.execute('select * from formalizations').fetchall()
        thread = Thread(target=publish)
        thread.start()
        try:
            assert done.wait(3), 'An idle read snapshot blocked heartbeat publication'
            assert not errors, errors
            module = Path(__file__).resolve().parents[3]/'overleaf-extension/companion/databaseState.mjs'
            # Both real SQLite clients share one file; no runtime callback or
            # in-memory readiness substitute is involved in this observation.
            script = '''import { openWorkerDatabase } from %s;
const db=openWorkerDatabase({filename:process.argv[1],owner:'lea-command-host',epoch:process.argv[2]});
try { db.fence(); console.log('observed'); } finally { db.close(); }''' % json.dumps(module.as_uri())
            result = subprocess.run([shutil.which('node') or 'node', '--input-type=module', '-e', script, str(db.DB_PATH), epoch],
                capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == 'observed'
        finally:
            thread.join(timeout=5)
    assert not thread.is_alive()


def test_unchanged_idle_targets_and_core_renew_without_rebuilding_or_notifications(fixture, monkeypatch):
    x = fixture
    finish(x)
    assert target_observations.capture(x['fid'])['state'] == 'observed'
    with db.write() as conn:
        old = (datetime.now(timezone.utc)-timedelta(seconds=31)).isoformat()
        conn.execute('update core_target_observations set observed_at=?', (old,))
        before_target = dict(conn.execute('select * from core_target_observations where formalization_id=?', (x['fid'],)).fetchone())
        before_core = dict(conn.execute('select * from core_observations where scope=?', (x['scope'],)).fetchone())
        counts = tuple(conn.execute('select (select count(*) from core_observations),(select count(*) from core_projection_values)').fetchone())
        watermark = observation.watermark(conn)
    def forbidden(*args, **kwargs):
        pytest.fail('Unchanged idle facts were recaptured instead of renewed')
    monkeypatch.setattr(core_observer, '_capture', forbidden)
    monkeypatch.setattr(target_observations, 'capture', forbidden)
    assert core_observer.capture(x['scope'], renew=True) == {'state': 'observed', 'observation_id': before_core['id'], 'processed': 0}
    assert target_observations.maintain()['renewed']
    with db.connect() as conn:
        after = dict(conn.execute('select * from core_target_observations where formalization_id=?', (x['fid'],)).fetchone())
        assert tuple(conn.execute('select (select count(*) from core_observations),(select count(*) from core_projection_values)').fetchone()) == counts
    assert after['observation_id'] == before_target['observation_id']
    for column in ('payload_json', 'queries_json', 'inputs_json'):
        assert after[column] == before_target[column]
    assert after['observed_at'] > old
    assert datetime.fromisoformat(after['expires_at']) <= datetime.now(timezone.utc)+timedelta(seconds=120)
    assert not observation.changes(watermark, project_id=x['project']['id'])['changes']
    assert proofs.get(x['fid'])['check_current']


def test_target_renewal_rechecks_concurrent_sql_changes(fixture, monkeypatch):
    x = fixture
    target_observations.capture(x['fid'])
    old = (datetime.now(timezone.utc)-timedelta(seconds=31)).isoformat()
    with db.write() as conn:
        conn.execute('update core_target_observations set observed_at=?', (old,))
    from app.read_projections import ReadSnapshot
    original = ReadSnapshot.unchanged
    changed = False
    def race(snapshot):
        nonlocal changed
        if not changed and db._write_connection.get() is None:
            changed = True
            store.update_formalization(x['fid'], statement='Changed during idle renewal')
        return original(snapshot)
    monkeypatch.setattr(ReadSnapshot, 'unchanged', race)
    assert target_observations.maintain()['state'] == 'retry'
    with db.connect() as conn:
        assert conn.execute('select observed_at from core_target_observations').fetchone()[0] == old
    assert not proofs.get(x['fid'])['check_current']


def test_scan_and_freshness_metadata_are_quiet_but_retractions_notify(fixture):
    x = fixture
    finish(x)
    workspace_observer.scan(x['project']['id'], x['path'].parent)
    with db.connect() as conn:
        watermark = observation.watermark(conn)
    with db.write() as conn:
        conn.execute('update core_projection_enrollment set last_scan_at=?', (db.utc_now(),))
        conn.execute('update projection_enrollment set last_scan_at=?', (db.utc_now(),))
    workspace_observer.scan(x['project']['id'], x['path'].parent)
    core_observer.capture(x['scope'], renew=True)
    assert not observation.changes(watermark, project_id=x['project']['id'])['changes']
    with db.write() as conn:
        conn.execute("update core_observations set expires_at='2000-01-01T00:00:00+00:00'")
        conn.execute("update workspace_observations set expires_at='2000-01-01T00:00:00+00:00'")
        conn.execute('update core_projection_enrollment set enabled=0')
        conn.execute('update projection_enrollment set enabled=0')
    kinds = {row['entity_type'] for row in observation.changes(watermark, project_id=x['project']['id'])['changes']}
    assert {'core_observations', 'workspace_observations', 'core_projection_enrollment', 'projection_enrollment'} <= kinds


def test_backup_includes_committed_proof_bytes_still_in_wal(fixture):
    from app import backup
    x = fixture
    db.configure_concurrency()
    with db.read_snapshot() as reader:
        before = reader.execute('select count(*) from timeline').fetchone()[0]
        errors = []
        def publish():
            try:
                store.add_code_step(x['session']['id'], x['run']['id'], 'Proof.lean',
                    content='-- committed in WAL\ntheorem result : True := by trivial', formalization_id=x['fid'])
            except BaseException as exc:
                errors.append(exc)
        thread = Thread(target=publish)
        thread.start(); thread.join(timeout=5)
        assert not thread.is_alive() and not errors
        assert reader.execute('select count(*) from timeline').fetchone()[0] == before
        assert Path(str(db.DB_PATH)+'-wal').exists()
        path = backup.snapshot('wal-history')
        with sqlite3.connect(path) as conn:
            assert conn.execute('select count(*) from timeline').fetchone()[0] == before + 1
            assert conn.execute("select content from artifact_blobs where content like '-- committed in WAL%'").fetchone()
            assert conn.execute('pragma integrity_check').fetchone()[0] == 'ok'


def test_renewal_trigger_migration_round_trip_preserves_history(fixture):
    from alembic import command
    from app import migrations
    x = fixture
    finish(x)
    with db.connect() as conn:
        blobs = [tuple(r) for r in conn.execute('select id,sha256,content from artifact_blobs order by id')]
        timeline = [tuple(r) for r in conn.execute('select id,after_blob_id from timeline order by id')]
    command.downgrade(migrations._config(), '0050_target_observations')
    with db.write() as conn:
        previous = observation.watermark(conn)
        conn.execute('update core_projection_enrollment set last_scan_at=?', (db.utc_now(),))
    assert observation.changes(previous, project_id=x['project']['id'])['changes']
    command.upgrade(migrations._config(), 'head')
    bootstrap.require_schema()
    with db.write() as conn:
        previous = observation.watermark(conn)
        conn.execute('update core_projection_enrollment set last_scan_at=?', (db.utc_now(),))
    assert not observation.changes(previous, project_id=x['project']['id'])['changes']
    with db.connect() as conn:
        assert [tuple(r) for r in conn.execute('select id,sha256,content from artifact_blobs order by id')] == blobs
        assert [tuple(r) for r in conn.execute('select id,after_blob_id from timeline order by id')] == timeline
