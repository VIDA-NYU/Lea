import assert from "node:assert/strict";
import test from "node:test";
import { createEventsClient } from "../extension/eventsClient.mjs";

// Manual fakes: a controllable EventSource and timer queue, so the client's
// connect/reconnect/backoff lifecycle is driven deterministically.
function makeHarness({ url = () => "http://c/events" } = {}) {
  const sources = [];
  class FakeEventSource {
    constructor(target) {
      this.target = target;
      this.listeners = new Map();
      this.closed = false;
      this.onopen = null;
      this.onerror = null;
      sources.push(this);
    }
    addEventListener(type, fn) {
      this.listeners.set(type, fn);
    }
    close() {
      this.closed = true;
    }
    emit(type, data) {
      this.listeners.get(type)?.({ data: JSON.stringify(data) });
    }
  }
  const timers = new Map();
  let nextId = 1;
  const events = [];
  const connection = [];
  const client = createEventsClient({
    url,
    onEvent: (type, data) => events.push({ type, data }),
    onConnectionChange: (connected) => connection.push(connected),
    EventSourceImpl: FakeEventSource,
    setTimeoutImpl: (fn, ms) => {
      const id = nextId++;
      timers.set(id, { fn, ms });
      return id;
    },
    clearTimeoutImpl: (id) => timers.delete(id),
    minBackoffMs: 100,
    maxBackoffMs: 800
  });
  const fireNextTimer = () => {
    const [id, timer] = [...timers.entries()][0] || [];
    if (!timer) throw new Error("no pending timer");
    timers.delete(id);
    timer.fn();
    return timer.ms;
  };
  return { client, sources, timers, events, connection, fireNextTimer };
}

test("connects, reports connection, and forwards typed events", () => {
  const h = makeHarness();
  h.client.start();
  assert.equal(h.sources.length, 1);
  h.sources[0].onopen();
  assert.deepEqual(h.connection, [true]);

  h.sources[0].emit("jobs-changed", { at: "t" });
  h.sources[0].emit("chat-updated", { targetKey: "k" });
  assert.deepEqual(h.events.map((e) => e.type), ["jobs-changed", "chat-updated"]);
  assert.equal(h.events[1].data.targetKey, "k");
  assert.equal(h.client.isConnected(), true);
});

test("reconnects with exponential backoff and resets it on success", () => {
  const h = makeHarness();
  h.client.start();
  h.sources[0].onopen();

  h.sources[0].onerror();
  assert.deepEqual(h.connection, [true, false]);
  assert.equal(h.fireNextTimer(), 100, "first retry at min backoff");

  h.sources[1].onerror();
  assert.equal(h.fireNextTimer(), 200, "backoff doubles");
  h.sources[2].onerror();
  assert.equal(h.fireNextTimer(), 400);

  h.sources[3].onopen();
  assert.equal(h.client.isConnected(), true);
  h.sources[3].onerror();
  assert.equal(h.fireNextTimer(), 100, "success resets the backoff");
});

test("backoff is capped at maxBackoffMs", () => {
  const h = makeHarness();
  h.client.start();
  for (const expected of [100, 200, 400, 800, 800]) {
    h.sources.at(-1).onerror();
    assert.equal(h.fireNextTimer(), expected);
  }
});

test("a null url retries instead of crashing (project id not resolvable yet)", () => {
  let target = null;
  const h = makeHarness({ url: () => target });
  h.client.start();
  assert.equal(h.sources.length, 0, "no EventSource constructed without a url");
  target = "http://c/events?projectId=doc-a";
  h.fireNextTimer();
  assert.equal(h.sources.length, 1);
  assert.equal(h.sources[0].target, "http://c/events?projectId=doc-a");
});

test("stop closes the source, cancels timers, and stays stopped", () => {
  const h = makeHarness();
  h.client.start();
  h.sources[0].onopen();
  h.client.stop();
  assert.equal(h.sources[0].closed, true);
  assert.equal(h.client.isConnected(), false);
  assert.equal(h.timers.size, 0);
  // A late error from the closed source must not schedule a reconnect.
  h.sources[0].onerror();
  assert.equal(h.timers.size, 0);
});

test("events from a superseded source are ignored", () => {
  const h = makeHarness();
  h.client.start();
  const first = h.sources[0];
  first.onerror();
  h.fireNextTimer();
  first.emit("jobs-changed", {});
  assert.equal(h.events.length, 0, "stale source events are dropped");
});
