import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import vm from 'node:vm';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const bundle = build({
  entryPoints: [new URL('./hooks/useProofStream.ts', import.meta.url).pathname],
  bundle: true, write: false, platform: 'node', format: 'cjs',
  plugins: [{name: 'isolated-hook', setup(b) {
    b.onResolve({filter: /^react$/}, () => ({path:'react', namespace:'fake'}));
    b.onResolve({filter: /^\.\.\/lib\/api$/}, () => ({path:'api', namespace:'fake'}));
    b.onResolve({filter: /^\.\.\/stores\/(proofSession|sessions)$/}, a => ({path:a.path.split('/').pop(), namespace:'fake'}));
    b.onLoad({filter: /.*/, namespace:'fake'}, a => ({contents: a.path === 'react'
      ? 'exports.useRef=x=>({current:x}); exports.useEffect=f=>globalThis.effects.push(f);'
      : a.path === 'api' ? 'exports.getSession=(...args)=>globalThis.getSession(...args); exports.awaitReformalization=(...args)=>globalThis.awaitReformalization(...args);'
      : `exports.use${a.path === 'proofSession' ? 'ProofSession':'Sessions'}=globalThis.${a.path};`}));
  }}],
});
const detail = (id, active=false) => ({id, messages:[{id:`message-${id}`, content:id}], code_steps:[], runs:[], formalizations:[],
  active_run:active?{id:`run-${id}`, status:'running'}:null});
async function setup() {
  const requests=[], operations=[], timers=new Map(), sources=[], effects=[], subscribers=[];
  let timerId=0;
  const state={formalizationScope:'project', messages:[], runStatusById:{}, runResultKindById:{}};
  const proxy=new Proxy(state,{get(t,k) {
    if(k==='resetSessionScoped') return ()=>{t.messages=[];};
    if(typeof k==='string' && k.startsWith('set')) {
      const field=k[3].toLowerCase()+k.slice(4);
      return value=>{const prev={...t}; t[field]=typeof value==='function'?value(t[field]):value; subscribers.forEach(fn=>fn(t,prev));};
    }
    return t[k];
  }});
  const sessions={selectedSessionId:'A',setSelectedSessionId(id){this.selectedSessionId=id;},refreshSessions:async()=>[]};
  class Source {
    static OPEN=1;
    listeners=new Map(); readyState=1;
    constructor(url){this.url=url;sources.push(this);}
    close(){this.closed=true;}
    addEventListener(name,fn){this.listeners.set(name,fn);}
    emit(name,payload={}){this.listeners.get(name)?.({data:JSON.stringify(payload)});}
  }
  const context={module:{exports:{}},require,AbortController,EventSource:Source,effects,
    setTimeout:fn=>{timers.set(++timerId,fn);return timerId;},clearTimeout:id=>timers.delete(id),
    setInterval:()=>0,clearInterval:()=>{},document:{visibilityState:'visible'},
    proofSession:{getState:()=>proxy,subscribe:fn=>{subscribers.push(fn);return()=>{};}},sessions:{getState:()=>sessions},
    awaitReformalization:(initial,_queued,owned)=>new Promise(resolve=>operations.push({initial,owned,resolve})),
    getSession:(id,signal)=>new Promise((resolve,reject)=>requests.push({id,signal,resolve,reject})),
  };
  context.exports=context.module.exports;context.globalThis=context;
  vm.runInNewContext((await bundle).outputFiles[0].text,context);
  const hook=context.module.exports.useProofStream();
  const cleanups=effects.map(fn=>fn());
  return {hook,state,proxy,sessions,requests,operations,sources,cleanup:()=>cleanups.forEach(fn=>fn?.()),
    tick:async()=>{const entries=[...timers];timers.clear();for(const [,fn] of entries)fn();await flush();}};
}
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};

