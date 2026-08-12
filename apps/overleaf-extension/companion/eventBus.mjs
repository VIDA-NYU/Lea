// Companion push channel (PLAN-system-hardening 3.1 / review C1).
//
// The extension used to learn about every state change by polling — statuses
// every 3 s during a run, the Lean pane every 4 s, chat every 4 s — each hit
// re-running per-target filesystem scans. This bus is the other half of the
// fix: every mutation site publishes a small typed event (keys, not payloads
// — the subscriber refetches the one thing that changed), and the companion's
// GET /events streams them to the extension as SSE.
//
// Deliberately tiny: synchronous fan-out, no history, no replay. A subscriber
// that connects late reconciles by refetching (the extension keeps a slow
// reconciliation poll for exactly that), so missed events are harmless.

export function createEventBus() {
  const listeners = new Set();
  return {
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    publish(event) {
      for (const listener of [...listeners]) {
        try {
          listener(event);
        } catch {
          // One broken subscriber (e.g. a half-closed SSE socket) must never
          // break publishing to the others — or the mutation that published.
        }
      }
    },
    get size() {
      return listeners.size;
    }
  };
}

// Publish helper used by server.mjs mutation sites. Lazily creates the bus so
// unit tests that hand-roll a bare `state` still work (and can observe events
// by reading state.eventBus afterwards). Event shape:
//   { type, overleafProjectId?, ...fields, at }
// Types (all "something changed — refetch it" signals):
//   jobs-changed         any job mutation was persisted (registration, turn
//                        progress, verdicts, terminal status, usage) — the one
//                        catch-all, published by the jobs persistence seam
//   chat-updated         a chat run started/streamed/settled for a target
//   repair-batch-updated a repair batch advanced
export function publishEvent(state, type, fields = {}) {
  if (!state) return;
  state.eventBus ||= createEventBus();
  state.eventBus.publish({
    type,
    ...fields,
    at: new Date().toISOString()
  });
}
