import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { createEventBus, publishEvent } from "../companion/eventBus.mjs";
import { createServer } from "../companion/server.mjs";

test("bus fans out to every subscriber and unsubscribe detaches", () => {
  const bus = createEventBus();
  const seenA = [];
  const seenB = [];
  const offA = bus.subscribe((event) => seenA.push(event.type));
  bus.subscribe((event) => seenB.push(event.type));

  bus.publish({ type: "jobs-changed" });
  offA();
  bus.publish({ type: "chat-updated" });

  assert.deepEqual(seenA, ["jobs-changed"]);
  assert.deepEqual(seenB, ["jobs-changed", "chat-updated"]);
});

test("a throwing subscriber cannot break publishing to the others", () => {
  const bus = createEventBus();
  const seen = [];
  bus.subscribe(() => {
    throw new Error("half-closed socket");
  });
  bus.subscribe((event) => seen.push(event.type));

  bus.publish({ type: "jobs-changed" });
  assert.deepEqual(seen, ["jobs-changed"]);
});

test("publishEvent lazily creates the bus on bare states and stamps fields", () => {
  const state = {};
  publishEvent(state, "jobs-changed", {});
  assert.ok(state.eventBus, "bus created on demand");

  const seen = [];
  state.eventBus.subscribe((event) => seen.push(event));
  publishEvent(state, "chat-updated", { overleafProjectId: "doc-a", targetKey: "k" });
  assert.equal(seen.length, 1);
  assert.equal(seen[0].type, "chat-updated");
  assert.equal(seen[0].overleafProjectId, "doc-a");
  assert.equal(seen[0].targetKey, "k");
  assert.ok(seen[0].at, "events carry a timestamp");
});

// HTTP-level contract: GET /events streams bus events as SSE, filtered by
// ?projectId=, with project-less events (the coarse jobs-changed) passing all
// filters. Reads the real socket the extension's EventSource will read.
test("GET /events streams bus events as SSE with project filtering", async () => {
  const appDir = await fs.mkdtemp(path.join(os.tmpdir(), "overleaf-events-"));
  const server = await createServer({
    settingsPath: path.join(appDir, "settings.json"),
    jobsPath: path.join(appDir, "jobs.json"),
    chatSessionsPath: path.join(appDir, "chatSessions.json"),
    env: {}
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const baseUrl = `http://127.0.0.1:${server.address().port}`;

  try {
    const response = await fetch(`${baseUrl}/events?projectId=doc-a`);
    assert.equal(response.status, 200);
    assert.match(response.headers.get("content-type") || "", /text\/event-stream/);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const readUntil = async (needle) => {
      const deadline = Date.now() + 3000;
      while (!buffer.includes(needle)) {
        if (Date.now() > deadline) throw new Error(`timed out waiting for ${needle} in:\n${buffer}`);
        const { value, done } = await reader.read();
        if (done) throw new Error(`stream ended before ${needle} arrived:\n${buffer}`);
        buffer += decoder.decode(value);
      }
    };

    await readUntil("event: hello");

    const bus = server.leaState.eventBus;
    bus.publish({ type: "chat-updated", overleafProjectId: "doc-b", targetKey: "other" });
    bus.publish({ type: "jobs-changed" });
    bus.publish({ type: "chat-updated", overleafProjectId: "doc-a", targetKey: "mine" });

    await readUntil('"targetKey":"mine"');
    assert.ok(buffer.includes("event: jobs-changed"), "project-less events pass the filter");
    assert.ok(!buffer.includes('"targetKey":"other"'), "other projects' events are filtered out");

    await reader.cancel();
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await fs.rm(appDir, { recursive: true, force: true });
  }
});