test('a late reconciliation cannot change the selected session',async()=>{
  const x=await setup();const pending=x.hook.reconcile('A');x.hook.applyDetail(detail('B'));
  x.requests[0].resolve(detail('A'));await pending;
  assert.equal(x.sessions.selectedSessionId,'B');assert.equal(x.state.messages[0].content,'B');x.cleanup();
});
test('a late reconnect response cannot reattach a switched-away run',async()=>{
  const x=await setup();x.hook.applyDetail(detail('A',true));x.sources[0].onerror();await x.tick();
  x.hook.applyDetail(detail('B'));x.requests[0].resolve(detail('A',true));await flush();
  assert.equal(x.sessions.selectedSessionId,'B');assert.equal(x.sources.length,1);x.cleanup();
});
test('a late resync response cannot restore an old session',async()=>{
  const x=await setup();x.hook.applyDetail(detail('A',true));x.sources[0].emit('resync');
  x.hook.applyDetail(detail('B'));x.requests[0].resolve(detail('A',true));await flush();
  assert.equal(x.sessions.selectedSessionId,'B');x.cleanup();
});
test('callbacks queued on a closed stream cannot write into the new chat',async()=>{
  const x=await setup();x.hook.applyDetail(detail('A',true));const old=x.sources[0];x.hook.applyDetail(detail('B'));
  old.emit('assistant_delta',{text:'OLD'});old.emit('done',{status:'error'});old.onopen();
  assert.deepEqual(x.state.messages.map(m=>m.content),['B']);assert.equal(x.state.runStatus,undefined);
  assert.equal(x.requests.length,0);x.cleanup();
});
test('overlapping navigation applies only the latest request, including A B A',async()=>{
  const x=await setup();const old=x.hook.loadSession('A');const mid=x.hook.loadSession('B');const last=x.hook.loadSession('A');
  x.requests[2].resolve(detail('A'));await last;x.requests[0].resolve({...detail('A'),messages:[{content:'STALE'}]});
  x.requests[1].reject(new Error('old failure'));await Promise.all([old,mid]);
  assert.equal(x.state.messages[0].content,'A');assert.equal(x.requests[0].signal.aborted,true);x.cleanup();
});
test('the active reconciliation still applies fresh history',async()=>{
  const x=await setup();const pending=x.hook.reconcile('A');x.requests[0].resolve(detail('A'));await pending;
  assert.equal(x.state.messages[0].content,'A');x.cleanup();
});
test('save and verify ownership expires on navigation or formalization change',async()=>{
  const x=await setup();const owned=x.hook.captureSelection();x.proxy.setFormalizationScope('target');x.proxy.setFormalizationScope('project');
  assert.equal(owned(),false);const next=x.hook.captureSelection();x.hook.closeStream();assert.equal(next(),false);x.cleanup();
});

// Execute the actual App callbacks with the same isolated stores and deferred API.
// This keeps tests independent of Monaco and the visual component imports.
import { readFile } from 'node:fs/promises';
import { transform } from 'esbuild';
const appSource=await readFile(new URL('./App.tsx',import.meta.url),'utf8');
const handlers=await transform(appSource.slice(appSource.indexOf('  const handleSaveAndCheck = async'),appSource.indexOf('  // F9:',appSource.indexOf('  const handleSaveAndCheck = async')))
  +'\nmodule.exports={handleSaveAndCheck,handleVerify};',{loader:'tsx',format:'cjs'});
function appCallbacks(x, overrides={}) {
  const calls=[];const noop=name=>(...args)=>calls.push([name,...args]);
  const context={module:{exports:{}},selectedSessionId:'A',sortedCode:[{path:'Target.lean'}],formalizationScope:'project',
    currentFormalizationSnapshot:null,captureSelection:x.hook.captureSelection,
    useProofSession:{getState:()=>x.proxy,setState:patch=>Object.assign(x.state,patch)},
    writeSessionFile:async()=>{},leanCheckSession:async()=>({status:'ok'}),verifySession:async()=>({status:'ok'}),
    reconcile:noop('reconcile'),refreshSessions:async()=>{},RevisionConflictError:class extends Error{},
    setCanvasRevisionMode:noop('revision'),bumpFormalizationRefresh:noop('refresh'),setError:noop('error'),
    setEditedPath:noop('edited'),setSafeVerify:noop('verify'),...overrides};
  vm.runInNewContext(handlers.code,context);
  return {...context.module.exports,calls};
}
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};}
test('App save/check completion cannot change another conversation or its busy state',async()=>{
  const x=await setup();const write=deferred(),check=deferred();
  const app=appCallbacks(x,{writeSessionFile:()=>write.promise,leanCheckSession:()=>check.promise});
  const pending=app.handleSaveAndCheck('new content');x.hook.applyDetail(detail('B'));
  write.resolve({});await flush();assert.equal(x.state.manualCheckSessionId,undefined);
  check.resolve({status:'ok'});await pending;assert.deepEqual(app.calls,[]);x.cleanup();
});
test('App delayed verification cannot replace the new chat verdict',async()=>{
  const x=await setup();const response=deferred();const app=appCallbacks(x,{verifySession:()=>response.promise});
  const pending=app.handleVerify();x.hook.applyDetail(detail('B'));response.resolve({status:'ok'});await pending;
  assert.deepEqual(app.calls,[]);x.cleanup();
});
test('App active save still reconciles and shows its result',async()=>{
  const x=await setup();const app=appCallbacks(x);await app.handleSaveAndCheck('new content');
  assert.deepEqual(app.calls.map(([name])=>name),['reconcile','edited','verify','revision','refresh']);x.cleanup();
});
test('App verification expires on formalization changes within a chat',async()=>{
  const x=await setup();const response=deferred();const app=appCallbacks(x,{verifySession:()=>response.promise});
  const pending=app.handleVerify();x.proxy.setFormalizationScope('target');response.resolve({status:'ok'});await pending;
  assert.deepEqual(app.calls,[]);x.cleanup();
});


