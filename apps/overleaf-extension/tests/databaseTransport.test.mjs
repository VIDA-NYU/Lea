import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtemp,readdir,rm,readFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createServer} from '../companion/server.mjs';
import {createSourcePublisher} from '../extension/sourcePublisher.mjs';
import {commandFetch} from '../extension/commandClient.mjs';

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

test('a reloaded tab learns the current generation but cannot take over a live publisher',async()=>{
  let live=true;const claims=[],publications=[];
  const publish=createSourcePublisher({documentId:()=> 'document',clientId:'reloaded',request:async(route,value)=>{
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
  live=false;assert.equal((await publish(input)).source_context_ref,'new-generation');
  assert.equal(publications[0].generation,8);assert.equal(publications[0].sequence,1);
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
