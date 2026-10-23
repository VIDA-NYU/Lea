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
      recovery.message+=' Acceptance is unconfirmed. Request key: '+recovery.commandKey+'. Retry with this key to recover its receipt.';
      throw recovery;
    }
  }
}

// Browser counterpart of workspaceResult: each read has its own HTTP deadline;
// waiting for accepted work never submits another query or cancels the worker.
export async function observeCommand(initial,read,{wait=ms=>new Promise(resolve=>setTimeout(resolve,ms)),onQueued=()=>{},timeoutMs=900000,clock=Date.now}={}) {
  if(initial?.workspace_command!==true)return initial;
  const deadline=clock()+timeoutMs;
  let value=initial,notified=false;
  for(;;) {
    const command=value.command;
    if(command.state==='succeeded')return command.result;
    if(['failed','cancelled','suspended','expired'].includes(command.state)) {
      const detail=command.result?.detail;
      throw Object.assign(new Error(typeof detail==='string'?detail:detail?.message||command.error||'Queued work requires reconciliation.'),
        {status:command.error_status||409,detail,commandId:command.id});
    }
    if(clock()>=deadline)throw Object.assign(new Error('The inventory request is still pending. Its database receipt is retained.'),{commandId:command.id});
    if(!notified){onQueued(command);notified=true;}
    await wait(1000);
    try{value=await read(command.id);}
    catch(error){if(error.status&&error.status<500&&error.status!==408)throw Object.assign(error,{commandId:command.id});}
  }
}
