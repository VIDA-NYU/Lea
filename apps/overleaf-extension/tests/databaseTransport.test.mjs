import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtemp,readdir,rm,readFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createServer} from '../companion/server.mjs';
import {createSourcePublisher} from '../extension/sourcePublisher.mjs';
import {commandFetch,observeCommand} from '../extension/commandClient.mjs';

test('transport runs without a workspace or writable job ledger and preserves command identity',async()=>{
  const root=await mkdtemp(path.join(os.tmpdir(),'lea-transport-'));
  const calls=[];let lost=true;
  const receipt={workspace_command:true,command:{id:'accepted',state:'succeeded',result:{statusCode:200,body:{jobId:'job',status:'in_progress'}}}};
  const server=await createServer({jobsPath:path.join(root,'forbidden/jobs.json'),env:{LEA_API_BASE_URL:'http://127.0.0.1:1'},fetchImpl:async(url,options)=>{
    calls.push({url,body:options.body&&JSON.parse(options.body)});
    if(url.endsWith('/commands')&&lost){lost=false;throw new Error('Response lost after commit');}
    if(url.includes('/receipts?'))return Response.json(receipt);
    if(url.endsWith('/api/health'))return Response.json({ok:true,capabilities:{database_boundary:{version:1,
      ui_access:'database',command_direction:'ui_to_worker',processes:'separate',
      queries:1,changes:1,commands:1,source_publications:1,editor:1}}});
    throw new Error('Unexpected route '+url);
  }});
  try{
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const url='http://127.0.0.1:'+server.address().port;
    const response=await fetch(url+'/formalize',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':'original'},body:JSON.stringify({overleafProjectId:'document'})});
    assert.deepEqual(await response.json(),{jobId:'job',status:'in_progress'});
    assert.ok(calls[0].url.endsWith('/api/health'),'Negotiate before admitting business work');
    assert.equal(calls.find(call=>call.body)?.body.idempotency_key,'original');
    assert.ok(calls.some(call=>/idempotency_key=original/.test(call.url)));
    assert.deepEqual(await readdir(root),[]);
    assert.equal((await (await fetch(url+'/health')).json()).transportOnly,true);
    assert.equal((await fetch(url+'/source-context/respond',{method:'POST'})).status,410);
  }finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));await rm(root,{recursive:true,force:true});}
});

