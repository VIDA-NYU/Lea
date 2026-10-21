// Transport/coordination outcomes are independent of a solver's mathematical result.
export function isSqliteBusy(error = {}) {
  return [5,6].includes((error.errcode || error.sqlite_errorcode || 0) & 255)
    || ['SQLITE_BUSY','SQLITE_LOCKED'].includes(error.code);
}
export function requestFailure(result = {}) {
  const detail = result.body?.detail || result.detail;
  const code = result.code || detail?.code || detail?.error || result.body?.error || null;
  const status = result.status ?? result.httpStatus;
  const fenced = ['command_fenced', 'worker_fenced', 'parent_deadline_expired', 'parent_scope_mismatch',
    'workspace_reconciliation_required', 'revision_conflict', 'source_changed', 'access_denied'].includes(code);
  const retry = !fenced && (['project_busy', 'project_renaming', 'database_busy', 'retry_database_busy',
    'projection_unavailable', 'adapter_draining', 'admission_disabled', 'worker_unavailable'].includes(code)
    || status == null || [408, 425, 429, 502, 503, 504].includes(status) || status >= 500);
  return { code, status, retry, error: result.error || result.message || 'Operation could not be completed.',
    retryAfterMs: Math.max(0, Number(result.retryAfterMs) || 0) };
}

export async function awaitAdmission(request, { onWait = async () => {}, shouldCancel = () => false,
  wait = ms => new Promise(resolve => setTimeout(resolve, ms)), retryDelayMs = 1000,
  safeToRetry = true } = {}) {
  for (let attempt = 0;; attempt++) {
    let result;
    try {
      if (await shouldCancel()) return { ok: false, cancelled: true, recoverable: true,
        stopReason: 'user_stop', error: 'Stopped before admission.' };
      result = await request();
    }
    catch (error) {
      if(error.code==='ERR_SQLITE_ERROR'&&!isSqliteBusy(error))throw error;
      if (!error.status && !error.code && error.name !== 'TypeError') throw error;
      result = { ok: false, error: error.message, status: isSqliteBusy(error)?503:error.status,
        code: isSqliteBusy(error)?'database_busy':error.code, detail: error.detail };
    }
    if (result.ok) return result;
    const failure = requestFailure(result);
    if (!safeToRetry || !failure.retry) return { ...result, ...failure };
    const delay = Math.min(30000, Math.max(failure.retryAfterMs, retryDelayMs * 2 ** Math.min(attempt, 5)));
    await onWait({ ...failure, attempt: attempt + 1, nextAttemptAt: Date.now() + delay });
    await wait(delay);
  }
}

export async function retrySqliteWrite(write, fence, wait = ms => new Promise(resolve => setTimeout(resolve, ms))) {
  for (;;) {
    try { fence();return write(); }
    catch (error) {
      if (!isSqliteBusy(error)) throw error;
      // Only the local SQL mutation retries after rollback; provider/tool calls
      // are outside this closure. Ownership is checked again, including expiry.
      await wait(100);
    }
  }
}

export function probeWorkerLease(fence, onLost) {
  try { fence();return true; }
  catch(error) {
    // No new operation can pass fence() during this uncertainty. Keep the
    // process alive so its accepted callbacks can reconcile after contention.
    if (error.code !== 'database_busy') onLost(error);
    return false;
  }
}
