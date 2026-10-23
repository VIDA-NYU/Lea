/** Snapshot first, then one cursor-addressed observer. Never owns UI selection. */
export function observeSessionList({ refresh, readCursor, onChanged = () => {},
  createSource = url => new EventSource(url), setTimer = setTimeout, clearTimer = clearTimeout }) {
  let source, timer, connectedOnce = false, stopped = false, refreshing = false, dirty = false, delay = 1000;
  const retry = () => {
    if (stopped || timer) return;
    timer = setTimer(() => { timer = undefined; void connect(); }, delay);
    delay = Math.min(delay * 2, 30000);
  };
  const update = async () => {
    dirty = true;
    if (refreshing) return;
    refreshing = true;
    try {
      while (dirty && !stopped) {
        dirty = false;
        await refresh();
        if (!stopped) onChanged();
      }
    } finally { refreshing = false; }
  };
  const connect = async () => {
    try {
      await refresh();
      if (stopped) return;
      // Initial navigation owns its detail load. Reconnect must invalidate any
      // already-open detail before advancing past changes missed offline.
      if (connectedOnce) onChanged();
      const cursor = readCursor();
      const owned = source = createSource(`/api/sessions/events${cursor === undefined ? '' : `?after=${cursor}`}`);
      connectedOnce = true;
      owned.onopen = () => { if (!stopped && source === owned) delay = 1000; };
      owned.addEventListener('sessions_changed', () => {
        if (!stopped && source === owned) void update().catch(() => {
          if (stopped || source !== owned) return;
          owned.close(); source = undefined; retry();
        });
      });
      owned.onerror = () => {
        if (stopped || source !== owned) return;
        owned.close(); source = undefined; retry();
      };
    } catch { retry(); }
  };
  void connect();
  return () => { stopped = true; source?.close(); if (timer) clearTimer(timer); };
}
