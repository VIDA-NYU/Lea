import test from "node:test";
import assert from "node:assert/strict";
import { renderSharedDependencies } from "../extension/leanPaneView.mjs";

function documentFixture() {
  const document = { createElement(tag) {
    return { tag, ownerDocument: document, children: [], dataset: {}, attributes: {}, listeners: {},
      appendChild(child) { this.children.push(child); return child; },
      setAttribute(key, value) { this.attributes[key] = value; },
      addEventListener(type, callback) { this.listeners[type] = callback; },
      set textContent(value) { this.text = value; },
      get textContent() { return (this.text || "") + this.children.map((child) => child.textContent).join(" "); },
      querySelectorAll() { return this.children.filter((child) => child.tag === "details" && child.open); }
    };
  } };
  return document;
}
const dependency = (changes = {}) => ({ id: "L", path: "Shared/L_123.lean", module_name: "Lea.Study.Shared.L_123",
  declaration_name: "Lea.Study.L_123", state: "failed", ready: false, targetLabels: ["A", "B"],
  specification: { name: "L", signature: "(h : P) : Q", sources: [{ file: "paper.tex", excerpt: "Assume P" }] },
  blocker: { category: "source_gap", detail: "Required bound is unspecified" },
  attempts: [{ generation: 1, state: "failed", session_id: "session" }], ...changes });

test("one shared failure exposes its obligation, both consumers, evidence, and retry", async () => {
  const document = documentFixture();
  const calls = [];
  const root = renderSharedDependencies([dependency()], { document, retry: async (dep) => calls.push(dep.id), open: () => {} });
  assert.match(root.textContent, /Waiting targets: A, B/);
  assert.match(root.textContent, /\(h : P\) : Q/);
  assert.match(root.textContent, /paper.tex/);
  assert.match(root.textContent, /Attempt 1: failed/);
  assert.match(root.textContent, /Required bound is unspecified/);
  const detail = root.children.find((node) => node.tag === "details");
  const retry = detail.children.find((node) => node.tag === "button");
  await retry.listeners.click();
  assert.deepEqual(calls, ["L"]);
});

test("verified reuse preserves disclosures and removes retry; interrupted work offers resume", () => {
  const document = documentFixture();
  const options = { document, key: "progress", retry: async () => {} };
  const before = renderSharedDependencies([dependency()], options);
  before.children.find((node) => node.tag === "details").open = true;
  const after = renderSharedDependencies([dependency({ ready: true, state: "ready", reused: true })], options);
  assert.match(after.textContent, /Reused · L/);
  assert.ok(!after.textContent.includes("Retry shared result"));
  assert.equal(after.children.find((node) => node.tag === "details").open, true);
  const interrupted = renderSharedDependencies([dependency({ state: "interrupted" })], options);
  assert.match(interrupted.textContent, /Resume shared result/);
});
