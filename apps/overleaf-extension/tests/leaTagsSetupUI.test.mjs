import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";
import { mountLeaTagsSetup } from "../extension/leaTagsSetupUI.mjs";

class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.events = {}; this.attributes = {};
    this.classList = { toggle() {} };
  }
  append(...children) { for (const child of children) this.appendChild(child); }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(name, callback) { this.events[name] = callback; }
  click() { return this.events.click?.(); }
  remove() {}
}

function harness(options = {}) {
  const body = new Element("body");
  const slot = new Element("div");
  const listeners = new Set();
  const messages = [];
  const copied = [];
  const downloads = [];
  let calls = 0;
  let release;
  const win = {
    crypto: { randomUUID: () => "operation" }, fetch: async () => {},
    URL: { createObjectURL(blob) { downloads.push(blob); return "blob:style"; }, revokeObjectURL() {} },
    navigator: { clipboard: { async writeText(text) { copied.push(text); } } },
    addEventListener: (_type, listener) => listeners.add(listener),
    removeEventListener: (_type, listener) => listeners.delete(listener),
    postMessage(data) {
      messages.push(data);
      if (data.action === "cancel") return;
      // An unrelated reply must not resolve the pending request.
      for (const listener of listeners) listener({ source: win, data: { ...data, type: "OL_LEAN_TAGS_SETUP_RESULT", requestId: "unrelated", ok: false } });
      for (const listener of listeners) listener({ source: win, data: { ...data, type: "OL_LEAN_TAGS_SETUP_RESULT", ok: true, result: {} } });
    }
  };
  const controls = mountLeaTagsSetup({
    slot, window: win, document: { body, createElement: tag => new Element(tag) },
    chrome: { runtime: { sendMessage(_message, callback) { callback({ ok: true, text: "bundled style" }); } } },
    getProjectId: () => "project", makeClient: () => ({}),
    async runSetup({ signal }) {
      calls++;
      if (options.pending) await new Promise((resolve, reject) => {
        release = resolve;
        signal.addEventListener("abort", () => reject(new Error("Setup stopped.")), { once: true });
      });
      if (options.error) throw new Error(options.error);
      return { changed: false, message: "Installed", manual: Boolean(options.manual) };
    }
  });
  return { slot, win, controls, copied, downloads, messages, get calls() { return calls; }, release: () => release?.() };
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test("setup row is accessible, prevents concurrent clicks, and ignores unrelated replies", async () => {
  const h = harness({ pending: true });
  const [button, hint, status, fallback] = h.slot.children;
  assert.equal(button.textContent, "Add Lea tags");
  assert.match(hint.textContent, /Create the style file/);
  assert.equal(status.attributes["aria-live"], "polite");
  const first = button.click();
  await flush();
  assert.equal(button.disabled, true);
  await button.click();
  assert.equal(h.calls, 1);
  h.release();
  await first;
  assert.equal(button.disabled, false);
  assert.equal(status.textContent, "Installed");
  assert.equal(fallback.hidden, true);
  assert.ok(h.messages.some(message => message.action === "restore"));
  h.controls.dispose();
});

test("manual completion offers a working bundled download and clipboard package line", async () => {
  const h = harness({ manual: true });
  await h.slot.children[0].click();
  const fallback = h.slot.children[3];
  assert.equal(fallback.hidden, false);
  await fallback.children[0].click();
  assert.equal(await h.downloads[0].text(), "bundled style");
  await fallback.children[1].click();
  assert.deepEqual(h.copied, ["\\usepackage{lea-tags}"]);
  h.controls.dispose();
});

test("closing the pane cancels pending work and does not restore the editor", async () => {
  const h = harness({ pending: true });
  const pending = h.slot.children[0].click();
  await flush();
  h.controls.dispose();
  await pending;
  assert.ok(h.messages.some(message => message.action === "cancel"));
  assert.ok(!h.messages.some(message => message.action === "restore"));
});

test("failed setup retains actionable manual controls", async () => {
  const h = harness({ error: "Read-only project" });
  await h.slot.children[0].click();
  assert.equal(h.slot.children[2].textContent, "Read-only project");
  assert.equal(h.slot.children[3].hidden, false);
  assert.equal(h.slot.children[0].disabled, false);
  h.controls.dispose();
});

test("background style access requires this extension, the main frame, and an Overleaf project", async () => {
  let listener;
  let reads = 0;
  const script = fs.readFileSync(new URL("../extension/background.js", import.meta.url), "utf8");
  vm.runInNewContext(script, {
    chrome: { runtime: { id: "extension", onMessage: { addListener(fn) { listener = fn; } }, getURL: path => "chrome-extension://extension/" + path } },
    fetch: async () => { reads++; return { ok: true, text: async () => "canonical style" }; },
    URL
  });
  const sender = { id: "extension", frameId: 0, tab: {}, url: "https://www.overleaf.com/project/0123456789abcdef01234567" };
  for (const patch of [{ id: "other" }, { frameId: 1 }, { tab: null }, { url: "http://127.0.0.1:5173/" }]) {
    let response;
    assert.equal(listener({ type: "READ_LEA_TAGS_STYLE" }, { ...sender, ...patch }, value => { response = value; }), false);
    assert.equal(response.ok, false);
  }
  const response = await new Promise(resolve => assert.equal(listener({ type: "READ_LEA_TAGS_STYLE" }, sender, resolve), true));
  assert.equal(response.text, "canonical style");
  assert.equal(reads, 1);
});
