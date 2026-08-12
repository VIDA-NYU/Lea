import assert from "node:assert/strict";
import test from "node:test";
import {
  canChatPaneItem,
  chatBubbleClass,
  chatComposerEnabled,
  chatRunActive,
  nextChatState,
  paneItemToChatTarget
} from "../extension/leanPaneView.mjs";

test("canChatPaneItem requires a declaration name or marker label", () => {
  assert.equal(canChatPaneItem({ leanDeclarationName: "compactness_criterion" }), true);
  assert.equal(canChatPaneItem({ label: "compactness_criterion" }), true);
  assert.equal(canChatPaneItem({}), false);
  assert.equal(canChatPaneItem(null), false);
});

test("paneItemToChatTarget shapes the companion chat payload", () => {
  const item = {
    leanKind: "theorem",
    leanDeclarationName: "compactness_criterion",
    label: "compactness_criterion",
    latexLabel: "thm:compactness",
    sourceFile: "main.tex",
    sourceStartLine: 2,
    sourceEndLine: 5,
    sourceHash: "h1",
    naturalLanguageLatex: "Every open cover has a finite subcover.",
    targetUses: ["finite_subcover"],
    targetContext: "Use compactness.",
    leanArtifactPath: "workspace/proofs/Lea/Project/compactness.lean",
    status: "invalid"
  };

  assert.deepEqual(paneItemToChatTarget(item, "project-1"), {
    overleafProjectId: "project-1",
    targetKind: "theorem",
    targetLabel: "compactness_criterion",
    latexLabel: "thm:compactness",
    sourceFile: "main.tex",
    sourceStartLine: 2,
    sourceEndLine: 5,
    sourceHash: "h1",
    naturalLanguageLatex: "Every open cover has a finite subcover.",
    targetUses: ["finite_subcover"],
    targetContext: "Use compactness.",
    leanDeclarationName: "compactness_criterion",
    recordedProofPath: "workspace/proofs/Lea/Project/compactness.lean",
    status: "invalid"
  });
});

test("paneItemToChatTarget maps a definition item to a definition target", () => {
  const target = paneItemToChatTarget({ leanKind: "def", label: "locally_finite_family" }, "project-1");
  assert.equal(target.targetKind, "definition");
  assert.equal(target.targetLabel, "locally_finite_family");
});

test("nextChatState resolves the panel state from flags and response", () => {
  assert.equal(nextChatState({ loading: true }), "loading-session");
  assert.equal(nextChatState({ response: { ok: true, status: "no-session" } }), "no-session");
  assert.equal(nextChatState({ response: { ok: true, status: "answered", messages: [] } }), "ready");
  assert.equal(nextChatState({ sending: true, response: { ok: true, status: "answered" } }), "running");
  assert.equal(nextChatState({ response: { ok: true, activeRun: { id: "r1", status: "running" } } }), "running");
  assert.equal(nextChatState({ response: { ok: false, error: "adapter_unavailable" } }), "adapter-unavailable");
  assert.equal(nextChatState({ error: new Error("boom") }), "error");
});

test("chatComposerEnabled only allows input when loaded and idle", () => {
  assert.equal(chatComposerEnabled("ready"), true);
  assert.equal(chatComposerEnabled("no-session"), true);
  assert.equal(chatComposerEnabled("running"), false);
  assert.equal(chatComposerEnabled("loading-session"), false);
  assert.equal(chatComposerEnabled("error"), false);
  assert.equal(chatComposerEnabled("adapter-unavailable"), false);
});

test("chatRunActive is true only while running", () => {
  assert.equal(chatRunActive("running"), true);
  assert.equal(chatRunActive("ready"), false);
});

test("chatBubbleClass distinguishes user and assistant bubbles", () => {
  assert.equal(chatBubbleClass("user"), "ol-lean-chat-bubble-user");
  assert.equal(chatBubbleClass("assistant"), "ol-lean-chat-bubble-assistant");
  assert.equal(chatBubbleClass("system"), "ol-lean-chat-bubble-assistant");
});
