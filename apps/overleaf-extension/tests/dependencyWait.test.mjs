import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { dependencyWait, dependencyWaitAction } from "../extension/dependencyWaitCore.mjs";
import { paneItemActions } from "../extension/leanPaneView.mjs";
import { leaStatusLabel } from "../extension/leaStatusCore.mjs";
import { buildSourceBundleCore, sourceHashInputs } from "../extension/sourceIdentityCore.mjs";
import { buildFormalizationSourceBundle } from "../shared/proofSource.mjs";
import { buildLeanPaneManifest } from "../shared/leanPaneManifest.mjs";

const fixture = overrides => ({ status: "paused", formalizable: true, stopReason: "dependency_wait",
  waitingForDependencies: ["L"], dependencies: [{ id: "L", state: "running", ready: false,
    specification: { name: "Separation lemma" }, current: true }], ...overrides });

test("dependency waits identify the supporting result and suppress generic Resume", () => {
  const item = fixture();
  assert.match(dependencyWait(item).message, /Separation lemma.*continue automatically/);
  assert.deepEqual(paneItemActions(item).primary, { id: "dependency-view", label: "View supporting result" });
  assert.equal(paneItemActions(item).overflow.some(action => action.id === "dependency-stop"), true);
  assert.equal(paneItemActions(item).overflow.some(action => action.id === "dependency-refresh"), true);
});

test("failures retain the actual blocker and an interrupted result remains inspectable", () => {
  for (const state of ["failed", "blocked", "stale", "needs_review", "interrupted"]) {
    const wait = dependencyWait(fixture({ dependencies: [{ id: "L", state,
      specification: { name: "Separation lemma" }, blocker: { detail: "An assumption is missing." } }] }));
    assert.equal(wait.state, "blocked");
    assert.match(wait.message, /An assumption is missing/);
  }
});

test("unknown registry state cannot imply readiness or suggest Resume", () => {
  for (const dependencies of [[], [{ id: "L", ready: true, current: false }]]) {
    const wait = dependencyWait(fixture({ dependencies }));
    assert.equal(wait.state, "unavailable");
    assert.equal(dependencyWaitAction(wait).id, "dependency-refresh");
  }
});

test("stopped waits and blocked continuation require explicit recovery actions", () => {
  assert.equal(dependencyWaitAction(dependencyWait(fixture({ dependencyCancelRequested: true }))).id, "dependency-arm");
  const wait = dependencyWait(fixture({ dependencyContinuationBlocker: "Original target turn allowance was exhausted." }));
  assert.match(wait.message, /allowance was exhausted/);
  assert.equal(dependencyWaitAction(wait).id, "dependency-refresh");
  assert.equal(dependencyWait(fixture({ dependencies: [{ id: "L", ready: true }] })).state, "ready");
});

test("unknown freshness is not presented as a confirmed source change", () => {
  const assessment = { confidence: "high", scope: "source_only" };
  assert.match(leaStatusLabel({ assessment, freshness: "unknown" }), /Freshness unavailable/);
  assert.doesNotMatch(leaStatusLabel({ assessment, freshness: "unknown" }), /Last assessed version|Source changed/);
  assert.match(leaStatusLabel({ assessment, freshness: "evidence_changed" }), /Surrounding source context changed/);
});

test("browser admission and inventory capture identical evidence from unchanged LaTeX", () => {
  const content = String.raw`\begin{theorem}
% lea: formalize label=target
True.
\end{theorem}
\begin{proof}
By triviality.
\end{proof}`;
  const files = [{ path: "main.tex", content }];
  const mirror = { revision: "a".repeat(64), mirroredFileCount: 1, verified: true };
  const item = buildLeanPaneManifest({ files, mirror }).items[0];
  const browserCore = buildSourceBundleCore(item, { files, mirror });
  const digest = value => createHash("sha256").update(JSON.stringify(value)).digest("hex");
  assert.equal(digest(sourceHashInputs(browserCore).evidence), item.sourceBundle.bundleHash);
  assert.equal(digest(sourceHashInputs(browserCore).identity), item.sourceBundle.sourceIdentityHash);
  const longFiles = [{ path: "main.tex", content: "x".repeat(13000) }];
  const longTarget = { ...item, sourceStartLine: 1, sourceEndLine: 1 };
  const browser = buildSourceBundleCore(longTarget, { files: longFiles });
  assert.equal(browser.relevantSource[0].content.length, 12000);
  assert.equal(browser.relevantSource[0].truncated, true);
  assert.deepEqual(browser, Object.fromEntries(Object.entries(buildFormalizationSourceBundle(longTarget,
    { files: longFiles })).filter(([key]) => !["bundleHash", "sourceIdentityHash"].includes(key))));
});
