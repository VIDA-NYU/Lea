import assert from 'node:assert/strict';
import test from 'node:test';
import {build} from 'esbuild';
import vm from 'node:vm';
const bundle=build({entryPoints:[new URL('./lib/api.ts',import.meta.url).pathname],bundle:true,write:false,platform:'node',format:'cjs'});
const storage=()=>{const map=new Map();return {getItem:k=>map.get(k),setItem:(k,v)=>map.set(k,v),removeItem:k=>map.delete(k)};};
async function api(fetch,sessionStorage){
  const context={module:{exports:{}},fetch,sessionStorage,crypto:globalThis.crypto};context.exports=context.module.exports;
  vm.runInNewContext((await bundle).outputFiles[0].text,context);return context.module.exports;
}
for(const kind of ['proof','formalization','reformalization','project'])test(`${kind} starts retain the same request after an uncertain response and tab reload`,async()=>{
  const disk=storage(),requests=[];let first=true;
  const fetch=async(url,init)=>{
    requests.push({url,body:JSON.parse(init.body)});
    if(first){first=false;throw new TypeError('response lost');}
    return new Response(JSON.stringify(kind==='project'?{id:'one-project'}:{run_id:'one-run',session_id:'one-chat'}));
  };
  const submit=client=>kind==='project'?client.createProject('Study','Description'):
    client.createRun('Prove it',undefined,'test',{project_slug:'study',
      ...(kind==='formalization'?{new_formalization:{display_title:'Lemma'}}:{}),
      ...(kind==='reformalization'?{focus_formalization_id:'f',artifact_operation:'reformalize'}:{})});
  const client=await api(fetch,disk);await assert.rejects(submit(client),/response lost/);
  await submit(await api(fetch,disk));
  assert.deepEqual(requests[0],requests[1]);assert.ok(requests[0].body.idempotency_key);
});
