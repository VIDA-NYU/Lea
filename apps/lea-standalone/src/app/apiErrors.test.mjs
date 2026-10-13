import assert from 'node:assert/strict';
import test from 'node:test';
import {build} from 'esbuild';
import vm from 'node:vm';
const bundled=await build({entryPoints:[new URL('./lib/api.ts',import.meta.url).pathname],bundle:true,write:false,platform:'node',format:'cjs'});
function client(body,status=409) {
  const context={module:{exports:{}},exports:{},fetch:async()=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}})};
  context.exports=context.module.exports;vm.runInNewContext(bundled.outputFiles[0].text,context);
  return context.module.exports;
}
test('a busy save retains the server message after revision-conflict inspection',async()=>{
  const api=client({detail:{error:'project_busy',message:'Wait for active project work.'}});
  await assert.rejects(api.writeSessionFile('session','Proof.lean','content'),/Wait for active project work/);
});
test('a stale proof check explains refreshing the current revision',async()=>{
  const api=client({detail:{code:'proof_revision_changed',message:'Refresh the current revision and check again.'}});
  await assert.rejects(api.leanCheckSession('session','Proof.lean'),/Refresh the current revision/);
});
test('an optimistic save conflict still carries its revision metadata',async()=>{
  const api=client({detail:{code:'revision_conflict',message:'Changed in another conversation.',current_revision:'new-revision'}});
  await assert.rejects(api.writeSessionFile('session','Proof.lean','content'),error=>
    error instanceof api.RevisionConflictError && error.currentRevision==='new-revision');
});
