// Observation owns no execution handles. Losing this observer never cancels work.
export function assertDatabaseBoundary(health) {
  const boundary=health?.capabilities?.database_boundary;
  if (boundary?.version!==1 || boundary.ui_access!=='database' || boundary.command_direction!=='ui_to_worker'
      || boundary.processes!=='separate' || ['queries','changes','commands','source_publications','editor'].some(key=>boundary[key]!==1)) {
    throw Object.assign(new Error('This UI requires the database-only API and separate Lea worker. Update the services together.'),
      {status:503,code:'database_boundary_incompatible'});
  }
  return boundary;
}

export async function workspaceResult(initial, read, { wait = ms => new Promise(resolve => setTimeout(resolve, ms)), timeoutMs = 900000 } = {}) {
  if (initial?.workspace_command !== true) return initial;
  const deadline = Date.now() + timeoutMs;
  let value = initial;
  while (true) {
    const command = value.command;
    if (command.state === 'succeeded') return command.result;
    if (['failed', 'cancelled', 'suspended', 'expired'].includes(command.state)) {
      const detail = command.result?.detail;
      throw Object.assign(new Error(typeof detail === 'string' ? detail : detail?.message || command.error || 'Workspace work requires reconciliation.'),
        { status: command.error_status || 409, detail, commandId: command.id });
    }
    if (Date.now() >= deadline) {
      throw Object.assign(new Error('Workspace work is still pending. Its receipt is retained; it can be observed again.'), { commandId: command.id });
    }
    await wait(150);
    try {
      value = await read(command.id);
    } catch (error) {
      if (error?.status && error.status < 500) throw Object.assign(error, {commandId:command.id});
      // A restarted API can make an observation temporarily unavailable. The
      // accepted command and its original receipt retain ownership in SQL.
      await wait(500);
    }
  }
}

export async function submitWithRecovery(send, lookup) {
  try {
    return await send();
  } catch (error) {
    // Only a transport failure is ambiguous. An HTTP rejection must be surfaced.
    if (error?.status) throw error;
    try { return await lookup(); } catch (lookupError) {
      if (lookupError?.status !== 404) throw error;
    }
    return send(); // Same idempotency key, same bytes, same base.
  }
}
