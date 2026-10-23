import assert from 'node:assert/strict';
import test from 'node:test';
import { awaitTargetObservation } from '../companion/targetObservationWait.mjs';

test('publication waits survive more than the former 60-second timeout and temporary outages', async () => {
  let elapsed = 0, waits = 0;
  const current = { validity_status: 'proved', projection: { state: 'observed' } };
  const result = await awaitTargetObservation({
    read: async () => elapsed >= 180000 ? current : waits % 3 === 0 ? null
      : { projection: { state: 'inputs_changed' } },
    requireProjection: true,
    onWait: async () => { waits++; },
    wait: async ms => { elapsed += ms; }
  });
  assert.equal(elapsed, 180000);
  assert.equal(result.evidence, current);
  assert.equal(result.cancelled, false);
});

test('an unavailable read after a pending projection keeps waiting', async () => {
  const states = [{ projection: { state: 'capturing' } }, null, { projection: { state: 'observed' } }];
  let waits = 0;
  const result = await awaitTargetObservation({ read: async () => states.shift(),
    wait: async () => { waits++; } });
  assert.equal(waits, 2);
  assert.equal(result.evidence.projection.state, 'observed');
});

test('a current mathematical review verdict is returned without being promoted', async () => {
  const current = { validity_status: 'needs_review', projection: { state: 'observed' } };
  const result = await awaitTargetObservation({ read: async () => current,
    wait: async () => assert.fail('A current review result must settle') });
  assert.equal(result.evidence, current);
});

test('publication waits remain cancellable and respect worker ownership', async () => {
  const result = await awaitTargetObservation({ requireProjection: true, read: async () => null,
    shouldCancel: () => true, wait: async () => assert.fail('Cancellation must stop waiting') });
  assert.equal(result.cancelled, true);
  await assert.rejects(awaitTargetObservation({ read: async () => { throw new Error('worker fenced'); } }), /worker fenced/);
});

test('SQL contention reading cancellation never downgrades current proof evidence',async()=>{
  let checks=0,waits=0,reads=0;
  const current={validity_status:'proved',projection:{state:'observed'}};
  const result=await awaitTargetObservation({read:async()=>++reads===1?{projection:{state:'capturing'}}:current,
    shouldCancel:()=>{if(++checks===1)throw {code:'ERR_SQLITE_ERROR',errcode:5};return false;},
    wait:async()=>waits++});
  assert.equal(result.evidence,current);assert.equal(waits,1);
  await assert.rejects(awaitTargetObservation({read:async()=>({projection:{state:'capturing'}}),
    shouldCancel:()=>{throw Object.assign(new Error('invalid SQL'),{code:'ERR_SQLITE_ERROR',errcode:1});}
  }),/invalid SQL/);
});

test('an already current proof wins a cancellation race',async()=>{
  const current={validity_status:'proved',projection:{state:'observed'}};
  const result=await awaitTargetObservation({read:async()=>current,
    shouldCancel:()=>assert.fail('Current evidence already settled before cancellation')});
  assert.equal(result.evidence,current);assert.equal(result.cancelled,false);
});