test('public transport has no engine, workspace, storage or supervisor dependencies',async()=>{
  const source=await readFile(new URL('../companion/server.mjs',import.meta.url),'utf8');
  assert.doesNotMatch(source,/node:(?:fs|path|child_process)|workerService|setInterval|jobsPath|leaRepoPath|spawn\(/);
});

test('runtime status reads SQL directly and distinguishes missing observations from compiler failures',async()=>{
  const calls=[];
  let phase='pending';
  const server=await createServer({fetchImpl:async(url,options)=>{
    calls.push({url,method:options.method});
    if(url.endsWith('/api/health'))return Response.json({capabilities:{database_boundary:{version:1,
      ui_access:'database',command_direction:'ui_to_worker',processes:'separate',
      queries:1,changes:1,commands:1,source_publications:1,editor:1}}});
    assert.ok(url.endsWith('/api/sessions/session/lean-check-runtime'));
    if(phase==='pending')return Response.json({detail:{error:'projection_unavailable',message:'Not published yet'}},{status:503});
    if(phase==='offline')throw new Error('Status connection lost');
    if(phase==='expired')return Response.json({state:'unavailable',projection:{state:'observation_expired'}});
    return Response.json({state:phase,last_failure:null,projection:{state:'observed'}});
  }});
  try{
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const url='http://127.0.0.1:'+server.address().port+'/lean-pane/check-runtime/session';
    for(const [value,message] of [['pending','Waiting for Lean runtime status'],['offline','Lean runtime status unavailable · retrying'],
      ['expired','Lean runtime status unavailable · retrying'],['unavailable','Lean check runtime unavailable'],['ready','']]){
      phase=value;
      const response=await fetch(url),body=await response.json();
      assert.equal(response.status,200);
      assert.equal(body.message,message);
      assert.equal(body.polling,value!=='ready');
      assert.equal(Boolean(body.runtime.observation),['pending','offline','expired'].includes(value));
    }
    assert.ok(calls.every(call=>call.method==='GET'),'Observation must never submit a command or compiler check');
  }finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});

test('project identity transport retains the UI envelope and the exact database namespace',async()=>{
  const calls=[];
  let phase='missing';
  const identity={exists:true,projectId:'sql-project',slug:'document',overleafProjectId:'document',
    projectName:'FailInduce1',nameSource:'overleaf',namespace:'Lea.FailInduce1OnlineLaTeXEditorOverleaf',
    namespaceRevision:7,renameInProgress:false,hasRecordedProofs:true,repoPath:'proofs/Lea/FailInduce1OnlineLaTeXEditorOverleaf'};
  const server=await createServer({fetchImpl:async(url,options)=>{
    calls.push({url,method:options.method});
    if(url.endsWith('/api/health'))return Response.json({capabilities:{database_boundary:{version:1,
      ui_access:'database',command_direction:'ui_to_worker',processes:'separate',
      queries:1,changes:1,commands:1,source_publications:1,editor:1}}});
    assert.ok(url.endsWith('/api/projects/by-slug/document/identity'));
    if(phase==='missing')return Response.json({detail:'No Lea project exists for this document yet.'},{status:404});
    if(phase==='offline')return Response.json({detail:'Database temporarily unavailable'},{status:503});
    return Response.json({...identity,renameInProgress:phase==='renaming'});
  }});
  try{
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const url='http://127.0.0.1:'+server.address().port+'/project/identity?overleafProjectId=document';
    let response=await fetch(url),value=await response.json();
    assert.equal(response.status,200);
    assert.equal(value.identity.exists,false);
    assert.equal(value.identity.namespace,null,'Do not invent a namespace before SQL creation');
    phase='created';
    response=await fetch(url);value=await response.json();
    assert.equal(response.status,200);
    assert.deepEqual(value,{ok:true,identity});
    phase='renaming';value=await (await fetch(url)).json();
    assert.equal(value.identity.renameInProgress,true);
    assert.equal(value.identity.namespaceRevision,7);
    phase='offline';response=await fetch(url);value=await response.json();
    assert.equal(response.status,503);
    assert.equal(value.identity,undefined,'An unavailable read cannot claim the project does not exist');
    assert.ok(calls.every(call=>call.method==='GET'),'Header reads cannot create or rename a project');
  }finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});

test('publisher serializes targets, retries identical bytes and renews without retaining duplicate file strings',async()=>{
  const calls=[];let lose=true;let sequence=0;let now=0;
  const publish=createSourcePublisher({documentId:()=> 'document',clientId:'tab',clock:()=>now,request:async(route,value)=>{
    calls.push({route,value});
    if(route.endsWith('/publisher'))return {generation:1,sequence};
    if(lose){lose=false;throw new Error('response lost');}
    sequence=value.sequence;return {source_context_ref:'p'+sequence};
  }});
  const input={source_bundle:{targetKind:'theorem',targetKey:'a'},files:{'main.tex':'current text'}};
  const [first,second]=await Promise.all([publish(input),publish(input)]);
  assert.deepEqual(first,second);
  assert.equal(calls.length,3);assert.deepEqual(calls[1].value,calls[2].value);
  now=31000;await publish(input);assert.equal(calls.at(-1).value.sequence,2);
});

test('a browser mutation retries with the original idempotency key after response loss',async()=>{
  const keys=[];
  const result=await commandFetch(async(url,options)=>{
    keys.push(options.headers.get('Idempotency-Key'));
    if(keys.length===1)throw new TypeError('network lost');
    return 'original receipt';
  },'http://localhost/formalize',{method:'POST',body:'same bytes'});
  assert.equal(result,'original receipt');assert.equal(keys.length,2);assert.equal(keys[0],keys[1]);
});

test('explicit review refresh renews the publisher even when identical source is cached', async () => {
  const calls = [];
  let sequence = 0;
  const publish = createSourcePublisher({ documentId: () => 'document', clientId: 'tab', clock: () => 0,
    request: async (route, value) => {
      calls.push({ route, value });
      if (route.endsWith('/publisher')) return { generation: 1, sequence };
      sequence = value.sequence;
      return { source_context_ref: 'review-source-' + sequence };
    } });
  const input = { source_bundle: { targetKind: 'definition', targetKey: 'stable-id' }, files: { 'main.tex': 'Current definition.' } };
  const first = await publish(input);
  assert.deepEqual(await publish(input), first);
  const renewed = await publish(input, { force: true });
  assert.notEqual(renewed.source_context_ref, first.source_context_ref);
  assert.equal(calls.filter(call => call.route.endsWith('/publisher')).length, 2);
  assert.equal(calls.at(-1).value.source_bundle.targetKey, 'stable-id');
});

test('an unacknowledged browser mutation does not claim durable acceptance',async()=>{
  await assert.rejects(commandFetch(async()=>{throw new TypeError('offline');},'http://localhost/formalize',{
    method:'POST',headers:{'Idempotency-Key':'retained-intent'},body:'same bytes'
  }),error=>{
    assert.equal(error.commandKey,'retained-intent');
    assert.match(error.message,/Acceptance is unconfirmed/);
    assert.doesNotMatch(error.message,/original request is retained/);
    return true;
  });
});

test('batch async admission returns a SQL receipt without waiting for any worker',async()=>{
  const calls=[];
  const receipt={workspace_command:true,command:{id:'queued-batch-command',state:'queued'}};
  const server=await createServer({fetchImpl:async(url,options)=>{
    calls.push({url,options});
    if(url.endsWith('/api/health'))return Response.json({capabilities:{database_boundary:{version:1,
      ui_access:'database',command_direction:'ui_to_worker',processes:'separate',
      queries:1,changes:1,commands:1,source_publications:1,editor:1}}});
    if(url.endsWith('/api/companion/commands'))return Response.json(receipt,{status:202});
    if(url.endsWith('/api/companion/commands/queued-batch-command'))return Response.json(receipt);
    throw new Error('Unexpected execution/observation '+url);
  }});
  try{
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const base='http://127.0.0.1:'+server.address().port;
    const preflight=await fetch(base+'/formalize/all',{method:'OPTIONS',headers:{Origin:'https://www.overleaf.com'}});
    assert.match(preflight.headers.get('Access-Control-Allow-Headers'),/Prefer/);
    for(const endpoint of ['/formalize/all','/stub/all','/lean-pane/manifest','/statuses']){
      const response=await fetch(base+endpoint,{method:'POST',headers:{'Content-Type':'application/json',Prefer:'respond-async','Idempotency-Key':endpoint},
        body:JSON.stringify({overleafProjectId:'document',items:[{source_context_ref:'frozen'}]})});
      assert.equal(response.status,202);
      assert.deepEqual(await response.json(),receipt);
    }
    assert.equal(calls.filter(call=>call.url.endsWith('/queued-batch-command')).length,0);
    const submissions=calls.filter(call=>call.url.endsWith('/api/companion/commands')).map(call=>JSON.parse(call.options.body));
    assert.deepEqual(submissions.map(value=>value.idempotency_key),['/formalize/all','/stub/all','/lean-pane/manifest','/statuses']);
    assert.equal(submissions[0].payload.items[0].source_context_ref,'frozen');
    assert.deepEqual(await (await fetch(base+'/commands/queued-batch-command')).json(),receipt);
  }finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});

test('a queued read exceeding 58 seconds observes its original receipt through transient outages',async()=>{
  let now=0,reads=0,queued=0;
  const receipt={workspace_command:true,command:{id:'inventory',state:'queued'}};
  const result=await observeCommand(receipt,async id=>{
    assert.equal(id,'inventory');reads++;
    if(reads===2)throw Object.assign(new Error('API restarted'),{status:503});
    if(reads===3)throw Object.assign(new Error('Read deadline exceeded'),{status:408});
    return now<59000?receipt:{workspace_command:true,command:{id,state:'succeeded',result:{statusCode:200,body:{items:[1,2,3]}}}};
  },{clock:()=>now,wait:async ms=>{now+=ms;},onQueued:()=>queued++});
  assert.deepEqual(result,{statusCode:200,body:{items:[1,2,3]}});
  assert.equal(queued,1);assert.equal(reads,59);
});

test('a terminal query failure and an observation deadline preserve command identity',async()=>{
  const receipt={workspace_command:true,command:{id:'original-query',state:'failed',error:'worker failure',error_status:409}};
  await assert.rejects(observeCommand(receipt,()=>assert.fail('Terminal receipt must not be read again')),
    error=>error.commandId==='original-query'&&error.status===409);
  let now=0;
  receipt.command.state='queued';
  await assert.rejects(observeCommand(receipt,async()=>receipt,{clock:()=>now,wait:async()=>{now+=1000;},timeoutMs:1500}),
    error=>error.commandId==='original-query'&&/receipt is retained/.test(error.message));
});

test('a reloaded tab learns the current generation but cannot take over a live publisher',async()=>{
  let live=true,now=0;const claims=[],publications=[];
  const publish=createSourcePublisher({documentId:()=> 'document',clientId:'reloaded',clock:()=>now,request:async(route,value)=>{
    if(route.endsWith('/publisher')) {
      claims.push(value.expected_generation);
      if(live||value.expected_generation!==7)throw Object.assign(new Error('publisher conflict'),{status:409,detail:{code:'publisher_conflict',generation:7}});
      return {generation:8,sequence:0};
    }
    publications.push(value);return {source_context_ref:'new-generation'};
  }});
  const input={source_bundle:{targetKind:'theorem',targetKey:'a'},files:{}};
  await assert.rejects(publish(input),/publisher conflict/);
  assert.deepEqual(claims,[0,7]);assert.equal(publications.length,0);
  live=false;now=1001;assert.equal((await publish(input)).source_context_ref,'new-generation');
  assert.equal(publications[0].generation,8);assert.equal(publications[0].sequence,1);
});

test('one live-lease conflict suppresses all target claims until SQL lease expiry',async()=>{
  let now=0,sequence=0;const claims=[],publications=[];
  const publish=createSourcePublisher({documentId:()=> 'document',clientId:'reloaded',clock:()=>now,request:async(route,value)=>{
    if(route.endsWith('/publisher')) {
      claims.push(value.expected_generation);
      if(now<90000)throw Object.assign(new Error('Other publisher is live'),{status:409,
        detail:{code:'publisher_conflict',generation:7,retry_after_ms:90000-now}});
      assert.equal(value.expected_generation,sequence?8:7);
      return {generation:8,sequence};
    }
    sequence=value.sequence;publications.push(value);return {source_context_ref:'p'+sequence};
  }});
  const input=i=>({document_id:'document',source_bundle:{targetKind:'theorem',targetKey:'t'+i},files:{}});
  const blocked=await Promise.allSettled(Array.from({length:9},(_,i)=>publish(input(i))));
  assert.ok(blocked.every(result=>result.status==='rejected'&&result.reason.retryAfterMs===90000));
  assert.deepEqual(claims,[0]);assert.equal(publications.length,0);
  now=45000;
  await assert.rejects(publish(input(0)),error=>error.code==='publisher_conflict'&&error.retryAfterMs===45000);
  assert.deepEqual(claims,[0]);
  now=90000;
  await Promise.all(Array.from({length:9},(_,i)=>publish(input(i))));
  assert.equal(claims[1],7);
  assert.equal(publications.length,9);
  assert.deepEqual(publications.map(value=>value.sequence),[1,2,3,4,5,6,7,8,9]);
  assert.ok(publications.every(value=>value.generation===8&&value.client_id==='reloaded'));
});

test('an expired generation is learned and claimed immediately without waiting',async()=>{
  const claims=[];
  const publish=createSourcePublisher({documentId:()=> 'document',clientId:'reloaded',request:async(route,value)=>{
    if(route.endsWith('/publisher')) {
      claims.push(value.expected_generation);
      if(value.expected_generation===0)throw Object.assign(new Error('Previous generation'),{status:409,
        detail:{code:'publisher_conflict',generation:3,retry_after_ms:0}});
      return {generation:4,sequence:0};
    }
    return {source_context_ref:'current'};
  }});
  assert.equal((await publish({source_bundle:{targetKind:'theorem',targetKey:'a'},files:{}})).source_context_ref,'current');
  assert.deepEqual(claims,[0,3]);
});

test('losing the publisher lease invalidates recently cached receipts across targets',async()=>{
  let lost=false,claims=0;
  const publish=createSourcePublisher({documentId:()=> 'document',clock:()=>0,request:async(route)=>{
    if(route.endsWith('/publisher')) {
      claims++;
      if(lost)throw Object.assign(new Error('New owner'),{status:409,detail:{code:'publisher_conflict',generation:2,retry_after_ms:90000}});
      return {generation:1,sequence:0};
    }
    return {source_context_ref:'old-receipt'};
  }});
  const input=key=>({source_bundle:{targetKind:'theorem',targetKey:key},files:{}});
  await publish(input('a'));
  lost=true;
  await assert.rejects(publish(input('b')),/New owner/);
  await assert.rejects(publish(input('a')),error=>error.code==='publisher_conflict');
  assert.equal(claims,2);
});

test('a publication conflict after response-loss recovery also suppresses repeated claims',async()=>{
  let claims=0;const attempts=[];
  const publish=createSourcePublisher({documentId:()=> 'document',clock:()=>0,request:async(route,value)=>{
    if(route.endsWith('/publisher')){claims++;return {generation:1,sequence:0};}
    attempts.push(value);
    if(attempts.length===1)throw new TypeError('Response lost');
    throw Object.assign(new Error('Owner changed'),{status:409,detail:{code:'publisher_conflict',retry_after_ms:90000}});
  }});
  const input={source_bundle:{targetKind:'theorem',targetKey:'a'},files:{}};
  await assert.rejects(publish(input),error=>error.code==='publisher_conflict'&&error.retryAfterMs===90000);
  await assert.rejects(publish(input),error=>error.code==='publisher_conflict');
  assert.equal(claims,1);assert.deepEqual(attempts[0],attempts[1]);
});

test('a blocked document does not prevent source publication after navigation',async()=>{
  let document='before';const calls=[];
  const publish=createSourcePublisher({documentId:()=>document,request:async(route,value)=>{
    calls.push(value.document_id);
    if(value.document_id==='before')throw Object.assign(new Error('Live lease'),{status:409,
      detail:{code:'publisher_conflict',generation:1,retry_after_ms:90000}});
    return route.endsWith('/publisher')?{generation:1,sequence:0}:{source_context_ref:'new-document'};
  }});
  const input={source_bundle:{targetKind:'theorem',targetKey:'a'},files:{}};
  await assert.rejects(publish(input),/Live lease/);
  document='after';
  assert.equal((await publish(input)).source_context_ref,'new-document');
  assert.deepEqual(calls,['before','after','after']);
});

test('navigation while claiming a publisher cannot submit the previous document',async()=>{
  let document='before';const calls=[];
  const publish=createSourcePublisher({documentId:()=>document,clientId:'tab',request:async(route,value)=>{
    calls.push(route);document='after';return {generation:1,sequence:0};
  }});
  await assert.rejects(publish({document_id:'before',source_bundle:{targetKind:'theorem',targetKey:'a'},files:{}}),/document changed/);
  assert.deepEqual(calls,['/source-context/publisher']);
});

test('a legacy nonterminal done frame cannot settle a running SQL observation',async()=>{
  const {streamApiRun}=await import('../companion/leaApiClient.mjs');
  const seen=[];
  const result=await streamApiRun({baseUrl:'http://localhost',runId:'run',onEvent:(type,data)=>seen.push(data.status),fetchImpl:async()=>new Response(
    'event: done\ndata: {"status":"running"}\n\nevent: done\ndata: {"status":"proved","result_kind":"proved"}\n\n',
    {headers:{'Content-Type':'text/event-stream'}})});
  assert.equal(result.ok,true);assert.equal(result.doneStatus,'proved');assert.deepEqual(seen,['proved']);
});
