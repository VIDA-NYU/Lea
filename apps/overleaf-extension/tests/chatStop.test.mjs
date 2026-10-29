import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Execute the shipped closure with deferred transport, so navigation and an
// outstanding stop request can be controlled independently of the companion.
const source=fs.readFileSync(new URL('../extension/content.js',import.meta.url),'utf8');
const handler=source.slice(source.indexOf('  async function stopChatRun()'),source.indexOf('  function chatTranscriptMessages()'));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject};};
const response=(ok=true,status=200,payload={ok})=>({ok,status,json:async()=>payload});
function setup(){
  const pending=deferred(),snapshots=[],requests=[];let polls=0;
  const context={Error,AbortSignal,leanPaneChatToken:1,leanPaneChatRunId:'run-A',leanPaneChatSessionId:'session-A',
    leanPaneChatStopping:false,leanPaneChatError:null,chatCompanionBaseUrl:async()=>'http://companion',
    companionFetch:(url,options)=>{requests.push({url,options});return pending.promise;},
    renderChatPanel:()=>snapshots.push({stopping:context.leanPaneChatStopping,error:context.leanPaneChatError}),
    pollChatSession:async()=>{polls++},module:{exports:{}}};
  vm.runInNewContext(handler+'\nmodule.exports=stopChatRun;',context);
  return {context,stop:context.module.exports,pending,snapshots,requests,polls:()=>polls};
}
test('Overleaf acknowledges Stop immediately and suppresses duplicate requests',async()=>{
  const x=setup();const request=x.stop();
  assert.equal(x.snapshots[0].stopping,true);
  await x.stop();assert.equal(x.requests.length,1);
  assert.equal(x.requests[0].options.signal instanceof AbortSignal,true);
  x.pending.resolve(response());await request;
  assert.equal(x.context.leanPaneChatStopping,true);assert.equal(x.polls(),1);
});
for(const failure of ['HTTP','network','timeout','invalid-success']){
  test(`Overleaf surfaces ${failure} Stop failures and permits retry`,async()=>{
    const x=setup();const request=x.stop();await Promise.resolve();
    if(failure==='HTTP')x.pending.resolve(response(false,503,{message:'Adapter unavailable'}));
    else if(failure==='invalid-success')x.pending.resolve(response(true,200,{ok:false,error:'Stop was rejected'}));
    else x.pending.reject(new Error(failure==='timeout'?'Stop request timed out':'Companion offline'));
    await request;
    assert.equal(x.context.leanPaneChatStopping,false);
    assert.ok(x.context.leanPaneChatError?.message);
    assert.equal(x.snapshots.at(-1).stopping,false);
    assert.equal(x.polls(),0);
    x.context.companionFetch=async()=>response();await x.stop();assert.equal(x.context.leanPaneChatStopping,true);
  });
}
test('Stop captures its target before async lookup and ignores failures after navigation',async()=>{
  const x=setup();const base=deferred();x.context.chatCompanionBaseUrl=()=>base.promise;
  const request=x.stop();
  x.context.leanPaneChatToken=2;x.context.leanPaneChatRunId='run-B';x.context.leanPaneChatSessionId='session-B';
  x.context.leanPaneChatStopping=false;base.resolve('http://companion');await new Promise(setImmediate);
  assert.deepEqual(JSON.parse(x.requests[0].options.body),{runId:'run-A',sessionId:'session-A'});
  x.pending.reject(new Error('old request failed'));await request;
  assert.equal(x.context.leanPaneChatStopping,false);assert.equal(x.context.leanPaneChatError,null);
  assert.equal(x.polls(),0);
});