test('reloading a preparing reformalization observes its future run',async()=>{
  const x=await setup();const pending=x.hook.loadSession('A');
  x.requests[0].resolve({...detail('A'),queued_reformalizations:[{id:'preparing-operation'}]});await pending;
  assert.equal(x.operations[0].initial.reformalization_id,'preparing-operation');
  assert.equal(x.operations[0].owned(),true);
  x.operations[0].resolve({run_id:'new-segment'});await flush();
  assert.equal(x.sources.length,1);assert.ok(x.sources[0].url.includes('new-segment'));x.cleanup();
});
test('a preparing retry cannot reattach after navigation',async()=>{
  const x=await setup();const pending=x.hook.loadSession('A');
  x.requests[0].resolve({...detail('A'),queued_reformalizations:[{id:'preparing-operation'}]});await pending;
  x.hook.applyDetail(detail('B'));
  assert.equal(x.operations[0].owned(),false);
  x.operations[0].resolve({run_id:'late-segment'});await flush();
  assert.equal(x.sources.length,0);assert.equal(x.sessions.selectedSessionId,'B');x.cleanup();
});

const startHandlers=await transform(appSource.slice(appSource.indexOf('  const handleStartProjectFormalization = async'),appSource.indexOf('  // F3:',appSource.indexOf('  const handleStartProjectFormalization = async')))
  +'\nmodule.exports={handleStartProjectFormalization};',{loader:'tsx',format:'cjs'});
function startCallbacks(x,overrides={}) {
  const calls=[];
  const context={module:{exports:{}},currentProject:{slug:'study',title:'Study',namespace:'Lea.Study'},selectedSessionId:'A',
    useSessions:{getState:()=>x.sessions},useModel:{getState:()=>({model:'fixture'})},captureSelection:x.hook.captureSelection,
    setError:()=>{},resetForNewSession:()=>calls.push('reset'),
    loadSession:async id=>{calls.push(['load',id]);x.state.messages=[{content:'Existing conversation'}];return detail(id);},
    setSelectedSessionId:id=>x.sessions.setSelectedSessionId(id),setFormalizations:()=>{},setFormalizationScope:()=>{},
    setMessages:x.proxy.setMessages,setView:()=>{},refreshSessions:async()=>{},
    SELECTED_SESSION_KEY:'selected',window:{localStorage:{setItem:()=>{}}},
    createRun:async (message,session,model,options,onQueued)=>{
      assert.equal(session,'A');onQueued({session_id:'A',reformalization_id:'retry'});
      assert.equal(x.state.messages[0].content,'Existing conversation');
      assert.equal(x.state.messages.length,2);
      return {run_id:'new-run',session_id:'A'};
    },...overrides};
  vm.runInNewContext(startHandlers.code,context);
  return {...context.module.exports,calls};
}
test('standalone reformalization reuses the selected target conversation and hydrates its history',async()=>{
  const x=await setup();const app=startCallbacks(x);
  await app.handleStartProjectFormalization({id:'B',sessions:[{id:'older'},{id:'A'}]},'Retry B',true);
  assert.deepEqual(app.calls,[['load','A'],['load','A']]);
  assert.equal(x.state.messages[0].content,'Existing conversation');x.cleanup();
});
test('late standalone admission cannot navigate back to an old conversation',async()=>{
  const x=await setup();const response=deferred();
  const app=startCallbacks(x,{createRun:async (...args)=>{await response.promise;args[4]({session_id:'A',reformalization_id:'retry'});return {run_id:'late-run',session_id:'A'};}});
  const pending=app.handleStartProjectFormalization({id:'B',sessions:[{id:'A'}]},'Retry B',true);await flush();
  x.hook.applyDetail(detail('B'));response.resolve();await pending;
  assert.equal(x.sessions.selectedSessionId,'B');assert.equal(x.state.messages[0].content,'B');x.cleanup();
});

test('queued standalone admission expires across navigation away and back to the same chat',async()=>{
  const x=await setup();const response=deferred();
  const app=startCallbacks(x,{createRun:async (...args)=>{args[4]({session_id:'A',reformalization_id:'retry'});await response.promise;return {run_id:'late-run',session_id:'A'};}});
  const pending=app.handleStartProjectFormalization({id:'target',sessions:[{id:'A'}]},'Retry B',true);await flush();
  x.hook.applyDetail(detail('B'));x.hook.applyDetail(detail('A'));
  response.resolve();await pending;
  assert.deepEqual(app.calls,[['load','A']]);assert.equal(x.state.messages[0].content,'A');x.cleanup();
});
