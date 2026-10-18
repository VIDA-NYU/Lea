// Worker-only SQLite ownership and compatibility records. No JSON writer remains
// in the companion transport. The daemon cache is disposable; each mutation is
// committed with a live worker epoch and the common transactional change trigger.
import { DatabaseSync } from 'node:sqlite';
import { randomUUID } from 'node:crypto';
import { AsyncLocalStorage } from 'node:async_hooks';
import path from 'node:path';
import { workspaceResult } from '../../../packages/lea-workspace-client/index.mjs';

const SECRET = /^(?:.*(?:api_?key|auth_?token|access_?token|refresh_?token|password|secret|credential)s?|authorization|github_?token)$/i;
export function redact(value) {
  if (Array.isArray(value)) return value.map(redact);
  if (!value || typeof value !== 'object') return typeof value === 'string'
    ? value.replace(/\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,})\b/g,'[redacted]') : value;
  return Object.fromEntries(Object.entries(value).filter(([k])=>!SECRET.test(k) && !/apiKey|authToken|api_keys|github_token/i.test(k)).map(([k,v])=>[k,redact(v)]));
}

export function openWorkerDatabase({filename,owner,epoch,baseUrl,leaRoot,fetchImpl=fetch}) {
  const db=new DatabaseSync(filename);db.exec('pragma busy_timeout=5000');
  const context=new AsyncLocalStorage();
  let batch=randomUUID();db.function('lea_change_batch',()=>batch);
  function fence() {
    if (!db.prepare("select 1 from command_workers where id=? and epoch=? and julianday(lease_until)>julianday('now')").get(owner,epoch))
      throw new Error('Companion worker epoch expired; restart and reconcile before execution.');
  }
  function write(fn) {
    db.exec('begin immediate');batch=randomUUID();
    try {
      fence();const active=context.getStore();
      if(active?.commandId) {
        const intent=db.prepare("select state,worker_epoch,lease_until from commands where id=?").get(active.commandId);
        if(!intent||!['claimed','succeeded'].includes(intent.state)||(intent.state==='claimed'&&(intent.worker_epoch!==epoch||Date.parse(intent.lease_until)<=Date.now())))throw new Error('The business command lost ownership; its accepted input is retained.');
      }
      const result=fn();db.exec('commit');return result;
    }
    catch(error) { db.exec('rollback');throw error; }
  }
  const bases=new Map();
  function headers() {
    const active=context.getStore();
    return {'X-Lea-Worker-Epoch':epoch,'X-Lea-Worker-Proof':process.env.LEA_WORKER_BRIDGE_KEY,
      ...(active?.commandId?{'X-Lea-Worker-Parent':active.commandId}:{}),...(active?.batchId?{'X-Lea-Batch-Id':active.batchId}:{})};
  }
  function runAccepted(identity,fn,{claimed=false,batchId=null}={}) {
    fence();
    const row=identity?db.prepare("select * from commands where id=? and kind like 'companion.%' and state in ('claimed','succeeded')").get(identity):null;
    if(!row || (claimed && row.state!=='claimed') || (row.state==='claimed' && row.worker_epoch!==epoch))throw new Error('The accepted business intent requires reconciliation before it can continue.');
    return context.run({commandId:identity,batchId},fn);
  }
  async function api(route,body) {
    fence();
    const response=await fetchImpl(baseUrl+route,{method:body?'POST':'GET',headers:{'Content-Type':'application/json',...headers()},...(body?{body:JSON.stringify(body)}:{}),signal:AbortSignal.timeout(30000)});
    const value=await response.json();
    if (!response.ok) throw Object.assign(new Error(value.detail?.message||value.detail||'Worker API operation failed'),{status:response.status});
    return value;
  }
  function projectFile(absolute) {
    const root=path.resolve(leaRoot,'workspace/proofs');
    const registryRoot=path.resolve(leaRoot,'workspace/projects');
    if(path.dirname(path.resolve(absolute))===registryRoot && path.extname(absolute)==='.md') {
      const project=db.prepare('select * from projects where slug=?').get(path.basename(absolute,'.md'));
      if(!project)throw Object.assign(new Error('Project registry has no database project binding.'),{code:'ENOENT'});
      return {project,path:'.lea/project-registry.md'};
    }
    for (const p of db.prepare('select * from projects').all()) {
      const repo=path.resolve(leaRoot,'workspace',p.repo_path||path.join('proofs',String(p.namespace).replaceAll('.',path.sep)));
      const relative=path.relative(repo,absolute);
      if (relative && !relative.startsWith('..') && !path.isAbsolute(relative)) return {project:p,path:relative.split(path.sep).join('/')};
    }
    throw new Error('Managed output has no database project binding.');
  }
  function put(name,input) {
    if(name==='jobs.json')for(const job of Object.values(input)) {
      if(!job.acceptedCommandId && context.getStore()?.commandId && db.prepare('select kind from commands where id=?').get(context.getStore().commandId)?.kind==='companion.dispatch')job.acceptedCommandId=context.getStore().commandId;
    }
    const value=redact(input);const encoded=JSON.stringify(value);
    if(Buffer.byteLength(encoded)>32*1024*1024)throw new Error('Companion state exceeds its 32 MiB bound');
    return write(()=>{
      const now=new Date().toISOString();
      const old=db.prepare('select payload from companion_documents where name=?').get(name);
      if(name==='jobs.json.batches.json' && old) {
        const previous=JSON.parse(old.payload);
        for(const [id,batch] of Object.entries(value))if(previous[id]?.cancelRequested)batch.cancelRequested=true;
      }
      const payload=JSON.stringify(value);
      if(old?.payload===payload)return;
      db.prepare('insert into companion_documents values(?,?,1,?) on conflict(name) do update set payload=excluded.payload,revision=companion_documents.revision+1,updated_at=excluded.updated_at').run(name,payload,now);
      if(name==='jobs.json') {
        if(Object.keys(value).length>10000)throw new Error('Companion job count exceeds its migration bound');
        for(const [id,job] of Object.entries(value)) {
          const run=job.apiRunId?db.prepare('select * from runs where id=?').get(job.apiRunId):null;
          const session=job.leaSessionId||job.recorderSessionId||run?.session_id||null;
          const form=job.formalizationId||run?.focus_formalization_id||null;
          db.prepare('insert into companion_jobs values(?,?,?,?,?,?,?,?,?) on conflict(id) do update set document_id=excluded.document_id,job_key=excluded.job_key,project_id=excluded.project_id,session_id=excluded.session_id,run_id=excluded.run_id,formalization_id=excluded.formalization_id,payload=excluded.payload,updated_at=excluded.updated_at')
            .run(id,job.overleafProjectId||null,job.jobKey||null,run?.project_id||null,session,job.apiRunId||null,form,JSON.stringify(job),now);
        }
        for(const row of db.prepare('select id,payload from companion_jobs').all())if(!Object.hasOwn(value,row.id)) {
          const oldJob=JSON.parse(row.payload);
          db.prepare('delete from companion_jobs where id=?').run(row.id);
          if(oldJob.logPath)db.prepare('delete from companion_documents where name=?').run('log:'+path.basename(oldJob.logPath));
        }
      }
      if(name==='chatSessions.json')for(const [targetKey,entry] of Object.entries(value)) {
        const session=entry.leaSessionId||entry.sessionId||null;
        const project=session?db.prepare('select project_id from sessions where id=?').get(session)?.project_id:null;
        db.prepare('insert into companion_associations values(?,?,?,?,?,?,?) on conflict(target_key) do update set session_id=excluded.session_id,formalization_id=excluded.formalization_id,payload=excluded.payload,updated_at=excluded.updated_at')
          .run(targetKey,targetKey.split(':')[0],project||null,session,entry.formalizationId||null,JSON.stringify(entry),now);
      }
    });
  }
  return {
    fence,put,headers,
    runCommand(identity,fn){return runAccepted(identity,fn,{claimed:true});},
    runAccepted,
    currentCommand(){return context.getStore()?.commandId;},
    sourceFor(document,kind,key) {
      fence();const row=db.prepare(`select p.* from companion_source_bindings b join source_publications p on p.id=b.publication_id
        where b.command_id=? and p.document_id=? and json_extract(p.bundle_json,'$.targetKind')=? and json_extract(p.bundle_json,'$.targetKey')=?`).get(context.getStore()?.commandId||'',document,kind,key);
      return row?{source_context_ref:row.id,sourceBundle:JSON.parse(row.bundle_json)}:null;
    },
    stampBatch(batch) {
      if(batch.migrationBlocked)return;
      const active=context.getStore();
      if(!batch.acceptedCommandId && active?.commandId) {
        const row=db.prepare('select kind from commands where id=?').get(active.commandId);
        if(row?.kind==='companion.dispatch')batch.acceptedCommandId=active.commandId;
      }
    },
    runBatch(batch,fn) {
      if(batch.migrationBlocked)throw new Error('Review this imported batch before continuing.');
      return runAccepted(batch.acceptedCommandId,fn,{batchId:batch.batchId});
    },
    currentBatch(){return context.getStore()?.batchId;},
    resumeBatch(batch) {
      const updates=[];
      for(const entry of batch.items||[])if(entry.state==='pending'&&entry.payload) {
        const pinned=this.sourceFor(batch.overleafProjectId,entry.targetKind,entry.targetLabel);
        if(!pinned||pinned.sourceBundle.bundleHash!==entry.payload.sourceBundle?.bundleHash)throw new Error('This retained batch needs current source review. Submit the current target batch again; its history is retained.');
        updates.push([entry,pinned]);
      }
      for(const [entry,pinned] of updates)Object.assign(entry.payload,pinned);
      batch.migrationBlocked=false;batch.acceptedCommandId=context.getStore()?.commandId;
    },
    checkBatchCancellation(batch) {
      const row=db.prepare("select payload from companion_documents where name='jobs.json.batches.json'").get();
      if(row && JSON.parse(row.payload)[batch.batchId]?.cancelRequested)batch.cancelRequested=true;
    },
    validateSource(document,sourceContext) {
      fence();
      const row=sourceContext.source_context_ref?db.prepare('select * from source_publications where id=?').get(sourceContext.source_context_ref):null;
      if(!row||row.document_id!==document||row.bundle_hash!==sourceContext.sourceBundle?.bundleHash)return {ok:false,error:'published_source_required',message:'Publish this target from the current Overleaf tab before submitting.'};
      const owner=db.prepare('select * from source_publishers where document_id=?').get(document);
      const source=JSON.parse(row.bundle_json);
      const head=db.prepare('select p.bundle_hash,p.files_json from source_heads h join source_publications p on p.id=h.publication_id where h.document_id=? and h.target_kind=? and h.target_key=?').get(document,source.targetKind,source.targetKey);
      const archive=db.prepare('select coverage,files_json from source_publications where document_id=? and generation=? order by sequence desc limit 1').get(document,row.generation);
      if(archive?.coverage==='complete'&&row.coverage==='complete'&&archive.files_json!==row.files_json)return {ok:false,error:'published_source_changed',message:'The document archive changed. Publish this target from its current archive.'};
      const pinned=db.prepare('select 1 from companion_source_bindings where command_id=? and publication_id=?').get(context.getStore()?.commandId||'',row.id);
      if(!owner||owner.client_id!==row.client_id||owner.generation!==row.generation||(!pinned&&Date.parse(owner.lease_until)<=Date.now()))return {ok:false,error:'publisher_conflict',message:'The source publisher changed or expired. Publish from the current tab again.'};
      if(head?.bundle_hash!==row.bundle_hash||head.files_json!==row.files_json)return {ok:false,error:'published_source_changed',message:'Source changed before submission. Publish its current revision again.'};
      return {ok:true,mirrorRevision:sourceContext.sourceBundle?.mirror?.revision||null};
    },
    publishedBundle(reference) {fence();const row=reference?db.prepare('select bundle_json from source_publications where id=?').get(reference):null;return row?JSON.parse(row.bundle_json):null;},
    get(name,fallback) {
      fence();const row=db.prepare('select payload from companion_documents where name=?').get(name);const value=row?JSON.parse(row.payload):fallback;
      if(name==='settings.json') {
        const record=db.prepare("select payload from application_settings where id='policy'").get();const policy=record?JSON.parse(record.payload):{};
        return {...value,leaRepoPath:leaRoot,leaApiBaseUrl:baseUrl,
          ...(policy.model?{leaModel:policy.model}:{}),...(policy.max_turns?{leaMaxTurns:policy.max_turns}:{}),
          ...(Object.hasOwn(policy,'max_spend_usd')?{leaMaxSpendUsd:policy.max_spend_usd}:{})};
      }
      if(name==='jobs.json'||name==='chatSessions.json') {
        const table=name==='jobs.json'?'companion_jobs':'companion_associations';const column=name==='jobs.json'?'id':'target_key';
        return Object.fromEntries(Object.keys(value).flatMap(id=>{
          const normalized=db.prepare('select payload from '+table+' where '+column+'=?').get(id);
          if(!normalized)return [];
          const item=JSON.parse(normalized.payload);
          if(name==='jobs.json')Object.assign(item,{leaApiBaseUrl:baseUrl,leaRepoPath:leaRoot,leaWorkspacePath:path.join(leaRoot,'workspace'),
            projectMarkdownPath:path.join(leaRoot,'workspace/projects',String(item.projectSlug||'unknown')+'.md')});
          return [[id,item]];
        }));
      }
      if(name==='jobs.json.batches.json')for(const batch of Object.values(value)) {
        if(batch.done)continue;
        const intent=batch.acceptedCommandId?db.prepare("select state,worker_epoch from commands where id=?").get(batch.acceptedCommandId):null;
        if(!intent||!['claimed','succeeded'].includes(intent.state)||(intent.state==='claimed'&&intent.worker_epoch!==epoch)) {
          batch.migrationBlocked=true;batch.pausedOn={reason:'reconciliation_required',detail:'Explicitly continue this retained batch before admitting further work.'};
        }
      }
      return value;
    },
    appendLog(logPath,text) { const name='log:'+path.basename(logPath);return write(()=>{const row=db.prepare('select payload from companion_documents where name=?').get(name);const content=((row?JSON.parse(row.payload):'')+redact(String(text))).slice(-65536);db.prepare('insert into companion_documents values(?,?,1,?) on conflict(name) do update set payload=excluded.payload,revision=companion_documents.revision+1,updated_at=excluded.updated_at').run(name,JSON.stringify(content),new Date().toISOString());}); },
    logTail(logPath,count) { const row=db.prepare('select payload from companion_documents where name=?').get('log:'+path.basename(logPath));return row?JSON.parse(row.payload).slice(-count):''; },
    async readFile(absolute) {
      const f=projectFile(absolute);const value=await api(`/api/workspace/file?project_id=${encodeURIComponent(f.project.id)}&path=${encodeURIComponent(f.path)}`);
      bases.set(absolute,value.file_revision);
      if(value.content===null && value.file_revision==='missing' && f.path==='.lea/project-registry.md') {
        const imported=db.prepare('select payload from companion_documents where name=?').get('project-registry-import:'+f.project.id);
        if(imported)return JSON.parse(imported.payload).content;
      }
      if(value.content===null)throw Object.assign(new Error('Managed content is not observed in SQL'),{code:value.file_revision==='missing'?'ENOENT':'projection_unavailable'});
      return value.content;
    },
    async writeFile(absolute,content) {
      const f=projectFile(absolute);
      const receipt=await api('/api/workspace/commands',{version:1,action:'project.save',idempotency_key:randomUUID(),
        expected_revision:bases.get(absolute)||'missing',arguments:{project_id:f.project.id,request:{path:f.path,content:String(content)}}});
      return workspaceResult(receipt,id=>api('/api/workspace/commands/'+id));
    },
    close(){db.close();},
  };
}
