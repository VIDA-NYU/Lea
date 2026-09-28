import { useEffect, useState } from 'react';
import { useProofSession } from '../stores/proofSession';
import type { LeanCheckRuntime } from '../lib/types';
import { fetchLeanCheckRuntime } from '../lib/api';
import { runtimeNeedsPolling, runtimeStatusMessage } from '../lib/leanCheckRuntime.mjs';

export function LeanCheckRuntimeStatus({ sessionId, active, refreshKey }: {
  sessionId?: string; active: boolean; refreshKey: number;
}) {
  const [runtime, setRuntime] = useState<LeanCheckRuntime | null>(null);
  const manualSession = useProofSession((s) => s.manualCheckSessionId);
  const checking = active || Boolean(sessionId && manualSession === sessionId);
  useEffect(() => {
    setRuntime(null);
    if (!sessionId) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    async function refresh() {
      clearTimeout(timer);
      if (document.hidden || disposed) return;
      controller?.abort();
      const request = new AbortController();
      controller = request;
      let next: LeanCheckRuntime;
      try { next = await fetchLeanCheckRuntime(sessionId!, request.signal); }
      catch { next = { state: 'unavailable' }; }
      if (disposed || request.signal.aborted || controller !== request) return;
      setRuntime(next);
      if (runtimeNeedsPolling(next, checking)) timer = setTimeout(refresh, 5000);
    }
    const visibility = () => { if (document.hidden) { clearTimeout(timer); controller?.abort(); } else void refresh(); };
    document.addEventListener('visibilitychange', visibility);
    void refresh();
    return () => { disposed = true; clearTimeout(timer); controller?.abort(); document.removeEventListener('visibilitychange', visibility); };
  }, [sessionId, checking, refreshKey]);
  const message = runtimeStatusMessage(runtime);
  if (!message) return null;
  return <div className="reconnect-chip" role="status" title={runtime?.last_failure?.message || undefined}>{message}</div>;
}
