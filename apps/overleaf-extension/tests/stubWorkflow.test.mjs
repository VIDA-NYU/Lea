import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {handleStub,handleStubAll,handleBatchCancel,handleGetStatuses,handleFormalize,reconcileRegistryUpdates,recoverInterruptedJobs} from '../companion/workerService.mjs';
import {stubDeclaration} from '../companion/stubArtifact.mjs';
async function until(predicate) {
  const end=Date.now()+5000;
  while(!predicate()){assert.ok(Date.now()<end,'workflow did not settle');await new Promise(resolve=>setTimeout(resolve,5));}
}
const target=label=>({overleafProjectId:'project-1',targetKind:'theorem',targetLabel:label,targetText:'True is true.'});
async function fixture(t,{codeFor,stepFor,observeStep,beforeStart,beforeEvents,doneStatus}={}){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'lea-stub-workflow-'));
  t.after(()=>fs.rm(root,{recursive:true,force:true,maxRetries:10,retryDelay:20}));
  const repo=path.join(root,'prover');
  await fs.mkdir(path.join(repo,'workspace/projects'),{recursive:true});
  await fs.mkdir(path.join(repo,'workspace/proofs/Lea/Project1'),{recursive:true});
  await fs.writeFile(path.join(repo,'pyproject.toml'),'[project]\nname="stub-fixture"\n');
  await fs.writeFile(path.join(repo,'workspace/lakefile.lean'),'import Lake\nopen Lake DSL\n');
  await fs.copyFile(new URL('../../lea-standalone/prover/workspace/lean-toolchain',import.meta.url),path.join(repo,'workspace/lean-toolchain'));
  const calls=[],runs=new Map();
  const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
  const fetchImpl=async(url,options={})=>{
    const u=String(url),body=options.body?JSON.parse(options.body):null;
    calls.push({url:u,body,method:options.method});
    if(u.endsWith('/api/health'))return json({capabilities:{lea_status:{version:1,admission_enabled:true,source_pause_policy:1}}});
    if(u.endsWith('/api/settings'))return json({},404);
    if(u.endsWith('/identity'))return json({slug:'project-1',projectName:'Project 1',namespace:'Lea.Project1',exists:true});
    if(u.endsWith('/api/runs')&&options.method==='POST'){
      const label=body.new_formalization?.declaration_name||body.focus_formalization_id?.replace('form-','')||'a';
      const id='run-'+(runs.size+1),sid=body.session_id||'session-'+(runs.size+1);
      const stub=body.purpose==='overleaf_stub',status=doneStatus||(stub?'answered':'proved');
      const code=codeFor?.(label)||`namespace Lea.Project1\ntheorem ${label} : True := by\n  ${stub?'sorry':'trivial'}\nend Lea.Project1\n`;
      const step={id:'step-'+id,seq:runs.size+1,run_id:id,path:label+'.lean',code,check_status:'ok',check_current:true,...stepFor?.(label)};
      runs.set(id,{id,sid,label,step,stub,status});
      await fs.writeFile(path.join(repo,'workspace/proofs/Lea/Project1',step.path),code);
      await beforeStart?.({id,label});
      return json({run_id:id,session_id:sid,project_id:'project',project_slug:'project-1',project_namespace:'Lea.Project1',focus_formalization_id:'form-'+label});
    }
    const event=u.match(/\/api\/runs\/([^/]+)\/events/);
    if(event){await beforeEvents?.(runs.get(event[1]));return new Response(`event: done\ndata: ${JSON.stringify({status:runs.get(event[1]).status})}\n\n`,{headers:{'Content-Type':'text/event-stream'}});}
    if(u.includes('/interrupt'))return json({status:'interrupting'});
    if(u.includes('/api/sessions/')){
      const matching=[...runs.values()].filter(run=>u.endsWith('/'+run.sid));
      const latest=matching.at(-1);
      return json({project_namespace:'Lea.Project1',runs:matching.map(run=>({id:run.id,status:run.status,input_tokens:1,output_tokens:1,cost_usd:0})),code_steps:[{...latest.step,...observeStep?.(latest)}]});
    }
    if(u.includes('/target-status'))return json({project_namespace:'Lea.Project1',targets:[...new Map([...runs.values()].map(run=>[run.label,run])).values()].map(run=>({
      declaration_name:run.label,formalization_id:'form-'+run.label,recorded:true,path:run.step.path,exists:true,
      has_sorry:run.stub,validity_status:run.stub?'failing':'proved',completion_run_id:run.stub?null:run.id,declaration_present:true,content:run.step.code,check_status:'ok',check_current:true,session_id:run.sid,projection:{state:'observed'}}))});
    if(u.endsWith('/artifacts'))return json({},404);
    if(/\/api\/runs\/[^/]+$/.test(u))return json({id:u.split('/').at(-1),status:runs.get(u.split('/').at(-1))?.status,usage_status:'final',input_tokens:1,output_tokens:1,cost_usd:0});
    return json({},404);
  };
  const state={settings:{leaRepoPath:repo,leaWorkspacePath:path.join(repo,'workspace'),leaApiBaseUrl:'http://fixture',
    leaModel:'o4-mini',leaMaxTurns:5,leaTexMirrorEnabled:false,leaMaxSpendUsd:null,leaJobTimeoutSeconds:5},
    jobsPath:path.join(root,'jobs.json'),repairBatchesPath:path.join(root,'batches.json'),jobs:{},env:{OPENAI_API_KEY:'fixture'},fetchImpl};
  return {state,calls,repo,runs};
}

