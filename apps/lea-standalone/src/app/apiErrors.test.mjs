import assert from 'node:assert/strict';
import test from 'node:test';
import {build} from 'esbuild';
import vm from 'node:vm';
const bundled=await build({entryPoints:[new URL('./lib/api.ts',import.meta.url).pathname],bundle:true,write:false,platform:'node',format:'cjs'});
function client(body,status=409) {
  const context={module:{exports:{}},exports:{},crypto:globalThis.crypto,fetch:async()=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}})};
  context.exports=context.module.exports;vm.runInNewContext(bundled.outputFiles[0].text,context);
  return context.module.exports;
}
test('a busy save retains the server message after revision-conflict inspection',async()=>{
  const api=client({detail:{error:'project_busy',message:'Wait for active project work.'}});
  await assert.rejects(api.writeSessionFile('session','Proof.lean','content',undefined,undefined,undefined,'loaded'),/Wait for active project work/);
});
test('a stale proof check explains refreshing the current revision',async()=>{
  const api=client({detail:{code:'proof_revision_changed',message:'Refresh the current revision and check again.'}});
  await assert.rejects(api.leanCheckSession('session','Proof.lean'),/Refresh the current revision/);
});
test('an optimistic save conflict still carries its revision metadata',async()=>{
  const api=client({detail:{code:'revision_conflict',message:'Changed in another conversation.',current_revision:'new-revision'}});
  await assert.rejects(api.writeSessionFile('session','Proof.lean','content',undefined,undefined,undefined,'loaded'),error=>
    error instanceof api.RevisionConflictError && error.currentRevision==='new-revision');
});
test('a pending runtime projection is reported as pending status, with no compiler failure',async()=>{
  const runtime=await client({detail:{error:'projection_unavailable',message:'Worker has not published this view yet.'}},503).fetchLeanCheckRuntime('session');
  assert.equal(runtime.state,'unavailable');
  assert.equal(runtime.observation?.state,'pending');
  assert.equal(runtime.last_failure,undefined);
});
test('runtime status HTTP failures retain their explanation for reconnection',async()=>{
  const runtime=await client({detail:'Status service restarting'},502).fetchLeanCheckRuntime('session');
  assert.equal(runtime.observation?.state,'unavailable');
  assert.equal(runtime.observation?.message,'Status service restarting');
});
