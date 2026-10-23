import test from 'node:test';
import assert from 'node:assert/strict';
import {workspaceResult,submitWithRecovery} from '../../../../packages/lea-workspace-client/index.mjs';

test('workspace observation waits for durable result and owns no cancellation handle',async()=>{
  const states=['claimed','succeeded'];const reads=[];
  const value=await workspaceResult({workspace_command:true,command:{id:'save',state:'queued'}},async id=>{
    reads.push(id);return {workspace_command:true,command:{id,state:states.shift(),result:{draft_id:'draft',saved:true}}};
  },{wait:async()=>{}});
  assert.equal(value.draft_id,'draft');assert.deepEqual(reads,['save','save']);
});
test('a retained save conflict carries current revision and receipt identity',async()=>{
  await assert.rejects(workspaceResult({workspace_command:true,command:{id:'save',state:'failed',error_status:409,
    result:{detail:{code:'revision_conflict',message:'Refresh current database content.',current_revision:'current'}}}},()=>{}),
    e=>e.status===409&&e.commandId==='save'&&e.detail.current_revision==='current');
});
test('a lost submit response is recovered by the original idempotency key',async()=>{
  let sends=0;const receipt={command:{id:'original'}};
  assert.equal(await submitWithRecovery(async()=>{sends++;throw new Error('connection lost');},async()=>receipt),receipt);
  assert.equal(sends,1);
});
test('a rejected admission is never retried',async()=>{
  await assert.rejects(submitWithRecovery(async()=>{throw Object.assign(new Error('full'),{status:429});},()=>assert.fail()),/full/);
});
test('receipt observation survives a transient API restart without resubmission',async()=>{
  let reads=0;
  const result=await workspaceResult({workspace_command:true,command:{id:'same',state:'claimed'}},async id=>{
    assert.equal(id,'same');reads++;
    if(reads===1)throw Object.assign(new Error('restarting'),{status:503});
    return {workspace_command:true,command:{id,state:'succeeded',result:{saved:true}}};
  },{wait:async()=>{}});
  assert.equal(reads,2);assert.equal(result.saved,true);
});

test('strict database boundary negotiation rejects missing and older services',async()=>{
  const {assertDatabaseBoundary}=await import('../../../../packages/lea-workspace-client/index.mjs');
  const boundary={version:1,ui_access:'database',command_direction:'ui_to_worker',processes:'separate',queries:1,changes:1,commands:1,source_publications:1,editor:1};
  assert.equal(assertDatabaseBoundary({capabilities:{database_boundary:boundary}}),boundary);
  for(const value of [undefined,{}, {...boundary,processes:'in-process'},{...boundary,editor:0},{...boundary,version:2}])
    assert.throws(()=>assertDatabaseBoundary({capabilities:{database_boundary:value}}),e=>e.code==='database_boundary_incompatible');
});
