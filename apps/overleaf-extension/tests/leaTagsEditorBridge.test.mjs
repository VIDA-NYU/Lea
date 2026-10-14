import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { installLeaTagsEditorBridge } from "../extension/leaTagsEditorBridge.mjs";
const styleText = fs.readFileSync(new URL("../extension/assets/lea-tags.sty", import.meta.url), "utf8");

function harness(options = {}) {
  const listeners = new Map();
  const documentListeners = new Map();
  const replies = [];
  const docs = new Map([
    ["main.tex", { id: "main", source: "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}" }],
    ["lea-tags.sty", { id: "style", source: options.styleSource || "" }]
  ]);
  let path = "main.tex";
  let activeView = makeView(docs.get(path));
  let exposedView = activeView;
  let exposedId = "main";
  let userClick;
  let delayed;
  function makeView(value) {
    return {
      state: { readOnly: Boolean(options.readOnly), doc: { record: value, toString: () => value.source, get length() { return value.source.length; } }, selection: { main: { anchor: 45, head: 45 } } },
      dispatch(transaction) {
        const value = this.state.doc.record;
        value.transactions ||= [];
        value.transactions.push(transaction);
        if (transaction.changes) value.source = value.source.slice(0, transaction.changes.from) + transaction.changes.insert + value.source.slice(transaction.changes.from);
      },
      contentDOM: { getAttribute: () => options.readOnly ? "false" : "true" }
    };
  }
  const win = {
    location: { pathname: "/project/project" },
    overleaf: { unstable: { store: { get: key => key === "editor.view" ? exposedView : exposedId } } },
    document: {
      querySelector: () => ({ getAttribute: () => docs.get(path).id }),
      addEventListener: (name, callback) => documentListeners.set(name, callback)
    },
    addEventListener(name, callback) { listeners.set(name, callback); },
    postMessage(data) { replies.push(data); },
    setInterval(callback) { this.tick = callback; },
    setTimeout(callback) {
      return setTimeout(() => { if (delayed) { delayed(); delayed = null; } callback(); }, 1);
    }
  };
  function select(next) {
    path = next;
    if (!options.reuseView) activeView = makeView(docs.get(path));
    else if (options.switchBuffer) activeView.state = makeView(docs.get(path)).state;
    exposedView = activeView;
    exposedId = docs.get(path).id;
  }
  installLeaTagsEditorBridge({
    window: win, getView: () => activeView, getActiveDocPath: () => path, timeoutMs: options.timeoutMs || 50,
    openDocByPath(next) {
      if (options.noOpen) return false;
      if (options.delayed) delayed = () => select(next);
      else select(next);
      return true;
    }
  });
  let sequence = 0;
  async function request(action, payload = {}) {
    const requestId = String(++sequence);
    listeners.get("message")({ source: win, data: { type: "OL_LEAN_TAGS_SETUP_REQUEST", operationId: "operation", requestId, projectId: "project", action, ...payload } });
    for (let i = 0; i < 100; i++) {
      const reply = replies.find(reply => reply.requestId === requestId);
      if (reply) return reply;
      await new Promise(resolve => setTimeout(resolve, 2));
    }
    throw new Error("No bridge reply");
  }
  return {
    docs, win, request, select, get path() { return path; },
    breakIdentity() { exposedView = {}; },
    wrongId() { exposedId = "other"; },
    userSwitch() { documentListeners.get("click")({ isTrusted: true, target: { closest: () => ({}) } }); },
    spoof(message) { listeners.get("message")({ source: {}, data: message }); }
  };
}

test("populates only the exact empty style document and enables the main in separate editor transactions", async () => {
  const h = harness();
  assert.equal((await h.request("begin")).ok, true);
  assert.equal((await h.request("populate", { document: { id: "style", path: "lea-tags.sty" }, styleText })).ok, true);
  assert.equal(h.docs.get("lea-tags.sty").source, styleText);
  assert.equal((await h.request("enable", { document: { id: "main", path: "main.tex" } })).result.changed, true);
  assert.equal(h.docs.get("main.tex").transactions.length, 1);
  assert.equal(h.docs.get("lea-tags.sty").transactions.length, 1);
  assert.equal((await h.request("enable", { document: { id: "main", path: "main.tex" } })).result.changed, false);
  assert.equal(h.docs.get("main.tex").transactions.length, 1);
});

