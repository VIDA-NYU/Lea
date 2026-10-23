import assert from 'node:assert/strict';
import test from 'node:test';
import {streamApiRun} from '../companion/leaApiClient.mjs';

test('SSE preserves UTF-8 across every byte boundary and flushes the final frame',async()=>{
  const content='∀ n, λ 🧮 中文';
  const encoded=new TextEncoder().encode(`event: message\ndata: ${JSON.stringify({content})}\n\nevent: done\ndata: ${JSON.stringify({status:'proved',result_detail:content})}`);
  const seen=[];
  const result=await streamApiRun({baseUrl:'http://localhost',runId:'r',onEvent:(type,data)=>seen.push([type,data]),
    fetchImpl:async()=>new Response(new ReadableStream({start(controller){
      for(const byte of encoded)controller.enqueue(new Uint8Array([byte]));controller.close();
    }}))});
  assert.equal(result.ok,true);
  assert.equal(seen[0][1].content,content);
  assert.equal(result.resultDetail,content);
});
