"""Imported batch history cannot retry work without a valid accepted command."""
import json
from pathlib import Path
import subprocess

import pytest

from app import commands, companion_import, companion_service, db


OWNER = 'batch-recovery-test'
COMPANION = Path(__file__).resolve().parents[3] / 'overleaf-extension/companion'


@pytest.mark.parametrize('command_state', [
    'missing_id', 'missing_row', 'malformed_id', 'queued', 'failed', 'cancelled',
    'expired', 'old_epoch', 'query', 'claimed', 'succeeded', 'succeeded_old_epoch',
])
def test_imported_batch_recovery_requires_a_valid_command(tmp_path, monkeypatch, command_state):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.sqlite3')
    db.init_db()
    epoch = commands.start_worker(OWNER)
    command_id = None
    if command_state == 'missing_row':
        command_id = 'deleted-command'
    elif command_state == 'malformed_id':
        command_id = {'unexpected': 'id'}
    elif command_state != 'missing_id':
        command_id = companion_service.admit('/formalize/all', {'overleafProjectId': 'document', 'items': []}, 'batch')['command']['id']
        if command_state != 'queued':
            token = commands.claim(OWNER, epoch, 'compute', kinds={'companion.dispatch'})
            assert token['id'] == command_id
            if command_state in {'succeeded', 'succeeded_old_epoch', 'query'}:
                commands.complete(token, {'accepted': True})
            with db.write() as conn:
                if command_state in {'failed', 'cancelled'}:
                    conn.execute('update commands set state=? where id=?', (command_state, command_id))
                elif command_state == 'expired':
                    conn.execute("update commands set lease_until='2000-01-01' where id=?", (command_id,))
                elif command_state in {'old_epoch', 'succeeded_old_epoch'}:
                    conn.execute("update commands set worker_epoch='previous-worker' where id=?", (command_id,))
                elif command_state == 'query':
                    conn.execute("update commands set kind='companion.query' where id=?", (command_id,))

    batch = {'batchId': 'retained-batch', 'operation': 'formalize', 'overleafProjectId': 'document',
             'done': True, 'reportState': 'saving', 'createdAt': '2026-10-12T12:00:00Z',
             'items': [{'targetKind': 'theorem', 'targetLabel': 'old-target', 'state': 'failed'}]}
    if command_id:
        batch['acceptedCommandId'] = command_id
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    original = json.dumps({'retained-batch': batch})
    (legacy / 'jobs.json.batches.json').write_text(original)
    companion_import.import_directory(legacy)
    valid = command_state in {'claimed', 'succeeded', 'succeeded_old_epoch'}

    script = '''
      import fs from 'node:fs';import assert from 'node:assert/strict';
      const input=JSON.parse(fs.readFileSync(0,'utf8'));
      const {openWorkerDatabase}=await import(input.database);
      const worker=await import(input.worker);
      const database=openWorkerDatabase(input);worker.setWorkerDatabase(database);
      const calls=[],errors=[];const originalError=console.error;
      console.error=(...args)=>errors.push(args.map(String).join(' '));
      const state={settings:{leaApiBaseUrl:input.baseUrl},env:{},jobs:{},
        repairBatchesPath:'/__sql__/jobs.json.batches.json',repairBatches:database.get('jobs.json.batches.json',{}),
        fetchImpl:async(url,options)=>{calls.push({url,method:options?.method});
          return Response.json({batch_id:'retained-batch',state:'ready'});}};
      try {
        const batch=state.repairBatches['retained-batch'];
        assert.equal(database.canResumeBatch(batch),input.valid);
        await worker.reconcileRecoveredBatches(state);await batch.reportSubmission;
        const saved=database.get('jobs.json.batches.json',{});
        if(input.valid){assert.equal(batch.reportId,'retained-batch');assert.equal(calls.length,1);}
        else {
          assert.equal(batch.migrationBlocked,true);assert.equal(batch.pausedOn.reason,'migration_review');
          assert.equal(batch.reportState,'save_failed');assert.match(batch.reportError,/command.*missing|no longer valid/);
          assert.equal(calls.length,0);assert.equal(saved['retained-batch'].migrationBlocked,true);
        }
        // Repeat maintenance and reload the durable records as on the next
        // worker startup. Neither may retry or emit the bookkeeping warning.
        for(let i=0;i<3;i++)await worker.reconcileRecoveredBatches(state);
        state.repairBatches=saved;
        await worker.reconcileRecoveredBatches(state);
        assert.equal(calls.length,input.valid?1:0);assert.deepEqual(errors,[]);
        assert.deepEqual(database.get('jobs.json.batches.json',{}),saved);
        process.stdout.write(JSON.stringify({calls:calls.length,blocked:batch.migrationBlocked||false}));
      }finally{console.error=originalError;worker.setWorkerDatabase(null);database.close();}
    '''
    result = subprocess.run(['node', '--input-type=module', '-e', script],
        input=json.dumps({'database': (COMPANION / 'databaseState.mjs').as_uri(),
                         'worker': (COMPANION / 'workerService.mjs').as_uri(),
                         'filename': str(db.DB_PATH), 'owner': OWNER, 'epoch': epoch,
                         'baseUrl': 'http://127.0.0.1:1', 'leaRoot': str(tmp_path / 'prover'), 'valid': valid}),
        text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['blocked'] is not valid
    assert (legacy / 'jobs.json.batches.json').read_text() == original
    with db.connect() as conn:
        assert conn.execute('select count(*) from runs').fetchone()[0] == 0
        retained = json.loads(conn.execute("select payload from companion_documents where name='jobs.json.batches.json'").fetchone()[0])['retained-batch']
    assert retained['items'] == batch['items']
