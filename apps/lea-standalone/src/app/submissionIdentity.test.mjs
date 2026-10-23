import assert from 'node:assert/strict';
import test from 'node:test';
import { createIntentRegistry } from './lib/submissionIdentity.mjs';
const storage = () => {
  const records = new Map();
  return { getItem:k=>records.get(k),setItem:(k,v)=>records.set(k,v),removeItem:k=>records.delete(k) };
};
test('concurrent starts deduplicate and a reload recovers the exact uncertain intent', async () => {
  const disk = storage(); let ids = 0, calls = 0, reject;
  const registry = createIntentRegistry('test',{storage:disk,makeId:()=>String(++ids)});
  const send = () => { calls++; return new Promise((_,no)=>{reject=no;}); };
  const one = registry.run({model:'x',message:'prove'},send);
  const two = registry.run({message:'prove',model:'x'},send);
  await Promise.resolve();assert.equal(calls,1);
  reject(new TypeError('lost response'));await Promise.allSettled([one,two]);
  const reloaded = createIntentRegistry('test',{storage:disk,makeId:()=>String(++ids)});
  const request = await reloaded.run({message:'prove',model:'x'},async p=>p);
  assert.equal(request.idempotency_key,'1');assert.equal(ids,1);
  await reloaded.run({message:'prove',model:'x'},async p=>assert.equal(p.idempotency_key,'2'));
});
test('409 and server failures retain identity; a definite rejection allows a corrected retry', async () => {
  let ids = 0;const r = createIntentRegistry('test',{storage:storage(),makeId:()=>String(++ids)});
  const payload={title:'Study'};
  for (const status of [409,503]) {
    await assert.rejects(r.run(payload,async()=>{throw Object.assign(new Error('rejected'),{status});}));
    assert.equal(r.prepare(payload).id,'1');
  }
  await assert.rejects(r.run(payload,async()=>{throw Object.assign(new Error('invalid'),{status:422});}));
  assert.equal(r.prepare(payload).id,'2');
});

test('failed persistence cannot send an unretained identity or rekey an acknowledged action',async()=>{
  const disk=storage();let blocked=true,calls=0;
  const saved=disk.setItem;
  disk.setItem=(...args)=>{if(blocked)throw new Error('storage full');saved(...args);};
  const r=createIntentRegistry('quota',{storage:disk,makeId:()=>String(++calls)});
  let sent=0;const send=async payload=>{sent++;return payload;};
  await assert.rejects(r.run({title:'Study'},send),/storage full/);
  await assert.rejects(r.run({title:'Study'},send),/storage full/);
  assert.equal(sent,0);
  blocked=false;
  disk.removeItem=()=>{throw new Error('cannot clear storage');};
  await assert.rejects(r.run({title:'Study'},send),/cannot clear storage/);
  assert.equal(r.prepare({title:'Study'}).id,'3');
  await assert.rejects(r.run({title:'Study'},async p=>{assert.equal(p.idempotency_key,'3');return p;}));
});
