import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { deriveCodeStepProofStatus } from './lib/proofDisplay.mjs';
import { observeSessionList } from './lib/sessionObserver.mjs';
import { restoreInitialSession } from './sessionDeepLink.mjs';
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
  const state={formalizationScope:'project', canvasRevisionMode:'current', codeSteps:[], codeIndex:0,
    messages:[], subagentProgress:{}, runStatusById:{}, runResultKindById:{}, formalizationRefreshToken:0};
  const proxy=new Proxy(state,{get(t,k) {
    if(k==='resetSessionScoped') return ()=>{t.messages=[];};
    if(k==='bumpFormalizationRefresh') return ()=>{t.formalizationRefreshToken+=1;};
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
    emit(name,payload={},lastEventId=''){this.listeners.get(name)?.({data:JSON.stringify(payload),lastEventId});}
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

test('a snapshot replay barrier prevents historical checks from certifying stale code',async()=>{
  const x=await setup();
  const checked={...step('proof',1),check_status:'ok',artifact_kind:'theorem',code:'theorem t : True := by trivial'};
  x.hook.applyDetail({...detail('A',true),run_event_cursors:{'run-A':'attempt:20'},
    code_steps:[{...checked,check_current:false,check_stale_reason:'dependency_changed'}]});
  // Keep transient subagent/approval replay while fencing canonical code.
  assert.doesNotMatch(x.sources[0].url,/since=/);
  x.sources[0].emit('subagent_progress',{child_id:'child',kind:'tool',tool:'read_file'},'attempt:9');
  assert.equal(x.state.subagentProgress.child.tool,'read_file');
  x.sources[0].emit('code_step',{...checked,check_current:true},'attempt:10');
  assert.equal(deriveCodeStepProofStatus(x.state.codeSteps[0]),'unchecked');
  x.sources[0].emit('code_step',{...checked,check_current:true},'attempt:21');
  assert.equal(deriveCodeStepProofStatus(x.state.codeSteps[0]),'proved');
  x.sources[0].emit('code_step',{...checked,check_current:false},'attempt:21');
  x.sources[0].emit('code_step',{...checked,check_current:false},'old-attempt:99');
  assert.equal(x.state.codeSteps[0].check_current,true);
  x.sources[0].emit('resync',{attempt_id:'attempt',cursor:5},'attempt:5');
  x.requests[0].resolve({...detail('A',true),run_event_cursors:{'run-A':'attempt:5'},
    code_steps:[{...checked,check_current:false}]});await flush();
  x.sources.at(-1).emit('code_step',{...checked,check_current:true},'attempt:6');
  assert.equal(x.state.codeSteps[0].check_current,true,'a restored snapshot replaces the old barrier');x.cleanup();
});

test('same-session idle reconciliations reject superseded successes and failures',async()=>{
  const x=await setup();x.hook.applyDetail(detail('A'));
  const first=x.hook.reconcile('A'),second=x.hook.reconcile('A');
  x.requests[1].resolve({...detail('A'),messages:[{id:'new',content:'latest'}]});await second;
  x.requests[0].resolve({...detail('A'),messages:[{id:'old',content:'stale'}]});await first;
  assert.equal(x.state.messages[0].id,'new');
  const old=x.hook.reconcile('A'),fresh=x.hook.reconcile('A');
  x.requests[3].resolve({...detail('A'),messages:[{id:'newest',content:'latest'}]});await fresh;
  x.requests[2].reject(new Error('obsolete failure'));await old;
  assert.equal(x.state.messages[0].id,'newest');x.cleanup();
});

test('snapshot status identities survive hydration without duplicate replay',async()=>{
  const x=await setup();
  const status={id:'saved-status',status:'lean_check',message:'lean_check: ok',check_status:'ok'};
  x.hook.applyDetail({...detail('A',true),status_events:[status],run_event_cursors:{'run-A':'attempt:10'}});
  x.sources[0].emit('status',status,'attempt:10');
  assert.equal(x.state.statusEvents.length,1);
  x.hook.applyDetail({...detail('A'),status_events:[status]},{preservePresentation:true});
  assert.equal(x.state.statusEvents[0].id,'saved-status');x.cleanup();
});

test('retention reset replaces a future cursor and preserves historical selection',async()=>{
  const x=await setup();x.hook.applyDetail(selectionDetail());selectHistory(x);
  const before=selectedPresentation(x);
  x.sources[0].emit('status',{},'attempt:9999');
  x.sources[0].emit('resync',{attempt_id:'attempt',cursor:5,reload:true},'attempt:5');
  assert.equal(x.sources[0].closed,true);
  x.requests[0].resolve(selectionDetail());await flush();
  assert.match(x.sources.at(-1).url,/since=attempt%3A5/);
  assert.deepEqual(selectedPresentation(x),before);x.cleanup();
});

test('persisted partial replay is idempotent and finalizes the same bubble',async()=>{
  const x=await setup();x.hook.applyDetail(detail('A',true));
  const message={id:'42',session_id:'A',run_id:'run-A',role:'assistant',created_at:'now',
    stream_sequence:1,stream_state:'streaming',live:true};
  x.sources[0].emit('assistant_delta',{text:'Hello',message},'attempt:10');
  x.sources[0].emit('assistant_delta',{text:'Hello',message},'attempt:10');
  x.sources[0].emit('assistant_delta',{text:' world',message:{...message,stream_sequence:2}},'attempt:11');
  assert.equal(x.state.messages.filter(m=>m.id==='42').length,1);
  assert.equal(x.state.messages.find(m=>m.id==='42').content,'Hello world');
  x.sources[0].emit('message',{...message,content:'Hello world',stream_sequence:3,stream_state:'final',live:false},'attempt:12');
  x.sources[0].emit('assistant_delta',{text:' world',message:{...message,stream_sequence:2}},'attempt:11');
  assert.equal(x.state.messages.find(m=>m.id==='42').live,false);
  assert.equal(x.state.messages.find(m=>m.id==='42').content,'Hello world');x.cleanup();
});

test('partial gaps reload committed state and resume after the persisted event cursor',async()=>{
  const x=await setup();x.hook.applyDetail(selectionDetail());selectHistory(x);
  const before=selectedPresentation(x);
  x.sources[0].emit('assistant_delta',{text:'missing prefix',message:{id:'42',stream_sequence:3}},'attempt:12');
  assert.equal(x.sources[0].closed,true);
  assert.equal(x.requests.length,1);
  x.requests[0].resolve({...selectionDetail(),messages:[{id:'42',content:'committed prefix',stream_sequence:3}]});
  await flush();
  assert.match(x.sources[1].url,/since=attempt%3A12/);
  assert.deepEqual(selectedPresentation(x),before);
  assert.equal(x.state.messages.find(m=>m.id==='42').content,'committed prefix');x.cleanup();
});

test('hydrated partial ignores already committed replay chunks',async()=>{
  const x=await setup();x.hook.applyDetail({...detail('A',true),messages:[{id:'42',content:'persisted',stream_sequence:5}]});
  x.sources[0].emit('assistant_delta',{text:'duplicate',message:{id:'42',stream_sequence:2}});
  assert.equal(x.state.messages[0].content,'persisted');x.cleanup();
});

const step = (id, seq, formalization_id = 'target-a', path = 'A.lean') => ({
  id, seq, formalization_id, path, code:`-- ${id}`, created_at:'2026-10-16T12:00:00Z',
});
const selectionDetail = (active = true) => ({ ...detail('A', active),
  formalizations:[{id:'target-a',activity:{status:'idle'}},{id:'target-b',activity:{status:'running'}}],
  latest_focus_formalization_id:'target-a', code_steps:[step('old-a',2),step('old-b',4,'target-b','B.lean')],
});
function selectedPresentation(x) {
  return {
    scope:x.state.formalizationScope, mode:x.state.canvasRevisionMode,
    step:x.state.codeSteps[x.state.codeIndex]?.id, override:x.state.composerScopeOverride,
    snapshot:x.state.currentFormalizationSnapshot, goal:x.state.goalSurface, edited:x.state.editedPath,
  };
}
function selectHistory(x) {
  x.proxy.setFormalizationScope('target-a');
  x.proxy.setCanvasRevisionMode('historical');
  x.proxy.setCodeIndex(0);
  x.proxy.setComposerScopeOverride('target-b');
  x.proxy.setCurrentFormalizationSnapshot({revision_token:'chosen-revision'});
  x.proxy.setGoalSurface({rendered:'chosen goal',line:5});
  x.proxy.setEditedPath('A.lean');
}

test('live steps, replay, and backfill update data without changing target or historical selection',async()=>{
  const x=await setup();x.hook.applyDetail(selectionDetail());selectHistory(x);
  const before=selectedPresentation(x);
  x.sources[0].emit('code_step',step('new-b',6,'target-b','B.lean'));
  x.sources[0].emit('code_step',step('earlier',1,'target-b','B.lean'));
  x.sources[0].emit('code_step',{...step('old-a',2),check_status:'ok'});
  assert.deepEqual(selectedPresentation(x),before);
  assert.equal(x.state.codeSteps.length,4);
  assert.equal(x.state.codeSteps[x.state.codeIndex].check_status,'ok');
  assert.equal(x.state.formalizationRefreshToken,3);x.cleanup();
});

for(const scope of ['project','new','target-a']) {
  test(`incoming work does not navigate from the ${scope} view`,async()=>{
    const x=await setup();x.hook.applyDetail(selectionDetail());x.proxy.setFormalizationScope(scope);
    const index=x.state.codeIndex;
    x.sources[0].emit('code_step',step('new-b',6,'target-b','B.lean'));
    assert.equal(x.state.formalizationScope,scope);assert.equal(x.state.codeIndex,index);x.cleanup();
  });
}

for(const refresh of ['reconcile','resync','reconnect','done']) {
  test(`${refresh} keeps presentation selected while refreshing authoritative history`,async()=>{
    const x=await setup();x.hook.applyDetail(selectionDetail());selectHistory(x);
    const before=selectedPresentation(x);
    let pending;
    if(refresh==='reconcile') pending=x.hook.reconcile('A');
    else if(refresh==='reconnect') {x.sources[0].onerror();await x.tick();}
    else x.sources[0].emit(refresh,refresh==='done'?{status:'proved'}:{});
    x.requests[0].resolve({...selectionDetail(refresh!=='done'),latest_focus_formalization_id:'target-b',
      code_steps:[step('earlier',1),...selectionDetail().code_steps,step('new-b',6,'target-b','B.lean')]});
    await pending;await flush();
    assert.deepEqual(selectedPresentation(x),before);
    assert.equal(x.state.codeSteps.length,4);x.cleanup();
  });
}

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
const observerEffect = await transform(appSource.slice(
  appSource.indexOf('  useEffect(() => observeSessionList('),
  appSource.indexOf('  const loadSession = async'),
), { loader: 'tsx', format: 'cjs' });
function appSessionObserver(x) {
  const sources = [];
  let stop;
  const context = {
    Error,
    useEffect: (effect) => { stop = effect(); },
    observeSessionList: options => observeSessionList({ ...options,
      createSource: () => {
        const source = { listeners: {}, addEventListener(name, listener) { this.listeners[name] = listener; }, close() {} };
        sources.push(source);
        return source;
      },
    }),
    refreshSessions: async () => [],
    useSessions: { getState: () => x.sessions },
    useProofSession: { getState: () => x.proxy },
    bumpFormalizationRefresh: x.proxy.bumpFormalizationRefresh,
    reconcile: x.hook.reconcile,
    captureSelection: x.hook.captureSelection,
    setError: x.proxy.setError,
  };
  vm.runInNewContext(observerEffect.code, context);
  return { sources, stop: () => stop() };
}

test('startup invalidation waits for navigation; a failed refresh preserves history and shows an error', async () => {
  const x = await setup(), observer = appSessionObserver(x);
  await flush();
  let consumed = false;
  const restore = restoreInitialSession({
    search: '?session=A&formalization=focus', sessions: [],
    loadFormalization: async () => ({ id: 'focus' }),
    loadSession: x.hook.loadSession,
    selectFormalization: x.proxy.setFormalizationScope,
    onRestored: () => { consumed = true; },
  });
  await flush();
  observer.sources[0].listeners.sessions_changed();
  await flush();
  assert.equal(x.requests.length, 1, 'a background refresh cannot replace the navigation request');
  x.requests[0].resolve(detail('A'));
  await restore;
  assert.equal(consumed, true);
  assert.equal(x.state.messages[0].content, 'A');
  assert.equal(x.state.formalizationScope, 'focus');
  assert.equal(x.requests.length, 2, 'changes received during loading are refreshed after hydration');
  x.requests[1].reject(new Error('Temporary refresh failure'));
  await flush();
  assert.match(x.state.error, /Could not refresh LeaChat: Temporary refresh failure/);
  assert.equal(x.state.messages[0].content, 'A');
  observer.stop(); x.cleanup();
});

test('startup load failure propagates to restoration even when a refresh is waiting', async () => {
  const x = await setup(), observer = appSessionObserver(x);
  await flush();
  let consumed = false;
  const restore = restoreInitialSession({ search: '?session=A', sessions: [],
    loadSession: x.hook.loadSession, onRestored: () => { consumed = true; } });
  const failure = assert.rejects(restore, /Session unavailable/);
  observer.sources[0].listeners.sessions_changed();
  await flush();
  assert.equal(x.requests.length, 1);
  x.requests[0].reject(new Error('Session unavailable'));
  await failure; await flush();
  assert.equal(consumed, false, 'the requested URL remains available for retry');
  assert.equal(x.requests.length, 1, 'a background refresh does not hide or retry the failed navigation');
  observer.stop(); x.cleanup();
});

test('changes received during navigation are applied afterward without changing the restored target', async () => {
  const x = await setup();
  const load = x.hook.loadSession('A'), refresh = x.hook.reconcile('A');
  assert.equal(x.requests.length, 1);
  x.requests[0].resolve(detail('A'));
  await load;
  x.proxy.setFormalizationScope('chosen-target');
  x.requests[1].resolve({ ...detail('A'), messages: [{ id: 'new-message', content: 'Updated history' }] });
  await refresh;
  assert.equal(x.state.messages[0].content, 'Updated history');
  assert.equal(x.state.formalizationScope, 'chosen-target');
  x.cleanup();
});

test('a refresh waiting for navigation cannot fetch or report errors after a session switch', async () => {
  const x = await setup(), observer = appSessionObserver(x);
  await flush();
  const first = x.hook.loadSession('A');
  observer.sources[0].listeners.sessions_changed(); await flush();
  const second = x.hook.loadSession('B');
  x.requests[0].resolve(detail('A')); await first; await flush();
  assert.equal(x.requests.length, 2);
  x.requests[1].resolve(detail('B')); await second;
  assert.equal(x.state.messages[0].content, 'B');
  assert.equal(x.state.error, undefined);
  observer.sources[0].listeners.sessions_changed(); await flush();
  const third = x.hook.loadSession('C');
  x.requests[2].reject(new Error('Old refresh failed'));
  x.requests[3].resolve(detail('C')); await third; await flush();
  assert.equal(x.state.messages[0].content, 'C');
  assert.equal(x.state.error, undefined);
  observer.stop(); x.cleanup();
});

const handlers=await transform(appSource.slice(appSource.indexOf('  const handleSaveAndCheck = async'),appSource.indexOf('  // F9:',appSource.indexOf('  const handleSaveAndCheck = async')))
  +'\nmodule.exports={handleSaveAndCheck,handleCheckPublished,handleVerify};',{loader:'tsx',format:'cjs'});
function appCallbacks(x, overrides={}) {
  const calls=[];const noop=name=>(...args)=>calls.push([name,...args]);
  const context={module:{exports:{}},selectedSessionId:'A',sortedCode:[{path:'Target.lean'}],formalizationScope:'project',
    currentFormalizationSnapshot:null,captureSelection:x.hook.captureSelection,
    useProofSession:{getState:()=>x.proxy,setState:patch=>Object.assign(x.state,patch)},useSessions:{getState:()=>x.sessions},
    writeSessionFile:async()=>({draft_id:'draft',file_revision:'new-file'}),leanCheckSession:async()=>({status:'ok'}),verifySession:async()=>({status:'ok'}),
    reconcile:noop('reconcile'),refreshSessions:async()=>{},RevisionConflictError:class extends Error{},
    setCanvasRevisionMode:noop('revision'),bumpFormalizationRefresh:noop('refresh'),setError:noop('error'),
    setEditedPath:noop('edited'),setSafeVerify:noop('verify'),...overrides};
  vm.runInNewContext(handlers.code,context);
  return {...context.module.exports,calls};
}
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};}
const draftBase={session_id:'A',path:'Target.lean',file_revision:'loaded-file'};
test('App save/check completion cannot change another conversation or its busy state',async()=>{
  const x=await setup();const write=deferred(),check=deferred();
  const app=appCallbacks(x,{writeSessionFile:()=>write.promise,leanCheckSession:()=>check.promise});
  const pending=app.handleSaveAndCheck('new content','Target.lean',draftBase,'draft');x.hook.applyDetail(detail('B'));
  write.resolve({});await flush();assert.equal(x.state.manualCheckSessionId,undefined);
  check.resolve({status:'ok'});await pending;assert.deepEqual(app.calls,[]);x.cleanup();
});
test('App delayed verification cannot replace the new chat verdict',async()=>{
  const x=await setup();const response=deferred();const app=appCallbacks(x,{verifySession:()=>response.promise});
  const pending=app.handleVerify();x.hook.applyDetail(detail('B'));response.resolve({status:'ok'});await pending;
  assert.deepEqual(app.calls,[]);x.cleanup();
});
test('App active save still reconciles and shows its result',async()=>{
  const x=await setup();const app=appCallbacks(x);
  const receipt=await app.handleSaveAndCheck('new content','Target.lean',draftBase,'draft');
  assert.equal(receipt.file_revision,'new-file');
  assert.deepEqual(app.calls.map(([name])=>name),['edited','verify','refresh']);
  await app.handleCheckPublished('Target.lean',{...draftBase,file_revision:receipt.file_revision});
  assert.deepEqual(app.calls.slice(3).map(([name])=>name),['reconcile','edited','verify','revision','refresh']);x.cleanup();
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

test('complete refresh snapshots restore children and approvals before resuming their cursor',async()=>{
  const x=await setup();
  const snapshot={...detail('A',true),run_event_cursors:{'run-A':'attempt:40'},
    subagent_observations:[{child_id:'working',progress:{text:'durable',tool:'read_file',turn:2,message_id:'child-message',stream_sequence:3}},
      {child_id:'queued',queued:true,progress:{text:''}},
      {child_id:'failed',finished:true,error:'Provider unavailable',stop_notice:'Stopped',progress:{text:''}}],
    approval_events:[{id:'history',approval_id:'history',run_id:'old',tool_name:'write_file',args:{},decision:'deny',seq:2.5},
      {id:'pending',approval_id:'pending',run_id:'run-A',tool_name:'write_file',args:{},seq:3.5}],
    active_run:{id:'run-A',status:'running',pending_approval:{approval_id:'pending',tool_name:'write_file',args:{}}}};
  x.hook.applyDetail(snapshot);
  assert.match(x.sources.at(-1).url,/since=attempt%3A40/);
  assert.equal(x.state.subagentProgress.working.text,'durable');
  assert.equal(x.state.subagentQueued.queued,true);
  assert.equal(x.state.subagentErrors.failed,'Provider unavailable');
  assert.equal(x.state.subagentStopNotices.failed,'Stopped');
  assert.equal(x.state.approvals.length,2);
  assert.equal(x.state.approvals[0].decision,'deny');
  assert.equal(x.state.approvals[1].decision,null);
  assert.equal(x.state.approvals[1].seq,3.5);
  x.sources.at(-1).emit('subagent_progress',{child_id:'working',kind:'text',text:' duplicate',
    message:{id:'child-message',stream_sequence:3}},'attempt:41');
  assert.equal(x.state.subagentProgress.working.text,'durable');
  x.sources.at(-1).emit('subagent_progress',{child_id:'working',kind:'text',text:' next',
    message:{id:'child-message',stream_sequence:4}},'attempt:42');
  assert.equal(x.state.subagentProgress.working.text,'durable next');
  x.hook.applyDetail({...snapshot,run_event_cursors:{'run-A':'attempt:42'},
    subagent_observations:[{child_id:'working',progress:{text:'durable next',message_id:'child-message',stream_sequence:4}}]},
    {preservePresentation:true});
  assert.equal(x.state.subagentProgress.working.text,'durable next');
  x.sources.at(-1).emit('subagent_progress',{child_id:'working',kind:'text',text:' gap',
    message:{id:'child-message',stream_sequence:6}},'attempt:43');
  assert.equal(x.sources.at(-1).closed,true);
  assert.equal(x.requests.length,1);x.cleanup();
});

test('historical unresolved gates remain visible without inventing a decision or accepting input',async()=>{
  const x=await setup();x.hook.applyDetail({...detail('A'),subagent_observations:[],
    approval_events:[{id:'old-gate',approval_id:'old-gate',run_id:'finished-run',tool_name:'write_file',args:{},decision:null,seq:2.5}]});
  assert.equal(x.state.approvals.length,1);
  assert.equal(x.state.approvals[0].decision,null);
  assert.equal(x.state.approvals[0].actionable,false);x.cleanup();
});

test('an accepted approval cannot reopen while the worker still exposes its pending gate',async()=>{
  const x=await setup();x.hook.applyDetail({...detail('A',true),subagent_observations:[],
    active_run:{id:'run-A',status:'running',pending_approval:{approval_id:'gate',tool_name:'write_file',args:{}}},
    approval_events:[{id:'gate',approval_id:'gate',run_id:'run-A',decision:'allow',actionable:false,seq:2.5}]});
  assert.equal(x.state.approvals.length,1);
  assert.equal(x.state.approvals[0].decision,'allow');assert.equal(x.state.approvals[0].actionable,false);x.cleanup();
});

test('a pending snapshot accepts first-attempt events from a legacy stream without discarding them',async()=>{
  const x=await setup();x.hook.applyDetail({...detail('A',true),subagent_observations:[],run_event_cursors:{'run-A':'pending:0'}});
  const stream=x.sources.at(-1);
  stream.emit('status',{id:'started',status:'running',message:'Started'},'first:1');
  stream.emit('approval_requested',{approval_id:'gate',tool_name:'write_file',args:{}},'first:2');
  assert.equal(x.state.approvals[0].actionable,true);
  stream.emit('code_step',step('first-code',1),'first:3');
  assert.equal(x.state.codeSteps[0].id,'first-code');x.cleanup();
});
