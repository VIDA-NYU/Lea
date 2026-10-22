// Capture is browser-owned. The runtime can only bind an already-published ID.
// Serializing publication preserves sequence order and exact replay after loss.
export function createSourcePublisher({documentId,request,clientId=crypto.randomUUID(),clock=Date.now}) {
  let document=null,generation=0,sequence=0,tail=Promise.resolve(),archive=null,blocked=null;
  const hash=async value=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(value)))),b=>b.toString(16).padStart(2,'0')).join('');
  const recent=new Map();
  const publisherConflict=error=>error.status===409&&error.detail?.code==='publisher_conflict';
  function deferConflict(error) {
    // Share one failed lease observation across all targets. Never repeatedly
    // contend with an active tab, or let a stale cached source evade fencing.
    recent.clear();archive=null;
    const reported=Number(error.detail?.retry_after_ms);
    const delay=Number.isFinite(reported)&&reported>0?Math.min(Math.ceil(reported),90000):1000;
    const failure=Object.assign(new Error(error.message||'Waiting for the document source publisher lease.'),{
      status:409,code:'publisher_conflict',detail:error.detail,retryAfterMs:delay
    });
    blocked={until:clock()+delay,error:failure};
    return failure;
  }
  async function publish(value, { force = false } = {}) {
    const current=documentId();
    if(value.document_id && value.document_id!==current)throw new Error('The document changed during source capture. Publish its current revision again.');
    if(current!==document){document=current;generation=0;sequence=0;archive=null;blocked=null;recent.clear();}
    if(blocked&&clock()<blocked.until)throw Object.assign(blocked.error,{retryAfterMs:Math.max(1,blocked.until-clock())});
    blocked=null;
    const fingerprint=await hash(value);
    const previous=recent.get(value.source_bundle.targetKind+':'+value.source_bundle.targetKey);
    if(!force && previous?.fingerprint===fingerprint && clock()-previous.at<30000)return previous.receipt;
    const claim=()=>request('/source-context/publisher',{document_id:document,client_id:clientId,expected_generation:generation});
    let authority;
    for(let attempt=0;attempt<2;attempt++) {
      try{authority=await claim();break;}
      catch(error){
        if(!publisherConflict(error))throw error;
        const observed=error.detail?.generation;
        const changed=Number.isSafeInteger(observed)&&observed>0&&observed!==generation;
        if(changed){generation=observed;sequence=0;archive=null;recent.clear();}
        // An expired generation can be claimed immediately using SQL's CAS.
        // A live owner supplies its remaining lease; wait without another POST.
        if(attempt===0&&changed&&!(error.detail?.retry_after_ms>0))continue;
        throw deferConflict(error);
      }
    }
    if(documentId()!==current)throw new Error('The document changed during publication. Publish its current revision again.');
    if(generation!==authority.generation)sequence=0;
    generation=authority.generation;sequence=Math.max(sequence,authority.sequence)+1;
    const filesHash=await hash(value.files||{});
    const original={...value,...(archive?.hash===filesHash?{files:{},archive_ref:archive.reference}:{}),version:1,document_id:document,client_id:clientId,generation,sequence};
    let receipt;
    for(let attempt=0;attempt<2;attempt++) {
      try{receipt=await request('/source-context/publish',original);break;}
      catch(error){
        if(publisherConflict(error))throw deferConflict(error);
        if(error.status||attempt===1)throw error;
      }
    }
    if(documentId()!==current)throw new Error('The document changed during publication. Publish its current revision again.');
    if(receipt.source_archive_ref)archive={hash:filesHash,reference:receipt.source_archive_ref};
    recent.set(value.source_bundle.targetKind+':'+value.source_bundle.targetKey,{fingerprint,receipt,at:clock()});
    return receipt;
  }
  return (value, options)=>{const work=tail.catch(()=>{}).then(()=>publish(value, options));tail=work;return work;};
}
