// Private adapter-owned business service; never launched by the UI transport.
import { createWorkerServer,setWorkerDatabase } from './workerService.mjs';
import { openWorkerDatabase } from './databaseState.mjs';

if (!process.env.LEA_WORKER_EPOCH || !process.env.LEA_WORKER_BRIDGE_KEY) throw new Error('Worker ownership is required');
const state=openWorkerDatabase({filename:process.env.LEA_WORKER_DB,owner:'lea-command-host',epoch:process.env.LEA_WORKER_EPOCH,
  baseUrl:process.env.LEA_API_BASE_URL,leaRoot:process.env.LEA_ROOT});
setWorkerDatabase(state);
const checkedFetch=async (url,options={})=>{
  state.fence();
  const target=new URL(url),allowed=new URL(process.env.LEA_API_BASE_URL);
  if(target.origin!==allowed.origin||!target.pathname.startsWith(allowed.pathname.replace(/\/$/,'' )+'/'))throw new Error('The saved API destination differs from the managed adapter; reconcile its connection metadata.');
  return fetch(url,{...options,redirect:'error',headers:{...options.headers,...state.headers()}});
};
const server=await createWorkerServer({settingsPath:'/__sql__/settings.json',jobsPath:'/__sql__/jobs.json',chatSessionsPath:'/__sql__/chatSessions.json',fetchImpl:checkedFetch});
server.listen(0,'127.0.0.1',()=>process.stdout.write(JSON.stringify({port:server.address().port})+'\n'));
// A lost parent/epoch stops every business loop; old callbacks cannot publish.
const timer=setInterval(()=>{try{state.fence();}catch{process.exit(75);}},1000);
process.on('SIGTERM',()=>{clearInterval(timer);server.closeAllConnections();server.close(()=>{state.close();process.exit(0);});});
