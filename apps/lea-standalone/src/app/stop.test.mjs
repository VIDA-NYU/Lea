import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';
import { transform } from 'esbuild';

const app = await fs.readFile(new URL('./App.tsx', import.meta.url), 'utf8');
const appStop = (await transform(app.slice(app.indexOf('  const handleInterrupt = async'), app.indexOf('  const selectStep ='))
  + '\nmodule.exports=handleInterrupt;', { loader: 'tsx' })).code;
const thread = await fs.readFile(new URL('./components/ChatThread.tsx', import.meta.url), 'utf8');
const start = thread.indexOf('  const stop = async (id: string)');
const childStop = (await transform(thread.slice(start, thread.indexOf('\n  return (', start))
  + '\nmodule.exports=stop;', { loader: 'tsx' })).code;
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve=a; reject=b; }); return {promise, resolve, reject}; };

function setup() {
  const response=deferred(), errors=[], reconciles=[];
  let calls=0;
  const state={currentRunId:'run-A', stoppingRunId:undefined, setStoppingRunId(id){this.stoppingRunId=id;}};
  const sessions={selectedSessionId:'A'};
  const context={module:{exports:{}},Error,useProofSession:{getState:()=>state},useSessions:{getState:()=>sessions},
    selectedSessionId:'A',setError:error=>errors.push(error),interruptRun:()=>{calls++;return response.promise;},
    reconcile:async id=>{reconciles.push(id);}};
  vm.runInNewContext(appStop,context);
  return {stop:context.module.exports,state,sessions,response,errors,reconciles,calls:()=>calls};
}

test('Stop acknowledges immediately, deduplicates clicks and waits for terminal status',async()=>{
  const x=setup(); const request=x.stop();
  assert.equal(x.state.stoppingRunId,'run-A');
  await x.stop(); assert.equal(x.calls(),1);
  x.response.resolve(); await request;
  assert.equal(x.state.stoppingRunId,'run-A');
  assert.equal(x.state.currentRunId,'run-A');
  assert.deepEqual(x.reconciles,['A']);
});
test('a failed Stop shows the error and permits a retry',async()=>{
  const x=setup();const request=x.stop();x.response.reject(new Error('Adapter offline'));await request;
  assert.equal(x.state.stoppingRunId,undefined);
  assert.equal(x.errors.at(-1),'Adapter offline');
  await x.stop();assert.equal(x.calls(),2);
});
test('late Stop failures cannot change another chat or a newer run',async()=>{
  for(const navigation of ['chat','run']){
    const x=setup(); const request=x.stop();
    if(navigation==='chat')x.sessions.selectedSessionId='B';
    else x.state.currentRunId='run-B';
    x.state.stoppingRunId='run-B';x.response.reject(new Error('old failure'));await request;
    assert.equal(x.state.stoppingRunId,'run-B');assert.deepEqual(x.errors,[undefined]);
  }
});
test('child Stop failures surface an error and restore the button for retry',async()=>{
  let stopping=new Set(), errors={}, calls=0;
  const response=deferred();
  const context={module:{exports:{}},Error,pendingStops:{current:new Set()},
    setStopping:fn=>stopping=fn(stopping),setStopErrors:fn=>errors=fn(errors),
    interruptSubagent:()=>{calls++;return response.promise;}};
  vm.runInNewContext(childStop,context);
  const request=context.module.exports('child');
  await context.module.exports('child');assert.equal(calls,1);assert.equal(stopping.has('child'),true);
  response.reject(new Error('Unable to reach adapter'));await request;
  assert.equal(stopping.has('child'),false);assert.equal(errors.child,'Unable to reach adapter');
  await context.module.exports('child');assert.equal(calls,2);
});

const api=await fs.readFile(new URL('./lib/api.ts',import.meta.url),'utf8');
const apiCode=(await transform(api.slice(api.indexOf('async function detailMessage'),api.indexOf('export async function listSessions'))
  +api.slice(api.indexOf('export async function interruptRun'),api.indexOf('export async function compactSession')),
  {loader:'ts',format:'cjs'})).code;
test('Stop clients surface HTTP failures and bound requests with a deadline',async()=>{
  const requests=[];
  const context={module:{exports:{}},exports:{},AbortSignal:{timeout:ms=>({deadline:ms})},
    fetch:async(url,options)=>{requests.push({url,options});return {ok:false,status:409,statusText:'Conflict',json:async()=>({detail:'Cannot stop this run'})};}};
  context.exports=context.module.exports;
  vm.runInNewContext(apiCode,context);
  await assert.rejects(context.module.exports.interruptRun('run'),/Cannot stop this run/);
  await assert.rejects(context.module.exports.interruptSubagent('child'),/Cannot stop this run/);
  assert.equal(requests[0].options.signal.deadline,10000);
  assert.equal(requests[1].options.signal.deadline,10000);
  context.fetch=async()=>({ok:false,status:404});
  await context.module.exports.interruptSubagent('already-finished');
});

test('narrow screens open with space for the chat and its Stop control',async()=>{
  const source=await fs.readFile(new URL('./hooks/useLayout.ts',import.meta.url),'utf8');
  const code=(await transform(source.slice(source.indexOf('export function useLayout()')),{loader:'ts',format:'cjs'})).code;
  for(const width of [390,1100]){
    const context={module:{exports:{}},window:{innerWidth:width,localStorage:{getItem:()=>null}},
      useState:value=>[typeof value==='function'?value():value,()=>{}],useEffect:()=>{},useRef:value=>({current:value})};
    context.exports=context.module.exports;
    vm.runInNewContext(code,context);
    const layout=context.module.exports.useLayout();
    assert.equal(layout.sidebarCollapsed,width<768);assert.equal(layout.canvasCollapsed,width<768);
  }
});
