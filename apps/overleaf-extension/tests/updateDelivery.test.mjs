import assert from 'node:assert/strict';
import test from 'node:test';
import {createServer} from '../companion/server.mjs';
import {createEventsClient} from '../extension/eventsClient.mjs';

const health={capabilities:{database_boundary:{version:1,ui_access:'database',command_direction:'ui_to_worker',processes:'separate',queries:1,changes:1,commands:1,source_publications:1,editor:1}}};
const page=(id,changes)=>`id: ${id}\nevent: changes\ndata: ${JSON.stringify({cursor:id,watermark:id,changes})}\n\n`;
async function transport({after,frames,expired=false,initiallyMissing=false,deleted=false}={}){
  const calls=[];let missing=initiallyMissing,reset=expired,identityReads=0;
  const server=await createServer({fetchImpl:async url=>{
    calls.push(url);
    if(url.endsWith('/api/health'))return Response.json(health);
    if(url.includes('/identity')){
      if(deleted&&identityReads++>0)return Response.json({detail:'missing'},{status:404});
      const value=missing?Response.json({detail:'missing'},{status:404}):Response.json({projectId:'sql-A'});
      missing=false;return value;
    }
    if(url.includes('limit=1'))return Response.json({detail:{watermark:100,floor:50}},{status:409});
    if(reset){reset=false;return Response.json({detail:{watermark:100,floor:50}},{status:409});}
    return new Response(frames||page(101,[]));
  }});
  const response={status:0,headersSent:false,chunks:[],writeHead(s){this.status=s;this.headersSent=true;},setHeader(){},write(s){this.chunks.push(s);},end(s){if(s)this.chunks.push(s);}};
  await server.listeners('request')[0]({method:'GET',url:'/events?projectId=document-A'+(after===undefined?'':`&after=${after}`),headers:{host:'127.0.0.1'},on(){}},response);
  return {...response,text:response.chunks.join(''),calls};
}

test('new and expired Overleaf observers reset to a committed watermark and resume',async()=>{
  for(const options of [{},{after:2,expired:true}]){
    const result=await transport(options);
    assert.equal(result.status,200);
    assert.match(result.text,/id: 100\nevent: resync/);
    assert.ok(result.calls.some(url=>url.includes('stream=true&after=100')));
  }
});

test('status, proof evidence, reviews and projection commits reach only their project',async()=>{
  const types=['lea_status_updates','proof_check_evidence','formalization_review_events','core_observations','core_projection_values','core_projection_enrollment'];
  const result=await transport({after:100,frames:page(110,types.map(entity_type=>({entity_type,project_id:'sql-A'})))+
    page(111,[{entity_type:'timeline',project_id:'sql-B'}])+page(112,[{entity_type:'peripheral_views',project_id:'sql-A'}])});
  assert.equal((result.text.match(/event: jobs-changed/g)||[]).length,1);
  for(const type of types)assert.match(result.text,new RegExp(type));
  assert.doesNotMatch(result.text,/sql-B|peripheral_views/);
  assert.match(result.text,/id: 111\nevent: cursor/);
  assert.match(result.text,/id: 112\nevent: cursor/);
});

test('an untouched Overleaf document starts receiving updates when its SQL project is created',async()=>{
  const result=await transport({after:100,initiallyMissing:true,frames:page(101,[{entity_type:'projects',project_id:'sql-A'}])});
  assert.match(result.text,/event: jobs-changed/);
  assert.equal(result.calls.filter(url=>url.includes('/identity')).length,2);
});

test('project deletion still delivers its tombstone after identity resolution returns missing',async()=>{
  const result=await transport({after:100,deleted:true,frames:page(101,[{entity_type:'projects',action:'delete',project_id:'sql-A'}])});
  assert.match(result.text,/event: jobs-changed/);assert.match(result.text,/"action":"delete"/);
});

test('in-stream resets carry a resumable id through the transport and client',async()=>{
  const result=await transport({after:100,frames:'event: resync\ndata: {"watermark":5,"floor":2}\n\n'});
  const sources=[],timers=[];
  class Source{constructor(url){this.url=url;this.listeners={};sources.push(this);}addEventListener(type,fn){this.listeners[type]=fn;}close(){}}
  const events=[];
  const client=createEventsClient({url:'http://companion/events?projectId=document-A',EventSourceImpl:Source,
    setTimeoutImpl:fn=>{timers.push(fn);return timers.length;},clearTimeoutImpl:()=>{},onEvent:(type,data)=>events.push({type,data})});
  client.start();
  for(const frame of result.text.split('\n\n')){
    const type=frame.match(/^event: (.+)$/m)?.[1],data=frame.match(/^data: (.+)$/m)?.[1];
    sources[0].listeners[type]?.({data,lastEventId:frame.match(/^id: (.+)$/m)?.[1]||''});
  }
  sources[0].onerror();timers.shift()();
  assert.equal(new URL(sources[1].url).searchParams.get('after'),'5');
  assert.equal(events[0].type,'resync');client.stop();
});

test('HTTP event transport delivers fragmented commits and removes other projects',async()=>{
  const server=await createServer({fetchImpl:async url=>{
    if(url.endsWith('/api/health'))return Response.json(health);
    if(url.includes('/identity'))return Response.json({projectId:'sql-A'});
    const frames=page(101,[{entity_type:'lea_status_updates',project_id:'sql-A'}])+
      page(102,[{entity_type:'timeline',project_id:'sql-B'}])+
      page(103,[{entity_type:'core_observations',project_id:'sql-A'}]);
    const bytes=new TextEncoder().encode(frames);
    return new Response(new ReadableStream({start(controller){
      controller.enqueue(bytes.slice(0,17));
      setTimeout(()=>{controller.enqueue(bytes.slice(17));controller.close();},10);
    }}));
  }});
  try{
    await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
    const response=await fetch(`http://127.0.0.1:${server.address().port}/events?projectId=document-A&after=100`);
    assert.equal(response.status,200);
    const text=await response.text();
    assert.equal((text.match(/event: jobs-changed/g)||[]).length,2);
    assert.match(text,/lea_status_updates/);assert.match(text,/core_observations/);
    assert.doesNotMatch(text,/sql-B/);assert.match(text,/id: 102\nevent: cursor/);
  }finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});
