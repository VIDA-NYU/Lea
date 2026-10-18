// Optional, stateless compatibility transport. Execution and application state
// belong to the adapter's database/worker. This process needs no workspace path,
// job directory, provider credentials, reconciliation timer or run supervisor.
import http from 'node:http';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { isAllowedHost } from './netguard.mjs';
import { workspaceResult,submitWithRecovery,assertDatabaseBoundary } from '../../../packages/lea-workspace-client/index.mjs';
import { toChatSessionResponse } from './chatPrompt.mjs';
import { usageProjection } from './usageProjection.mjs';

export function companionListenHost(env=process.env){return env.LEA_COMPANION_HOST||'127.0.0.1';}
function allowedOrigin(value){return !value||value==='https://www.overleaf.com'||/^(chrome|moz)-extension:\/\//.test(value);}
function send(response,status,value){response.writeHead(status,{'Content-Type':'application/json'});response.end(JSON.stringify(value));}
function slug(value){let s=String(value||'').trim().replace(/[^A-Za-z0-9_-]/g,'_')||'unknown';if(!/^[A-Za-z0-9]/.test(s))s='project_'+s;return s.slice(0,80);}
async function body(request){let n=0;const chunks=[];for await(const chunk of request){n+=chunk.length;if(n>32*1024*1024)throw Object.assign(new Error('Input exceeds 32 MiB'),{status:413});chunks.push(chunk);}try{return chunks.length?JSON.parse(Buffer.concat(chunks).toString('utf8')):{};}catch{throw Object.assign(new Error('Invalid JSON'),{status:400});}}

export async function createServer({env=process.env,fetchImpl=fetch}={}) {
  const base=String(env.LEA_API_BASE_URL||'http://127.0.0.1:8001').replace(/\/+$/,'');
  let compatible=false;
  async function api(route,payload,method='GET') {
    if (!compatible && route!=='/api/health') await api('/api/health');
    const response=await fetchImpl(base+route,{method,headers:{'Content-Type':'application/json'},...(payload?{body:JSON.stringify(payload)}:{})});
    const value=await response.json();
    if(!response.ok)throw Object.assign(new Error(value.detail?.message||value.detail||value.message||'Adapter unavailable'),{status:response.status,detail:value.detail});
    if(route==='/api/health'){assertDatabaseBoundary(value);compatible=true;}
    return value;
  }
  async function observe(initial){return workspaceResult(initial,id=>api('/api/companion/commands/'+id));}
  const server=http.createServer(async(request,response)=>{
    try {
      if(!isAllowedHost(request.headers.host,env))return send(response,400,{error:'invalid_host'});
      if(!allowedOrigin(request.headers.origin))return send(response,403,{error:'forbidden_origin'});
      if(request.headers.origin){response.setHeader('Access-Control-Allow-Origin',request.headers.origin);response.setHeader('Vary','Origin');response.setHeader('Access-Control-Allow-Headers','Content-Type,Idempotency-Key');response.setHeader('Access-Control-Allow-Methods','GET,POST,PUT,OPTIONS');}
      if(request.method==='OPTIONS'){response.writeHead(204);response.end();return;}
      const url=new URL(request.url,'http://localhost');const pathname=url.pathname;
      if(pathname==='/events'&&request.method==='GET') {
        if(!compatible)await api('/api/health');
        const abort=new AbortController();request.on('close',()=>abort.abort());
        const cursor=request.headers['last-event-id']||url.searchParams.get('after')||'0';
        const upstream=await fetchImpl(base+'/api/changes?stream=true&after='+encodeURIComponent(cursor),{signal:abort.signal});
        if(!upstream.ok)return send(response,upstream.status,await upstream.json());
        response.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-cache'});response.write('event: hello\ndata: {"ok":true}\n\n');
        let pending='';const decoder=new TextDecoder();
        const relevant=new Set(['companion_jobs','companion_associations','formalizations','runs','timeline','proof_checks','formalization_reviews','project_dependencies','reformalization_operations']);
        for await(const bytes of upstream.body){pending+=decoder.decode(bytes,{stream:true});let end;
          while((end=pending.indexOf('\n\n'))>=0){const frame=pending.slice(0,end);pending=pending.slice(end+2);const id=frame.match(/^id: (.+)$/m)?.[1];const data=frame.match(/^data: (.+)$/m)?.[1];
            const event=frame.match(/^event: (.+)$/m)?.[1];
            if(event==='resync'){response.write('event: resync\ndata: '+(data||'{}')+'\n\n');continue;}
            const page=data?JSON.parse(data):{};
            const changes=(page.changes||[]).filter(change=>relevant.has(change.entity_type)||(change.entity_type==='companion_documents'&&String(change.entity_id).includes('batches')));
            if(changes.length)response.write((id?'id: '+id+'\n':'')+'event: jobs-changed\ndata: '+JSON.stringify({...page,changes})+'\n\n');
            else response.write((id?'id: '+id+'\n':'')+'event: cursor\ndata: {}\n\n');
          }
        }
        response.end();return;
      }
      if(pathname==='/source-context/pending'||pathname==='/source-context/respond')return send(response,410,{error:'source_callbacks_retired',message:'Publish immutable source instead.'});
      if(request.method==='GET') {
        if(pathname==='/commands/receipts')return send(response,200,await api('/api/companion/receipts?'+url.searchParams));
        if(pathname.startsWith('/commands/'))return send(response,200,await api('/api/companion/commands/'+encodeURIComponent(decodeURIComponent(pathname.slice(10)))));
        if(pathname==='/health'){const value=await api('/api/health');return send(response,200,{...value,ok:true,transportOnly:true});}
        if(pathname==='/settings'){const value=await api('/api/companion/settings');return send(response,200,{...value,leaApiBaseUrl:base,leaUiBaseUrl:env.LEA_UI_BASE_URL||value.leaUiBaseUrl||'http://localhost:5173'});}
        if(pathname.startsWith('/jobs/'))return send(response,200,await api('/api/companion/jobs/'+encodeURIComponent(decodeURIComponent(pathname.slice(6)))));
        if(pathname==='/usage'){const stats=await api('/api/stats'),settings=await api('/api/companion/settings');const value=usageProjection(stats,slug(url.searchParams.get('overleafProjectId')));return send(response,200,{...value,leaMaxSpendUsd:settings.leaMaxSpendUsd,leaCurrentSpendUsd:value.allTime.costUsd,leaSpendLimitReached:settings.leaMaxSpendUsd!=null&&value.allTime.costUsd>=settings.leaMaxSpendUsd});}
        if(pathname==='/settings/models')return send(response,200,await api('/api/models'));
        if(pathname==='/settings/models/requirements')return send(response,200,await api('/api/models/requirements?'+url.searchParams));
        if(pathname.startsWith('/lean-pane/check-runtime/'))return send(response,200,await api('/api/sessions/'+encodeURIComponent(decodeURIComponent(pathname.split('/').at(-1)))+'/lean-check-runtime'));
        if(pathname.startsWith('/lean-pane/chat/session/')){const sid=decodeURIComponent(pathname.split('/').at(-1));return send(response,200,toChatSessionResponse(await api('/api/sessions/'+encodeURIComponent(sid)),{leaSessionId:sid}));}
        const project=slug(url.searchParams.get('overleafProjectId')||url.searchParams.get('projectId'));
        const paths={'/project/identity':'/identity','/project/graph':'/graph','/share/github':'/share','/project/github-import/status':'/github-imports/'+encodeURIComponent(url.searchParams.get('importId')||''),'/formalize/all/reports':'/formalize-batch-reports'};
        if(paths[pathname])return send(response,200,await api('/api/projects/by-slug/'+encodeURIComponent(project)+paths[pathname]));
        if(pathname.startsWith('/formalize/all/reports/'))return send(response,200,await api('/api/projects/by-slug/'+encodeURIComponent(project)+'/formalize-batch-reports/'+encodeURIComponent(decodeURIComponent(pathname.slice(23)))));
        if(pathname==='/project-export'){const upstream=await fetchImpl(base+'/api/projects/by-slug/'+encodeURIComponent(project)+'/export');response.writeHead(upstream.status,Object.fromEntries([...upstream.headers].filter(([k])=>['content-type','content-disposition'].includes(k))));for await(const bytes of upstream.body)response.write(bytes);response.end();return;}
        if(pathname==='/lean-pane/lea-status/updates'){const fid=url.searchParams.get('formalizationId');return send(response,200,await api('/api/formalizations/'+encodeURIComponent(fid)+'/lea-status/updates?'+url.searchParams));}
        return send(response,404,{error:'not_found'});
      }
      const payload=await body(request);
      if(pathname.startsWith('/formalize/all/reports/')&&pathname.endsWith('/retry')) {
        const batchId=decodeURIComponent(pathname.slice(23,-6));
        const accepted=await api('/api/projects/by-slug/'+encodeURIComponent(slug(payload.overleafProjectId))+'/formalize-batch-reports/'+encodeURIComponent(batchId)+'/retry',{},'POST');
        return send(response,200,await workspaceResult(accepted,id=>api('/api/workspace/commands/'+id)));
      }
      if(pathname==='/project-export') {
        const accepted=await api('/api/projects/by-slug/'+encodeURIComponent(slug(payload.overleafProjectId))+'/export',{},'POST');
        const artifact=await workspaceResult(accepted,id=>api('/api/workspace/commands/'+id));
        const upstream=await fetchImpl(base+artifact.download_url);
        response.writeHead(upstream.status,Object.fromEntries([...upstream.headers].filter(([k])=>['content-type','content-disposition'].includes(k))));
        for await(const bytes of upstream.body)response.write(bytes);response.end();return;
      }
      if(pathname==='/source-context/publisher')return send(response,200,await api('/api/overleaf/publishers',payload,'POST'));
      if(pathname==='/source-context/publish')return send(response,201,await api('/api/overleaf/publications',payload,'POST'));
      if(pathname==='/mirror-tex')return send(response,200,await api('/api/projects/by-slug/'+encodeURIComponent(slug(payload.overleafProjectId))+'/mirror',{files:payload.files,mode:payload.mode||'reconcile',overleaf_project_title:payload.overleafProjectTitle},'POST'));
      if(pathname==='/settings/models/refresh'){const accepted=await api('/api/models/refresh',{},'POST');const result=await workspaceResult(accepted,id=>api('/api/workspace/commands/'+id));return send(response,200,result);}
      if(pathname==='/settings/lea'||pathname==='/settings/github-token') {
        const values={};for(const [from,to] of Object.entries({leaModel:'model',leaMaxTurns:'max_turns',leaMaxSpendUsd:'max_spend_usd'}))if(Object.hasOwn(payload,from))values[to]=payload[from];
        const keys={...(payload.leaApiKeys||{})};for(const [provider,value] of Object.entries(payload.leaProviderApiKeys||{})){const name={openai:'OPENAI_API_KEY',anthropic:'ANTHROPIC_API_KEY',google:'GOOGLE_API_KEY',portkey:'PORTKEY_API_KEY'}[provider];if(name)keys[name]=value;}
        if(Object.keys(keys).length)values.api_keys=Object.fromEntries(Object.entries(keys).map(([k,v])=>[k,typeof v==='object'?v:{value:v}]));
        if(pathname==='/settings/github-token')values.github_token={value:payload.token||payload.value,clear:payload.clear===true};
        const accepted=await api('/api/settings',values,'PUT');await workspaceResult(accepted,id=>api('/api/workspace/commands/'+id));
        const preferences=Object.fromEntries(['leaJobTimeoutSeconds','leaTexMirrorEnabled','leaPauseOnSourceIssue'].filter(k=>Object.hasOwn(payload,k)).map(k=>[k,payload[k]]));
        if(Object.keys(preferences).length)await api('/api/companion/preferences',preferences,'PUT');
        return send(response,200,await api('/api/companion/settings'));
      }
      const key=request.headers['idempotency-key']||randomUUID();const submission={path:pathname,method:request.method,idempotency_key:key,payload};
      const accepted=await submitWithRecovery(()=>api('/api/companion/commands',submission,'POST'),()=>api('/api/companion/receipts?idempotency_key='+encodeURIComponent(key)));
      const result=await observe(accepted);return send(response,result.statusCode,result.body);
    }catch(error){if(!response.headersSent)send(response,error.status||502,{error:'adapter_operation_failed',message:error.message,detail:error.detail});else response.end();}
  });
  return server;
}

if(fileURLToPath(import.meta.url)===process.argv[1]){const server=await createServer();server.listen(Number(process.env.LEA_COMPANION_PORT||31245),companionListenHost(),()=>console.log('Overleaf transport ready'));}
