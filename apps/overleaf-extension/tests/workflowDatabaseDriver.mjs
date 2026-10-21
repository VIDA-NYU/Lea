// Real API/SQLite workflow; pytest supplies a deterministic prover and crash gate.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import path from 'node:path';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { openWorkerDatabase } from '../companion/databaseState.mjs';
import { runApiProofJob } from '../companion/leaApiClient.mjs';
import { setWorkerDatabase, applyProofOutcomeToJob, reconcileRecoveredBatches, handleGetStatuses } from '../companion/workerService.mjs';
const input=JSON.parse(readFileSync(0,'utf8'));
const database=openWorkerDatabase(input);setWorkerDatabase(database);
let publicationGate=false;
const state={settings:{leaRepoPath:input.leaRoot,leaApiBaseUrl:input.baseUrl},
  jobsPath:'/__sql__/jobs.json',repairBatchesPath:'/__sql__/jobs.json.batches.json',
  jobs:database.get('jobs.json',{}),fetchImpl:async(url,options)=>{
    const response=await fetch(url,options);
    if(!publicationGate&&String(url).includes('/target-status')) {
      const body=await response.text();publicationGate=true;
      const held=new DatabaseSync(input.filename);held.exec('begin exclusive');
      setTimeout(()=>{held.exec('commit');held.close();},800);
      return new Response(body,{status:response.status,headers:response.headers});
    }
    return response;
  },env:{}};
try {
  await database.runAccepted(input.parent,async()=>{
    if(!state.jobs.first)state.jobs.first={...input.first,acceptedCommandId:input.parent};
    const labels=['next','independent','false_claim','dependent'];
    for(const label of labels){
      const job=state.jobs[label] ||= {jobId:label,jobKey:`observed:theorem:${label}`,targetLabel:label,
        targetKind:'theorem',overleafProjectId:'observed',projectSlug:'observed',projectNamespace:'Lea.Observed',
        acceptedCommandId:input.parent,status:'in_progress',startedAt:new Date().toISOString(),logPath:`/__sql__/${label}.log`,
        request:{message:`Formalize ${label}`,projectSlug:'observed',idempotencyKey:`workflow-${label}`,
          newFormalization:{display_title:label,declaration_name:label,kind:'theorem',origin:'overleaf',origin_key:`observed:theorem:${label}`}}};
      if(job.finishedAt)continue;
      job.workflowPhase='admitting';database.put('jobs.json',state.jobs);
      const exit=await runApiProofJob({...job.request,fetchImpl:fetch,baseUrl:input.baseUrl,timeoutMs:10000,busyRetryDelayMs:1,
        onAdmissionWait:async failure=>{
          job.admission={state:'waiting',...failure};database.put('jobs.json',state.jobs);
          if(input.phase==='crash')throw new Error('simulated process exit');
        },onRunStarted:async(runId,sessionId,body)=>{
          Object.assign(job,{apiRunId:runId,leaSessionId:sessionId,formalizationId:body.focus_formalization_id,
            workflowPhase:'executing',admission:{state:'accepted',runId}});
          database.put('jobs.json',state.jobs);
          await fetch(input.baseUrl+'/test/release',{method:'POST'});
        }});
      assert.equal(exit.ok,true,exit.error);
      const target={targetKind:'theorem',targetLabel:label,overleafProjectId:'observed',projectSlug:'observed',
        projectNamespace:'Lea.Observed',projectMarkdownPath:path.join(input.leaRoot,'workspace/projects/observed.md')};
      await applyProofOutcomeToJob({state,job,target,exit});
      assert.equal(job.certificate.validity,label==='false_claim'?'disproved':'proved');
    }
    // The recovered batch consumes durable job outcomes, never HTTP failures.
    state.repairBatches={workflow:{batchId:'formalize-batch-workflow',operation:'formalize',overleafProjectId:'observed',
      acceptedCommandId:input.parent,createdAt:state.jobs.first.startedAt,done:false,running:false,
      usesByLabel:new Map([['dependent',['next']]]),items:Object.values(state.jobs).map(j=>({targetKind:'theorem',
        targetLabel:j.targetLabel,state:'running',runJobId:j.jobId}))}};
    const retained=database.get('jobs.json.batches.json',{}).workflow;
    if(retained?.done)state.repairBatches.workflow={...retained,usesByLabel:new Map(retained.usesByLabel||[]),reportSubmission:null};
    if(input.phase==='seal'||input.phase==='stale-report'){
      const batch=state.repairBatches.workflow;batch.done=true;
      for(const entry of batch.items)entry.state=state.jobs[entry.runJobId].status;
      if(input.phase==='stale-report'){
        delete batch.reportId;batch.reportState='save_failed';batch.reportError='HTTP 403: A private worker credential is required';
      }
      database.put('jobs.json.batches.json',{workflow:{...batch,usesByLabel:[...batch.usesByLabel]}});
      process.stdout.write(JSON.stringify({sealed:true}));return;
    }
    for(let i=0;i<5;i++)await reconcileRecoveredBatches(state);
    await state.repairBatches.workflow.reportSubmission;
    const statuses=await handleGetStatuses({overleafProjectId:'observed',targets:Object.values(state.jobs).map(j=>({
      targetKind:'theorem',targetLabel:j.targetLabel,targetText:'True.'}))},state);
    assert.equal(statuses.statusCode,200);
    for(const name of ['result','next','independent','dependent'])assert.equal(statuses.body.statuses[`theorem:${name}`].status,'formalized');
    assert.equal(statuses.body.statuses['theorem:false_claim'].status,'disproved');
    // Execute the production standalone stores against the same SQL snapshot.
    const require=createRequire(new URL('../../lea-standalone/package.json',import.meta.url));
    const {build}=require('esbuild');
    const appDir=new URL('../../lea-standalone/src/app/',import.meta.url).pathname;
    const bundle=await build({stdin:{contents:"export {useSessions} from './stores/sessions';export {useProjects} from './stores/projects';",resolveDir:appDir},
      bundle:true,write:false,platform:'node',format:'cjs',external:['zustand']});
    const context={module:{exports:{}},require,fetch:(url,options)=>fetch(new URL(url,input.baseUrl),options),
      setTimeout,clearTimeout,AbortController,URL,crypto:globalThis.crypto};context.exports=context.module.exports;
    vm.runInNewContext(bundle.outputFiles[0].text,context);
    const {useSessions,useProjects}=context.module.exports;
    assert.equal(useProjects.getState().projects.length,0);
    await useSessions.getState().refreshSessions();
    assert.equal(useProjects.getState().projects.some(p=>p.id===input.projectId),true);
    assert.equal(useSessions.getState().sessions.filter(s=>s.project_id===input.projectId).length,5);
    process.stdout.write(JSON.stringify({jobs:state.jobs,batch:state.repairBatches.workflow.done,uis:true,
      reportId:state.repairBatches.workflow.reportId,reportError:state.repairBatches.workflow.reportError}));
  });
}catch(error){
  if(input.phase==='crash'&&error.message==='simulated process exit')process.stdout.write(JSON.stringify({retained:state.jobs.next}));
  else throw error;
}finally{setWorkerDatabase(null);database.close();}