test('stub workflow saves the checked statement, reports stub status and reuses its session for Formalize',async t=>{
  const {state,calls}=await fixture(t);
  const result=await handleStub(target('a'),state);
  assert.equal(result.statusCode,200,JSON.stringify(result.body));
  const job=state.jobs[result.body.jobId];
  assert.equal(job.status,'sorry_stub');assert.equal(job.registryUpdate.state,'saved');assert.equal(job.leanStatement,'theorem a : True');
  const request=calls.find(call=>call.url.endsWith('/api/runs')).body;
  assert.equal(request.purpose,'overleaf_stub');assert.ok(request.source_bundle.bundleHash);
  const statuses=await handleGetStatuses({overleafProjectId:'project-1',targets:[target('a')]},state);
  assert.equal(statuses.body.statuses['theorem:a'].status,'sorry_stub');
  await handleFormalize(target('a'),state);
  await until(()=>calls.filter(call=>call.url.endsWith('/api/runs')).length===2);
  const proof=calls.filter(call=>call.url.endsWith('/api/runs'))[1].body;
  assert.equal(proof.session_id,job.leaSessionId);assert.equal(proof.focus_formalization_id,job.formalizationId);
  assert.match(proof.message,/Existing sorry stub to complete/);
  await until(()=>Object.values(state.jobs).every(job=>job.finishedAt));
  const completed=Object.values(state.jobs).find(other=>other!==job);
  assert.equal(completed.status,'formalized',completed.error||completed.resultDetail);
  const finished=await handleGetStatuses({overleafProjectId:'project-1',targets:[target('a')]},state);
  assert.equal(finished.body.statuses['theorem:a'].status,'formalized');
});
for(const [label,code] of [
  ['definition','def a : Prop := True\ntheorem helper : True := by sorry'],
  ['unrelated sorry','theorem a : True := by trivial\ntheorem helper : True := by sorry'],
  ['anonymous helper','theorem a : True := by trivial\nexample : True := by sorry'],
  ['command outside theorem','theorem a : True := by trivial\n#check (sorry : True)'],
  ['comment','/- theorem a : True := by sorry -/\ntheorem helper : True := by sorry'],
  ['string','theorem a : True := by\n  let text := "sorry"\n  trivial'],
  ['prefix',"theorem a' : True := by sorry"],
  ['ambiguous','namespace X\ntheorem a : True := by sorry\nend X\nnamespace Y\ntheorem a : True := by sorry\nend Y']
])test(`stub workflow rejects ${label} instead of accepting another placeholder`,async t=>{
  const {state}=await fixture(t,{codeFor:()=>code});
  const result=await handleStub(target('a'),state);
  assert.equal(result.statusCode,500);assert.equal(result.body.status,'failed');
});
for(const step of [{check_status:'error'},{check_current:false},{run_id:'older-run'}])test(`stub workflow rejects stale evidence ${JSON.stringify(step)}`,async t=>{
  const {state}=await fixture(t,{stepFor:()=>step});assert.equal((await handleStub(target('a'),state)).body.status,'failed');
});
test('stub declaration binds a lemma with binder defaults and nested comments to its own admit body',()=>{
  assert.equal(stubDeclaration('namespace X\n/- outer /- sorry -/ -/\nlemma a (n : Nat := 0) : n = n := by\n  admit\nend X','a').statement,'lemma a (n : Nat := 0) : n = n');
});
test('a completed stub waits for its current observation before publication without rerunning the model',async t=>{
  let reads=0;
  const {state,calls}=await fixture(t,{observeStep:()=>++reads===1
    ?{check_current:false,projection:{state:'inputs_changed'}}
    :{check_current:true,projection:{state:'observed'}}});
  const result=await handleStub(target('a'),state);
  assert.equal(result.statusCode,200,JSON.stringify(result.body));
  assert.ok(reads>=2);assert.equal(state.jobs[result.body.jobId].status,'sorry_stub');
  assert.equal(state.jobs[result.body.jobId].waitingForObservation,undefined);
  assert.equal(calls.filter(call=>call.url.endsWith('/api/runs')).length,1);
});
test('Stub all advances only after each successful stub observation is published',async t=>{
  const reads=new Map();
  const {state}=await fixture(t,{observeStep:run=>{
    const count=(reads.get(run.label)||0)+1;reads.set(run.label,count);
    return {check_current:count>1,projection:{state:count>1?'observed':'capturing'}};
  },beforeStart:({label})=>{if(label==='b')assert.ok(reads.get('a')>=2);}});
  const result=await handleStubAll({overleafProjectId:'project-1',items:[target('a'),target('b')]},state);
  const batch=state.repairBatches[result.body.batchId];await until(()=>batch.done);
  assert.deepEqual(batch.items.map(item=>item.state),['stubbed','stubbed']);
});
test('a persisted stub observation wait reconnects to its completed run after restart',async t=>{
  let recovering=false,reads=0;
  const {state,calls}=await fixture(t,{observeStep:()=>recovering&&++reads===1
    ?{check_current:false,projection:{state:'capturing'}}
    :{check_current:true,projection:{state:'observed'}}});
  const result=await handleStub(target('a'),state);
  const saved=JSON.parse(JSON.stringify(state.jobs));
  const job=saved[result.body.jobId];
  Object.assign(job,{status:'in_progress',finalStatus:null,finishedAt:null,workflowPhase:'observing',waitingForObservation:true});
  delete job.registryUpdate;
  recovering=true;
  const restarted={...state,jobs:saved};await recoverInterruptedJobs(restarted);
  await until(()=>job.status==='sorry_stub'&&job.registryUpdate?.state==='saved');
  assert.ok(reads>=2);assert.equal(job.waitingForObservation,undefined);
  assert.equal(calls.filter(call=>call.url.endsWith('/api/runs')).length,1);
});
test('Stop cancels a stub waiting for asynchronous observations',async t=>{
  const {state}=await fixture(t,{observeStep:()=>({check_current:false,projection:{state:'capturing'}})});
  const result=await handleStubAll({overleafProjectId:'project-1',items:[target('a'),target('b')]},state);
  const batch=state.repairBatches[result.body.batchId];
  await until(()=>Object.values(state.jobs).some(job=>job.waitingForObservation));
  await handleBatchCancel({batchId:batch.batchId},state);await until(()=>batch.done);
  assert.equal(Object.values(state.jobs).length,1);
  assert.equal(Object.values(state.jobs)[0].status,'failed');
  assert.equal(Object.values(state.jobs)[0].workflowPhase,'settled');assert.equal(batch.canceled,true);
});
test('stub publication failure retains success and retries after restart without another run',async t=>{
  const {state,calls,repo}=await fixture(t);const registry=path.join(repo,'workspace/projects/project-1.md');await fs.mkdir(registry);
  const result=await handleStub(target('a'),state);assert.equal(result.statusCode,200);
  assert.equal(state.jobs[result.body.jobId].registryUpdate.state,'pending');
  const saved=JSON.parse(await fs.readFile(state.jobsPath,'utf8'));assert.equal(saved[result.body.jobId].status,'sorry_stub');
  await fs.rm(registry,{recursive:true});await fs.writeFile(registry,'# Project\n\n<!-- lea:theorem name="other" proof="Other.lean" -->\n');
  const restarted={...state,jobs:saved};await reconcileRegistryUpdates(restarted,{now:Date.now()+60000});
  assert.equal(restarted.jobs[result.body.jobId].registryUpdate.state,'saved');
  assert.match(await fs.readFile(registry,'utf8'),/name="other"/);assert.match(await fs.readFile(registry,'utf8'),/name="a"/);
  assert.equal(calls.filter(call=>call.url.endsWith('/api/runs')).length,1);
});
for(const during of ['admission','execution'])test(`Stub all stops during ${during} and never starts the next target`,async t=>{
  let release;const gate=new Promise(resolve=>{release=resolve;});
  const {state,calls}=await fixture(t,{doneStatus:'cancelled',...(during==='admission'?{beforeStart:()=>gate}:{beforeEvents:()=>gate})});
  const result=await handleStubAll({overleafProjectId:'project-1',items:[target('a'),target('b')]},state);
  const batch=state.repairBatches[result.body.batchId];
  try{
    await until(()=>calls.some(call=>call.url.endsWith(during==='admission'?'/api/runs':'/events')));assert.ok(batch.items[0].runJobId);
    await handleBatchCancel({batchId:batch.batchId},state);release();await until(()=>batch.done);
    assert.equal(batch.canceled,true);assert.equal(calls.filter(call=>call.url.endsWith('/interrupt')).length,1);
    assert.equal(calls.filter(call=>call.url.endsWith('/api/runs')).length,1);assert.deepEqual(batch.items.map(item=>item.state),['canceled','canceled']);
  }finally{release();await until(()=>batch.done);}
});
test('Stub all executes sequentially and preserves success beside a rejected artifact',async t=>{
  const {state,calls}=await fixture(t,{codeFor:label=>label==='bad'?'def bad : Prop := True':undefined});
  const result=await handleStubAll({overleafProjectId:'project-1',items:[target('a'),target('bad'),target('b')]},state);
  const batch=state.repairBatches[result.body.batchId];await until(()=>batch.done);
  assert.deepEqual(batch.items.map(item=>item.state),['stubbed','failed','stubbed']);
  assert.deepEqual(calls.filter(call=>call.url.endsWith('/api/runs')||call.url.endsWith('/events')).map(call=>call.url.endsWith('/events')?'settle':'start'),['start','settle','start','settle','start','settle']);
});
