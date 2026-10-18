// One browser action keeps its identity even when the response is lost. The
// transport resolves the receipt in SQL and never replays the business handler.
export async function commandFetch(fetchImpl,url,options={}) {
  const headers=new Headers(options.headers||{});
  if(!headers.has('Idempotency-Key'))headers.set('Idempotency-Key',crypto.randomUUID());
  const original={...options,headers};
  try{return await fetchImpl(url,original);}
  catch(error) {
    if(options.signal?.aborted)throw error;
    try{return await fetchImpl(url,original);}
    catch(recovery) {
      recovery.commandKey=headers.get('Idempotency-Key');
      recovery.message+=' The original request is retained as '+recovery.commandKey+'. Reconnect to review its receipt.';
      throw recovery;
    }
  }
}
