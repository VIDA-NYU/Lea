import assert from 'node:assert/strict';
import test from 'node:test';
import { requestFailure, awaitAdmission, retrySqliteWrite, probeWorkerLease } from '../companion/workflowPolicy.mjs';
import { runApiProofJob } from '../companion/leaApiClient.mjs';
import { syncInventoryOnce } from '../companion/workerService.mjs';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

test('coordination codes share one policy and preserve permanent conflicts', () => {
  for (const [status, code] of [[409,'project_busy'],[503,'database_busy'],[409,'project_renaming'],[429,'capacity']]) {
    assert.equal(requestFailure({status,body:{detail:{code}}}).retry,true);
  }
  for (const code of ['command_fenced','worker_fenced','workspace_reconciliation_required','revision_conflict']) {
    assert.equal(requestFailure({code}).retry,false);
  }
  assert.equal(requestFailure({status:403}).retry,false);
});

test('admission retains identity across contention, transport loss and cancellation',async()=>{
  const request={idempotency_key:'one',source_context_ref:'immutable'};const seen=[],pending=[];
  const results=[{ok:false,status:409,body:{detail:{error:'project_busy'}}},
    {ok:false,error:'response lost'}, {ok:true,body:{run_id:'one-run'}}];
  const result=await awaitAdmission(async()=>{seen.push(JSON.stringify(request));return results.shift();},
    {onWait:async failure=>pending.push(failure),wait:async()=>{}});
  assert.equal(result.body.run_id,'one-run');assert.equal(new Set(seen).size,1);
  assert.equal(pending.length,2);
  const cancelled=await awaitAdmission(()=>assert.fail('cancelled admission must not execute'),{shouldCancel:()=>true});
  assert.equal(cancelled.stopReason,'user_stop');
});

test('initial run admission retries HTTP busy without becoming a solver failure',async()=>{
  const posted=[];let waits=0;
  const result=await runApiProofJob({baseUrl:'http://fixture',message:'Prove',idempotencyKey:'stable',
    busyRetryDelayMs:1,onAdmissionWait:async()=>waits++,timeoutMs:1000,
    fetchImpl:async(url,options)=>{
      if(url.endsWith('/api/runs')){
        posted.push(options.body);
        return new Response(JSON.stringify(posted.length===1?{detail:{error:'project_busy'}}:{run_id:'run',session_id:'s'}),{status:posted.length===1?503:200});
      }
      if(url.includes('/events'))return new Response('event: done\ndata: {"status":"proved"}\n\n');
      return new Response(JSON.stringify({id:'run',status:'proved',result_kind:'proved',usage_status:'final'}));
    }});
  assert.equal(result.ok,true);assert.equal(waits,1);assert.equal(posted[0],posted[1]);
});

test('unchanged inventory coalesces concurrent writes and survives worker restart',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'lea-inventory-'));
  try {
    let writes=0;
    const request={baseUrl:'http://fixture',slug:'project',targets:[{label:'one',source_hash:'same'}],
      fetchImpl:async()=>{writes++;await new Promise(resolve=>setTimeout(resolve,10));return new Response('{}');}};
    const state={jobsPath:path.join(dir,'jobs.json')};const identity={projectId:'p',namespace:'Lea.P',namespaceRevision:1};
    await Promise.all(Array.from({length:10},()=>syncInventoryOnce(state,identity,request)));
    assert.equal(writes,1);
    await syncInventoryOnce({jobsPath:state.jobsPath},identity,request);assert.equal(writes,1);
    await syncInventoryOnce(state,{...identity,projectId:'new-project'},request);assert.equal(writes,2);
    await syncInventoryOnce(state,identity,{...request,targets:[{label:'one',source_hash:'edited'}]});assert.equal(writes,3);
  } finally {await fs.rm(dir,{recursive:true,force:true});}
});

test('SQL publication retries only contention and rechecks ownership before retry',async()=>{
  let attempts=0,fences=0;
  assert.equal(await retrySqliteWrite(()=>{if(++attempts<3)throw {errcode:5};return 'saved';},()=>fences++,async()=>{}),'saved');
  assert.equal(attempts,3);assert.equal(fences,3);
  let owned=true;
  await assert.rejects(retrySqliteWrite(()=>{throw {errcode:6};},()=>{if(!owned)throw new Error('fenced');},async()=>{owned=false;}),/fenced/);
  await assert.rejects(retrySqliteWrite(()=>{throw new Error('invalid schema');},()=>{},async()=>assert.fail('not contention')),/invalid schema/);
});

test('worker lifetime survives only typed database contention and otherwise stops',()=>{
  let lost=0;const onLost=()=>lost++;
  assert.equal(probeWorkerLease(()=>{},onLost),true);
  assert.equal(probeWorkerLease(()=>{throw {code:'database_busy'};},onLost),false);
  assert.equal(lost,0);
  for(const code of ['worker_fenced','corrupt_database']) {
    assert.equal(probeWorkerLease(()=>{throw {code};},onLost),false);
  }
  assert.equal(lost,2);
});

test('busy cancellation reads wait before admission, while real SQL faults propagate',async()=>{
  let checks=0,requests=0,waits=0;
  const result=await awaitAdmission(async()=>{requests++;return {ok:true};},{
    shouldCancel:()=>{if(++checks===1)throw {code:'ERR_SQLITE_ERROR',errcode:5};return false;},
    wait:async()=>waits++});
  assert.equal(result.ok,true);assert.equal(requests,1);assert.equal(waits,1);
  await assert.rejects(awaitAdmission(()=>assert.fail('no admission'),{
    shouldCancel:()=>{throw Object.assign(new Error('invalid SQL'),{code:'ERR_SQLITE_ERROR',errcode:1});}
  }),/invalid SQL/);
});
