// Pytest drives the production API, real agent/tool dispatch and migrated SQL.
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import {openWorkerDatabase} from '../companion/databaseState.mjs';
import {setWorkerDatabase,handleStub,handleStubAll,handleGetStatuses,reconcileRegistryUpdates} from '../companion/workerService.mjs';
const input=JSON.parse(readFileSync(0,'utf8'));
const database=openWorkerDatabase(input);setWorkerDatabase(database);
const state={settings:{leaRepoPath:input.leaRoot,leaWorkspacePath:path.join(input.leaRoot,'workspace'),
  leaApiBaseUrl:input.baseUrl,leaModel:'o4-mini',leaMaxTurns:4,leaJobTimeoutSeconds:15,leaTexMirrorEnabled:false},
  env:{OPENAI_API_KEY:'test-key'},fetchImpl:fetch,jobsPath:'/__sql__/jobs.json',repairBatchesPath:'/__sql__/jobs.json.batches.json',
  jobs:database.get('jobs.json',{})};
try{
  await database.runAccepted(input.parent,async()=>{
    if(input.phase==='generate'){
      const result=input.batch?await handleStubAll({overleafProjectId:'doc',items:input.targets},state):await handleStub(input.targets[0],state);
      assert.equal(result.statusCode,200,JSON.stringify(result.body));
      if(input.batch){
        const batch=state.repairBatches[result.body.batchId];
        const deadline=Date.now()+20000;
        while(!batch.done){assert.ok(Date.now()<deadline,JSON.stringify(batch));await new Promise(resolve=>setTimeout(resolve,20));}
        assert.deepEqual(batch.items.map(item=>item.state),input.targets.map(()=> 'stubbed'));
      }
    }else await reconcileRegistryUpdates(state,{now:Date.now()+60000});
    const statuses=await handleGetStatuses({overleafProjectId:'doc',targets:input.targets},state);
    assert.equal(statuses.statusCode,200,JSON.stringify(statuses.body));
    for(const target of input.targets)assert.equal(statuses.body.statuses[`theorem:${target.targetLabel}`].status,'sorry_stub',JSON.stringify(statuses.body));
    process.stdout.write(JSON.stringify({jobs:Object.values(state.jobs)}));
  });
}finally{setWorkerDatabase(null);database.close();}
