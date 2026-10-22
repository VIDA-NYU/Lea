import assert from 'node:assert/strict';
import test from 'node:test';
import {build} from 'esbuild';
import vm from 'node:vm';
import {createRequire} from 'node:module';
const bundle=build({entryPoints:[new URL('./stores/projects.ts',import.meta.url).pathname],bundle:true,write:false,platform:'node',format:'cjs',external:['zustand'],
  plugins:[{name:'project-api',setup(b){
    b.onResolve({filter:/^\.\.\/lib\/api$/},()=>({path:'api',namespace:'fake'}));
    b.onLoad({filter:/.*/,namespace:'fake'},()=>({contents:'exports.getProject=id=>globalThis.request(id);exports.createProject=()=>globalThis.createProject();'}));
  }}]});
async function setup(){
  const requests=[];let create;
  const context={module:{exports:{}},require:createRequire(import.meta.url),
    request:id=>new Promise((resolve,reject)=>requests.push({id,resolve,reject})),createProject:()=>new Promise(resolve=>{create=resolve;})};
  context.exports=context.module.exports;context.globalThis=context;
  vm.runInNewContext((await bundle).outputFiles[0].text,context);
  return {store:context.module.exports.useProjects,requests,created:project=>create(project)};
}
const flush=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
for(const action of ['closeProject','invalidateNavigation'])test(`a delayed project open expires on ${action}`,async()=>{
  const x=await setup();const pending=x.store.getState().openProject('A');
  x.store.getState()[action]();x.requests[0].resolve({id:'A',sessions:[]});
  assert.equal(await pending,null);assert.equal(x.store.getState().currentProject,undefined);
});
test('A to B to A navigation accepts only the newest A request',async()=>{
  const x=await setup();const a=x.store.getState().openProject('A'),b=x.store.getState().openProject('B'),fresh=x.store.getState().openProject('A');
  x.requests[2].resolve({id:'A',title:'latest',sessions:[]});await fresh;
  x.requests[0].resolve({id:'A',title:'obsolete',sessions:[]});x.requests[1].resolve({id:'B',sessions:[]});
  assert.deepEqual(await Promise.all([a,b]),[null,null]);assert.equal(x.store.getState().currentProject.title,'latest');
});
test('global session pages cannot erase scoped membership and refreshes coalesce',async()=>{
  const x=await setup();const open=x.store.getState().openProject('A');
  x.requests[0].resolve({id:'A',sessions:[{id:'older-project-chat'}],cursor:1});await open;
  x.store.getState().applySnapshot([{id:'A',title:'renamed'}],Array.from({length:100},(_,i)=>({id:`other-${i}`})));
  x.store.getState().applySnapshot([],[]);
  assert.equal(x.store.getState().currentProject.sessions[0].id,'older-project-chat');
  assert.equal(x.requests.length,2);
  x.requests[1].resolve({id:'A',sessions:[{id:'scoped-chat'}],cursor:2});await flush();
  assert.equal(x.requests.length,3);
  x.requests[2].resolve({id:'A',sessions:[{id:'scoped-chat'}],cursor:3});await flush();
  assert.equal(x.store.getState().currentProject.sessions[0].id,'scoped-chat');
  assert.equal(x.store.getState().currentProject.cursor,3);
  const deleted=x.store.getState().refreshCurrentProject();
  x.requests[3].reject(Object.assign(new Error('deleted'),{status:404}));await deleted;
  assert.equal(x.store.getState().currentProjectDeleted,true);
  assert.equal(x.store.getState().selectedProjectId,'A');
});
test('project creation cannot reopen a window after another navigation',async()=>{
  const x=await setup();const pending=x.store.getState().createAndOpen('Study');
  x.store.getState().invalidateNavigation();x.created({id:'created'});
  assert.equal(await pending,null);assert.equal(x.requests.length,0);
  assert.equal(x.store.getState().projects[0].id,'created');
});

test('an invalidation received while opening cannot be lost before scoped hydration',async()=>{
  const x=await setup();const pending=x.store.getState().openProject('A');
  x.store.getState().applySnapshot([{id:'A'}],[]);
  x.requests[0].resolve({id:'A',sessions:[{id:'obsolete'}],cursor:10});await flush();
  assert.equal(x.requests.length,2);
  assert.equal(x.store.getState().currentProject,undefined);
  x.requests[1].resolve({id:'A',sessions:[{id:'new'}],cursor:11});await pending;
  assert.equal(x.store.getState().currentProject.sessions[0].id,'new');
});
