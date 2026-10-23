// Companion push-channel client (PLAN-system-hardening 3.1 / review C1).
//
// Owns one EventSource on the companion's GET /events and its reconnect
// policy. content.js maps the events onto its existing refresh functions and
// stretches its poll cadences to slow reconciliation intervals while the
// stream is connected — the polls stay as the fallback, they just stop being
// the primary. Pure lifecycle logic, injectable timers/EventSource, so the
// test suite can drive it deterministically (same pattern as
// editorHookWatchdog.mjs).

export const EVENTS_RECONNECT_MIN_MS = 1000;
export const EVENTS_RECONNECT_MAX_MS = 60000;
export const EVENT_TYPES = ["jobs-changed", "chat-updated", "repair-batch-updated", "resync", "project-identity-changed"];

export function createEventsClient({
  url,
  onEvent,
  onConnectionChange = null,
  eventTypes = EVENT_TYPES,
  EventSourceImpl = globalThis.EventSource,
  setTimeoutImpl = (fn, ms) => setTimeout(fn, ms),
  clearTimeoutImpl = (id) => clearTimeout(id),
  minBackoffMs = EVENTS_RECONNECT_MIN_MS,
  maxBackoffMs = EVENTS_RECONNECT_MAX_MS
}) {
  let source = null;
  let timer = null;
  let backoff = minBackoffMs;
  let connected = false;
  let stopped = true;
  const cursors = new Map();

  const setConnected = (value) => {
    if (connected === value) return;
    connected = value;
    if (onConnectionChange) onConnectionChange(value);
  };

  function scheduleReconnect() {
    if (stopped || timer !== null) return;
    timer = setTimeoutImpl(() => {
      timer = null;
      connect();
    }, backoff);
    backoff = Math.min(backoff * 2, maxBackoffMs);
  }

  function dropSource() {
    if (!source) return;
    try {
      source.close();
    } catch {
      // already closed
    }
    source = null;
  }

  function connect() {
    if (stopped || source) return;
    // url() may legitimately be null while the page/context is still
    // resolving (no project id yet) — keep retrying on the backoff clock.
    const target = typeof url === "function" ? url() : url;
    if (!target || !EventSourceImpl) {
      scheduleReconnect();
      return;
    }
    let candidate;
    try {
      const cursor = cursors.get(target);
      const resume = new URL(target);
      if (cursor !== undefined) resume.searchParams.set('after', cursor);
      candidate = new EventSourceImpl(cursor === undefined ? target : resume.href);
    } catch {
      scheduleReconnect();
      return;
    }
    source = candidate;
    candidate.onopen = () => {
      if (source !== candidate) return;
      backoff = minBackoffMs;
      setConnected(true);
    };
    for (const type of new Set([...eventTypes, 'cursor'])) {
      candidate.addEventListener(type, (event) => {
        if (source !== candidate) return;
        if (/^\d+$/.test(event?.lastEventId || '')) {
          const previous = cursors.get(target);
          if (type === 'resync' || previous === undefined || BigInt(event.lastEventId) > BigInt(previous)) {
            cursors.set(target, event.lastEventId);
            if (cursors.size > 64) cursors.delete(cursors.keys().next().value);
          }
        }
        if (type === 'cursor') return;
        let data = null;
        try {
          data = JSON.parse(event?.data ?? "null");
        } catch {
          data = null;
        }
        onEvent(type, data);
      });
    }
    // Manage reconnection ourselves (EventSource's built-in retry has no
    // backoff and never re-resolves the URL after a settings change).
    candidate.onerror = () => {
      if (source !== candidate) return;
      dropSource();
      setConnected(false);
      scheduleReconnect();
    };
  }

  return {
    start() {
      stopped = false;
      if (!source && timer === null) connect();
    },
    stop() {
      stopped = true;
      if (timer !== null) {
        clearTimeoutImpl(timer);
        timer = null;
      }
      dropSource();
      setConnected(false);
    },
    isConnected() {
      return connected;
    }
  };
}