test("a selected tab cannot authorize a mismatched editor view or document ID", async () => {
  for (const breakIdentity of ["breakIdentity", "wrongId"]) {
    const h = harness();
    h[breakIdentity]();
    const reply = await h.request("begin");
    assert.equal(reply.ok, false);
    assert.equal(reply.error.code, "editor_identity");
    assert.ok(!h.docs.get("main.tex").transactions);
  }
});

test("does not replace a collaborator's text or populate an arbitrary file", async () => {
  const h = harness({ styleSource: "Someone else's change" });
  await h.request("begin");
  const reply = await h.request("populate", { document: { id: "style", path: "lea-tags.sty" }, styleText });
  assert.equal(reply.error.code, "style_changed");
  assert.equal(h.docs.get("lea-tags.sty").source, "Someone else's change");
  const main = await h.request("populate", { document: { id: "main", path: "main.tex" }, styleText });
  assert.equal(main.ok, false);
});

test("waits for a delayed editor and times out when the expected document cannot open", async () => {
  const delayed = harness({ delayed: true });
  await delayed.request("begin");
  assert.equal((await delayed.request("read", { document: { id: "style", path: "lea-tags.sty" } })).ok, true);
  const missing = harness({ noOpen: true, timeoutMs: 2 });
  await missing.request("begin");
  assert.equal((await missing.request("read", { document: { id: "style", path: "lea-tags.sty" } })).error.code, "editor_timeout");
});

test("a new tab and store ID cannot rebind the previous document's buffer", async () => {
  const h = harness({ reuseView: true, timeoutMs: 2 });
  await h.request("begin");
  const reply = await h.request("populate", { document: { id: "style", path: "lea-tags.sty" }, styleText });
  assert.equal(reply.error.code, "editor_timeout");
  assert.ok(!h.docs.get("main.tex").transactions);
  assert.equal(h.docs.get("lea-tags.sty").source, "");
});

test("a reused view can safely switch to the new document's buffer", async () => {
  const h = harness({ reuseView: true, switchBuffer: true });
  await h.request("begin");
  const reply = await h.request("populate", { document: { id: "style", path: "lea-tags.sty" }, styleText });
  assert.equal(reply.ok, true);
  assert.equal(h.docs.get("lea-tags.sty").source, styleText);
  assert.ok(!h.docs.get("main.tex").transactions);
});

test("read-only documents refuse writes and genuine file clicks cancel remaining work", async () => {
  const readOnly = harness({ readOnly: true });
  await readOnly.request("begin");
  assert.equal((await readOnly.request("enable", { document: { id: "main", path: "main.tex" } })).error.code, "permission");
  const switched = harness();
  await switched.request("begin");
  switched.userSwitch();
  assert.equal((await switched.request("enable", { document: { id: "main", path: "main.tex" } })).error.code, "cancelled");
  assert.ok(!switched.docs.get("main.tex").transactions);
});

test("project navigation cancels and restoration does not override an independent file switch", async () => {
  const h = harness();
  await h.request("begin");
  h.win.location.pathname = "/project/other";
  h.win.tick();
  assert.equal((await h.request("enable", { document: { id: "main", path: "main.tex" } })).error.code, "cancelled");
  const switched = harness();
  await switched.request("begin");
  switched.select("lea-tags.sty");
  await switched.request("restore");
  assert.equal(switched.path, "lea-tags.sty");
});

test("keyboard file switching cancels before the next operation even before polling runs", async () => {
  const h = harness();
  await h.request("begin");
  h.select("lea-tags.sty");
  const reply = await h.request("enable", { document: { id: "main", path: "main.tex" } });
  assert.equal(reply.error.code, "cancelled");
  assert.equal(h.path, "lea-tags.sty");
  assert.ok(!h.docs.get("main.tex").transactions);
});
