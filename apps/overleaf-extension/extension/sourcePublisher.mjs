// Capture is browser-owned. The runtime can only bind an already-published ID.
// Serializing publication preserves sequence order and exact replay after loss.
export function createSourcePublisher({documentId,request,clientId=crypto.randomUUID(),clock=Date.now}) {
  let document=null,generation=0,sequence=0,tail=Promise.resolve(),archive=null;
  const hash=async value=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(value)))),b=>b.toString(16).padStart(2,'0')).join('');
  const recent=new Map();
  async function publish(value) {
    const current=documentId();
    if(value.document_id && value.document_id!==current)throw new Error('The document changed during source capture. Publish its current revision again.');
    if(current!==document){document=current;generation=0;sequence=0;archive=null;recent.clear();}
    const fingerprint=await hash(value);
    const previous=recent.get(value.source_bundle.targetKind+':'+value.source_bundle.targetKey);
    if(previous?.fingerprint===fingerprint && clock()-previous.at<30000)return previous.receipt;
    const claim=()=>request('/source-context/publisher',{document_id:document,client_id:clientId,expected_generation:generation});
    let authority;
    try{authority=await claim();}
    catch(error){
      const observed=error.detail?.generation;
      if(error.status!==409||error.detail?.code!=='publisher_conflict'||!Number.isSafeInteger(observed)||observed<1||observed===generation)throw error;
      // Learn the compare-and-swap generation after a reload. The server still
      // rejects this claim while another tab's lease is live.
      generation=observed;sequence=0;archive=null;recent.clear();
      authority=await claim();
    }
    if(documentId()!==current)throw new Error('The document changed during publication. Publish its current revision again.');
    if(generation!==authority.generation)sequence=0;
    generation=authority.generation;sequence=Math.max(sequence,authority.sequence)+1;
    const filesHash=await hash(value.files||{});
    const original={...value,...(archive?.hash===filesHash?{files:{},archive_ref:archive.reference}:{}),version:1,document_id:document,client_id:clientId,generation,sequence};
    let receipt;
    try{receipt=await request('/source-context/publish',original);}
    catch(error){if(error.status)throw error;receipt=await request('/source-context/publish',original);}
    if(documentId()!==current)throw new Error('The document changed during publication. Publish its current revision again.');
    if(receipt.source_archive_ref)archive={hash:filesHash,reference:receipt.source_archive_ref};
    recent.set(value.source_bundle.targetKind+':'+value.source_bundle.targetKey,{fingerprint,receipt,at:clock()});
    return receipt;
  }
  return value=>{const work=tail.catch(()=>{}).then(()=>publish(value));tail=work;return work;};
}
