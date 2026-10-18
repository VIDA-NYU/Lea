import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { observeSessionList } from './lib/sessionObserver.mjs';
const flush = async()=>{for(let i=0;i<12;i++)await Promise.resolve();};

test('session observer closes dropped streams, snapshots before resume and ignores retired callbacks',async()=>{
  let cursor=4, refreshes=0, changes=0;
  const sources=[],timers=new Map();
  const stop=observeSessionList({refresh:async()=>{refreshes++;cursor++;},readCursor:()=>cursor,
    onChanged:()=>changes++,createSource:url=>{const s={url,listeners:{},close(){this.closed=true;},
      addEventListener(k,fn){this.listeners[k]=fn;}};sources.push(s);return s;},
    setTimer:fn=>{timers.set(1,fn);return 1;},clearTimer:id=>timers.delete(id)});
  await flush();assert.equal(sources[0].url,'/api/sessions/events?after=5');
  sources[0].listeners.sessions_changed();await flush();
  assert.equal(changes,1);assert.equal(refreshes,2);
  sources[0].onerror();assert.equal(sources[0].closed,true);
  timers.get(1)();timers.clear();await flush();
  assert.equal(sources.length,2);assert.equal(sources[1].url,'/api/sessions/events?after=7');
  sources[0].listeners.sessions_changed();sources[0].onerror();await flush();
  assert.equal(refreshes,3);assert.equal(timers.size,0);
  stop();sources[1].listeners.sessions_changed();await flush();
  assert.equal(sources[1].closed,true);assert.equal(refreshes,3);
});

test('session store rejects reordered responses and preserves user selection across reset watermarks',async()=>{
  const requests=[];
  const bundled=await build({entryPoints:[new URL('./stores/sessions.ts',import.meta.url).pathname],
    bundle:true,write:false,platform:'node',format:'cjs',external:['zustand'],
    plugins:[{name:'snapshot',setup(b){
      b.onResolve({filter:/^\.\.\/lib\/api$/},()=>({path:'api',namespace:'fake'}));
      b.onLoad({filter:/.*/,namespace:'fake'},()=>({contents:'exports.sessionListSnapshot=()=>globalThis.snapshot();'}));
    }}]});
  const context={module:{exports:{}},require:createRequire(import.meta.url),
    snapshot:()=>new Promise(resolve=>requests.push(resolve))};
  context.exports=context.module.exports;context.globalThis=context;
  vm.runInNewContext(bundled.outputFiles[0].text,context);
  const store=context.module.exports.useSessions;
  store.getState().setSelectedSessionId('user-choice');
  const first=store.getState().refreshSessions(),second=store.getState().refreshSessions();
  requests[1]({sessions:[{id:'new'}],cursor:30});await second;
  requests[0]({sessions:[{id:'old'}],cursor:10});await first;
  assert.equal(store.getState().sessions[0].id,'new');
  const reset=store.getState().refreshSessions();requests[2]({sessions:[{id:'restored'}],cursor:2});await reset;
  assert.equal(store.getState().cursor,2);
  assert.equal(store.getState().sessions[0].id,'restored');
  assert.equal(store.getState().selectedSessionId,'user-choice');
});
