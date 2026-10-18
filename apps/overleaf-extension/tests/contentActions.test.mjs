import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { buildLeanPaneManifest } from "../shared/leanPaneManifest.mjs";

const repoRoot = path.resolve(import.meta.dirname, "../../..");
const contentScriptPath = path.join(repoRoot, "apps/overleaf-extension/extension/content.js");
const contentScript = fs.readFileSync(contentScriptPath, "utf8");
const modelPickerScriptPath = path.join(repoRoot, "apps/overleaf-extension/extension/modelPicker.js");
const modelPickerScript = fs.readFileSync(modelPickerScriptPath, "utf8");
const contentStyles = fs.readFileSync(
  path.join(repoRoot, "apps/overleaf-extension/extension/content.css"),
  "utf8"
);

const CASES = [
  ["unformalized", { status: "unformalized", leaSessionId: "stale-session" }, false],
  ["unknown", { status: "unknown", leaSessionId: "stale-session" }, false],
  ["formalized", { status: "formalized" }, true],
  ["disproved", { status: "disproved" }, true],
  ["in_progress", { status: "in_progress" }, true],
  ["sorry_stub", { status: "sorry_stub" }, true],
  ["failed with unformalized effective status", { status: "failed", effectiveStatus: "unformalized", leaSessionId: "stale-session" }, false],
  ["failed with sorry_stub effective status", { status: "failed", effectiveStatus: "sorry_stub" }, true]
];

test("project activity remains visible with the pane closed and no editor targets", async () => {
  let projectActivity = { activeJobs: 1, activeBatches: 0 };
  const harness = createContentHarness({ status: "unformalized" }, {}, {
    fetchOverride: async (url) => url.endsWith("/statuses")
      ? { ok: true, async json() { return { statuses: {}, projectActivity }; } }
      : undefined
  });
  await harness.setVisibility(false);
  await harness.runTimersAt(250);
  const button = harness.document.body.querySelector(".ol-lean-pane-trigger");
  assert.equal(button.classList.contains("is-working"), true);
  assert.match(button.attributes["aria-label"], /Lea is working/);
  assert.equal(harness.countSelector(".ol-lean-project-pane"), 0);
  const request = harness.fetchCalls.find((call) => call.url.endsWith("/statuses"));
  assert.deepEqual(JSON.parse(request.options.body).targets, []);

  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickButtonLabel("Close Lean pane");
  assert.equal(button.classList.contains("is-working"), true);
  assert.equal(button.attributes["aria-expanded"], "false");

  projectActivity = { activeJobs: 0, activeBatches: 0 };
  await harness.runTimersAt(3000);
  assert.equal(button.classList.contains("is-working"), false);
  assert.equal(button.attributes["aria-label"], "Open Lean project pane");
});

test("a queued batch keeps the loading wheel active between items and stops when paused", async () => {
  let projectActivity = { activeJobs: 0, activeBatches: 1 };
  const harness = createContentHarness({ status: "formalized" }, {}, {
    fetchOverride: async (url) => url.endsWith("/statuses")
      ? { ok: true, async json() { return { statuses: {}, projectActivity }; } }
      : undefined
  });
  await harness.loadStatusForVisibleTheorem();
  const button = harness.document.body.querySelector(".ol-lean-pane-trigger");
  assert.equal(button.classList.contains("is-working"), true);
  projectActivity = { activeJobs: 0, activeBatches: 0 };
  await harness.runTimersAt(3000);
  assert.equal(button.classList.contains("is-working"), false);
});

test("the loading wheel starts during formalization admission and clears on rejection", async () => {
  let rejectStart;
  const harness = createContentHarness({ status: "unformalized" }, {}, {
    storage: { leaTexMirrorEnabled: false },
    fetchOverride: async (url) => {
      if (url.endsWith("/formalize")) return new Promise((resolve) => {
        rejectStart = () => resolve({ ok: false, status: 400, async json() { return { message: "Cannot start this run." }; } });
      });
    }
  });
  await harness.loadStatusForVisibleTheorem();
  harness.openTargetPopover();
  harness.clickButtonText("Formalize");
  const button = harness.document.body.querySelector(".ol-lean-pane-trigger");
  assert.equal(button.classList.contains("is-working"), true);
  await flushPromises();
  assert.ok(rejectStart);
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickButtonLabel("Close Lean pane");
  rejectStart();
  await flushPromises();
  assert.equal(button.classList.contains("is-working"), false);
});

test("a status response from before admission cannot clear a newly started formalization", async () => {
  let releaseStatus;
  let delayStatus = false;
  const harness = createContentHarness({ status: "unformalized" }, {}, {
    storage: { leaTexMirrorEnabled: false },
    fetchOverride: async (url) => {
      if (!url.endsWith("/statuses")) return;
      const response = { ok: true, async json() { return {
        statuses: { "theorem:demo_theorem": { status: "unformalized" } },
        projectActivity: { activeJobs: 0, activeBatches: 0 }
      }; } };
      if (delayStatus) return new Promise((resolve) => { releaseStatus = () => resolve(response); });
      return response;
    }
  });
  await harness.loadStatusForVisibleTheorem();
  delayStatus = true;
  await harness.runTimersAt(30000);
  assert.ok(releaseStatus);
  const releasePreviousStatus = releaseStatus;
  harness.openTargetPopover();
  harness.clickButtonText("Formalize");
  await flushPromises();
  const button = harness.document.body.querySelector(".ol-lean-pane-trigger");
  assert.equal(button.classList.contains("is-working"), true);
  releasePreviousStatus();
  await flushPromises();
  assert.equal(button.classList.contains("is-working"), true);
  releaseStatus();
  await flushPromises();
  assert.equal(button.classList.contains("is-working"), false);
});

test("project activity retries after a companion outage and reconciles on tab visibility", async () => {
  let failing = false;
  let activeJobs = 1;
  const harness = createContentHarness({ status: "unformalized" }, {}, {
    fetchOverride: async (url) => {
      if (!url.endsWith("/statuses")) return;
      if (failing) throw new Error("Companion unavailable");
      return { ok: true, async json() { return { statuses: {}, projectActivity: { activeJobs, activeBatches: 0 } }; } };
    }
  });
  await harness.loadStatusForVisibleTheorem();
  const button = harness.document.body.querySelector(".ol-lean-pane-trigger");
  await harness.setVisibility(true);
  assert.equal(button.dataset.pageHidden, "true");
  const callsWhileHidden = harness.fetchCalls.length;
  await harness.runTimersAt(3000);
  assert.equal(harness.fetchCalls.length, callsWhileHidden);
  failing = true;
  await harness.setVisibility(false);
  await harness.runTimersAt(250);
  assert.equal(button.dataset.pageHidden, "false");
  assert.ok(harness.pendingTimers().includes(30000));
  failing = false;
  activeJobs = 0;
  await harness.runTimersAt(30000);
  assert.equal(button.classList.contains("is-working"), false);
});

for (const [name, statusInfo, shouldShow] of CASES) {
  test(`View in Lea UI visibility: ${name}`, async () => {
    const harness = createContentHarness(statusInfo);
    await harness.loadStatusForVisibleTheorem();

    harness.window.postMessage({
      type: "OL_LEAN_TARGET_CLICK",
      target: harness.target,
      clientX: 16,
      clientY: 20
    }, "*");

    assert.equal(harness.hasViewInLeaUiButton(), shouldShow);
  });
}

test("a label-free editor badge resolves its job key and shows a readable source name", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {
      targetLabel: "lea_auto_theorem_candidate",
      labelSource: "generated",
      displayTitle: "thm:even-square",
      latexLabel: "thm:even-square",
      from: 0,
      to: 70
    },
    {
      resolveTargets: {
        targets: [{ from: 0, to: 70, targetKind: "theorem", targetLabel: "lea_auto_theorem_stable", targetUses: [] }]
      }
    }
  );
  await harness.loadVisibleTheorems({
    activeTex: "\\begin{theorem}\\label{thm:even-square}\n% lea: formalize\nA theorem.\n\\end{theorem}"
  });
  assert.equal(harness.target.targetLabel, "lea_auto_theorem_stable");
  const statusCall = harness.fetchCalls.find((call) => call.url.endsWith("/statuses"));
  assert.equal(JSON.parse(statusCall.options.body).targets[0].targetLabel, "lea_auto_theorem_stable");
  harness.openTargetPopover();
  await flushPromises();
  assert.match(harness.bodyText(), /Source: thm:even-square/);
  assert.equal(harness.hasButtonText("Formalize"), true);
});

test("diagnostic markers render a non-runnable fix badge and popover", async () => {
  const harness = createContentHarness({ status: "unformalized" });
  const diagnostic = {
    code: "missing_label",
    message: "Lea marker is missing an explicit label=... value.",
    syntax: "diagnostic",
    coords: { left: 40, top: 50 }
  };
  await harness.loadVisibleTheorems({ diagnostics: [diagnostic] });

  assert.equal(harness.hasButtonText("fix marker"), true);

  harness.window.postMessage({
    type: "OL_LEAN_DIAGNOSTIC_CLICK",
    diagnostic,
    clientX: 20,
    clientY: 20
  }, "*");

  assert.match(harness.bodyText(), /Lea marker is missing an explicit label/);
  assert.equal(harness.hasButtonText("Formalize"), false);
});

test("targets without coordinates do not render floating status badges", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    { coords: null }
  );
  await harness.loadStatusForVisibleTheorem();

  assert.equal(harness.hasButtonText("unformalized"), false);
});

test("status refresh sends the canonical version-2 source identity", async () => {
  const harness = createContentHarness({ status: "formalized", sourceFreshness: "current" });
  await harness.loadStatusForVisibleTheorem();

  const statusCall = harness.fetchCalls.find((call) => call.url.endsWith("/statuses"));
  assert.ok(statusCall, "expected a status request");
  const request = JSON.parse(statusCall.options.body);
  assert.match(request.targets[0].sourceIdentityHash, /^[a-f0-9]{64}$/);
});

test("formalize requests and pane refresh hash unchanged targets identically across LaTeX environments", async () => {
  const cases = [
    { environment: "theorem", marker: "% lea: formalize label=sample", kind: "theorem" },
    { environment: "lemma", marker: "% lea: formalize label=sample", kind: "theorem" },
    { environment: "proposition", marker: "% lea: formalize label=sample", kind: "theorem" },
    { environment: "corollary", marker: "% lea: formalize label=sample", kind: "theorem" },
    { environment: "definition", marker: "% lea: define label=sample", kind: "definition" },
    { environment: "claim", marker: "\\leatheorem{label=sample}", kind: "theorem" },
    { environment: "theorem", marker: "% lea: formalize", latexLabel: "thm:generated", kind: "theorem" }
  ];
  for (const { environment, marker, latexLabel, kind } of cases) {
    const source = [
      `\\begin{${environment}}${latexLabel ? `\\label{${latexLabel}}` : ""}`,
      marker,
      "A sample statement.",
      `\\end{${environment}}`,
      ...(kind === "theorem" ? ["\\begin{proof}", "By inspection.", "\\end{proof}"] : [])
    ].join("\n");
    const manifest = buildLeanPaneManifest({
      overleafProjectId: "unknown", files: [{ path: "main.tex", content: source }]
    });
    assert.equal(manifest.items.length, 1, environment);
    const item = manifest.items[0];
    assert.equal(item.targetKind, kind, environment);
    const harness = createContentHarness({ status: "unformalized" }, {}, {
      locationPath: "/project/unknown",
      manifest: { ...manifest, items: [{ ...item, status: "missing-stub" }] }
    });
    await harness.loadVisibleTheorems({ activeTex: source });
    harness.clickPaneTrigger();
    await flushPromises();
    harness.clickPaneTreeRowText("main.tex");
    harness.clickFirstPaneItem();
    harness.clickButtonText("Formalize");
    await flushPromises();
    const call = harness.fetchCalls.find((entry) => entry.url.endsWith("/formalize"));
    assert.ok(call, `expected /formalize for ${environment}`);
    const request = JSON.parse(call.options.body);
    assert.equal(request.sourceBundle.targetKind, kind, environment);
    assert.equal(request.sourceBundle.sourceIdentityHash, item.formalizationInputHash, environment);
  }
});

test("a source-stale formalization is labeled out of date on the LaTeX badge and in its popover", async () => {
  const harness = createContentHarness({
    status: "formalized",
    sourceFreshness: "stale",
    sourceFreshnessMessage: "The LaTeX source changed after this Lean artifact was generated.",
    leaSessionId: "sess-stale"
  });
  await harness.loadStatusForVisibleTheorem();

  assert.equal(harness.hasButtonText("out of date"), true);
  harness.openTargetPopover();
  assert.match(harness.bodyText(), /LaTeX source changed after this Lean artifact was generated/);
  assert.equal(harness.hasButtonText("Re-formalize"), true);
  assert.equal(harness.hasViewInLeaUiButton(), true);
});

test("a current formalization still offers Re-formalize in its popover", async () => {
  const harness = createContentHarness({
    status: "formalized",
    sourceFreshness: "current",
    leaSessionId: "sess-current"
  });
  await harness.loadStatusForVisibleTheorem();

  harness.openTargetPopover();
  assert.equal(harness.hasButtonText("Re-formalize"), true);
  assert.equal(harness.hasButtonText("Check status"), false);
  assert.equal(harness.hasViewInLeaUiButton(), true);
  assert.equal(harness.buttonHelpText("Re-formalize"),
    "Try again using the previous attempt and current LaTeX, keeping existing Lean work.");
});

test("definition targets use definition copy and do not show Stub", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    { targetKind: "definition", targetLabel: "DemoDefinition", targetText: "A definition." }
  );
  await harness.loadStatusForVisibleTheorem();

  harness.window.postMessage({
    type: "OL_LEAN_TARGET_CLICK",
    target: harness.target,
    clientX: 16,
    clientY: 20
  }, "*");

  assert.equal(harness.hasButtonText("Formalize definition"), true);
  assert.equal(harness.hasButtonText("Stub"), false);
});

test("definition success renders a defined badge", async () => {
  const harness = createContentHarness(
    { status: "formalized", resultKind: "defined" },
    { targetKind: "definition", targetLabel: "DemoDefinition", targetText: "A definition." }
  );
  await harness.loadStatusForVisibleTheorem();

  assert.equal(harness.hasButtonText("defined"), true);
  assert.equal(harness.hasButtonText("formalized"), false);
});

test("personal approval toggles in browser-local storage and updates the source badge", async () => {
  const status = {
    status: "formalized",
    approvalEligible: true,
    approvalRevision: "revision-1",
    approvalIneligibleReason: ""
  };
  const harness = createContentHarness(status);
  await harness.loadVisibleTheorems();

  assert.equal(
    harness.hasButtonLabel("Mark demo_theorem as personally audited and approved"),
    true
  );
  harness.clickButtonLabel("Mark demo_theorem as personally audited and approved");
  await flushPromises();

  const key = "project-1:theorem:demo_theorem";
  assert.equal(harness.localStorageState.leaHumanApprovalsV1[key].revision, "revision-1");
  assert.equal(harness.hasButtonLabel("Remove personal approval for demo_theorem"), true);
  assert.equal(harness.countSelector(".ol-lean-human-approval-approved"), 1);

  harness.clickButtonLabel("Remove personal approval for demo_theorem");
  await flushPromises();
  assert.equal(harness.localStorageState.leaHumanApprovalsV1[key], undefined);
  assert.equal(harness.countSelector(".ol-lean-human-approval-approved"), 0);
});

test("a changed approval revision automatically removes the local note without resurrecting it", async () => {
  const key = "project-1:theorem:demo_theorem";
  const harness = createContentHarness(
    {
      status: "formalized",
      approvalEligible: true,
      approvalRevision: "revision-new",
      approvalIneligibleReason: ""
    },
    {},
    {
      localStorage: {
        leaHumanApprovalsV1: {
          [key]: { revision: "revision-old", approvedAt: "2026-07-01T00:00:00.000Z" }
        }
      }
    }
  );
  await harness.loadVisibleTheorems();

  assert.equal(harness.localStorageState.leaHumanApprovalsV1[key], undefined);
  assert.equal(harness.countSelector(".ol-lean-human-approval-approved"), 0);
  assert.equal(
    harness.hasButtonLabel("Mark demo_theorem as personally audited and approved"),
    true
  );
});

test("the Lean pane shows the same stored approval as the in-source tag", async () => {
  const approval = {
    approvalEligible: true,
    approvalRevision: "shared-revision",
    approvalIneligibleReason: ""
  };
  const item = {
    id: "theorem:demo_theorem:0",
    kind: "theorem",
    label: "demo_theorem",
    status: "valid",
    sourceFile: "main.tex",
    sourceStartLine: 1,
    sourceEndLine: 4,
    naturalLanguageRendered: "A theorem.",
    naturalLanguageLatex: "A theorem.",
    leanKind: "theorem",
    leanDeclarationName: "demo_theorem",
    leanArtifactContent: "theorem demo_theorem : True := by trivial",
    ...approval
  };
  const harness = createContentHarness(
    { status: "formalized", ...approval },
    {},
    {
      locationPath: "/project/unknown",
      manifest: { ok: true, rootFile: "main.tex", items: [item], diagnostics: [] }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickButtonLabel("Mark demo_theorem as personally audited and approved");
  await flushPromises();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  assert.equal(harness.countSelector(".ol-lean-human-approval-approved"), 1);
  harness.clickFirstPaneItem();

  assert.equal(harness.countSelector(".ol-lean-human-approval-approved"), 2);
  assert.equal(harness.hasButtonLabel("Remove personal approval for demo_theorem"), true);
});

test("Lean pane trigger opens a project pane and renders manifest items", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [{
          id: "theorem:thm:main",
          kind: "theorem",
          label: "thm:main",
          title: "Main theorem",
          status: "missing-stub",
          sourceFile: "main.tex",
          sourceStartLine: 1,
          sourceEndLine: 3,
          naturalLanguageRendered: "A theorem.",
          naturalLanguageLatex: "A theorem.",
          leanKind: "theorem"
        }],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();

  harness.clickPaneTrigger();
  await flushPromises();

  assert.match(harness.bodyText(), /Lean pane/);
  assert.match(harness.bodyText(), /Lean namespace: Lea\.TestProject/);
  harness.clickPaneTreeRowText("main.tex");
  assert.match(harness.bodyText(), /Main theorem/);
  assert.match(harness.bodyText(), /Lean Check:unformalized/);
  assert.match(harness.bodyText(), /Lea Status:Not assessed/);
});

test("Lean pane falls back to the live TeX file when the Overleaf archive is unavailable", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      failProjectArchive: true,
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [{
          id: "theorem:thm:main",
          kind: "theorem",
          label: "thm:main",
          title: "Main theorem",
          status: "missing-stub",
          sourceFile: "main.tex",
          sourceStartLine: 1,
          sourceEndLine: 3,
          naturalLanguageRendered: "A theorem.",
          naturalLanguageLatex: "A theorem.",
          leanKind: "theorem"
        }],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();

  harness.clickPaneTrigger();
  await flushPromises();

  assert.match(harness.bodyText(), /Lean namespace: Lea\.TestProject/);
  assert.match(harness.bodyText(), /The Overleaf archive was unavailable; showing the open TeX file\./);
  assert.doesNotMatch(harness.bodyText(), /Loading project inventory/);
  assert.ok(
    harness.fetchCalls.some((call) => call.url.includes("/lean-pane/manifest")),
    "the fallback source should still be sent to the companion manifest endpoint"
  );
});

test("Lean pane times out a hanging Overleaf archive instead of remaining on the loading screen", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      hangProjectArchive: true,
      hangProjectIdentity: true,
      hangHumanApprovals: true,
      manifest: { ok: true, rootFile: "main.tex", items: [], diagnostics: [] }
    }
  );
  await harness.loadVisibleTheorems();

  harness.clickPaneTrigger();
  await flushPromises();
  assert.match(harness.bodyText(), /Loading the full Overleaf project inventory in the background/);
  assert.doesNotMatch(harness.bodyText(), /Loading project inventory/);

  await harness.runScheduledTimers();

  assert.match(harness.bodyText(), /The Overleaf archive was unavailable; showing the open TeX file\./);
  assert.doesNotMatch(harness.bodyText(), /Loading project inventory/);
});

test("settings open over the Lean pane and closing them preserves the pane", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: { ok: true, rootFile: "main.tex", items: [], diagnostics: [] }
    }
  );
  await harness.loadVisibleTheorems();

  harness.clickPaneTrigger();
  await flushPromises();
  assert.equal(harness.countSelector(".ol-lean-project-pane"), 1);

  harness.clickButtonLabel("Open Lea settings and usage");
  await flushPromises();
  assert.equal(harness.countSelector(".ol-lean-settings-popover"), 1);
  assert.equal(harness.countSelector(".ol-lean-project-pane"), 1);

  harness.clickButtonLabel("Close Lea popover");
  assert.equal(harness.countSelector(".ol-lean-settings-popover"), 0);
  assert.equal(harness.countSelector(".ol-lean-project-pane"), 1);
});

test("a missing model API key shows a one-click settings nudge", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      companionSettings: {
        leaModel: "o4-mini",
        leaModelRequirements: {
          model: "o4-mini",
          provider: "OpenAI",
          satisfied: false,
          required_keys: [{ env: "OPENAI_API_KEY", label: "OpenAI API key", configured: false }]
        }
      }
    }
  );
  await flushPromises();

  assert.equal(harness.countSelector(".ol-lean-api-key-nudge"), 1);
  assert.equal(harness.countSelector(".ol-lean-settings-trigger-attention"), 1);
  assert.match(harness.bodyText(), /To start proving, add an API key for your selected model/);
  assert.equal(harness.hasButtonText("Open Settings"), true);

  harness.clickButtonText("Open Settings");
  await flushPromises();
  assert.equal(harness.countSelector(".ol-lean-settings-popover"), 1);
});

test("the API-key nudge stays hidden when the selected model is configured", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      companionSettings: {
        leaModel: "o4-mini",
        leaModelRequirements: {
          model: "o4-mini",
          provider: "OpenAI",
          satisfied: true,
          required_keys: [{ env: "OPENAI_API_KEY", label: "OpenAI API key", configured: true }]
        }
      }
    }
  );
  await flushPromises();

  assert.equal(harness.countSelector(".ol-lean-api-key-nudge"), 0);
  assert.equal(harness.countSelector(".ol-lean-settings-trigger-attention"), 0);
});

test("settings popover defaults source pausing off and saves an explicit opt-in", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    { companionSettings: { leaModel: "o4-mini", leaMaxTurns: 20, leaPauseOnSourceIssue: false } }
  );
  await harness.loadVisibleTheorems();
  harness.clickButtonLabel("Open Lea settings and usage");
  await flushPromises();
  const popover = harness.settingsPopover();
  const checkbox = popover.querySelector("[data-role='source-pause']");
  assert.equal(checkbox.checked, false);
  checkbox.checked = true;
  checkbox.dispatchEvent({ type: "change" });
  const save = popover.querySelector("[data-role='save-settings']");
  assert.equal(save.disabled, false);
  save.dispatchEvent({ type: "click" });
  await flushPromises();
  const request = harness.fetchCalls.find((call) => String(call.url).endsWith("/settings/lea") && call.options?.method === "POST");
  assert.equal(JSON.parse(request.options.body).leaPauseOnSourceIssue, true);
  assert.equal(harness.storageSetCalls.some(value=>Object.hasOwn(value,'leaPauseOnSourceIssue')),false);
});

test("settings populate before a slow model catalog and retain edits when it arrives", async () => {
  let completeCatalog;
  const harness = createContentHarness({ status: "unformalized" }, {}, {
    companionSettings: { leaModel: "gpt-5.6-sol", leaMaxTurns: 100, leaMaxSpendUsd: null },
    storage: { leaMaxSpendUsd: 19 },
    fetchOverride: async (url) => url.endsWith("/settings/models")
      ? new Promise((resolve) => { completeCatalog = resolve; }) : undefined
  });
  await harness.loadVisibleTheorems();
  harness.clickButtonLabel("Open Lea settings and usage");
  await flushPromises();
  const popover = harness.settingsPopover();
  assert.equal(popover.querySelector("[data-role='max-turns']").value, "100");
  assert.equal(popover.querySelector("[data-role='max-spend']").value, "");
  const input = popover.querySelector("[data-role='max-turns']");
  input.value = "75";
  input.dispatchEvent({ type: "input" });
  completeCatalog({ ok: true, json: async () => ({ models: [{ id: "gpt-5.6-sol", family: "openai" }], degraded: false }) });
  await flushPromises();
  assert.equal(input.value, "75");
  assert.equal(popover.querySelector("[data-role='save-settings']").disabled, false);
});

test("unavailable settings show cached values, block saves, and offer a working retry", async () => {
  let unavailable = true;
  const harness = createContentHarness({ status: "unformalized" }, {}, {
    storage: { leaMaxTurns: 77 },
    companionSettings: { leaModel: "gpt-5.6-sol", leaMaxTurns: 100 },
    fetchOverride: async (url) => {
      if (url.endsWith("/settings") && unavailable) throw new Error("Connection refused");
    }
  });
  await harness.loadVisibleTheorems();
  harness.clickButtonLabel("Open Lea settings and usage");
  await flushPromises();
  const popover = harness.settingsPopover();
  const input = popover.querySelector("[data-role='max-turns']");
  assert.equal(input.value, "77");
  assert.equal(input.disabled, true);
  input.dispatchEvent({ type: "input" });
  assert.equal(popover.querySelector("[data-role='save-settings']").disabled, true);
  assert.match(popover.querySelector(".ol-lean-popover-status").textContent, /could not load|unable to load/i);
  const retry = popover.querySelector("[data-role='retry-settings']");
  assert.ok(retry && !retry.hidden);
  unavailable = false;
  retry.dispatchEvent({ type: "click" });
  await flushPromises();
  assert.equal(input.value, "100");
  assert.equal(input.disabled, false);
  assert.equal(retry.hidden, true);
});

test("a stalled settings request times out instead of leaving an editable empty form", async () => {
  const harness = createContentHarness({ status: "unformalized" }, {}, {
    fetchOverride: async (url, options) => url.endsWith("/settings")
      ? new Promise((_, reject) => options.signal?.addEventListener("abort", () => reject(new Error("Aborted"))))
      : undefined
  });
  await harness.loadVisibleTheorems();
  harness.clickButtonLabel("Open Lea settings and usage");
  await flushPromises();
  const popover = harness.settingsPopover();
  assert.equal(popover.querySelector("[data-role='max-turns']").disabled, true);
  await harness.runTimersAt(10000);
  await flushPromises();
  assert.match(popover.querySelector(".ol-lean-popover-status").textContent, /timed out/i);
  assert.equal(popover.querySelector("[data-role='retry-settings']").hidden, false);
});

test("settings popover renders an accessible persisted resize handle", async () => {
  const harness = createContentHarness({ status: "unformalized" });
  await harness.loadVisibleTheorems();

  harness.clickButtonLabel("Open Lea settings and usage");
  await flushPromises();

  assert.equal(harness.countSelector(".ol-lean-settings-popover-resizer"), 1);
  assert.equal(harness.settingsPopoverWidthStyle(), "360px");
  assert.deepEqual(harness.settingsPopoverResizerValues(), {
    orientation: "vertical",
    min: "360",
    max: "720",
    now: "360"
  });
});

test("settings popover drag resizing grows left, clamps, and persists independently", async () => {
  const harness = createContentHarness({ status: "unformalized" });
  await harness.loadVisibleTheorems();

  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickButtonLabel("Open Lea settings and usage");
  await flushPromises();
  harness.dragSettingsPopoverResizer({ startX: 360, moves: [260] });

  assert.equal(harness.settingsPopoverWidthStyle(), "460px");
  assert.deepEqual(harness.lastStorageSet(), { settingsPopoverWidthPx: 460 });
  assert.equal(harness.countSelector(".ol-lean-project-pane"), 1);

  harness.dragSettingsPopoverResizer({ startX: 260, moves: [1000] });

  assert.equal(harness.settingsPopoverWidthStyle(), "360px");
  assert.deepEqual(harness.lastStorageSet(), { settingsPopoverWidthPx: 360 });
  assert.equal(harness.countSelector(".ol-lean-project-pane"), 1);
});

test("settings popover applies its stored width when reopened", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    { storage: { settingsPopoverWidthPx: 640 } }
  );
  await harness.loadVisibleTheorems();

  harness.clickButtonLabel("Open Lea settings and usage");
  await flushPromises();
  assert.equal(harness.settingsPopoverWidthStyle(), "640px");

  harness.clickButtonLabel("Close Lea popover");
  harness.clickButtonLabel("Open Lea settings and usage");
  await flushPromises();
  assert.equal(harness.settingsPopoverWidthStyle(), "640px");
});

test("settings popover keyboard resizing honors min and max", async () => {
  const harness = createContentHarness({ status: "unformalized" });
  await harness.loadVisibleTheorems();

  harness.clickButtonLabel("Open Lea settings and usage");
  await flushPromises();
  harness.keySettingsPopoverResizer("ArrowLeft");

  assert.equal(harness.settingsPopoverWidthStyle(), "384px");
  assert.deepEqual(harness.lastStorageSet(), { settingsPopoverWidthPx: 384 });

  harness.keySettingsPopoverResizer("ArrowRight", { shiftKey: true });
  assert.equal(harness.settingsPopoverWidthStyle(), "360px");

  harness.keySettingsPopoverResizer("End");
  assert.equal(harness.settingsPopoverWidthStyle(), "720px");
  assert.deepEqual(harness.settingsPopoverResizerValues(), {
    orientation: "vertical",
    min: "360",
    max: "720",
    now: "720"
  });
  assert.deepEqual(harness.lastStorageSet(), { settingsPopoverWidthPx: 720 });
});

test("settings popover clamps and stays bottom-right anchored when the viewport narrows", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    { storage: { settingsPopoverWidthPx: 700 } }
  );
  await harness.loadVisibleTheorems();

  harness.clickButtonLabel("Open Lea settings and usage");
  await flushPromises();
  assert.equal(harness.settingsPopoverWidthStyle(), "700px");

  harness.window.innerWidth = 600;
  harness.window.dispatchEvent({ type: "resize" });

  assert.equal(harness.settingsPopoverWidthStyle(), "576px");
  assert.deepEqual(harness.settingsPopoverAnchorStyle(), {
    right: "20px",
    left: "auto",
    top: "auto"
  });
  assert.deepEqual(harness.lastStorageSet(), { settingsPopoverWidthPx: 576 });
});

test("closing settings during a resize removes the drag lifecycle", async () => {
  const harness = createContentHarness({ status: "unformalized" });
  await harness.loadVisibleTheorems();

  harness.clickButtonLabel("Open Lea settings and usage");
  await flushPromises();
  harness.startSettingsPopoverResize(360);
  assert.equal(harness.bodyHasClass("ol-lean-settings-resizing"), true);

  harness.clickButtonLabel("Close Lea popover");
  assert.equal(harness.bodyHasClass("ol-lean-settings-resizing"), false);
  harness.moveSettingsPopoverResize(100);
  harness.finishSettingsPopoverResize(100);

  harness.clickButtonLabel("Open Lea settings and usage");
  await flushPromises();
  assert.equal(harness.settingsPopoverWidthStyle(), "360px");
  assert.equal(
    harness.storageSetCalls.some((values) => Object.prototype.hasOwnProperty.call(values, "settingsPopoverWidthPx")),
    false
  );
});

test("GitHub token settings use a full-width editor with cancel, reveal, save, and remove states", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      companionSettings: { githubTokenConfigured: false },
      manifest: { ok: true, rootFile: "main.tex", items: [], diagnostics: [] }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickButtonLabel("Open Lea settings and usage");
  await flushPromises();

  assert.deepEqual(harness.githubTokenState(), {
    configured: "false",
    status: "Not set",
    description: "Add a token to push Lean projects to GitHub.",
    toggle: "Add GitHub token",
    clearHidden: true,
    summaryHidden: false,
    editorHidden: true,
    inputType: "password",
    inputValue: "",
    visibility: "Show"
  });
  assert.match(
    contentStyles,
    /\.ol-lean-github-token-field\s*\{[^}]*width:\s*100%/s,
    "the credential field should occupy the full card width"
  );

  harness.clickButtonRole("github-token-toggle");
  assert.equal(harness.githubTokenState().summaryHidden, true);
  assert.equal(harness.githubTokenState().editorHidden, false);

  harness.setGithubTokenValue("ghp_test-token");
  harness.clickButtonRole("github-token-visibility");
  assert.equal(harness.githubTokenState().inputType, "text");
  assert.equal(harness.githubTokenState().visibility, "Hide");

  harness.clickButtonRole("github-token-cancel");
  assert.equal(harness.githubTokenState().editorHidden, true);
  assert.equal(harness.githubTokenState().inputType, "password");
  assert.equal(harness.githubTokenState().inputValue, "");

  harness.clickButtonRole("github-token-toggle");
  harness.setGithubTokenValue("ghp_saved-token");
  harness.submitGithubToken();
  await flushPromises();

  const saveCall = harness.fetchCalls.find((call) => (
    call.url.endsWith("/settings/github-token")
    && JSON.parse(call.options?.body || "{}").value
  ));
  assert.deepEqual(JSON.parse(saveCall?.options?.body || "{}"), { value: "ghp_saved-token" });
  assert.equal(harness.githubTokenState().configured, "true");
  assert.equal(harness.githubTokenState().status, "Saved");
  assert.equal(harness.githubTokenState().editorHidden, true);
  assert.equal(harness.githubTokenState().clearHidden, false);

  harness.clickButtonRole("github-token-clear");
  await flushPromises();
  const removeCall = harness.fetchCalls.find((call) => (
    call.url.endsWith("/settings/github-token")
    && JSON.parse(call.options?.body || "{}").clear
  ));
  assert.deepEqual(JSON.parse(removeCall?.options?.body || "{}"), { clear: true });
  assert.equal(harness.githubTokenState().configured, "false");
  assert.equal(harness.githubTokenState().status, "Not set");
});

test("GitHub import refreshes a Share panel that was opened before the project existed", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: { ok: true, rootFile: "main.tex", items: [], diagnostics: [] },
      shareStatus(calls) {
        const projectEnsured = calls.some((call) => call.url.includes("/project/github-import/preview"));
        return { ok: true, exists: projectEnsured, remoteUrl: null, tokenConfigured: true };
      },
      githubImportPreview: {
        preview_id: "preview-1",
        plan: {
          counts: { add: 1, already_present: 0, path_conflict: 0, declaration_conflict: 0 },
          files: [{
            source_path: "Imported.lean",
            destination_path: "Imported.lean",
            disposition: "add",
            reason: "New Lean file"
          }],
          reusable_declarations: 1,
          blocking_error: null
        }
      },
      githubImportConfirm: {
        id: "import-1",
        status: "complete",
        reused: false,
        counts: {
          dispositions: { add: 1, already_present: 0, path_conflict: 0, declaration_conflict: 0 },
          matched_declarations: 0,
          reusable_declarations: 1,
          checks: { ok: 1, error: 0, pending: 0 }
        }
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();

  harness.clickButtonText("Share");
  await flushPromises();
  assert.match(harness.bodyText(), /This document has no Lea project yet/);

  harness.clickButtonText("Add Lean files from GitHub");
  await flushPromises();
  harness.setGithubImportUrl("https://github.com/example/formalizations");
  harness.clickButtonText("Analyze");
  await flushPromises();
  assert.equal(
    harness.fetchCalls.filter((call) => call.url.includes("/share/github?")).length,
    2,
    "preview should re-read Share state because it ensures the project"
  );
  assert.doesNotMatch(harness.bodyText(), /This document has no Lea project yet/);
  assert.match(harness.bodyText(), /Save a GitHub remote/);

  harness.clickButtonText("Add 1 Lean file");
  await flushPromises();

  assert.equal(
    harness.fetchCalls.filter((call) => call.url.includes("/share/github?")).length,
    3,
    "completed import should refresh Share state again"
  );
});

test("GitHub import closes after confirmation and locks matched theorems while checks run", async () => {
  const manifestItem = {
    id: "theorem:demo_theorem:0",
    kind: "theorem",
    label: "demo_theorem",
    status: "missing-stub",
    formalizable: true,
    sourceFile: "main.tex",
    sourceStartLine: 1,
    sourceEndLine: 4,
    naturalLanguageRendered: "A theorem.",
    naturalLanguageLatex: "A theorem.",
    leanKind: "theorem",
    leanDeclarationName: "demo_theorem",
    sourceBundle: { version: 2, targetKey: "demo_theorem", bundleHash: "bundle-demo" },
  };
  const queuedManifestItem = {
    ...manifestItem,
    id: "theorem:queued_theorem:1",
    label: "queued_theorem",
    naturalLanguageRendered: "Another theorem.",
    naturalLanguageLatex: "Another theorem.",
    leanDeclarationName: "queued_theorem",
    sourceBundle: { version: 2, targetKey: "queued_theorem", bundleHash: "bundle-queued" },
  };
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: { ok: true, rootFile: "main.tex", items: [manifestItem, queuedManifestItem], diagnostics: [] },
      githubImportPreview: {
        preview_id: "preview-queued",
        plan: {
          counts: { add: 2 },
          files: [
            {
              source_path: "Demo.lean",
              destination_path: "Demo.lean",
              disposition: "add",
              reason: "New Lean file",
              declarations: [{
                match: {
                  origin_key: "project-1:theorem:demo_theorem",
                  declaration_name: "demo_theorem",
                },
              }],
            },
            {
              source_path: "Queued.lean",
              destination_path: "Queued.lean",
              disposition: "add",
              reason: "New Lean file",
              declarations: [{
                match: {
                  origin_key: "project-1:theorem:queued_theorem",
                  declaration_name: "queued_theorem",
                },
              }],
            },
          ],
          reusable_declarations: 0,
          blocking_error: null,
        },
      },
      githubImportConfirm: {
        id: "import-queued",
        status: "checking",
        files: [
          { destination_path: "Demo.lean", check_status: "pending" },
          { destination_path: "Queued.lean", check_status: "pending" },
        ],
        declarations: [
          { declaration_name: "demo_theorem", destination_path: "Demo.lean", formalization_id: "formalization-1" },
          { declaration_name: "queued_theorem", destination_path: "Queued.lean", formalization_id: "formalization-2" },
        ],
        counts: {
          dispositions: { add: 2 },
          matched_declarations: 2,
          reusable_declarations: 0,
          checks: { pending: 2, ok: 0, error: 0 },
        },
      },
      githubImportStatus: {
        id: "import-queued",
        status: "complete",
        files: [
          { destination_path: "Demo.lean", check_status: "ok" },
          { destination_path: "Queued.lean", check_status: "ok" },
        ],
        declarations: [
          { declaration_name: "demo_theorem", destination_path: "Demo.lean", formalization_id: "formalization-1" },
          { declaration_name: "queued_theorem", destination_path: "Queued.lean", formalization_id: "formalization-2" },
        ],
        counts: {
          dispositions: { add: 2 },
          matched_declarations: 2,
          reusable_declarations: 0,
          checks: { pending: 0, ok: 2, error: 0 },
        },
      },
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickButtonText("Share");
  await flushPromises();
  harness.clickButtonText("Add Lean files from GitHub");
  await flushPromises();
  harness.setGithubImportUrl("https://github.com/example/formalizations");
  harness.clickButtonText("Analyze");
  await flushPromises();
  const previewCall = harness.fetchCalls.find((call) => call.url.includes("/project/github-import/preview"));
  const previewRequest = JSON.parse(previewCall.options.body);
  assert.equal(previewRequest.targets[0].sourceBundle.bundleHash, "bundle-demo");
  assert.equal(previewRequest.targets[1].sourceBundle.bundleHash, "bundle-queued");
  harness.clickButtonText("Add 2 Lean files");
  await flushPromises();

  assert.equal(harness.countSelector(".ol-lean-github-import-dialog"), 0);
  assert.match(harness.bodyText(), /2 formalizations remaining · Checking demo_theorem/);
  harness.clickButtonRole("toggle");
  assert.deepEqual(harness.githubImportQueue(), [
    { label: "demo_theorem", state: "Checking now" },
    { label: "queued_theorem", state: "Queued" },
  ]);
  assert.deepEqual(harness.githubImportNoticeState(), {
    expanded: "true",
    detailsHidden: false,
    minimizeHidden: false,
  });

  harness.clickOutsideGithubImportNotice();
  assert.deepEqual(harness.githubImportNoticeState(), {
    expanded: "false",
    detailsHidden: true,
    minimizeHidden: true,
  });
  assert.equal(
    harness.countSelector(".ol-lean-github-import-notice"),
    1,
    "clicking away should minimize the active import queue instead of dismissing it"
  );

  harness.clickButtonRole("toggle");
  harness.clickButtonLabel("Minimize GitHub import status");
  assert.deepEqual(harness.githubImportNoticeState(), {
    expanded: "false",
    detailsHidden: true,
    minimizeHidden: true,
  });
  assert.equal(harness.countSelector(".ol-lean-github-import-notice"), 1);
  assert.equal(harness.countSelector(".ol-lean-project-check-chip-in-progress"), 2);
  harness.openTargetPopover();
  assert.equal(harness.hasButtonText("Checking import…"), true);

  await harness.runScheduledTimers();

  assert.match(harness.bodyText(), /GitHub import complete/);
  assert.equal(harness.hasButtonText("Checking import…"), false);
  assert.ok(
    harness.fetchCalls.some((call) => call.url.includes("/project/github-import/status")),
    "the background tracker should poll independently of the closed dialog"
  );
});

test("GitHub push uses a Lea confirmation dialog instead of the browser confirm", async () => {
  const remoteUrl = "https://github.com/example/formalizations";
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: { ok: true, rootFile: "main.tex", items: [], diagnostics: [] },
      shareStatus: { ok: true, exists: true, remoteUrl, tokenConfigured: true }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickButtonText("Share");
  await flushPromises();

  harness.clickButtonText("Push to GitHub");
  await flushPromises();

  assert.deepEqual(harness.githubPushDialog(), {
    role: "dialog",
    modal: "true",
    label: "ol-lean-github-push-title"
  });
  assert.match(harness.bodyText(), /Push project\?/);
  assert.match(harness.bodyText(), /Repositoryhttps:\/\/github\.com\/example\/formalizations/);
  assert.match(harness.bodyText(), /Branchmain/);
  assert.equal(harness.confirmCalls.length, 0);
  assert.equal(
    harness.fetchCalls.filter((call) => call.url.includes("/share/github/push")).length,
    0,
    "opening the dialog must not start a push"
  );

  harness.clickButtonText("Cancel");
  await flushPromises();
  assert.equal(harness.githubPushDialog(), null);
  assert.equal(
    harness.fetchCalls.filter((call) => call.url.includes("/share/github/push")).length,
    0,
    "canceling the dialog must not start a push"
  );

  harness.clickButtonText("Push to GitHub");
  await flushPromises();
  harness.clickButtonRole("confirm-push");
  await flushPromises();

  assert.equal(harness.githubPushDialog(), null);
  assert.equal(harness.confirmCalls.length, 0);
  assert.equal(
    harness.fetchCalls.filter((call) => call.url.includes("/share/github/push")).length,
    1
  );
  assert.match(harness.bodyText(), /Pushed to https:\/\/github\.com\/example\/formalizations\./);
});

test("project rename uses an accessible Lea dialog with a live namespace preview", async () => {
  const manifest = (calls) => {
    const renamed = calls.some((call) => call.url.endsWith("/project/identity") && call.options?.method === "PUT");
    const namespace = renamed ? "Lea.FourierNotes" : "Lea.TestProject";
    return {
      ok: true,
      rootFile: "main.tex",
      items: [{
        id: "theorem:demo_theorem:0",
        kind: "theorem",
        label: "demo_theorem",
        title: "Demo theorem",
        status: "valid",
        sourceFile: "main.tex",
        sourceStartLine: 1,
        sourceEndLine: 4,
        documentOrder: 0,
        naturalLanguageLatex: "A theorem.",
        leanKind: "theorem",
        leanDeclarationName: "demo_theorem",
        leanArtifactContent: `namespace ${namespace}\n\ntheorem demo_theorem : True := by trivial\n\nend ${namespace}`
      }],
      diagnostics: []
    };
  };
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      // The harness uses the active buffer directly for the synthetic
      // "unknown" project, avoiding an unrelated Overleaf ZIP fixture.
      locationPath: "/project/unknown",
      manifest,
      projectIdentity: {
        projectId: "adapter-project-unknown",
        overleafProjectId: "unknown",
        slug: "unknown",
        projectName: "Test Project",
        namespace: "Lea.TestProject",
        exists: true,
        hasRecordedProofs: true
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();

  harness.clickButtonText("Rename");
  await flushPromises();

  assert.deepEqual(harness.projectIdentityDialog(), {
    role: "dialog",
    modal: "true",
    label: "ol-lean-project-identity-title"
  });
  assert.match(harness.bodyText(), /Rename project/);
  assert.match(harness.bodyText(), /Display nameTest Project/);
  assert.match(harness.bodyText(), /Lean namespaceLea\.TestProject/);
  assert.equal(harness.promptCalls.length, 0);
  assert.equal(harness.confirmCalls.length, 0);

  harness.setProjectIdentityName("Fourier Notes");
  await harness.runScheduledTimers();

  assert.equal(harness.projectIdentityNamespace(), "Lea.FourierNotes");
  assert.match(harness.bodyText(), /Lea\.TestProject → Lea\.FourierNotes/);
  assert.match(harness.bodyText(), /migrate recorded proof files/);

  harness.clickButtonText("Save changes");
  await flushPromises();

  const saveCall = harness.fetchCalls.find((call) => call.url.endsWith("/project/identity") && call.options?.method === "PUT");
  assert.ok(saveCall, "expected project identity PUT");
  assert.deepEqual(JSON.parse(saveCall.options.body), {
    overleafProjectId: "unknown",
    projectName: "Fourier Notes",
    mode: "rename-namespace",
    namespace: "Lea.FourierNotes",
    expectedNamespace: "Lea.TestProject",
    createIfMissing: true
  });
  assert.equal(harness.projectIdentityDialog(), null);
  assert.match(harness.bodyText(), /Fourier Notes/);
  assert.match(harness.bodyText(), /Project name and Lean namespace saved/);
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();
  assert.match(harness.bodyText(), /namespace Lea\.FourierNotes/);
  assert.doesNotMatch(harness.bodyText(), /namespace Lea\.TestProject/);
  assert.equal(
    harness.fetchCalls.filter((call) => call.url.includes("/lean-pane/manifest")).length,
    2,
    "rename should refresh the open Lean pane manifest"
  );
});

test("project rename can keep the existing namespace when the suggested namespace is occupied", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/project-1",
      manifest: { ok: true, rootFile: "main.tex", items: [], diagnostics: [] },
      projectPreview: {
        project_name: "Shared Notes",
        namespace: "Lea.SharedNotes",
        available: false,
        suggestions: ["Lea.SharedNotes2", "Lea.SharedNotes2026"]
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickButtonText("Rename");
  await flushPromises();

  harness.setProjectIdentityName("Shared Notes");
  await harness.runScheduledTimers();

  assert.match(harness.bodyText(), /Lea\.SharedNotes is already in use/);
  assert.equal(harness.hasButtonText("Lea.SharedNotes2"), true);
  harness.setProjectIdentitySync(false);
  assert.match(harness.bodyText(), /Only the display name will change/);

  harness.clickButtonText("Save changes");
  await flushPromises();

  const saveCall = harness.fetchCalls.find((call) => call.url.endsWith("/project/identity") && call.options?.method === "PUT");
  assert.ok(saveCall, "expected project identity PUT");
  const body = JSON.parse(saveCall.options.body);
  assert.equal(body.mode, "display-only");
  assert.equal(body.namespace, "");
  assert.equal(harness.promptCalls.length, 0);
  assert.equal(harness.confirmCalls.length, 0);
});

test("Lean pane renders a persisted drag resize handle", async () => {
  const harness = createContentHarness({ status: "unformalized" });
  await harness.loadVisibleTheorems();

  harness.clickPaneTrigger();
  await flushPromises();

  assert.equal(harness.countSelector(".ol-lean-project-pane-resizer"), 1);
  assert.equal(harness.leanPaneWidthStyle(), "520px");
});

test("Lean pane drag resizing grows left, clamps at minimum, and persists", async () => {
  const harness = createContentHarness({ status: "unformalized" });
  await harness.loadVisibleTheorems();

  harness.clickPaneTrigger();
  await flushPromises();
  harness.dragLeanPaneResizer({ startX: 520, moves: [420] });

  assert.equal(harness.leanPaneWidthStyle(), "620px");
  assert.deepEqual(harness.lastStorageSet(), { leanPaneWidthPx: 620 });

  harness.dragLeanPaneResizer({ startX: 420, moves: [1000] });

  assert.equal(harness.leanPaneWidthStyle(), "360px");
  assert.deepEqual(harness.lastStorageSet(), { leanPaneWidthPx: 360 });
});

test("Lean pane applies stored width when reopened", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    { storage: { leanPaneWidthPx: 700 } }
  );
  await harness.loadVisibleTheorems();

  harness.clickPaneTrigger();
  await flushPromises();

  assert.equal(harness.leanPaneWidthStyle(), "700px");
});

test("Lean pane keyboard resizing honors min and max and persists", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    { storage: { leanPaneWidthPx: 520 } }
  );
  await harness.loadVisibleTheorems();

  harness.clickPaneTrigger();
  await flushPromises();
  harness.keyLeanPaneResizer("ArrowLeft");

  assert.equal(harness.leanPaneWidthStyle(), "544px");
  assert.deepEqual(harness.lastStorageSet(), { leanPaneWidthPx: 544 });

  harness.keyLeanPaneResizer("ArrowRight", { shiftKey: true });
  harness.keyLeanPaneResizer("ArrowRight", { shiftKey: true });
  harness.keyLeanPaneResizer("ArrowRight", { shiftKey: true });

  assert.equal(harness.leanPaneWidthStyle(), "360px");
  assert.deepEqual(harness.lastStorageSet(), { leanPaneWidthPx: 360 });

  harness.keyLeanPaneResizer("End");

  assert.equal(harness.leanPaneWidthStyle(), "1000px");
  assert.deepEqual(harness.lastStorageSet(), { leanPaneWidthPx: 1000 });
});

test("Lean pane viewport resize clamps an oversized stored width", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    { storage: { leanPaneWidthPx: 900 } }
  );
  await harness.loadVisibleTheorems();

  harness.clickPaneTrigger();
  await flushPromises();
  assert.equal(harness.leanPaneWidthStyle(), "900px");

  harness.window.innerWidth = 600;
  harness.window.dispatchEvent({ type: "resize" });

  assert.equal(harness.leanPaneWidthStyle(), "576px");
  assert.deepEqual(harness.lastStorageSet(), { leanPaneWidthPx: 576 });
});

test("Lean pane renders every source file equally with file rows collapsed by default", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [
          {
            id: "theorem:main:0",
            kind: "theorem",
            label: "main",
            title: "Main theorem",
            status: "valid",
            sourceFile: "main.tex",
            documentOrder: 0,
            naturalLanguageLatex: "Main theorem.",
            leanKind: "theorem"
          },
          {
            id: "definition:defs:1",
            kind: "definition",
            label: "defs",
            title: "Section definition",
            status: "missing-stub",
            sourceFile: "sections/defs.tex",
            documentOrder: 1,
            naturalLanguageLatex: "A definition.",
            leanKind: "def"
          },
          {
            id: "lemma:supp:2",
            kind: "lemma",
            label: "supp",
            title: "Supplemental lemma",
            status: "valid",
            sourceFile: "supp.tex",
            documentOrder: 2,
            naturalLanguageLatex: "A supplemental result.",
            leanKind: "theorem"
          }
        ],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();

  assert.match(harness.bodyText(), /3 labeled items across 3 \.tex files\./);
  assert.deepEqual(harness.paneTreeRowTexts().map((text) => text.replace(/\s+/g, " ")), [
    "▸main.tex1 item",
    "▸sections/1 itemmissing stub",
    "▸supp.tex1 item"
  ]);
  assert.equal(harness.countSelector(".ol-lean-project-progress"), 2);
  assert.doesNotMatch(harness.bodyText(), /Main theorem/);
  assert.doesNotMatch(harness.bodyText(), /Section definition/);
  assert.doesNotMatch(harness.bodyText(), /Supplemental lemma/);

  harness.clickPaneTreeRowText("main.tex");
  assert.match(harness.bodyText(), /Main theorem/);
  harness.clickPaneTreeRowText("sections/");
  harness.clickPaneTreeRowText("defs.tex");
  assert.match(harness.bodyText(), /Section definition/);
  harness.clickPaneTreeRowText("supp.tex");
  assert.match(harness.bodyText(), /Supplemental lemma/);
});

test("Lean pane file rows render proportional progress segments", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [
          { id: "t1", kind: "theorem", label: "t1", status: "valid", sourceFile: "main.tex", documentOrder: 0, naturalLanguageLatex: "A.", leanKind: "theorem" },
          { id: "t2", kind: "theorem", label: "t2", status: "defined", sourceFile: "main.tex", documentOrder: 1, naturalLanguageLatex: "B.", leanKind: "def" },
          { id: "t3", kind: "theorem", label: "t3", status: "disproved", leanCheck: { status: "checked" }, sourceFile: "main.tex", documentOrder: 2, naturalLanguageLatex: "C.", leanKind: "theorem" },
          { id: "t4", kind: "theorem", label: "t4", status: "stub-generated", sourceFile: "main.tex", documentOrder: 3, naturalLanguageLatex: "D.", leanKind: "theorem" },
          { id: "t5", kind: "theorem", label: "t5", status: "invalid", sourceFile: "main.tex", documentOrder: 4, naturalLanguageLatex: "E.", leanKind: "theorem" },
          { id: "t6", kind: "theorem", label: "t6", status: "missing-stub", sourceFile: "main.tex", documentOrder: 5, naturalLanguageLatex: "F.", leanKind: "theorem", inProgress: true },
          { id: "t7", kind: "theorem", label: "t7", status: "stale", resultKind: "disproved", leanCheck: { status: "checked" }, sourceFile: "main.tex", documentOrder: 6, naturalLanguageLatex: "G.", leanKind: "theorem" },
          { id: "t8", kind: "theorem", label: "t8", status: "unknown", sourceFile: "main.tex", documentOrder: 7, naturalLanguageLatex: "H.", leanKind: "theorem" }
        ],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();

  const [progress] = harness.paneProgresses();
  assert.equal(progress.role, "img");
  assert.equal(progress.label, "main.tex: 8 Lea items, 2 verified, 1 disproved, 1 sorry-stubbed, 1 failed, 1 out of date, 2 unformalized, 1 in progress.");
  assert.equal(progress.inProgress, true);
  assert.deepEqual(progress.segments.map((segment) => [segment.bucket, segment.count, segment.width]), [
    ["success", "2", "25%"],
    ["disproved", "1", "12.5%"],
    ["sorry-stubbed", "1", "12.5%"],
    ["failed", "1", "12.5%"],
    ["out-of-date", "1", "12.5%"],
    ["unformalized", "2", "25%"]
  ]);

  harness.clickPaneTreeRowText("main.tex");
  assert.match(harness.bodyText(), /2 verified · 1 disproved/);
  assert.match(harness.bodyText(), /Counterexample verified · Claim disproved/);
  assert.equal(harness.countSelector(".ol-lean-project-check-chip-disproved"), 1);
  assert.equal(harness.countSelector(".ol-lean-project-item.is-expanded"), 0);
  harness.clickPaneItemHeaderText("t7");
  assert.match(harness.bodyText(), /Out of date.*LaTeX changed after this Lean artifact was generated/i);
});

for (const failure of ["timeout", "server", "network"]) {
  test(`Lean pane ${failure} refresh preserves the loaded tree, edit draft and retries`, async () => {
    let fail = false;
    const harness = createContentHarness({ status: "unformalized" }, {}, {
      locationPath: "/project/unknown",
      manifest: { ok: true, rootFile: "main.tex", items: [editableItem({ inProgress: true })], diagnostics: [] },
      fetchOverride: async (url, options) => {
        if (!url.includes("/lean-pane/manifest") || !fail) return;
        if (failure === "timeout") return new Promise((_, reject) => options.signal.addEventListener("abort", () => reject(new Error("Aborted"))));
        if (failure === "network") throw new Error("Network unavailable");
        return { ok: false, status: 503, json: async () => ({ message: "Inventory busy" }) };
      }
    });
    await harness.loadVisibleTheorems();
    harness.clickPaneTrigger();
    await flushPromises();
    harness.clickPaneTreeRowText("main.tex");
    harness.clickFirstPaneItem();
    harness.clickButtonText("Edit");
    await flushPromises();
    harness.setEditTextareaValue("theorem unsaved : True := by trivial");
    const textarea = harness.editTextarea();
    textarea.dispatchEvent({ type: "input" });
    const tree = harness.document.body.querySelector(".ol-lean-project-tree");
    const body = harness.document.body.querySelector(".ol-lean-project-pane-body");
    body.scrollTop = 140;

    fail = true;
    await harness.runTimersAt(5000);
    if (failure === "timeout") await harness.runTimersAt(10000);
    assert.ok(harness.document.body.querySelector(".ol-lean-project-tree") === tree, "refresh must preserve tree DOM");
    assert.ok(harness.editTextarea() === textarea, "refresh must preserve edit DOM");
    assert.equal(textarea.value, "theorem unsaved : True := by trivial");
    assert.equal(body.scrollTop, 140);
    assert.match(harness.bodyText(), /last loaded inventory.*out of date/i);
    assert.equal(harness.countSelector(".ol-lean-project-pane-refresh-warning"), 1);
    assert.equal(harness.pendingTimers().filter((delay) => delay === 5000).length, 1);

    fail = false;
    await harness.runTimersAt(5000);
    assert.equal(harness.countSelector(".ol-lean-project-pane-refresh-warning"), 0);
    assert.ok(harness.document.body.querySelector(".ol-lean-project-tree"));
    assert.equal(harness.editTextarea()?.value, "theorem unsaved : True := by trivial");
  });
}

test("Lean pane initial inventory failure retries without a running item and stops when closed", async () => {
  let fail = true;
  const harness = createContentHarness({ status: "unformalized" }, {}, {
    locationPath: "/project/unknown",
    manifest: { ok: true, rootFile: "main.tex", items: [editableItem()], diagnostics: [] },
    fetchOverride: async (url) => {
      if (url.includes("/lean-pane/manifest") && fail) return { ok: false, status: 503, json: async () => ({ message: "Inventory busy" }) };
    }
  });
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  assert.match(harness.bodyText(), /Inventory busy.*Retrying/i);
  fail = false;
  await harness.runTimersAt(5000);
  assert.equal(harness.countSelector(".ol-lean-project-pane-refresh-warning"), 0);
  assert.ok(harness.document.body.querySelector(".ol-lean-project-tree"));
  fail = true;
  harness.clickButtonLabel("Refresh Lean pane");
  await flushPromises();
  const count = harness.fetchCalls.filter((call) => call.url.includes("/lean-pane/manifest")).length;
  harness.clickButtonLabel("Close Lean pane");
  await harness.runTimersAt(5000);
  assert.equal(harness.fetchCalls.filter((call) => call.url.includes("/lean-pane/manifest")).length, count);
});

test("inventory retries back off, pause while hidden, and recover on visibility", async () => {
  let fail = true;
  const harness = createContentHarness({}, {}, {
    locationPath: "/project/unknown",
    manifest: { ok: true, items: [], diagnostics: [] },
    fetchOverride: async (url) => {
      if (url.includes("/lean-pane/manifest") && fail) throw new Error("Connection refused");
    }
  });
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  for (const delay of [5000, 10000, 20000, 30000]) {
    assert.ok(harness.pendingTimers().includes(delay));
    await harness.runTimersAt(delay);
  }
  assert.equal(harness.countSelector(".ol-lean-project-pane-refresh-warning"), 1);
  await harness.setVisibility(true);
  const count = harness.fetchCalls.filter((call) => call.url.includes("/lean-pane/manifest")).length;
  await harness.runTimersAt(30000);
  assert.equal(harness.fetchCalls.filter((call) => call.url.includes("/lean-pane/manifest")).length, count);
  fail = false;
  await harness.setVisibility(false);
  assert.equal(harness.countSelector(".ol-lean-project-pane-refresh-warning"), 0);
});

test("a refresh failure suspends personal approval until a fresh inventory arrives", async () => {
  let fail = false;
  const harness = createContentHarness({}, {}, {
    locationPath: "/project/unknown",
    manifest: { ok: true, items: [editableItem({ approvalEligible: true, approvalRevision: "revision-1" })], diagnostics: [] },
    fetchOverride: async (url) => {
      if (url.includes("/lean-pane/manifest") && fail) throw new Error("Connection refused");
    }
  });
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();
  assert.equal(harness.document.body.querySelector(".ol-lean-human-approval-pane").disabled, false);
  fail = true;
  harness.clickButtonLabel("Refresh Lean pane");
  await flushPromises();
  const button = harness.document.body.querySelector(".ol-lean-human-approval-pane");
  assert.equal(button.disabled, true);
  button.dispatchEvent({ type: "click" });
  await flushPromises();
  assert.equal(harness.storageSetCalls.length, 0);
  fail = false;
  await harness.runTimersAt(5000);
  assert.equal(harness.document.body.querySelector(".ol-lean-human-approval-pane").disabled, false);
});

for (const superseding of ["refresh", "close", "navigation"]) {
  test(`a delayed failed inventory cannot overwrite a newer ${superseding}`, async () => {
    let delayed = false, rejectDelayed;
    const harness = createContentHarness({}, {}, {
      locationPath: "/project/unknown",
      manifest: { ok: true, items: [editableItem()], diagnostics: [] },
      fetchOverride: async (url) => {
        if (url.includes("/lean-pane/manifest") && delayed) {
          delayed = false;
          return new Promise((_, reject) => { rejectDelayed = reject; });
        }
      }
    });
    await harness.loadVisibleTheorems();
    harness.clickPaneTrigger();
    await flushPromises();
    delayed = true;
    harness.clickButtonLabel("Refresh Lean pane");
    await flushPromises();
    assert.ok(rejectDelayed);
    if (superseding === "refresh") harness.clickButtonLabel("Refresh Lean pane");
    if (superseding === "close") harness.clickButtonLabel("Close Lean pane");
    if (superseding === "navigation") harness.window.location.pathname = "/project/another-project";
    await flushPromises();
    const before = harness.paneText();
    rejectDelayed(new Error("Old request failed"));
    await flushPromises();
    assert.equal(harness.paneText(), before);
    assert.equal(harness.countSelector(".ol-lean-project-pane-refresh-warning"), 0);
    assert.doesNotMatch(harness.bodyText(), /Old request failed/);
  });
}

test("Lean pane polling refresh preserves expanded folder and file rows", async () => {
  let manifestCalls = 0;
  const manifest = () => {
    const sectionStatus = manifestCalls === 0 ? "in-progress" : "invalid";
    manifestCalls += 1;
    return {
      ok: true,
      rootFile: "main.tex",
      items: [
        {
          id: "theorem:main:0",
          kind: "theorem",
          label: "main",
          title: "Main theorem",
          status: "valid",
          sourceFile: "main.tex",
          documentOrder: 0,
          naturalLanguageLatex: "Main theorem.",
          leanKind: "theorem"
        },
        {
          id: "lemma:section:1",
          kind: "lemma",
          label: "section",
          title: "Section lemma",
          status: sectionStatus,
          inProgress: sectionStatus === "in-progress",
          sourceFile: "sections/defs.tex",
          documentOrder: 1,
          naturalLanguageLatex: "A lemma.",
          leanKind: "theorem"
        }
      ],
      diagnostics: []
    };
  };
  const harness = createContentHarness({ status: "unformalized" }, {}, {
    locationPath: "/project/unknown",
    manifest
  });
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("sections/");
  harness.clickPaneTreeRowText("defs.tex");
  assert.match(harness.bodyText(), /Section lemma/);
  assert.match(harness.bodyText(), /in progress/);

  await harness.runScheduledTimers();

  assert.match(harness.bodyText(), /Section lemma/);
  assert.match(harness.bodyText(), /invalid/);
  assert.equal(harness.countSelector(".ol-lean-project-item-header"), 1);
});

test("Lean pane expanded detail shows copy actions only for generated content", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [{
          id: "theorem:thm:main",
          kind: "theorem",
          label: "thm:main",
          status: "valid",
          sourceFile: "main.tex",
          sourceStartLine: 1,
          sourceEndLine: 3,
          naturalLanguageRendered: "A theorem.",
          naturalLanguageLatex: "A theorem.",
          leanKind: "theorem",
          leanDeclarationName: "main_theorem",
          leanStub: "theorem main_theorem : True",
          leanArtifactPath: "workspace/proofs/Main.lean",
          leanArtifactContent: "theorem main_theorem : True := by\n  trivial\n"
        }],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();

  harness.clickPaneTreeRowText("main.tex");
  assert.match(harness.bodyText(), /Theorem: main_theorem.*thm:main/);
  assert.match(harness.bodyText(), /Lean Check:unformalized/);
  assert.match(harness.bodyText(), /Lea Status:Not assessed/);
  assert.equal(harness.hasButtonText("Re-formalize"), true);
  assert.equal(harness.hasButtonLabel("Go to source"), true);
  assert.equal(harness.countSelector(".ol-lean-project-natural"), 0);
  assert.equal(harness.countSelector(".ol-lean-live-status"), 0);
  assert.equal(harness.hasButtonLabel("Copy stub"), false);
  harness.clickFirstPaneItem();

  assert.equal(harness.countSelector(".ol-lean-project-natural"), 1);
  assert.equal(harness.countSelector(".ol-lean-live-status"), 1);
  assert.equal(harness.hasButtonLabel("Copy stub"), true);
  assert.equal(harness.hasButtonLabel("Copy artifact"), true);
  assert.match(harness.bodyText(), /workspace\/proofs\/Main\.lean/);
});

test("Lean pane opens a saved LeaChat session from the collapsed summary even when its status is unknown", async () => {
  const harness = createContentHarness({ status: "unformalized" }, {}, {
    manifest: { ok: true, rootFile: "main.tex", diagnostics: [], items: [{
      id: "definition:CapacityEncoding", label: "CapacityEncoding", kind: "definition",
      status: "unknown", sourceFile: "main.tex", leanKind: "def",
      leanDeclarationName: "CapacityEncoding", leaSessionId: "saved-session",
      leanCheck: { status: "checked" }
    }] }
  });
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  assert.equal(harness.countSelector(".ol-lean-project-item.is-expanded"), 0);
  assert.equal(harness.hasButtonText("Open in LeaChat"), true);
  assert.equal(harness.hasButtonLabel("Open in LeaChat"), true);
  const summaryActions = harness.document.body.querySelector(".ol-lean-project-summary-actions");
  assert.equal(summaryActions.querySelector(".ol-lean-open-chat")?.textContent, "Open in LeaChat");
  harness.clickButtonText("Open in LeaChat");
  await flushPromises();
  const opened = harness.runtimeMessages.find((message) => message.type === "OPEN_LEA_SESSION");
  assert.equal(new URL(opened.url).searchParams.get("session"), "saved-session");
  assert.ok(!harness.fetchCalls.some((call) => call.url.endsWith("/lean-pane/chat/session")));
  assert.equal(harness.countSelector(".ol-lean-project-item.is-expanded"), 0);
  harness.clickFirstPaneItem();
  assert.equal(harness.countSelector(".ol-lean-open-chat"), 1);
  const detailActions = harness.document.body.querySelector(".ol-lean-project-detail-actions");
  assert.equal(detailActions?.querySelector(".ol-lean-open-chat") || null, null);
});

test("Lean pane shows navigable uses and used-by relationships across project files", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: {
        ok: true,
        items: [
          {
            id: "theorem:support:0",
            kind: "theorem",
            label: "support",
            status: "valid",
            sourceFile: "foundations/base.tex",
            documentOrder: 0,
            naturalLanguageLatex: "A supporting theorem.",
            leanKind: "theorem",
            targetUses: []
          },
          {
            id: "theorem:isolated:1",
            kind: "theorem",
            label: "isolated",
            status: "valid",
            sourceFile: "main.tex",
            documentOrder: 1,
            naturalLanguageLatex: "An unrelated theorem.",
            leanKind: "theorem",
            targetUses: []
          },
          {
            id: "theorem:result:2",
            kind: "theorem",
            label: "result",
            status: "invalid",
            sourceFile: "sections/result.tex",
            documentOrder: 2,
            naturalLanguageLatex: "The main result.",
            leanKind: "theorem",
            targetUses: ["support", "outside_inventory"]
          }
        ],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("sections/");
  harness.clickPaneTreeRowText("result.tex");
  assert.equal(harness.countSelector(".ol-lean-project-relationships"), 0);
  harness.clickPaneItemHeaderText("result");

  assert.equal(harness.countSelector(".ol-lean-project-relationships"), 1);
  assert.deepEqual(
    harness.relationshipChips().map((chip) => ({
      text: chip.text,
      direction: chip.direction,
      navigable: chip.navigable,
      unavailable: chip.unavailable
    })),
    [
      { text: "support", direction: "uses", navigable: true, unavailable: false },
      { text: "outside_inventory", direction: "uses", navigable: false, unavailable: true }
    ]
  );
  const supportChip = harness.relationshipChips()[0];
  assert.match(supportChip.className, /ol-lean-project-relationship-chip-valid/);
  assert.match(supportChip.ariaLabel, /Open dependency support, currently valid/);
  const outsideChip = harness.relationshipChips()[1];
  assert.equal(outsideChip.ariaDisabled, "true");
  assert.match(outsideChip.title, /not present in the current Lean-pane inventory/);

  harness.clickRelationshipChip("support");
  assert.equal(harness.focusedPaneItemId(), "theorem:support:0");
  assert.equal(harness.firstFocusedPaneItemScrolled(), true);
  assert.ok(harness.paneTreeRowTexts().some((text) => text.includes("base.tex")));
  assert.ok(harness.relationshipChips().some((chip) => (
    chip.text === "result"
    && chip.direction === "used-by"
    && chip.navigable
    && /currently invalid/.test(chip.ariaLabel)
  )));

  harness.clickRelationshipChip("result", "used-by");
  assert.equal(harness.focusedPaneItemId(), "theorem:result:2");
});

test("Lean pane relationship chips survive polling and reflect refreshed target status", async () => {
  let manifestCalls = 0;
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: () => {
        const first = manifestCalls === 0;
        manifestCalls += 1;
        return {
          ok: true,
          items: [
            {
              id: "theorem:support:0",
              kind: "theorem",
              label: "support",
              status: first ? "valid" : "invalid",
              sourceFile: "main.tex",
              documentOrder: 0,
              naturalLanguageLatex: "Support.",
              leanKind: "theorem",
              targetUses: []
            },
            {
              id: "theorem:result:1",
              kind: "theorem",
              label: "result",
              status: first ? "in-progress" : "valid",
              inProgress: first,
              sourceFile: "main.tex",
              documentOrder: 1,
              naturalLanguageLatex: "Result.",
              leanKind: "theorem",
              targetUses: ["support"]
            }
          ],
          diagnostics: []
        };
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickPaneItemHeaderText("result");

  assert.ok(harness.relationshipChips().some((chip) => (
    chip.text === "support" && /relationship-chip-valid/.test(chip.className)
  )));

  await harness.runScheduledTimers();

  assert.ok(harness.relationshipChips().some((chip) => (
    chip.text === "support" && /relationship-chip-invalid/.test(chip.className)
  )));
});

test("Lean pane renders lightweight math and highlighted Lean code", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [{
          id: "theorem:main_theorem:0",
          kind: "theorem",
          label: "main_theorem",
          title: "Main theorem",
          status: "valid",
          sourceFile: "main.tex",
          sourceStartLine: 1,
          sourceEndLine: 4,
          naturalLanguageRendered: "For every x in R, x^2 >= 0.",
          naturalLanguageLatex: "For every $x \\in \\mathbb{R}$, $x^2 \\ge 0$.",
          leanKind: "theorem",
          leanDeclarationName: "main_theorem",
          leanStub: "theorem main_theorem : Nat := 2",
          leanArtifactPath: "workspace/proofs/Main.lean",
          leanArtifactContent: "theorem main_theorem : Nat := by\n  -- proof\n  exact 2\n"
        }],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();

  harness.clickPaneTreeRowText("main.tex");
  assert.equal(harness.countSelector(".ol-lean-project-math"), 0);
  harness.clickFirstPaneItem();
  assert.ok(harness.countSelector(".ol-lean-project-math") >= 2);
  assert.equal(harness.countSelector(".ol-lean-project-math-sup"), 1);
  assert.ok(harness.countSelector(".ol-lean-project-lean-kw") >= 1);
  assert.ok(harness.countSelector(".ol-lean-project-lean-ty") >= 1);
  assert.ok(harness.countSelector(".ol-lean-project-lean-num") >= 1);
  assert.match(harness.bodyText(), /For every x ∈ ℝ, x2 ≥ 0\./);

  assert.equal(harness.hasButtonLabel("Copy stub"), true);
  assert.equal(harness.hasButtonLabel("Copy artifact"), true);
  assert.ok(harness.countSelector(".ol-lean-project-lean-com") >= 1);
});

test("Lean pane uses KaTeX for standard notation and styles surrounding LaTeX prose", async () => {
  const renderCalls = [];
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      katex: {
        render(source, element, options) {
          renderCalls.push({ source, options });
          const rendered = element.appendChild(new FakeElement("span"));
          rendered.className = "katex";
          rendered.textContent = source.replace("\\triangleq", "≜");
        }
      },
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [{
          id: "definition:notation:0",
          kind: "definition",
          label: "notation",
          title: "Notation",
          status: "missing-stub",
          sourceFile: "main.tex",
          sourceStartLine: 1,
          sourceEndLine: 4,
          naturalLanguageLatex: "Set $f(x) \\triangleq x^2$ and call it \\emph{canonical}.",
          leanKind: "def",
          leanDeclarationName: "notation"
        }],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();

  assert.equal(renderCalls.length, 1);
  assert.equal(renderCalls[0].source, "f(x) \\triangleq x^2");
  assert.equal(renderCalls[0].options.trust, false);
  assert.equal(renderCalls[0].options.throwOnError, true);
  assert.equal(renderCalls[0].options.output, "htmlAndMathml");
  assert.equal(harness.countSelector(".katex"), 1);
  assert.equal(harness.countSelector(".ol-lean-project-latex-em"), 1);
  assert.match(harness.bodyText(), /f\(x\) ≜ x\^2 and call it canonical\./);
});

test("Lean pane preserves readable math when KaTeX rejects an expression", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      katex: {
        render() {
          throw new Error("Undefined control sequence");
        }
      },
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [{
          id: "theorem:fallback:0",
          kind: "theorem",
          label: "fallback",
          status: "missing-stub",
          sourceFile: "main.tex",
          sourceStartLine: 1,
          sourceEndLine: 3,
          naturalLanguageLatex: "Assume $\\ProjectSpecific x \\subseteq X$.",
          leanKind: "theorem",
          leanDeclarationName: "fallback"
        }],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();

  assert.equal(harness.countSelector(".ol-lean-project-math-fallback"), 1);
  assert.match(harness.bodyText(), /\\ProjectSpecific x ⊆ X/);
});

test("Lean pane 'Go to source' posts a navigate message with the item's offsets", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [{
          id: "theorem:main_theorem:0",
          kind: "theorem",
          label: "main_theorem",
          status: "missing-stub",
          sourceFile: "main.tex",
          sourceStartLine: 1,
          sourceEndLine: 4,
          sourceStartOffset: 3,
          sourceEndOffset: 42,
          naturalLanguageRendered: "A theorem.",
          naturalLanguageLatex: "A theorem.",
          leanKind: "theorem",
          leanDeclarationName: "main_theorem",
          formalizable: true
        }],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickButtonLabel("Go to source");

  const navigate = harness.postedMessages.find((message) => message.type === "OL_LEAN_NAVIGATE");
  assert.ok(navigate, "expected an OL_LEAN_NAVIGATE message");
  assert.equal(navigate.sourceFile, "main.tex");
  assert.equal(navigate.from, 3);
  assert.equal(navigate.to, 42);
});

test("source popover 'Show in Lean pane' opens, expands, and highlights the matching item", async () => {
  const harness = createContentHarness(
    { status: "formalized" },
    { targetLabel: "main_theorem" },
    {
      locationPath: "/project/unknown",
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [{
          id: "theorem:main_theorem:0",
          kind: "theorem",
          label: "main_theorem",
          status: "valid",
          sourceFile: "sections/defs.tex",
          sourceStartLine: 1,
          sourceEndLine: 4,
          sourceStartOffset: 0,
          sourceEndOffset: 42,
          naturalLanguageRendered: "A theorem.",
          naturalLanguageLatex: "A theorem.",
          leanKind: "theorem",
          leanDeclarationName: "main_theorem",
          leanArtifactContent: "theorem main_theorem : True := by\n  trivial\n"
        }],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();

  harness.openTargetPopover();
  assert.equal(harness.hasButtonText("Show in Lean pane"), true);

  harness.clickButtonText("Show in Lean pane");
  await flushPromises();

  assert.match(harness.bodyText(), /Lean pane/);
  assert.match(harness.bodyText(), /No generated Lean artifact|theorem main_theorem/);
  assert.equal(harness.countSelector(".ol-lean-project-detail"), 1);
  assert.equal(harness.countSelector(".ol-lean-project-item-focus"), 1);
  assert.ok(harness.paneTreeRowTexts().some((text) => text.includes("sections/")));
  assert.ok(harness.paneTreeRowTexts().some((text) => text.includes("defs.tex")));
  assert.equal(harness.firstFocusedPaneItemScrolled(), true);
  assert.match(harness.bodyText(), /Opened main_theorem in the Lean pane\./);
});

test("source popover 'Show in Lean pane' matches by Lean declaration name", async () => {
  const harness = createContentHarness(
    { status: "formalized" },
    { targetLabel: "main_theorem" },
    {
      locationPath: "/project/unknown",
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [{
          id: "theorem:display-label:0",
          kind: "theorem",
          label: "display-label",
          status: "valid",
          sourceFile: "main.tex",
          sourceStartLine: 1,
          sourceEndLine: 4,
          sourceStartOffset: 3,
          sourceEndOffset: 42,
          naturalLanguageRendered: "A theorem.",
          naturalLanguageLatex: "A theorem.",
          leanKind: "theorem",
          leanDeclarationName: "main_theorem"
        }],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();

  harness.openTargetPopover();
  harness.clickButtonText("Show in Lean pane");
  await flushPromises();

  assert.equal(harness.countSelector(".ol-lean-project-detail"), 1);
  assert.equal(harness.countSelector(".ol-lean-project-item-focus"), 1);
  assert.match(harness.bodyText(), /Opened display-label in the Lean pane\./);
});

test("source popover reports when 'Show in Lean pane' cannot find a matching item", async () => {
  const harness = createContentHarness(
    { status: "formalized" },
    { targetLabel: "main_theorem" },
    {
      locationPath: "/project/unknown",
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [{
          id: "theorem:other_theorem:0",
          kind: "theorem",
          label: "other_theorem",
          status: "valid",
          sourceFile: "main.tex",
          sourceStartLine: 1,
          sourceEndLine: 4,
          sourceStartOffset: 99,
          sourceEndOffset: 120,
          naturalLanguageRendered: "Other theorem.",
          naturalLanguageLatex: "Other theorem.",
          leanKind: "theorem",
          leanDeclarationName: "other_theorem"
        }],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();

  harness.openTargetPopover();
  harness.clickButtonText("Show in Lean pane");
  await flushPromises();

  assert.match(harness.bodyText(), /Could not find main_theorem in the Lean pane\./);
  assert.equal(harness.countSelector(".ol-lean-project-item-focus"), 0);
});

test("Lean pane 'Formalize' starts a run via the /formalize endpoint", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [{
          id: "theorem:main_theorem:0",
          kind: "theorem",
          label: "main_theorem",
          status: "missing-stub",
          sourceFile: "main.tex",
          sourceStartLine: 1,
          sourceEndLine: 4,
          sourceStartOffset: 0,
          sourceEndOffset: 42,
          naturalLanguageRendered: "A theorem.",
          naturalLanguageLatex: "A theorem.",
          leanKind: "theorem",
          leanDeclarationName: "main_theorem",
          formalizable: true,
          targetUses: ["helper_lemma"],
          targetContext: "Use the helper."
        }],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickButtonText("Formalize");
  await flushPromises();

  const call = harness.fetchCalls.find((entry) => entry.url.includes("/formalize"));
  assert.ok(call, "expected a POST to /formalize");
  assert.equal(call.options.method, "POST");
  const body = JSON.parse(call.options.body);
  assert.equal(body.targetLabel, "main_theorem");
  assert.equal(body.targetKind, "theorem");
  assert.deepEqual(body.targetUses, ["helper_lemma"]);
  assert.equal(body.targetContext, "Use the helper.");
  assert.equal(body.projectName, "Test Project");
  assert.equal(body.projectNamespace, "Lea.TestProject");
  assert.equal(body.sourceFile, "main.tex");
  assert.equal(body.sourceStartLine, 1);
  assert.equal(body.sourceEndLine, 4);
  assert.equal(body.mirroredSourcePath, ".lea/files/overleaf/main.tex");
  assert.match(body.sourceExcerpt, /A theorem\./);
});

test("Lean pane 'Continue best effort' resumes an eligible source obstruction with an explicit override", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [{
          id: "theorem:compactness_criterion:0",
          kind: "theorem",
          label: "compactness_criterion",
          status: "paused",
          sourceFile: "main.tex",
          sourceStartLine: 1,
          sourceEndLine: 4,
          sourceStartOffset: 0,
          sourceEndOffset: 42,
          naturalLanguageRendered: "Every open cover has a finite subcover.",
          naturalLanguageLatex: "Every open cover has a finite subcover.",
          leanKind: "theorem",
          leanDeclarationName: "compactness_criterion",
          formalizable: true,
          leanCheck: { status: "paused", stopReason: "source_obstruction" },
          sourceBundle: { proof: "", proofAssociation: { status: "missing" } }
        }],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems({ activeTex: "\\begin{theorem}\n% lea: formalize label=compactness_criterion\nEvery open cover has a finite subcover.\n\\end{theorem}" });
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();
  harness.clickButtonText("Continue best effort");
  await flushPromises();

  const call = harness.fetchCalls.find((entry) => entry.url.endsWith("/formalize"));
  assert.ok(call, "expected a POST to /formalize");
  const body = JSON.parse(call.options.body);
  assert.equal(body.targetLabel, "compactness_criterion");
  assert.equal(body.resume, true);
  assert.equal(body.bestEffort, true);
});

function pausedSourceManifest() {
  return { ok: true, rootFile: "main.tex", diagnostics: [], items: [{
    id: "theorem:demo_theorem:0", kind: "theorem", label: "demo_theorem", status: "paused",
    sourceFile: "main.tex", sourceStartLine: 1, sourceEndLine: 4,
    naturalLanguageLatex: "Old statement.", leanKind: "theorem", formalizable: true,
    apiRunId: "saved-run", leanCheck: { status: "paused", stopReason: "turn_cap" }
  }] };
}

test("Resume reparses live statement, proof and source hashes instead of cached pane text", async () => {
  const harness = createContentHarness({ status: "paused" }, {}, {
    locationPath: "/project/unknown", manifest: pausedSourceManifest()
  });
  await harness.loadVisibleTheorems({ activeTex: "\\begin{theorem}\n% lea: formalize label=demo_theorem\nNew statement.\n\\end{theorem}\n\\begin{proof}\nNew author proof.\n\\end{proof}" });
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();
  harness.clickButtonText("Resume");
  assert.ok(harness.hasButtonText("Resuming…"));
  await flushPromises();
  const call = harness.fetchCalls.find((entry) => entry.url.endsWith("/formalize"));
  assert.ok(call);
  const body = JSON.parse(call.options.body);
  assert.equal(body.resume, true);
  assert.equal(body.resumeRunId, "saved-run");
  assert.match(body.targetText, /New statement\./);
  assert.doesNotMatch(body.targetText, /Old statement/);
  assert.match(body.sourceBundle.proof, /New author proof\./);
  assert.equal(body.sourceBundle.proofAssociation.status, "associated");
  assert.equal(body.sourceHash, createHash("sha256").update(body.targetText.trim()).digest("hex"));
});

test("checkpoint failure stays inline and Re-formalize remains an explicit fresh action", async () => {
  const harness = createContentHarness({ status: "paused" }, {}, {
    locationPath: "/project/unknown", manifest: pausedSourceManifest(),
    formalizeFailure: { status: 409, error: "checkpoint_continuation_rejected",
      message: "Saved agent state is unavailable. Use Re-formalize." }
  });
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();
  harness.clickButtonText("Resume");
  await flushPromises();
  assert.match(harness.paneText(), /Saved agent state is unavailable/);
  assert.ok(harness.hasButtonText("Resume"));
  assert.ok(harness.hasButtonText("Re-formalize"));
  harness.clickButtonText("Re-formalize");
  await flushPromises();
  const calls = harness.fetchCalls.filter((entry) => entry.url.endsWith("/formalize"));
  assert.equal(calls.length, 2);
  assert.equal(JSON.parse(calls[0].options.body).resume, true);
  assert.equal(JSON.parse(calls[1].options.body).resume, false);
});

test("Lean pane keeps Formalize after an upstream dependency blocks startup", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      formalizeError: "Formalize referenced theorem first: helper_lemma.",
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [{
          id: "theorem:main_theorem:0",
          kind: "theorem",
          label: "main_theorem",
          status: "missing-stub",
          sourceFile: "main.tex",
          sourceStartLine: 1,
          sourceEndLine: 4,
          naturalLanguageLatex: "A theorem.",
          leanKind: "theorem",
          formalizable: true,
          targetUses: ["helper_lemma"]
        }],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  assert.equal(harness.countSelector(".ol-lean-project-detail"), 0);

  harness.clickButtonText("Formalize");
  await flushPromises();

  assert.equal(harness.countSelector(".ol-lean-project-detail"), 1);
  assert.equal(harness.hasButtonText("Formalize"), true);
  assert.equal(harness.hasButtonText("Retry formalize"), false);
  assert.deepEqual(harness.paneActionError(), {
    role: "alert",
    live: "assertive",
    text: "Dependency must be formalized firstFormalize referenced theorem first: helper_lemma. No Lea run was started."
  });
  assert.equal(harness.hasButtonLabel("Dismiss error message"), true);
  harness.clickButtonLabel("Dismiss error message");
  assert.equal(harness.paneActionError(), null);
});

test("source popover explains when an upstream dependency blocks formalization startup", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    { targetUses: ["helper_lemma"] },
    {
      locationPath: "/project/unknown",
      formalizeError: "Formalize referenced theorem first: helper_lemma."
    }
  );
  await harness.loadStatusForVisibleTheorem();
  harness.openTargetPopover();

  harness.clickButtonText("Formalize");
  await flushPromises();

  assert.deepEqual(harness.popoverActionError(), {
    role: "alert",
    live: "assertive",
    text: "Formalization blockedFormalize referenced theorem first: helper_lemma. No Lea run was started."
  });
  assert.equal(harness.hasButtonText("Formalize"), true);
});

test("a missing-key formalization error links directly to credential settings", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      formalizeFailure: {
        status: 400,
        error: "missing_openai_key",
        message: "OpenAI API key must be set before formalizing."
      }
    }
  );
  await harness.loadStatusForVisibleTheorem();
  harness.openTargetPopover();

  harness.clickButtonText("Formalize");
  await flushPromises();

  assert.deepEqual(harness.popoverActionError(), {
    role: "alert",
    live: "assertive",
    text: "API key requiredOpenAI API key must be set before formalizing.Add API key"
  });
  harness.clickButtonText("Add API key");
  await flushPromises();
  assert.equal(harness.countSelector(".ol-lean-settings-popover"), 1);
});

test("source popover reports a cost cap inline without a separate floating notice", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      formalizeFailure: {
        status: 402,
        error: "max_spend_reached",
        message: "Max spend limit has been reached."
      }
    }
  );
  await harness.loadStatusForVisibleTheorem();
  harness.openTargetPopover();

  harness.clickButtonText("Formalize");
  await flushPromises();

  assert.deepEqual(harness.popoverActionError(), {
    role: "alert",
    live: "assertive",
    text: "Action failedMax spend limit has been reached."
  });
  assert.equal(harness.countSelector(".ol-lean-cost-cap-notice"), 0);
});

test("Lean pane cost-cap dismissal survives refresh and clears for a retry", async () => {
  const item = {
    id: "theorem:main_theorem:0",
    kind: "theorem",
    label: "main_theorem",
    status: "missing-stub",
    sourceFile: "main.tex",
    sourceStartLine: 1,
    sourceEndLine: 4,
    naturalLanguageLatex: "A theorem.",
    leanKind: "theorem",
    formalizable: true
  };
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      formalizeFailure: (calls) => calls.filter((call) => call.url.endsWith("/formalize")).length === 1
        ? {
            status: 402,
            error: "max_spend_reached",
            message: "Max spend limit has been reached."
          }
        : null,
      manifest: { ok: true, rootFile: "main.tex", items: [item], diagnostics: [] }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();

  harness.clickButtonText("Formalize");
  await flushPromises();

  assert.deepEqual(harness.paneActionError(), {
    role: "alert",
    live: "assertive",
    text: "Cost cap reachedLea could not complete this formalization because the configured maximum spend has been reached. Increase or clear the cap in Lea settings, then try again.Open settings"
  });
  assert.equal(harness.countSelector(".ol-lean-cost-cap-notice"), 0);
  assert.equal(harness.hasButtonText("Formalize"), true);
  assert.equal(harness.hasButtonLabel("Dismiss error message"), true);

  harness.clickButtonLabel("Dismiss error message");
  assert.equal(harness.paneActionError(), null);

  harness.clickButtonLabel("Refresh Lean pane");
  await flushPromises();
  assert.equal(harness.paneActionError(), null);

  harness.clickButtonText("Formalize");
  await flushPromises();
  assert.equal(harness.paneActionError(), null);
});

test("Lean pane explains a max-spend failure reported after a run started", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [{
          id: "theorem:main_theorem:0",
          kind: "theorem",
          label: "main_theorem",
          status: "invalid",
          finalStatus: "max_spend",
          failureCode: "max_spend_reached",
          failureMessage: "Max spend limit reached. Lea run was cancelled.",
          sourceFile: "main.tex",
          sourceStartLine: 1,
          sourceEndLine: 4,
          naturalLanguageLatex: "A theorem.",
          leanKind: "theorem",
          formalizable: true
        }],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();

  assert.match(harness.paneActionError()?.text || "", /Cost cap reached/);
  assert.match(harness.paneActionError()?.text || "", /Increase or clear the cap in Lea settings/);
  assert.equal(harness.countSelector(".ol-lean-cost-cap-notice"), 0);
  harness.clickButtonLabel("Dismiss error message");
  assert.equal(harness.paneActionError(), null);

  harness.clickButtonLabel("Refresh Lean pane");
  await flushPromises();
  assert.equal(harness.paneActionError(), null);
});

test("Formalize all renders an accessible, collapsible queue with active turn progress", async () => {
  const items = Array.from({ length: 8 }, (_unused, index) => {
    const number = index + 1;
    return {
      id: `theorem:t${number}:${index}`,
      kind: "theorem",
      label: `t${number}`,
      status: "missing-stub",
      sourceFile: "main.tex",
      sourceStartLine: number,
      sourceEndLine: number,
      naturalLanguageLatex: `Theorem ${number}.`,
      leanKind: "theorem",
      leanDeclarationName: `t${number}`,
      formalizable: true,
      ...(number === 4 ? { turnProgress: { current: 7, max: 20 } } : {})
    };
  });
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: { ok: true, rootFile: "main.tex", items, diagnostics: [] },
      targetBatch: {
        ok: true,
        batchId: "formalize-batch-1",
        operation: "formalize",
        done: false,
        running: true,
        pausedOn: null,
        items: [
          { targetKind: "theorem", targetLabel: "t1", state: "formalized" },
          { targetKind: "theorem", targetLabel: "t2", state: "formalized" },
          { targetKind: "theorem", targetLabel: "t3", state: "formalized" },
          // Initial launch snapshots can still report the first dispatch as
          // pending even though the batch loop itself is already running.
          { targetKind: "theorem", targetLabel: "t4", state: "pending" },
          { targetKind: "theorem", targetLabel: "t5", state: "pending" },
          { targetKind: "theorem", targetLabel: "t6", state: "pending" },
          { targetKind: "theorem", targetLabel: "t7", state: "pending" },
          { targetKind: "theorem", targetLabel: "t8", state: "pending" }
        ]
      }
    }
  );

  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickButtonText("Formalize all (8)");
  await flushPromises();

  let queue = harness.batchQueue();
  assert.ok(queue, "expected a batch queue card");
  assert.deepEqual(queue.progress, {
    role: "progressbar",
    label: "Formalize all: 3 of 8 processed. 3 verified · 0 disproved · 0 incomplete · 0 failed · 0 skipped",
    min: "0",
    max: "8",
    now: "3"
  });
  assert.match(queue.text, /Current · 4 of 8●t4Formalizing… · Lea turn 7 of 20/);
  assert.equal(queue.pendingCount, 3, "only the next three queued items start expanded");
  assert.equal(queue.completedCount, 0, "completed work starts collapsed");
  assert.match(queue.text, /t5Queued · position 5 of 8/);
  assert.match(queue.text, /\+1 more queued/);
  assert.match(queue.text, /Show 3 completed/);

  harness.clickButtonText("+1 more queued");
  queue = harness.batchQueue();
  assert.equal(queue.pendingCount, 4);
  assert.match(queue.text, /t8Queued · position 8 of 8/);

  harness.clickButtonText("Show 3 completed");
  queue = harness.batchQueue();
  assert.equal(queue.completedCount, 3);
  assert.match(queue.text, /t1formalized and verified\./);
  assert.match(queue.text, /Hide 3 completed/);
});

function queuePaneItem(label, overrides = {}) {
  return {
    id: `theorem:${label}:0`, kind: "theorem", label, targetKind: "theorem",
    status: "missing-stub", sourceFile: "main.tex", sourceStartLine: 1, sourceEndLine: 4,
    naturalLanguageLatex: `Statement of ${label}.`, leanKind: "theorem",
    leanDeclarationName: label, formalizable: true, ...overrides
  };
}

async function startNavigationQueue(items, entries, options = {}) {
  const batch = {
    ok: true, batchId: "navigation-batch", operation: "formalize", done: false,
    running: true, pausedOn: null, items: entries
  };
  const harness = createContentHarness({ status: "unformalized" }, {}, {
    locationPath: "/project/unknown",
    manifest: { ok: true, rootFile: "main.tex", items, diagnostics: [] },
    targetBatch: batch, repairStatus: batch, ...options
  });
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.leanPane().querySelector(".ol-lean-formalize-all-button").click();
  await flushPromises();
  for(let turn=0;turn<100&&!harness.batchQueue()?.progress;turn++)await flushPromises();
  return harness;
}

function queueNavigationButton(harness, label, action, kind = "theorem") {
  return harness.leanPane().querySelectorAll(".ol-lean-batch-queue-action").find((button) => {
    const key = JSON.parse(button.dataset.batchNavigationKey);
    return key[2] === kind && key[3] === label && key[4] === action;
  });
}

test("Formalize all keeps both navigation buttons across all queue sections and polling", async () => {
  const entries = [
    ["current", "running"], ["next1", "pending"], ["next2", "pending"],
    ["next3", "pending"], ["next4", "pending"], ["completed", "formalized"],
    ["failed", "failed"], ["incomplete", "incomplete"], ["review", "needs_review"],
    ["disproved", "disproved"], ["skipped", "skipped"], ["canceled", "canceled"]
  ].map(([targetLabel, state]) => ({ targetKind: "theorem", targetLabel, state }));
  const harness = await startNavigationQueue(entries.map((entry) => queuePaneItem(entry.targetLabel)), entries);
  const checkButtons = (count) => {
    const rows = harness.leanPane().querySelectorAll(".ol-lean-batch-queue-item");
    assert.equal(rows.length, count);
    for (const row of rows) {
      const label = row.querySelector("strong").textContent;
      const buttons = row.querySelectorAll(".ol-lean-batch-queue-action");
      assert.deepEqual(buttons.map((button) => button.textContent), ["Show in Lean pane", "Open in LeaChat"]);
      assert.equal(buttons[0].attributes["aria-label"], `Show ${label} in Lean pane`);
      assert.equal(buttons[1].attributes["aria-label"], `Open ${label} in LeaChat`);
    }
  };
  checkButtons(9);
  harness.clickButtonText("+1 more queued");
  harness.clickButtonText("+1 more item needing attention");
  harness.clickButtonText("Show 1 completed");
  checkButtons(12);
  const progress = harness.batchQueue().progress;
  await harness.runTimersAt(0);
  checkButtons(12);
  assert.deepEqual(harness.batchQueue().progress, progress);
  assert.equal(harness.hasButtonText("Stop"), true);
});

for (const targetKind of ["theorem", "definition"]) {
  test(`queue Lean navigation expands and focuses the exact ${targetKind} before an artifact exists`, async () => {
    const items = [
      queuePaneItem("same", { sourceFile: "chapters/proofs.tex", leanDeclarationName: "renamed_theorem" }),
      queuePaneItem("same", { id: "definition:same:1", kind: "definition", targetKind: "definition",
        leanKind: "def", sourceFile: "chapters/nested/definitions.tex" })
    ];
    const harness = await startNavigationQueue(items, [{ targetKind, targetLabel: "same", state: "pending" }]);
    const progress = harness.batchQueue().progress;
    const before = harness.fetchCalls.length;
    queueNavigationButton(harness, "same", "lean-pane", targetKind).click();
    await flushPromises();
    const item = items[targetKind === "definition" ? 1 : 0];
    assert.equal(harness.focusedPaneItemId(), item.id);
    assert.equal(harness.firstFocusedPaneItemScrolled(), true);
    assert.equal(harness.countSelector(".ol-lean-project-detail"), 1);
    assert.ok(harness.paneTreeRowTexts().some((text) => text.includes("chapters/")));
    assert.ok(harness.paneTreeRowTexts().some((text) => text.includes(path.basename(item.sourceFile))));
    assert.equal(harness.document.activeElement.closest(".ol-lean-project-item").dataset.itemId, item.id);
    await harness.runTimersAt(0);
    assert.equal(harness.document.activeElement.closest(".ol-lean-project-item").dataset.itemId, item.id);
    assert.deepEqual(harness.batchQueue().progress, progress);
    assert.ok(!harness.fetchCalls.slice(before).some((call) => /\/(formalize|stub|lean-pane\/chat\/send)$/.test(call.url)));
  });
}

test("queued items open their saved LeaChat session and enable new sessions after refresh", async () => {
  let items = [queuePaneItem("saved", { leaSessionId: "prior-session" }), queuePaneItem("new")];
  const manifest = () => ({ ok: true, rootFile: "main.tex", items, diagnostics: [] });
  const harness = await startNavigationQueue(items, items.map((item) => ({
    targetKind: "theorem", targetLabel: item.label, state: "pending"
  })), { manifest });
  const before = harness.fetchCalls.length;
  const saved = queueNavigationButton(harness, "saved", "lea-chat");
  assert.equal(saved.disabled, false);
  saved.click();
  await flushPromises();
  let opened = harness.runtimeMessages.filter((message) => message.type === "OPEN_LEA_SESSION");
  assert.equal(new URL(opened[0].url).searchParams.get("session"), "prior-session");
  assert.equal(harness.fetchCalls.length, before, "opening a saved session performs no companion request");
  const pending = queueNavigationButton(harness, "new", "lea-chat");
  assert.equal(pending.disabled, true);
  assert.equal(pending.title, "No LeaChat session is available yet.");
  pending.click();
  assert.equal(harness.runtimeMessages.filter((message) => message.type === "OPEN_LEA_SESSION").length, 1);
  items = [items[0], { ...items[1], leaSessionUrl: "http://localhost:5173/?session=new-session&formalization=form-1" }];
  harness.clickButtonLabel("Refresh Lean pane");
  await flushPromises();
  const ready = queueNavigationButton(harness, "new", "lea-chat");
  assert.equal(ready.disabled, false);
  ready.click();
  await flushPromises();
  opened = harness.runtimeMessages.filter((message) => message.type === "OPEN_LEA_SESSION");
  assert.equal(opened[1].url, items[1].leaSessionUrl);
  assert.ok(!harness.fetchCalls.slice(before).some((call) => /\/(formalize|stub|lean-pane\/chat\/(send|session))$/.test(call.url)));
});

test("queue navigation resolves refreshed manifest items rather than captured item objects", async () => {
  let item = queuePaneItem("moving", { leaSessionId: "old-session" });
  const harness = await startNavigationQueue([item], [{ targetKind: "theorem", targetLabel: "moving", state: "running" }], {
    manifest: () => ({ ok: true, items: [item], diagnostics: [] })
  });
  const originalLeanButton = queueNavigationButton(harness, "moving", "lean-pane");
  const originalChatButton = queueNavigationButton(harness, "moving", "lea-chat");
  item = { ...item, id: "theorem:moving:7", sourceFile: "nested/moved.tex", leaSessionId: "new-session" };
  harness.clickButtonLabel("Refresh Lean pane");
  await flushPromises();
  originalLeanButton.click();
  originalChatButton.click();
  await flushPromises();
  assert.equal(harness.focusedPaneItemId(), item.id);
  const opened = harness.runtimeMessages.find((message) => message.type === "OPEN_LEA_SESSION");
  assert.equal(new URL(opened.url).searchParams.get("session"), "new-session");
});

for (const situation of ["missing", "ambiguous", "other-project"]) {
  test(`queue navigation stays disabled for a ${situation} manifest target`, async () => {
    const unrelated = queuePaneItem("unrelated");
    const item = queuePaneItem("target", { leaSessionId: "saved-session" });
    const items = situation === "missing" ? [unrelated]
      : situation === "ambiguous" ? [item, { ...item, id: "theorem:target:1", sourceFile: "other.tex" }]
        : [unrelated, { ...item, overleafProjectId: "other-project" }];
    const harness = await startNavigationQueue(items, [{ targetKind: "theorem", targetLabel: "target", state: "pending" }]);
    for (const action of ["lean-pane", "lea-chat"]) {
      const button = queueNavigationButton(harness, "target", action);
      assert.equal(button.disabled, true);
      button.click();
    }
    assert.equal(harness.focusedPaneItemId(), "");
    assert.ok(!harness.runtimeMessages.some((message) => message.type === "OPEN_LEA_SESSION"));
  });
}

test("queue opening stays busy and preserves keyboard focus across polling, including tab failures", async () => {
  let finishOpen;
  const harness = await startNavigationQueue([queuePaneItem("saved", { leaSessionId: "session-1" })], [
    { targetKind: "theorem", targetLabel: "saved", state: "running" }
  ], { runtimeSendMessage(message, callback) {
    if (message.type === "OPEN_LEA_SESSION") finishOpen = callback;
    else callback?.({ ok: true });
  } });
  const button = queueNavigationButton(harness, "saved", "lea-chat");
  button.focus();
  button.click();
  await flushPromises();
  assert.equal(button.attributes["aria-busy"], "true");
  await harness.runTimersAt(0);
  const replacement = queueNavigationButton(harness, "saved", "lea-chat");
  assert.notEqual(replacement, button);
  assert.equal(harness.document.activeElement, replacement);
  assert.equal(replacement.attributes["aria-disabled"], "true");
  replacement.click();
  await flushPromises();
  assert.equal(harness.runtimeMessages.filter((message) => message.type === "OPEN_LEA_SESSION").length, 1);
  finishOpen({ ok: false, message: "Could not open Lea session tab." });
  await flushPromises();
  assert.equal(replacement.attributes["aria-busy"], "false");
  assert.equal(replacement.attributes["aria-disabled"], "false");
  assert.match(harness.bodyText(), /Could not open Lea session tab/);
  const status = harness.leanPane().querySelector(".ol-lean-project-pane-status");
  assert.equal(status.attributes.role, "status");
  assert.equal(status.attributes["aria-live"], "polite");
  harness.clickButtonLabel("Refresh Lean pane");
  await flushPromises();
  assert.match(harness.bodyText(), /Could not open Lea session tab/);
});

test("queue session navigation never falls back to the Lea homepage when a session disappears", async () => {
  let item = queuePaneItem("saved", { leaSessionId: "session-1" });
  const harness = await startNavigationQueue([item], [{ targetKind: "theorem", targetLabel: "saved", state: "pending" }], {
    manifest: () => ({ ok: true, items: [item], diagnostics: [] }),
    fetchOverride: async (url) => url.endsWith("/lean-pane/chat/session")
      ? { ok: true, json: async () => ({ ok: true, status: "no-session", leaSessionUrl: null }) } : undefined
  });
  const oldButton = queueNavigationButton(harness, "saved", "lea-chat");
  item = { ...item, leaSessionId: undefined };
  harness.clickButtonLabel("Refresh Lean pane");
  await flushPromises();
  oldButton.click();
  await flushPromises();
  assert.match(harness.bodyText(), /No LeaChat session is available yet/);
  assert.ok(!harness.runtimeMessages.some((message) => message.type === "OPEN_LEA_SESSION"));
  assert.ok(harness.fetchCalls.some((call) => call.url.endsWith("/lean-pane/chat/session")));
});

for (const outcome of ["formalized", "failed"]) {
  test(`queue focus follows a running row into its ${outcome} outcome group`, async () => {
    const entries = Array.from({ length: 6 }, (_, index) => ({
      targetKind: "theorem", targetLabel: `failed${index}`, state: "failed"
    }));
    entries.push({ targetKind: "theorem", targetLabel: "current", state: "running" });
    const harness = await startNavigationQueue(entries.map((entry) => queuePaneItem(entry.targetLabel)), entries);
    queueNavigationButton(harness, "current", "lean-pane").focus();
    entries.at(-1).state = outcome;
    await harness.runTimersAt(0);
    const replacement = queueNavigationButton(harness, "current", "lean-pane");
    assert.ok(replacement, "the focused outcome remains visible");
    assert.equal(harness.document.activeElement, replacement);
    assert.equal(harness.batchQueue().completedCount, outcome === "formalized" ? 1 : 0);
    assert.equal(harness.batchQueue().attentionItemCount, outcome === "failed" ? 7 : 5);
  });
}

for (const operation of ["stub", "repair"]) {
  test(`${operation} all queue rows do not gain Formalize all navigation controls`, async () => {
    const batch = { ok: true, batchId: "other-batch", operation, done: false, running: true,
      items: [{ targetKind: "theorem", targetLabel: "target", state: "running" }] };
    const harness = await startNavigationQueue([queuePaneItem("target")], batch.items, { targetBatch: batch, repairStatus: batch });
    assert.equal(harness.countSelector(".ol-lean-batch-queue-action"), 0);
  });
}

test("Formalize all limits cost-cap failures to five statements until expanded", async () => {
  const items = Array.from({ length: 9 }, (_unused, index) => ({
    id: `theorem:cap_t${index + 1}:${index}`,
    kind: "theorem",
    label: `cap_t${index + 1}`,
    status: "missing-stub",
    sourceFile: "main.tex",
    sourceStartLine: index + 1,
    sourceEndLine: index + 1,
    naturalLanguageLatex: `Theorem ${index + 1}.`,
    leanKind: "theorem",
    leanDeclarationName: `cap_t${index + 1}`,
    formalizable: true
  }));
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: { ok: true, rootFile: "main.tex", items, diagnostics: [] },
      targetBatch: {
        ok: true,
        batchId: "formalize-cost-cap",
        operation: "formalize",
        done: true,
        running: false,
        pausedOn: null,
        items: items.map((item, index) => ({
          targetKind: "theorem",
          targetLabel: item.label,
          state: index === 0 ? "failed" : "skipped",
          reason: index === 0 ? "global_spend_cap" : "max_spend"
        }))
      }
    }
  );

  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickButtonText("Formalize all (9)");
  await flushPromises();

  let queue = harness.batchQueue();
  assert.equal(queue.attentionItemCount, 5);
  assert.equal(queue.attentionExpanded, "false");
  assert.match(queue.text, /\+4 more unformalized statements/);
  assert.doesNotMatch(queue.text, /cap_t9/);

  harness.clickButtonText("+4 more unformalized statements");
  queue = harness.batchQueue();
  assert.equal(queue.attentionItemCount, 9);
  assert.equal(queue.attentionExpanded, "true");
  assert.match(queue.text, /cap_t9/);

  harness.clickButtonText("Show fewer unformalized statements");
  queue = harness.batchQueue();
  assert.equal(queue.attentionItemCount, 5);
  assert.equal(queue.attentionExpanded, "false");
  assert.doesNotMatch(queue.text, /cap_t9/);
});

test("completed Formalize all shows its saved synthesis", async () => {
  const report = {
    batch_id: "formalize-batch-report",
    finished_at: "2026-01-01T00:01:00Z",
    state: "ready",
    narrative: "t1 was verified. Review the source gap in t2.",
    facts: {
      summary: "1 verified, 0 disproved, 1 failed, 0 skipped, 0 already verified, 0 stopped.",
      items: [
        { targetLabel: "t1", state: "formalized", reason: null, reportingIncomplete: false },
        { targetLabel: "t2", state: "failed", reason: "source_gap", reportingIncomplete: true }
      ]
    }
  };
  const items = ["t1", "t2"].map((label, index) => ({
    id: `theorem:${label}:${index}`, kind: "theorem", label, status: "missing-stub",
    sourceFile: "main.tex", sourceStartLine: index + 1, sourceEndLine: index + 1,
    naturalLanguageLatex: `Theorem ${label}.`, leanKind: "theorem",
    leanDeclarationName: label, formalizable: true
  }));
  const harness = createContentHarness({ status: "unformalized" }, {}, {
    locationPath: "/project/unknown",
    manifest: { ok: true, rootFile: "main.tex", items, diagnostics: [] },
    targetBatch: {
      ok: true, batchId: report.batch_id, operation: "formalize", done: true,
      running: false, pausedOn: null, reportId: report.batch_id, reportState: "ready", report,
      items: [
        { targetKind: "theorem", targetLabel: "t1", state: "formalized" },
        { targetKind: "theorem", targetLabel: "t2", state: "failed", reason: "source_gap" }
      ]
    },
    repairStatus: {
      ok: true, batchId: report.batch_id, operation: "formalize", done: true,
      running: false, pausedOn: null, reportId: report.batch_id, reportState: "ready", report,
      items: [
        { targetKind: "theorem", targetLabel: "t1", state: "formalized" },
        { targetKind: "theorem", targetLabel: "t2", state: "failed", reason: "source_gap" }
      ]
    }
  });
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickButtonText("Formalize all (2)");
  await flushPromises();
  assert.ok(harness.batchQueue(), harness.paneText());
  assert.match(harness.batchQueue().text, /Formalize all report1 verified/);
  assert.match(harness.batchQueue().text, /t1 was verified\. Review the source gap in t2\./);
});

test("saved Formalize all reports reopen from project history", async () => {
  const report = {
    batch_id: "formalize-batch-report", finished_at: "2026-01-01T00:01:00Z",
    state: "ready", narrative: "Review the source gap in t2.",
    facts: { summary: "1 failed.", items: [
      { targetLabel: "t2", state: "failed", reason: "source_gap", reportingIncomplete: true }
    ] }
  };
  const harness = createContentHarness({ status: "unformalized" }, {}, {
    locationPath: "/project/project-1",
    manifest: { ok: true, rootFile: "main.tex", items: [{
      id: "theorem:t2", kind: "theorem", label: "t2", status: "invalid",
      sourceFile: "main.tex", sourceStartLine: 1, sourceEndLine: 1,
      naturalLanguageLatex: "Theorem t2.", leanKind: "theorem"
    }], diagnostics: [] },
    batchReports: { reports: [{ batch_id: report.batch_id, finished_at: report.finished_at, state: "ready" }] },
    batchReportDetail: report
  });
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  const disclosure = harness.leanPane().querySelector(".ol-lean-batch-report-history").querySelector("button");
  assert.equal(disclosure.tagName, "BUTTON");
  assert.equal(disclosure.attributes["aria-expanded"], "false");
  harness.clickButtonText("Past Formalize all reports");
  assert.equal(harness.leanPane().querySelector(".ol-lean-batch-report-history").querySelector("button").attributes["aria-expanded"], "true");
  assert.match(harness.paneText(), /Summary ready/);
  harness.clickButtonContaining("Summary ready");
  await flushPromises();
  assert.match(harness.paneText(), /no final Lea Status assessment/);
  assert.equal(harness.hasButtonLabel("Show t2 in Lean pane"), true);
  harness.clickButtonLabel("Show t2 in Lean pane");
  assert.match(harness.paneText(), /Theorem t2/);
});

test("report history exposes loading, empty, and retryable error states", async () => {
  let finishLoading;
  const pending = new Promise((resolve) => { finishLoading = resolve; });
  const harness = createContentHarness({ status: "unformalized" }, {}, {
    locationPath: "/project/project-1",
    manifest: { ok: true, rootFile: "main.tex", items: [], diagnostics: [] },
    batchReports: () => pending
  });
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickButtonText("Past Formalize all reports");
  assert.match(harness.paneText(), /Loading reports/);
  finishLoading({ reports: [] });
  await flushPromises();
  assert.match(harness.paneText(), /No past Formalize all reports yet/);

  const errorHarness = createContentHarness({ status: "unformalized" }, {}, {
    locationPath: "/project/project-1",
    manifest: { ok: true, rootFile: "main.tex", items: [], diagnostics: [] },
    reportListError: "Report service unavailable"
  });
  await errorHarness.loadVisibleTheorems();
  errorHarness.clickPaneTrigger();
  await flushPromises();
  errorHarness.clickButtonText("Past Formalize all reports");
  assert.match(errorHarness.paneText(), /Report service unavailable/);
  assert.equal(errorHarness.hasButtonText("Reload reports"), true);
});

test("stopping a batch preserves a completed race winner and reports stopped items separately", async () => {
  const items = Array.from({ length: 4 }, (_unused, index) => {
    const number = index + 1;
    return {
      id: `theorem:stop_t${number}:${index}`,
      kind: "theorem",
      label: `stop_t${number}`,
      status: "missing-stub",
      sourceFile: "main.tex",
      sourceStartLine: number,
      sourceEndLine: number,
      naturalLanguageLatex: `Stop theorem ${number}.`,
      leanKind: "theorem",
      leanDeclarationName: `stop_t${number}`,
      formalizable: true
    };
  });
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: { ok: true, rootFile: "main.tex", items, diagnostics: [] },
      targetBatch: {
        ok: true,
        batchId: "formalize-stop-race",
        operation: "formalize",
        done: false,
        running: true,
        pausedOn: null,
        items: [
          { targetKind: "theorem", targetLabel: "stop_t1", state: "running" },
          { targetKind: "theorem", targetLabel: "stop_t2", state: "pending" },
          { targetKind: "theorem", targetLabel: "stop_t3", state: "pending" },
          { targetKind: "theorem", targetLabel: "stop_t4", state: "pending" }
        ]
      },
      batchCancel: {
        ok: true,
        batchId: "formalize-stop-race",
        operation: "formalize",
        done: true,
        canceled: true,
        running: false,
        pausedOn: null,
        items: [
          { targetKind: "theorem", targetLabel: "stop_t1", state: "formalized" },
          { targetKind: "theorem", targetLabel: "stop_t2", state: "canceled" },
          { targetKind: "theorem", targetLabel: "stop_t3", state: "canceled" },
          { targetKind: "theorem", targetLabel: "stop_t4", state: "canceled" }
        ]
      }
    }
  );

  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickButtonText("Formalize all (4)");
  await flushPromises();
  harness.clickButtonText("Stop");
  await flushPromises();

  let queue = harness.batchQueue();
  assert.match(queue.text, /Formalize allStopped1 verified · 0 disproved · 0 incomplete · 0 failed · 0 skipped · 3 stopped/);
  assert.deepEqual(queue.progress, {
    role: "progressbar",
    label: "Formalize all: 4 of 4 processed. 1 verified · 0 disproved · 0 incomplete · 0 failed · 0 skipped · 3 stopped",
    min: "0",
    max: "4",
    now: "4"
  });
  assert.deepEqual(queue.progressSegments, ["success", "canceled", "canceled", "canceled"]);
  assert.match(queue.text, /stop_t2stopped\./);
  assert.match(queue.text, /Show 1 completed/);
  harness.clickButtonText("Show 1 completed");
  queue = harness.batchQueue();
  assert.match(queue.text, /stop_t1formalized and verified\./);
  assert.doesNotMatch(queue.text, /stop_t1stopped\./);
});

// --- Manual edit (docs/FEATURE-overleaf-lean-pane-manual-edit.md) ----------

function editableItem(overrides = {}) {
  return {
    id: "theorem:main_theorem:0",
    kind: "theorem",
    label: "main_theorem",
    status: "valid",
    sourceFile: "main.tex",
    sourceStartLine: 1,
    sourceEndLine: 4,
    naturalLanguageRendered: "A theorem.",
    naturalLanguageLatex: "A theorem.",
    leanKind: "theorem",
    leanDeclarationName: "main_theorem",
    leanArtifactContent: "theorem main_theorem : True := by\n  sorry\n",
    ...overrides
  };
}

test("Lean pane 'Edit' only appears for items with a recorded artifact", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: {
        ok: true,
        rootFile: "main.tex",
        items: [editableItem({ id: "theorem:missing:0", label: "missing", leanArtifactContent: undefined, status: "missing-stub" })],
        diagnostics: []
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();

  assert.equal(harness.hasButtonText("Edit"), false);
});

test("Lean pane 'Edit' opens an inline textarea pre-filled with the artifact and shows the pre-save impact preview", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: { ok: true, rootFile: "main.tex", items: [editableItem()], diagnostics: [] },
      editStart: {
        ok: true,
        leaSessionId: "sess-1",
        path: "main_theorem.lean",
        content: "theorem main_theorem : True := by\n  sorry\n",
        dependents: [{ targetLabel: "corollary_a", moduleName: "Lea.P.corollary_a", relativePath: "corollary_a.lean" }]
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();

  harness.clickButtonText("Edit");
  await flushPromises();

  const textarea = harness.editTextarea();
  assert.ok(textarea, "expected an edit textarea to render");
  assert.match(textarea.value, /theorem main_theorem : True/);
  assert.match(harness.bodyText(), /Editing this may affect 1 downstream item: corollary_a\./);
  assert.equal(harness.hasButtonText("Save"), true);
  assert.equal(harness.hasButtonText("Cancel"), true);
  // the read-only artifact view and the Edit button itself are replaced while editing
  assert.equal(harness.hasButtonText("Edit"), false);
});

test("Lean pane edit 'Cancel' discards the draft and makes no save request", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: { ok: true, rootFile: "main.tex", items: [editableItem()], diagnostics: [] }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();
  harness.clickButtonText("Edit");
  await flushPromises();

  harness.clickButtonText("Cancel");

  assert.equal(harness.editTextarea(), null);
  assert.equal(harness.hasButtonText("Edit"), true);
  assert.ok(!harness.fetchCalls.some((call) => call.url.includes("/lean-pane/edit/save")));
});

test("Lean pane edit 'Save' posts the edited content and renders the cascade impact summary", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: { ok: true, rootFile: "main.tex", items: [editableItem()], diagnostics: [] },
      editSave: {
        ok: true,
        unchanged: false,
        ownResult: { checkStatus: "ok", classification: { kind: "signature" } },
        dependentsImpact: [
          { targetLabel: "corollary_a", status: "invalid", attributed: true, busy: false, brokenByUpstream: { targetLabel: "main_theorem", renamed: false } }
        ]
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();
  harness.clickButtonText("Edit");
  await flushPromises();
  harness.setEditTextareaValue("theorem main_theorem (h : True) : True := by\n  sorry\n");

  harness.clickButtonText("Save");
  await flushPromises();

  const saveCall = harness.fetchCalls.find((call) => call.url.includes("/lean-pane/edit/save"));
  assert.ok(saveCall, "expected a POST to /lean-pane/edit/save");
  const body = JSON.parse(saveCall.options.body);
  assert.equal(body.targetLabel, "main_theorem");
  assert.equal(body.targetKind, "theorem");
  assert.match(body.content, /theorem main_theorem \(h : True\)/);

  // edit view closes and the pane re-fetches the manifest (the plan's stated
  // approach: reuse the normal refresh path rather than a bespoke per-item patch)
  assert.equal(harness.editTextarea(), null);
  // the edited item's OWN outcome is not repeated here -- it now drives that
  // item's status chip directly (a separate, adapter-level fix); this note
  // is scoped to what only cross-item impact can tell you
  assert.doesNotMatch(harness.bodyText(), /Own check:/);
  assert.match(harness.bodyText(), /1 downstream item affected: 1 broken\./);
  assert.match(harness.bodyText(), /corollary_a: broken by this edit\./);
});

function reviewResponse(body, status = 200) {
  return { ok: status < 400, status, async json() { return body; } };
}

function reviewFixture(outcomes = ['proved', 'disproved']) {
  return { ok: true, review: { eligible: true, revision_token: 'revision-1', allowed_outcomes: outcomes,
    reason: 'Lean check passed.', candidate_declaration: 'claim' },
    source_snapshot: { statement: 'Full current LaTeX.', proof: 'Associated author proof.', context: 'Source context.' },
    artifacts: [{ path: 'Claim.lean', content: 'theorem claim : True := by trivial\n' + '-- full content\n'.repeat(400) }] };
}

function reviewButton(h, text) {
  const button = h.document.body.querySelectorAll('button').find(button => button.textContent === text);
  assert.ok(button, `Expected ${text}`);
  return button;
}

test('review approval loads full evidence, requires a theorem outcome, and never formalizes', async () => {
  const status = { status: 'needs_review', review: { eligible: true }, leaSessionId: 'session-1' };
  const fixture = reviewFixture();
  const h = createContentHarness(status, {}, { fetchOverride(url, options) {
    if (url.endsWith('/review/context')) return reviewResponse(fixture);
    if (url.endsWith('/review/decision')) {
      const request = JSON.parse(options.body).decision;
      assert.equal(request.outcome, 'disproved');
      assert.equal(request.revision_token, 'revision-1');
      Object.assign(status, { status: 'disproved', review: { approval: { id: 'approval-1', outcome: 'disproved', created_at: '2026-01-01' } } });
      return reviewResponse({ formalization: { validity_status: 'disproved' } });
    }
  } });
  await h.loadVisibleTheorems();
  h.openTargetPopover();
  assert.equal(h.hasViewInLeaUiButton(), true);
  assert.equal(h.hasButtonText('Re-formalize'), true);
  reviewButton(h, 'Approve result').click();
  await flushPromises();
  const dialog = h.document.querySelector('.ol-lean-review-dialog');
  assert.equal(dialog.attributes.role, 'dialog');
  assert.equal(dialog.attributes['aria-modal'], 'true');
  assert.ok(dialog.textContent.includes(fixture.artifacts[0].content));
  assert.equal(reviewButton(h, 'Choose an outcome').disabled, true);
  const radios = dialog.querySelectorAll('input');
  assert.equal(radios.length, 2);
  assert.ok(radios.every(radio => !radio.checked));
  radios.find(radio => radio.value === 'disproved').dispatchEvent({ type: 'change' });
  reviewButton(h, 'Approve as counterexample').click();
  await flushPromises();
  assert.equal(h.document.querySelector('.ol-lean-review-dialog'), null);
  assert.equal(h.hasButtonText('Withdraw approval'), true);
  assert.match(h.bodyText(), /Approved by user/);
  assert.ok(!h.fetchCalls.some(call => call.url.endsWith('/formalize')));
  assert.equal(h.storageSetCalls.length, 0, 'approval does not set personal audit notes');
  reviewButton(h, 'Withdraw approval').click();
  await flushPromises();
  const decisions = h.fetchCalls.filter(call => call.url.endsWith('/review/decision'));
  assert.equal(JSON.parse(decisions[1].options.body).decision.action, 'withdraw');
});

test('definition approval has a fixed outcome and cancel or Escape restores trigger focus', async () => {
  for (const escape of [false, true]) {
    const h = createContentHarness({ status: 'needs_review', review: { eligible: true } }, { targetKind: 'definition' }, {
      fetchOverride(url) { if (url.endsWith('/review/context')) return reviewResponse(reviewFixture(['defined'])); }
    });
    await h.loadVisibleTheorems();
    h.openTargetPopover();
    const trigger = reviewButton(h, 'Approve result');
    trigger.click();
    await flushPromises();
    const dialog = h.document.querySelector('.ol-lean-review-dialog');
    assert.equal(dialog.querySelectorAll('input').length, 0);
    assert.equal(reviewButton(h, 'Approve as defined').disabled, false);
    if (escape) dialog.dispatchEvent({ type: 'cancel' });
    else reviewButton(h, 'Cancel').click();
    assert.equal(h.document.querySelector('.ol-lean-review-dialog'), null);
    assert.equal(trigger.focused, true);
    assert.ok(!h.fetchCalls.some(call => /\/(formalize|review\/decision)$/.test(call.url)));
  }
});

test('approval offline retry retains outcome and stale revision reload requires fresh selection', async () => {
  let submissions = 0;
  let loads = 0;
  const h = createContentHarness({ status: 'needs_review', review: { eligible: true } }, {}, {
    fetchOverride(url) {
      if (url.endsWith('/review/context')) {
        const fixture = reviewFixture();
        fixture.review.revision_token = `revision-${++loads}`;
        return reviewResponse(fixture);
      }
      if (url.endsWith('/review/decision')) {
        return reviewResponse({ ok: false, message: ++submissions === 1 ? 'Offline. Retry.' : 'Inputs changed.' }, submissions === 1 ? 503 : 409);
      }
    }
  });
  await h.loadVisibleTheorems();
  h.openTargetPopover();
  reviewButton(h, 'Approve result').click();
  await flushPromises();
  h.document.querySelector('.ol-lean-review-dialog').querySelectorAll('input')[0].dispatchEvent({ type: 'change' });
  reviewButton(h, 'Approve as formalized').click();
  await flushPromises();
  assert.match(h.bodyText(), /Offline/);
  assert.equal(reviewButton(h, 'Approve as formalized').disabled, false);
  reviewButton(h, 'Approve as formalized').click();
  await flushPromises();
  assert.equal(loads, 2);
  assert.equal(reviewButton(h, 'Choose an outcome').disabled, true);
  const calls = h.fetchCalls.filter(call => call.url.endsWith('/review/decision'));
  assert.equal(JSON.parse(calls[0].options.body).decision.idempotency_key, JSON.parse(calls[1].options.body).decision.idempotency_key);
  assert.ok(!h.fetchCalls.some(call => call.url.endsWith('/formalize')));
});

test('pane approval opens the shared dialog and Escape leaves the pane open', async () => {
  const h = createContentHarness({ status: 'needs_review', review: { eligible: true } }, {}, {
    locationPath: '/project/unknown',
    manifest: { ok: true, rootFile: 'main.tex', diagnostics: [], items: [{ id: 'review-item', label: 'demo_theorem',
      kind: 'theorem', status: 'needs-review', review: { eligible: true }, formalizable: true,
      leanKind: 'theorem', naturalLanguageLatex: 'A theorem.', sourceFile: 'main.tex' }] },
    fetchOverride(url) { if (url.endsWith('/review/context')) return reviewResponse(reviewFixture()); }
  });
  await h.loadVisibleTheorems();
  h.clickPaneTrigger();
  await flushPromises();
  h.clickPaneTreeRowText('main.tex');
  const trigger = reviewButton(h, 'Approve result');
  assert.ok(trigger);
  assert.equal(h.hasButtonText('Re-formalize'), true);
  trigger.click();
  await flushPromises();
  const dialog = h.document.querySelector('.ol-lean-review-dialog');
  assert.ok(dialog);
  dialog.dispatchEvent({ type: 'keydown', key: 'Escape' });
  assert.equal(h.document.querySelector('.ol-lean-review-dialog'), null);
  assert.ok(h.document.querySelector('.ol-lean-project-pane'));
  assert.equal(trigger.focused, true);
  assert.ok(!h.fetchCalls.some(call => call.url.endsWith('/formalize')));
});

test('review loading failure exposes reload and update-required errors without retrying formalization', async () => {
  const h = createContentHarness({ status: 'needs_review', review: { eligible: true } }, {}, {
    fetchOverride(url) { if (url.endsWith('/review/context')) return reviewResponse({}, 404); }
  });
  await h.loadVisibleTheorems();
  h.openTargetPopover();
  reviewButton(h, 'Approve result').click();
  assert.equal(reviewButton(h, 'Choose an outcome').disabled, true);
  await flushPromises();
  assert.match(h.bodyText(), /updated Lea companion and adapter/);
  assert.equal(reviewButton(h, 'Reload result').hidden, false);
  assert.ok(!h.fetchCalls.some(call => call.url.endsWith('/formalize')));
});

test("Lea tag setup remains available when an empty inventory cannot reach the companion", async () => {
  const h = createContentHarness({ status: "unformalized" }, {}, {
    locationPath: "/project/unknown",
    fetchOverride(url) {
      if (String(url).endsWith("/lean-pane/manifest")) {
        return { ok: false, status: 503, json: async () => ({ message: "Companion is offline" }) };
      }
    }
  });
  await h.loadVisibleTheorems({ activeTex: "" });
  h.clickPaneTrigger();
  await flushPromises();
  await flushPromises();
  assert.equal(h.hasButtonText("Add Lea tags"), true);
  const setup = h.document.querySelector(".ol-lean-tags-setup");
  assert.ok(setup);
  const status = setup.querySelector(".ol-lean-tags-setup-status");
  assert.equal(status.attributes["aria-live"], "polite");
  assert.match(h.bodyText(), /Companion is offline/);
});

test("opening an Overleaf project adopts its browser title without downloading or creating source mirrors", async () => {
  const identity = { projectId: "existing", slug: "project-1", overleafProjectId: "project-1",
    projectName: "project-1", nameSource: "generated", namespace: "Lea.Project1", exists: true };
  const h = createContentHarness({ status: "unformalized" }, {}, {
    documentTitle: "Topology Notes — Overleaf", projectIdentity: identity,
    fetchOverride(url, options) {
      if (url.endsWith("/project/identity/default-name")) return { ok: true, status: 200,
        json: async () => ({ identity: { ...identity, projectName: "Topology Notes", nameSource: "overleaf" } }) };
    }
  });
  await h.loadVisibleTheorems({ activeTex: "\\title{Different paper title}\nText" });
  await flushPromises();
  const call = h.fetchCalls.find(c => c.url.endsWith("/project/identity/default-name"));
  assert.ok(call);
  assert.deepEqual(JSON.parse(call.options.body), { overleafProjectId: "project-1", overleafProjectTitle: "Topology Notes" });
  assert.equal(h.fetchCalls.some(c => /download\/zip|mirror-tex|\/formalize$/.test(c.url)), false);
  h.clickPaneTrigger();
  await flushPromises();
  assert.match(h.document.querySelector(".ol-lean-project-pane-titlewrap").textContent, /Topology Notes/);
});

test("opening a previously untouched named project uses only the existing-project endpoint", async () => {
  const h = createContentHarness({ status: "unformalized" }, {}, {
    documentTitle: "Untouched - Overleaf",
    projectIdentity: { exists: false, nameSource: "generated", projectName: "Overleaf Project" },
    fetchOverride(url) {
      if (url.endsWith("/project/identity/default-name")) return { ok: false, status: 404,
        json: async () => ({ error: "project_missing" }) };
    }
  });
  await h.loadVisibleTheorems();
  assert.equal(h.fetchCalls.filter(c => c.options?.method === "PUT").length, 0);
  assert.equal(h.fetchCalls.some(c => /download\/zip|mirror-tex/.test(c.url)), false);
});

for (const [documentTitle, activeTitle, expected, mirrorEnabled] of [
  ["Source Name - Overleaf", "Other Title", "Source Name", true],
  ["Overleaf", "Paper Title", "Paper Title", true],
  ["", "", "", true],
  ["No Mirror – Overleaf", "Other Title", "No Mirror", false],
]) {
  test(`formalization carries the source title separately (${documentTitle || "missing title"}, mirror ${mirrorEnabled})`, async () => {
    const h = createContentHarness({ status: "unformalized" }, {}, {
      documentTitle, companionSettings: { leaTexMirrorEnabled: mirrorEnabled },
      fetchOverride(url) {
        if (url.includes("/download/zip")) {
          // Stored main.tex entry; collectProjectTexFiles overlays the live buffer.
          const zip = Buffer.from("UEsDBBQAAAAAAPW5TV0AAAAAAAAAAAAAAAAIAAAAbWFpbi50ZXhQSwECFAMUAAAAAAD1uU1dAAAAAAAAAAAAAAAACAAAAAAAAAAAAAAAgAEAAAAAbWFpbi50ZXhQSwUGAAAAAAEAAQA2AAAAJgAAAAAA", "base64");
          return { ok: true, arrayBuffer: async () => zip.buffer.slice(zip.byteOffset, zip.byteOffset + zip.byteLength) };
        }
      }
    });
    await h.loadVisibleTheorems({ activeTex: `${activeTitle ? `\\title{${activeTitle}}\n` : ""}\\begin{theorem}\n% lea: formalize label=demo_theorem\nA theorem.\n\\end{theorem}` });
    h.openTargetPopover();
    h.clickButtonText("Formalize");
    await flushPromises();
    const call = h.fetchCalls.find(c => c.url.endsWith("/formalize"));
    assert.ok(call);
    const body = JSON.parse(call.options.body);
    assert.equal(body.overleafProjectTitle, expected);
    assert.notEqual(body.projectName, undefined);
    const mirrors = h.fetchCalls.filter(c => c.url.endsWith("/mirror-tex"));
    if (mirrorEnabled) {
      assert.ok(mirrors.length);
      assert.equal(JSON.parse(mirrors[0].options.body).overleafProjectTitle, expected);
      await h.loadVisibleTheorems({ activeTex: "\\title{Changed paper title}\nEdited source" });
      await h.runScheduledTimers();
      const upsert = h.fetchCalls.find(c => c.url.endsWith("/mirror-tex") && JSON.parse(c.options.body).mode === "upsert");
      assert.ok(upsert, "activated mirroring should send the edited active buffer");
      assert.equal(JSON.parse(upsert.options.body).overleafProjectTitle,
        documentTitle && documentTitle !== "Overleaf" ? expected : "Changed paper title");
    } else assert.equal(mirrors.length, 0);
  });
}

test("Formalize all carries the Overleaf title on every queued item without source mirroring", async () => {
  const items = ["first", "second"].map((label, index) => ({
    id: `theorem:${label}:${index}`, kind: "theorem", label, status: "missing-stub",
    sourceFile: "main.tex", sourceStartLine: index + 1, sourceEndLine: index + 1,
    naturalLanguageLatex: `Theorem ${label}.`, leanKind: "theorem", formalizable: true
  }));
  const h = createContentHarness({ status: "unformalized" }, {}, {
    documentTitle: "Batch Notes - Overleaf", companionSettings: { leaTexMirrorEnabled: false },
    manifest: { ok: true, rootFile: "main.tex", items, diagnostics: [] },
    targetBatch: { ok: true, batchId: "batch", operation: "formalize", done: false, running: true,
      items: items.map(item => ({ targetKind: "theorem", targetLabel: item.label, state: "pending" })) }
  });
  await h.loadVisibleTheorems();
  h.clickPaneTrigger();
  await flushPromises();
  h.clickButtonText("Formalize all (2)");
  await flushPromises();
  const call = h.fetchCalls.find(c => c.url.endsWith("/formalize/all"));
  assert.ok(call);
  const body = JSON.parse(call.options.body);
  assert.equal(body.items.length, 2);
  assert.ok(body.items.every(item => item.overleafProjectTitle === "Batch Notes" && item.projectName === "Test Project"));
  assert.equal(h.fetchCalls.some(c => c.url.endsWith("/mirror-tex")), false);
});

for (const mirrorEnabled of [false,true]) {
  test(`source is browser-published without reverse callbacks (mirror ${mirrorEnabled})`,async()=>{
    let socket;
    class EventSource {
      listeners=new Map();
      constructor(){socket=this;}
      addEventListener(type,callback){this.listeners.set(type,callback);}
      close(){}
    }
    const h=createContentHarness({status:'formalized'},{},{EventSource,companionSettings:{leaTexMirrorEnabled:mirrorEnabled}});
    await h.loadVisibleTheorems();
    assert.equal(socket.listeners.has('source-refresh-requested'),false);
    const publication=h.fetchCalls.find(call=>call.url.endsWith('/source-context/publish'));
    assert.ok(publication,'visible source must be published by the browser');
    assert.equal(JSON.parse(publication.options.body).source_bundle.targetKey,'demo_theorem');
    assert.equal(h.fetchCalls.some(call=>call.url.endsWith('/source-context/respond')),false);
    assert.equal(h.fetchCalls.some(call=>call.url.endsWith('/lean-pane/manifest')),false);
  });
}

test('pane refresh preserves selected source/code text until the range is cleared', async () => {
  const item = editableItem();
  const harness = createContentHarness({ status: 'formalized' }, {}, {
    manifest: { ok: true, rootFile: 'main.tex', items: [item], diagnostics: [] },
  });
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText('main.tex');
  harness.clickFirstPaneItem();
  const selected = harness.document.body.querySelector('.ol-lean-project-item');
  assert.ok(selected);
  const selection = { isCollapsed: false, anchorNode: selected, focusNode: selected };
  harness.document.getSelection = () => selection;
  harness.clickButtonLabel('Refresh Lean pane');
  await flushPromises();
  assert.equal(harness.document.body.querySelector('.ol-lean-project-item'), selected);
  selection.isCollapsed = true;
  harness.document.dispatchEvent({ type: 'selectionchange' });
  await flushPromises();
  assert.notEqual(harness.document.body.querySelector('.ol-lean-project-item'), selected);
});

function createContentHarness(statusInfo, theoremPatch = {}, options = {}) {
  const document = new FakeDocument();
  document.title = options.documentTitle || "";
  const timers = [];
  const fetchCalls = [];
  const runtimeMessages = [];
  const postedMessages = [];
  const promptCalls = [];
  const confirmCalls = [];
  const storageSetCalls = [];
  const storageState = { ...(options.storage || {}) };
  const localStorageState = { ...(options.localStorage || {}) };
  const storageChangeListeners = [];
  let currentProjectIdentity = options.projectIdentity || {
    projectId: "adapter-project-1",
    overleafProjectId: "project-1",
    slug: "project-1",
    projectName: "Test Project",
    namespace: "Lea.TestProject",
    exists: true,
    hasRecordedProofs: true
  };
  let nextTimerId = 1;

  const window = {
    innerWidth: 1024,
    innerHeight: 768,
    location: { pathname: options.locationPath || "/project/project-1" },
    prompt(text, value) {
      promptCalls.push({ text: String(text), value: String(value || "") });
      return options.promptResponse ?? value ?? null;
    },
    confirm(text) {
      confirmCalls.push(String(text));
      return options.confirmResponse !== false;
    },
    _listeners: new Map(),
    addEventListener(type, listener) {
      const listeners = this._listeners.get(type) || [];
      listeners.push(listener);
      this._listeners.set(type, listeners);
    },
    removeEventListener(type, listener) {
      removeListener(this._listeners, type, listener);
    },
    dispatchEvent(event) {
      const eventObject = normalizeFakeEvent(event, this);
      for (const listener of this._listeners.get(eventObject.type) || []) {
        listener(eventObject);
      }
      return !eventObject.defaultPrevented;
    },
    postMessage(data) {
      postedMessages.push(data);
      for (const listener of this._listeners.get("message") || []) {
        listener({ source: window, data });
      }
    },
    open() {
      return {};
    }
  };
  document.defaultView = window;

  const target = {
    targetKind: "theorem",
    targetLabel: "demo_theorem",
    targetText: "A theorem.",
    targetUses: [],
    targetContext: "",
    coords: { left: 12, top: 18 },
    ...theoremPatch
  };

  const context = {
    AbortController,
    EventSource: options.EventSource,
    URL,
    URLSearchParams,
    TextEncoder,
    katex: options.katex,
    clearTimeout(id) {
      const index = timers.findIndex((timer) => timer.id === id);
      if (index !== -1) timers.splice(index, 1);
    },
    crypto: {
      randomUUID: () => "test-source-client",
      subtle: {
        async digest(_algorithm, input) {
          const digest = createHash("sha256").update(Buffer.from(input)).digest();
          return digest.buffer.slice(digest.byteOffset, digest.byteOffset + digest.byteLength);
        }
      }
    },
    document,
    fetch: async (url, fetchOptions) => {
      fetchCalls.push({ url: String(url), options: fetchOptions });
      const override = await options.fetchOverride?.(String(url), fetchOptions, fetchCalls);
      if (override !== undefined) return override;
      if (options.hangProjectIdentity && String(url).includes("/project/identity?")) {
        return new Promise((_resolve, reject) => {
          const rejectOnAbort = () => reject(new Error("Project identity request aborted."));
          if (fetchOptions?.signal?.aborted) rejectOnAbort();
          else fetchOptions?.signal?.addEventListener("abort", rejectOnAbort, { once: true });
        });
      }
      if (options.hangProjectArchive && String(url).includes("/download/zip")) {
        return new Promise((_resolve, reject) => {
          const rejectOnAbort = () => reject(new Error("Project archive request aborted."));
          if (fetchOptions?.signal?.aborted) rejectOnAbort();
          else fetchOptions?.signal?.addEventListener("abort", rejectOnAbort, { once: true });
        });
      }
      if (options.failProjectArchive && String(url).includes("/download/zip")) {
        return {
          ok: false,
          status: 503,
          async json() { return {}; },
          async arrayBuffer() { return new ArrayBuffer(0); }
        };
      }
      const failingRepairStart = Boolean(options.failRepairStart) && String(url).includes("/lean-pane/repair/start");
      const formalizeRequest = String(url).endsWith("/formalize");
      const formalizeFailure = formalizeRequest
        ? typeof options.formalizeFailure === "function"
          ? options.formalizeFailure(fetchCalls)
          : options.formalizeFailure || (options.formalizeError
            ? { status: 400, error: "unresolved_uses", message: options.formalizeError }
            : null)
        : null;
      const failingFormalize = Boolean(formalizeFailure);
      const failingReportList = Boolean(options.reportListError)
        && String(url).includes("/formalize/all/reports?");
      return {
        ok: !failingRepairStart && !failingFormalize && !failingReportList,
        status: failingRepairStart ? 400 : failingFormalize ? formalizeFailure.status || 400 : failingReportList ? 503 : 200,
        async json() {
          if (failingReportList) return { message: options.reportListError };
          if (String(url).includes("/lea-status/updates")) return options.leaStatusHistory || { updates: [], has_more: false };
          if (String(url).endsWith("/targets/resolve")) return options.resolveTargets || { targets: [] };
          if (failingRepairStart) {
            return { ok: false, error: "repair_start_failed", message: options.failRepairStart };
          }
          if (failingFormalize) {
            return { ok: false, ...formalizeFailure };
          }
          if (String(url).includes("/project/identity/preview")) {
            const request = JSON.parse(fetchOptions?.body || "{}");
            const preview = typeof options.projectPreview === "function"
              ? options.projectPreview(request, fetchCalls)
              : options.projectPreview;
            return preview || {
              ok: true,
              project_name: request.projectName,
              namespace: request.namespace || `Lea.${String(request.projectName || "Project").replace(/[^A-Za-z0-9]+/g, "")}`,
              available: true,
              suggestions: []
            };
          }
          if (String(url).endsWith("/project/identity") && fetchOptions?.method === "PUT") {
            const request = JSON.parse(fetchOptions?.body || "{}");
            const update = typeof options.projectIdentityUpdate === "function"
              ? options.projectIdentityUpdate(request, fetchCalls)
              : options.projectIdentityUpdate;
            const response = update || {
              ok: true,
              identity: {
                ...currentProjectIdentity,
                projectName: request.projectName,
                namespace: request.mode === "rename-namespace" ? request.namespace : currentProjectIdentity.namespace
              }
            };
            if (response?.identity) currentProjectIdentity = response.identity;
            return response;
          }
          if (String(url).includes("/project/identity?")) {
            return {
              ok: true,
              identity: currentProjectIdentity
            };
          }
          if(String(url).endsWith('/source-context/publisher'))return {generation:1,sequence:0,lease_seconds:90};
          if(String(url).endsWith('/source-context/publish'))return {source_context_ref:'published-source',generation:1,sequence:1};
          if (String(url).endsWith("/settings")) {
            return {...storageState,...options.companionSettings};
          }
          if (String(url).endsWith("/settings/lea") && fetchOptions?.method === "POST") {
            return { ...options.companionSettings, ...JSON.parse(fetchOptions.body || "{}") };
          }
          if (String(url).endsWith("/settings/github-token")) {
            return options.githubTokenUpdate || { ok: true };
          }
          if (String(url).includes("/share/github?")) {
            return typeof options.shareStatus === "function"
              ? options.shareStatus(fetchCalls)
              : options.shareStatus || { ok: true, exists: true, remoteUrl: null, tokenConfigured: true };
          }
          if (String(url).includes("/project/github-import/preview")) {
            return options.githubImportPreview || {
              preview_id: "preview-1",
              plan: { counts: {}, files: [], reusable_declarations: 0, blocking_error: null }
            };
          }
          if (String(url).includes("/project/github-import/confirm")) {
            return options.githubImportConfirm || {
              id: "import-1",
              status: "complete",
              counts: { dispositions: {}, matched_declarations: 0, reusable_declarations: 0, checks: {} }
            };
          }
          if (String(url).includes("/project/github-import/status")) {
            return typeof options.githubImportStatus === "function"
              ? options.githubImportStatus(fetchCalls)
              : options.githubImportStatus || {
                id: "import-1",
                status: "complete",
                counts: { dispositions: {}, matched_declarations: 0, reusable_declarations: 0, checks: {} }
              };
          }
          if (String(url).includes("/lean-pane/manifest")) {
            return typeof options.manifest === "function"
              ? options.manifest(fetchCalls)
              : options.manifest || { ok: true, rootFile: "main.tex", items: [], diagnostics: [] };
          }
          if (String(url).includes("/formalize/all/reports/") && String(url).endsWith("/retry")) {
            return options.batchReportRetry || options.batchReportDetail || {};
          }
          if (String(url).includes("/formalize/all/reports/")) {
            return options.batchReportDetail || {};
          }
          if (String(url).includes("/formalize/all/reports")) {
            return typeof options.batchReports === "function"
              ? options.batchReports(fetchCalls) : options.batchReports || { reports: [] };
          }
          if (String(url).includes("/formalize/all") || String(url).includes("/stub/all")) {
            return options.targetBatch || {
              ok: true,
              batchId: "target-batch-1",
              operation: String(url).includes("/stub/all") ? "stub" : "formalize",
              done: false,
              pausedOn: null,
              items: []
            };
          }
          if (String(url).includes("/formalize")) {
            return { jobId: "job-1", status: "in_progress" };
          }
          if (String(url).includes("/lean-pane/edit/start")) {
            return typeof options.editStart === "function"
              ? options.editStart(fetchCalls)
              : (options.editStart || {
                  ok: true,
                  leaSessionId: "sess-1",
                  path: "main_theorem.lean",
                  content: "theorem main_theorem : True := by\n  sorry\n",
                  dependents: []
                });
          }
          if (String(url).includes("/lean-pane/edit/save")) {
            return typeof options.editSave === "function"
              ? options.editSave(fetchCalls)
              : (options.editSave || {
                  ok: true,
                  unchanged: false,
                  ownResult: { checkStatus: "ok", classification: { kind: "proof-only" } },
                  dependentsImpact: []
                });
          }
          if (String(url).includes("/lean-pane/repair/start")) {
            return options.repairStart || { status: "in_progress", jobId: "repair-job-1" };
          }
          if (String(url).includes("/lean-pane/repair/all/cancel")) {
            return options.batchCancel || options.repairAll || { ok: true, batchId: "batch-1", done: true, canceled: true, pausedOn: null, items: [] };
          }
          if (String(url).includes("/lean-pane/repair/all/continue")) {
            return options.repairContinue || options.repairAll || { ok: true, batchId: "batch-1", done: false, pausedOn: null, items: [] };
          }
          if (String(url).includes("/lean-pane/repair/all")) {
            return options.repairAll || { ok: true, batchId: "batch-1", done: false, pausedOn: null, items: [] };
          }
          if (String(url).includes("/lean-pane/repair/status")) {
            return options.repairStatus || options.repairAll || { ok: true, batchId: "batch-1", done: true, pausedOn: null, items: [] };
          }
          return { statuses: { [`${target.targetKind}:${target.targetLabel}`]: statusInfo } };
        }
      };
    },
    globalThis: null,
    setTimeout(callback, delay) {
      const id = nextTimerId++;
      timers.push({ id, callback, delay });
      return id;
    },
    window,
    chrome: {
      runtime: {
        id: "test-extension",
        // Real .mjs web-accessible resources resolve to their on-disk path so the
        // pane's lazy `import(chrome.runtime.getURL(...))` works under the harness.
        getURL: (file) => file.endsWith(".mjs")
          ? path.join(repoRoot, "apps/overleaf-extension/extension", file)
          : `chrome-extension://test/${file}`,
        sendMessage(message, callback) {
          runtimeMessages.push(message);
          if (options.runtimeSendMessage) return options.runtimeSendMessage(message, callback);
          callback?.({ ok: true });
        },
        lastError: null
      },
      storage: {
        sync: {
          async get(defaults) {
            return { ...defaults, ...storageState };
          },
          async set(values) {
            storageSetCalls.push({ ...values });
            Object.assign(storageState, values);
          }
        },
        local: {
          async get(defaults) {
            if (options.hangHumanApprovals) return new Promise(() => {});
            return { ...defaults, ...localStorageState };
          },
          async set(values) {
            storageSetCalls.push({ ...values });
            for (const [key, value] of Object.entries(values)) {
              const oldValue = localStorageState[key];
              localStorageState[key] = value;
              const change = { [key]: { oldValue, newValue: value } };
              for (const listener of storageChangeListeners) listener(change, "local");
            }
          }
        },
        onChanged: {
          addListener(listener) {
            storageChangeListeners.push(listener);
          }
        }
      }
    }
  };
  context.globalThis = context;
  context.self = window;
  context.location = window.location;

  vm.runInNewContext(modelPickerScript, context, { filename: modelPickerScriptPath });
  vm.runInNewContext(contentScript, context, {
    filename: contentScriptPath,
    // content.js loads web-accessible-resource modules via
    // `import(chrome.runtime.getURL(...))`; getURL returns an absolute path for .mjs
    // resources and the default loader imports them, so the pane's lazy module load
    // works under the test harness without --experimental-vm-modules.
    importModuleDynamically: vm.constants.USE_MAIN_CONTEXT_DEFAULT_LOADER
  });
  timers.splice(0, timers.length);

  return {
    target,
    window,
    document,
    runtimeMessages,
    pendingTimers() { return timers.map(({ delay }) => delay); },
    async setVisibility(hidden) {
      document.hidden = hidden;
      document.dispatchEvent({ type: "visibilitychange" });
      await flushPromises();
    },
    async loadStatusForVisibleTheorem() {
      await this.loadVisibleTheorems();
    },
    async loadVisibleTheorems({ diagnostics = [], activeTex } = {}) {
      window.postMessage({
        type: "OL_LEAN_TARGETS_VISIBLE",
        targets: [target],
        diagnostics,
        // A caller-supplied activeTex marks the source as changed, which is
        // what schedules a Lean-pane manifest refresh (content.js only
        // refreshes the pane when the tex actually changed).
        activeTex: activeTex || "\\begin{theorem}\n% lea: formalize label=demo_theorem\nA theorem.\n\\end{theorem}",
        activePath: "main.tex"
      }, "*");
      assert.ok(timers.length > 0, "expected status refresh to be scheduled");
      const scheduledTimers = timers.splice(0, timers.length);
      for (const timer of scheduledTimers) {
        timer.callback();
      }
      await flushPromises();
    },
    hasViewInLeaUiButton() {
      return document.body
        .querySelectorAll("button")
        .some((button) => button.dataset.role === "open-lea-session" && button.textContent === "View in Lea UI");
    },
    hasButtonText(text) {
      return document.body
        .querySelectorAll("button")
        .some((button) => button.textContent === text);
    },
    buttonHelpText(text) {
      return document.body.querySelectorAll("button")
        .find((button) => button.textContent === text)?.title || "";
    },
    hasButtonLabel(label) {
      return document.body
        .querySelectorAll("button")
        .some((button) => button.attributes["aria-label"] === label);
    },
    clickButtonLabel(label) {
      const button = document.body
        .querySelectorAll("button")
        .find((candidate) => candidate.attributes["aria-label"] === label);
      assert.ok(button, `expected a button labeled "${label}"`);
      button.click();
    },
    openTargetPopover() {
      window.postMessage({
        type: "OL_LEAN_TARGET_CLICK",
        target,
        clientX: 16,
        clientY: 20
      }, "*");
    },
    clickPaneTrigger() {
      const button = document.body.querySelector(".ol-lean-pane-trigger");
      assert.ok(button, "expected Lean pane trigger");
      button.click();
    },
    clickFirstPaneItem() {
      const button = document.body.querySelector(".ol-lean-project-item-header");
      assert.ok(button, "expected Lean pane item header");
      button.click();
    },
    clickPaneItemHeaderText(text) {
      const button = document.body
        .querySelectorAll(".ol-lean-project-item-header")
        .find((candidate) => candidate.textContent.includes(text));
      assert.ok(button, `expected a pane item header containing "${text}"`);
      button.click();
    },
    clickFirstPaneTreeRow(kind = "") {
      const selector = kind ? `.ol-lean-project-tree-row-${kind}` : ".ol-lean-project-tree-row";
      const button = document.body.querySelector(selector);
      assert.ok(button, `expected Lean pane tree row ${kind || ""}`.trim());
      button.click();
    },
    clickPaneTreeRowText(text) {
      const button = document.body
        .querySelectorAll(".ol-lean-project-tree-row")
        .find((candidate) => candidate.textContent.includes(text));
      assert.ok(button, `expected a tree row containing "${text}"`);
      button.click();
    },
    clickButtonText(text) {
      const button = document.body
        .querySelectorAll("button")
        .find((candidate) => candidate.textContent === text);
      assert.ok(button, `expected a button labeled "${text}"`);
      button.click();
    },
    clickButtonContaining(text) {
      const button = document.body.querySelectorAll("button")
        .find((candidate) => candidate.textContent.includes(text));
      assert.ok(button, `expected a button containing "${text}"`);
      button.click();
    },
    paneText() {
      return document.body.querySelector(".ol-lean-project-pane-body")?.textContent || "";
    },
    clickButtonRole(role) {
      const button = document.body.querySelector(`[data-role='${role}']`);
      assert.ok(button, `expected a button with role "${role}"`);
      button.click();
    },
    clickOutsideGithubImportNotice() {
      document.dispatchEvent({ type: "click", target: document.body });
    },
    editTextarea() {
      return document.body.querySelector(".ol-lean-project-edit-textarea");
    },
    setProjectIdentityName(value) {
      const input = document.body.querySelector(".ol-lean-project-identity-input");
      assert.ok(input, "expected project identity input");
      input.value = value;
      input.dispatchEvent({ type: "input" });
    },
    setGithubImportUrl(value) {
      const input = document.body.querySelector("[data-role='url']");
      assert.ok(input, `expected GitHub import URL input; body was: ${document.body.textContent}`);
      input.value = value;
    },
    setGithubTokenValue(value) {
      const input = document.body.querySelector("[data-role='github-token-input']");
      assert.ok(input, "expected GitHub token input");
      input.value = value;
    },
    submitGithubToken() {
      const form = document.body.querySelector("[data-role='github-token-form']");
      assert.ok(form, "expected GitHub token form");
      form.dispatchEvent({ type: "submit" });
    },
    setProjectIdentitySync(checked) {
      const input = document.body.querySelector(".ol-lean-project-identity-sync-input");
      assert.ok(input, "expected project identity sync input");
      input.checked = Boolean(checked);
      input.dispatchEvent({ type: "change" });
    },
    projectIdentityDialog() {
      const dialog = document.body.querySelector(".ol-lean-project-identity-dialog");
      return dialog ? {
        role: dialog.attributes.role,
        modal: dialog.attributes["aria-modal"],
        label: dialog.attributes["aria-labelledby"]
      } : null;
    },
    githubPushDialog() {
      const dialog = document.body.querySelector(".ol-lean-github-push-dialog");
      return dialog ? {
        role: dialog.attributes.role,
        modal: dialog.attributes["aria-modal"],
        label: dialog.attributes["aria-labelledby"]
      } : null;
    },
    projectIdentityNamespace() {
      return document.body.querySelector(".ol-lean-project-identity-namespace-value")?.textContent || "";
    },
    setEditTextareaValue(value) {
      const textarea = this.editTextarea();
      assert.ok(textarea, "expected the edit textarea to be present");
      textarea.value = value;
    },
    settingsPopover() {
      return document.body.querySelector(".ol-lean-settings-popover");
    },
    settingsPopoverResizer() {
      return document.body.querySelector(".ol-lean-settings-popover-resizer");
    },
    settingsPopoverWidthStyle() {
      return this.settingsPopover()?.style["--ol-lean-settings-width"] || "";
    },
    settingsPopoverResizerValues() {
      const resizer = this.settingsPopoverResizer();
      return resizer ? {
        orientation: resizer.attributes["aria-orientation"],
        min: resizer.attributes["aria-valuemin"],
        max: resizer.attributes["aria-valuemax"],
        now: resizer.attributes["aria-valuenow"]
      } : null;
    },
    settingsPopoverAnchorStyle() {
      const popover = this.settingsPopover();
      return popover ? {
        right: popover.style.right || "",
        left: popover.style.left || "",
        top: popover.style.top || ""
      } : null;
    },
    startSettingsPopoverResize(startX) {
      const resizer = this.settingsPopoverResizer();
      assert.ok(resizer, "expected settings popover resizer");
      resizer.dispatchEvent({
        type: "mousedown",
        button: 0,
        clientX: startX,
        preventDefault() {},
        stopPropagation() {}
      });
    },
    moveSettingsPopoverResize(clientX) {
      document.dispatchEvent({ type: "mousemove", clientX });
    },
    finishSettingsPopoverResize(clientX) {
      document.dispatchEvent({ type: "mouseup", clientX });
    },
    dragSettingsPopoverResizer({ startX, moves }) {
      this.startSettingsPopoverResize(startX);
      for (const clientX of moves) this.moveSettingsPopoverResize(clientX);
      this.finishSettingsPopoverResize(moves[moves.length - 1] ?? startX);
    },
    keySettingsPopoverResizer(key, patch = {}) {
      const resizer = this.settingsPopoverResizer();
      assert.ok(resizer, "expected settings popover resizer");
      resizer.dispatchEvent({ type: "keydown", key, ...patch });
    },
    bodyHasClass(className) {
      return document.body.classList.contains(className);
    },
    leanPane() {
      return document.body.querySelector(".ol-lean-project-pane");
    },
    leanPaneResizer() {
      return document.body.querySelector(".ol-lean-project-pane-resizer");
    },
    leanPaneWidthStyle() {
      return this.leanPane()?.style["--ol-lean-pane-width"] || "";
    },
    dragLeanPaneResizer({ startX, moves }) {
      const resizer = this.leanPaneResizer();
      assert.ok(resizer, "expected Lean pane resizer");
      resizer.dispatchEvent({
        type: "mousedown",
        button: 0,
        clientX: startX,
        preventDefault() {},
        stopPropagation() {}
      });
      for (const clientX of moves) {
        document.dispatchEvent({ type: "mousemove", clientX });
      }
      document.dispatchEvent({ type: "mouseup", clientX: moves[moves.length - 1] ?? startX });
    },
    keyLeanPaneResizer(key, patch = {}) {
      const resizer = this.leanPaneResizer();
      assert.ok(resizer, "expected Lean pane resizer");
      resizer.dispatchEvent({ type: "keydown", key, ...patch });
    },
    async runScheduledTimers() {
      const scheduledTimers = timers.splice(0, timers.length);
      for (const timer of scheduledTimers) {
        timer.callback();
      }
      await flushPromises();
    },
    async runTimersAt(delay) {
      const ready = timers.filter((timer) => timer.delay === delay);
      for (const timer of ready) timers.splice(timers.indexOf(timer), 1);
      for (const timer of ready) timer.callback();
      await flushPromises();
    },
    fetchCalls,
    promptCalls,
    confirmCalls,
    storageSetCalls,
    storageState,
    localStorageState,
    postedMessages,
    lastStorageSet() {
      return storageSetCalls[storageSetCalls.length - 1] || null;
    },
    bodyText() {
      return document.body.textContent;
    },
    countSelector(selector) {
      return document.body.querySelectorAll(selector).length;
    },
    githubTokenState() {
      const card = document.body.querySelector(".ol-lean-github-token-card");
      const status = document.body.querySelector("[data-role='github-token-status']");
      const description = document.body.querySelector("[data-role='github-token-description']");
      const toggle = document.body.querySelector("[data-role='github-token-toggle']");
      const clear = document.body.querySelector("[data-role='github-token-clear']");
      const summary = document.body.querySelector("[data-role='github-token-summary-actions']");
      const editor = document.body.querySelector("[data-role='github-token-editor']");
      const input = document.body.querySelector("[data-role='github-token-input']");
      const visibility = document.body.querySelector("[data-role='github-token-visibility']");
      return {
        configured: card?.dataset.configured || "",
        status: status?.textContent || "",
        description: description?.textContent || "",
        toggle: toggle?.textContent || "",
        clearHidden: Boolean(clear?.hidden),
        summaryHidden: Boolean(summary?.hidden),
        editorHidden: Boolean(editor?.hidden),
        inputType: input?.type || "",
        inputValue: input?.value || "",
        visibility: visibility?.textContent || ""
      };
    },
    githubImportQueue() {
      const list = document.body.querySelector(".ol-lean-github-import-queue");
      if (!list) return [];
      return list.children.map((row) => ({
        label: row.children[1]?.textContent || "",
        state: row.children[2]?.textContent || ""
      }));
    },
    githubImportNoticeState() {
      const notice = document.body.querySelector(".ol-lean-github-import-notice");
      if (!notice) return null;
      return {
        expanded: notice.querySelector("[data-role='toggle']")?.attributes["aria-expanded"] || "",
        detailsHidden: Boolean(notice.querySelector("[data-role='details']")?.hidden),
        minimizeHidden: Boolean(notice.querySelector("[data-role='dismiss']")?.hidden),
      };
    },
    paneActionError() {
      const alert = document.body.querySelector(".ol-lean-project-action-error");
      return alert ? {
        role: alert.attributes.role,
        live: alert.attributes["aria-live"],
        text: alert.textContent
      } : null;
    },
    popoverActionError() {
      const alert = document.body.querySelector(".ol-lean-popover-status-error");
      return alert ? {
        role: alert.attributes.role,
        live: alert.attributes["aria-live"],
        text: alert.textContent
      } : null;
    },
    paneTreeRowTexts() {
      return document.body
        .querySelectorAll(".ol-lean-project-tree-row")
        .map((row) => row.textContent);
    },
    relationshipChips() {
      return document.body
        .querySelectorAll(".ol-lean-project-relationship-chip")
        .map((chip) => ({
          text: chip.textContent,
          direction: chip.dataset.relationshipDirection,
          targetLabel: chip.dataset.targetLabel,
          ariaLabel: chip.attributes["aria-label"],
          ariaDisabled: chip.attributes["aria-disabled"],
          navigable: chip.classList.contains("is-navigable"),
          unavailable: chip.classList.contains("is-unavailable"),
          className: chip.className,
          title: chip.title
        }));
    },
    clickRelationshipChip(text, direction = "uses") {
      const chip = document.body
        .querySelectorAll(".ol-lean-project-relationship-chip")
        .find((candidate) => (
          candidate.textContent === text
          && candidate.dataset.relationshipDirection === direction
          && candidate.classList.contains("is-navigable")
        ));
      assert.ok(chip, `expected navigable ${direction} relationship chip "${text}"`);
      chip.click();
    },
    paneProgresses() {
      return document.body
        .querySelectorAll(".ol-lean-project-progress")
        .map((progress) => ({
          role: progress.attributes.role,
          label: progress.attributes["aria-label"],
          inProgress: progress.classList.contains("ol-lean-project-progress-in-progress"),
          segments: progress
            .querySelectorAll(".ol-lean-project-progress-segment")
            .map((segment) => ({
              bucket: segment.dataset.bucket,
              count: segment.dataset.count,
              percent: segment.dataset.percent,
              width: segment.style.width,
              title: segment.title
            }))
        }));
    },
    batchQueue() {
      const queue = document.body.querySelector(".ol-lean-batch-queue");
      if (!queue) return null;
      const progress = queue.querySelector(".ol-lean-batch-queue-progress");
      return {
        text: queue.textContent,
        progress: progress ? {
          role: progress.attributes.role,
          label: progress.attributes["aria-label"],
          min: progress.attributes["aria-valuemin"],
          max: progress.attributes["aria-valuemax"],
          now: progress.attributes["aria-valuenow"]
        } : null,
        progressSegments: progress
          ? progress.querySelectorAll(".ol-lean-batch-queue-progress-segment")
            .map((segment) => segment.className.replace("ol-lean-batch-queue-progress-segment ol-lean-batch-queue-progress-", ""))
          : [],
        pendingCount: queue.querySelectorAll(".ol-lean-batch-queue-item-pending").length,
        completedCount: queue.querySelectorAll(".ol-lean-batch-queue-item-completed").length,
        attentionCount: queue.querySelectorAll(".ol-lean-batch-queue-attention").length,
        attentionItemCount: queue.querySelector(".ol-lean-batch-queue-attention")
          ?.querySelectorAll(".ol-lean-batch-queue-item").length || 0,
        attentionExpanded: queue.querySelector(".ol-lean-batch-queue-attention")
          ?.querySelector(".ol-lean-batch-queue-disclosure")
          ?.attributes["aria-expanded"] || null
      };
    },
    firstFocusedPaneItemScrolled() {
      const item = document.body.querySelector(".ol-lean-project-item-focus");
      return Boolean(item?.scrollIntoViewOptions);
    },
    focusedPaneItemId() {
      return document.body.querySelector(".ol-lean-project-item-focus")?.dataset.itemId || "";
    }
  };
}

async function flushPromises() {
  for (let i = 0; i < 8; i += 1) {
    await Promise.resolve();
  }
  for (let turn=0;turn<8;turn++) await new Promise(resolve=>setTimeout(resolve,0));
  for (let i = 0; i < 12; i += 1) {
    await Promise.resolve();
  }
}

class FakeDocument {
  constructor() {
    this.body = new FakeElement("body");
    this.documentElement = new FakeElement("html");
    this.head = new FakeElement("head");
    for (const element of [this.body, this.documentElement, this.head]) element.ownerDocument = this;
    this.activeElement = this.body;
    this._listeners = new Map();
  }

  addEventListener(type, listener) {
    const listeners = this._listeners.get(type) || [];
    listeners.push(listener);
    this._listeners.set(type, listeners);
  }

  removeEventListener(type, listener) {
    removeListener(this._listeners, type, listener);
  }

  dispatchEvent(event) {
    const eventObject = normalizeFakeEvent(event, this);
    for (const listener of this._listeners.get(eventObject.type) || []) {
      listener(eventObject);
    }
    return !eventObject.defaultPrevented;
  }

  createElement(tagName) {
    const element = new FakeElement(tagName);
    element.ownerDocument = this;
    return element;
  }

  querySelector(selector) { return this.body.querySelector(selector); }

  querySelectorAll(selector) { return this.body.querySelectorAll(selector); }

  createElementNS(_namespace, tagName) {
    return this.createElement(tagName);
  }

  createTextNode(text) {
    return new FakeText(text);
  }
}

class FakeText {
  constructor(text) {
    this.nodeType = 3;
    this.textContent = text;
    this.parentNode = null;
  }
}

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = {
      setProperty: (name, value) => {
        this.style[name] = String(value);
      }
    };
    this.attributes = {};
    this._listeners = new Map();
    this.hidden = false;
    this._textContent = "";
    this._className = "";
    this.classList = {
      contains: (className) => this.className.split(/\s+/).includes(className),
      add: (...classNames) => {
        const next = new Set(this.className.split(/\s+/).filter(Boolean));
        for (const className of classNames) next.add(className);
        this.className = [...next].join(" ");
      },
      remove: (...classNames) => {
        const remove = new Set(classNames);
        this.className = this.className
          .split(/\s+/)
          .filter((className) => className && !remove.has(className))
          .join(" ");
      },
      toggle: (className, force) => {
        const present = this.classList.contains(className);
        const shouldAdd = force === undefined ? !present : Boolean(force);
        if (shouldAdd) this.classList.add(className);
        else this.classList.remove(className);
        return shouldAdd;
      }
    };
    this.scrollTop = 0;
    this.scrollIntoViewOptions = null;
  }

  get ownerDocument() { return this._ownerDocument || this.parentNode?.ownerDocument; }

  set ownerDocument(value) { this._ownerDocument = value; }

  focus() {
    this.focused = true;
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
  }

  blur() {}

  scrollIntoView(options) {
    this.scrollIntoViewOptions = options || true;
  }

  get className() {
    return this._className;
  }

  set className(value) {
    this._className = String(value || "");
  }

  get isConnected() {
    return Boolean(this.parentNode);
  }

  get textContent() {
    return this._textContent + this.children.map((child) => child.textContent || "").join("");
  }

  set textContent(value) {
    this._textContent = String(value || "");
    this.children = [];
  }

  set innerHTML(value) {
    this._textContent = "";
    this.children = [];
    const html = String(value || "");
    if (html.includes('data-role="share-remote"')) {
      const remote = this.appendChild(new FakeElement("input"));
      remote.dataset.role = "share-remote";
      const save = this.appendChild(new FakeElement("button"));
      save.dataset.role = "share-save";
      save.textContent = "Save remote";
      const push = this.appendChild(new FakeElement("button"));
      push.dataset.role = "share-push";
      push.textContent = "Push to GitHub";
      const exportButton = this.appendChild(new FakeElement("button"));
      exportButton.dataset.role = "share-export";
      exportButton.textContent = "Download .zip";
      const importButton = this.appendChild(new FakeElement("button"));
      importButton.dataset.role = "github-import";
      importButton.textContent = "Add Lean files from GitHub";
      const hint = this.appendChild(new FakeElement("p"));
      hint.dataset.role = "share-hint";
      hint.hidden = true;
      const status = this.appendChild(new FakeElement("p"));
      status.dataset.role = "share-status";
      status.textContent = "Loading share status...";
      return;
    }
    if (html.includes("ol-lean-github-import-dialog")) {
      const dialog = this.appendChild(new FakeElement("section"));
      dialog.className = "ol-lean-github-import-dialog";
      const close = dialog.appendChild(new FakeElement("button"));
      close.dataset.role = "close";
      close.textContent = "x";
      const url = dialog.appendChild(new FakeElement("input"));
      url.dataset.role = "url";
      const result = dialog.appendChild(new FakeElement("div"));
      result.dataset.role = "result";
      const status = dialog.appendChild(new FakeElement("p"));
      status.dataset.role = "status";
      const cancel = dialog.appendChild(new FakeElement("button"));
      cancel.dataset.role = "cancel";
      cancel.textContent = "Cancel";
      const analyze = dialog.appendChild(new FakeElement("button"));
      analyze.dataset.role = "analyze";
      analyze.textContent = "Analyze";
      const confirm = dialog.appendChild(new FakeElement("button"));
      confirm.dataset.role = "confirm";
      confirm.textContent = "Add Lean files";
      confirm.hidden = true;
      return;
    }
    if (html.includes("lea-model-picker-trigger")) {
      const trigger = this.appendChild(new FakeElement("button"));
      trigger.className = "lea-model-picker-trigger";
      trigger.setAttribute("role", "combobox");
      trigger.setAttribute("aria-expanded", "false");
      const value = trigger.appendChild(new FakeElement("span"));
      value.dataset.role = "model-picker-value";
      const popover = this.appendChild(new FakeElement("div"));
      popover.className = "lea-model-picker-popover";
      popover.hidden = true;
      const search = popover.appendChild(new FakeElement("input"));
      search.className = "lea-model-picker-search";
      search.value = "";
      const heading = popover.appendChild(new FakeElement("div"));
      heading.className = "lea-model-picker-heading";
      const catalogStatus = popover.appendChild(new FakeElement("div"));
      catalogStatus.className = "lea-model-picker-status";
      const catalogStatusText = catalogStatus.appendChild(new FakeElement("span"));
      catalogStatusText.className = "lea-model-picker-status-text";
      const refreshModels = catalogStatus.appendChild(new FakeElement("button"));
      refreshModels.className = "lea-model-picker-refresh";
      const results = popover.appendChild(new FakeElement("div"));
      results.className = "lea-model-picker-results";
      return;
    }
    if (html.includes("ol-lean-settings-group")) {
      const close = this.appendChild(new FakeElement("button"));
      close.dataset.role = "close";
      close.setAttribute("aria-label", "Close Lea popover");
      const status = this.appendChild(new FakeElement("p"));
      status.className = "ol-lean-popover-status";
      const model = this.appendChild(new FakeElement("div"));
      model.dataset.role = "model";
      const maxTurns = this.appendChild(new FakeElement("input"));
      maxTurns.dataset.role = "max-turns";
      const maxSpend = this.appendChild(new FakeElement("input"));
      maxSpend.dataset.role = "max-spend";
      const texMirror = this.appendChild(new FakeElement("input"));
      texMirror.dataset.role = "tex-mirror";
      const sourcePause = this.appendChild(new FakeElement("input"));
      sourcePause.dataset.role = "source-pause";
      const save = this.appendChild(new FakeElement("button"));
      save.dataset.role = "save-settings";
      const retry = this.appendChild(new FakeElement("button"));
      retry.dataset.role = "retry-settings";
      retry.hidden = true;
      retry.textContent = "Retry loading";
      const githubPanel = this.appendChild(new FakeElement("section"));
      githubPanel.dataset.role = "github-token-panel";
      const githubCard = githubPanel.appendChild(new FakeElement("div"));
      githubCard.className = "ol-lean-github-token-card";
      const githubDescription = githubCard.appendChild(new FakeElement("span"));
      githubDescription.dataset.role = "github-token-description";
      const githubStatus = githubCard.appendChild(new FakeElement("strong"));
      githubStatus.dataset.role = "github-token-status";
      const githubSummary = githubCard.appendChild(new FakeElement("div"));
      githubSummary.dataset.role = "github-token-summary-actions";
      const githubToggle = githubSummary.appendChild(new FakeElement("button"));
      githubToggle.dataset.role = "github-token-toggle";
      const githubClear = githubSummary.appendChild(new FakeElement("button"));
      githubClear.dataset.role = "github-token-clear";
      githubClear.hidden = true;
      const githubEditor = githubCard.appendChild(new FakeElement("div"));
      githubEditor.dataset.role = "github-token-editor";
      githubEditor.hidden = true;
      const githubForm = githubEditor.appendChild(new FakeElement("form"));
      githubForm.dataset.role = "github-token-form";
      const githubInput = githubForm.appendChild(new FakeElement("input"));
      githubInput.dataset.role = "github-token-input";
      githubInput.type = "password";
      const githubVisibility = githubForm.appendChild(new FakeElement("button"));
      githubVisibility.dataset.role = "github-token-visibility";
      githubVisibility.textContent = "Show";
      const githubCancel = githubForm.appendChild(new FakeElement("button"));
      githubCancel.dataset.role = "github-token-cancel";
      githubCancel.textContent = "Cancel";
      const githubSave = githubForm.appendChild(new FakeElement("button"));
      githubSave.dataset.role = "github-token-save";
      githubSave.textContent = "Save token";
      const editProjectName = this.appendChild(new FakeElement("button"));
      editProjectName.dataset.role = "edit-project-name";
      return;
    }
    if (html.includes("ol-lean-popover-title")) {
      this.appendChild(new FakeElement("p")).className = "ol-lean-popover-title";
      const meta = this.appendChild(new FakeElement("p"));
      meta.className = "ol-lean-popover-meta";
      meta.appendChild(new FakeElement("strong"));
      const actions = this.appendChild(new FakeElement("div"));
      actions.className = "ol-lean-popover-actions";
      actions.dataset.role = "theorem-actions";
      const lean = this.appendChild(new FakeElement("pre"));
      lean.className = "ol-lean-popover-lean";
      lean.hidden = true;
      const warning = this.appendChild(new FakeElement("p"));
      warning.className = "ol-lean-popover-warning";
      warning.hidden = true;
      const status = this.appendChild(new FakeElement("p"));
      status.className = "ol-lean-popover-status";
      return;
    }
    if (html.includes("ol-lean-trigger-mark")) {
      const mark = this.appendChild(new FakeElement("span"));
      mark.className = "ol-lean-trigger-mark";
      mark.textContent = "L";
      return;
    }
  }

  appendChild(child) {
    child.remove?.();
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  append(...children) {
    for (const child of children) this.appendChild(typeof child === "string" ? new FakeText(child) : child);
  }

  insertBefore(child, reference) {
    child.parentNode = this;
    const index = this.children.indexOf(reference);
    if (index === -1) this.children.push(child);
    else this.children.splice(index, 0, child);
    return child;
  }

  replaceChildren(...children) {
    for (const child of this.children) {
      child.parentNode = null;
    }
    this.children = [];
    for (const child of children) {
      this.appendChild(child);
    }
  }

  remove() {
    if (!this.parentNode) return;
    const siblings = this.parentNode.children;
    const index = siblings.indexOf(this);
    if (index !== -1) siblings.splice(index, 1);
    this.parentNode = null;
  }

  addEventListener(type, listener) {
    const listeners = this._listeners.get(type) || [];
    listeners.push(listener);
    this._listeners.set(type, listeners);
  }

  removeEventListener(type, listener) {
    removeListener(this._listeners, type, listener);
  }

  dispatchEvent(event) {
    const eventObject = normalizeFakeEvent(event, this);
    for (const listener of this._listeners.get(eventObject.type) || []) {
      listener(eventObject);
    }
    return !eventObject.defaultPrevented;
  }

  click() {
    for (const listener of this._listeners.get("click") || []) {
      listener({
        preventDefault() {},
        stopPropagation() {},
        clientX: 0,
        clientY: 0,
        target: this
      });
    }
  }

  replaceWith(next) {
    if (!this.parentNode) return;
    const siblings = this.parentNode.children;
    const index = siblings.indexOf(this);
    if (index === -1) return;
    next.parentNode = this.parentNode;
    siblings[index] = next;
    this.parentNode = null;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name.startsWith("data-")) {
      this.dataset[toDatasetKey(name.slice(5))] = String(value);
    }
  }

  getAttribute(name) { return this.attributes[name] ?? null; }

  getBoundingClientRect() {
    return { left: 0, top: 0, right: 120, bottom: 32, width: 120, height: 32 };
  }

  contains(target) {
    if (target === this) return true;
    return this.children.some((child) => child.contains?.(target));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  closest(selector) {
    let node = this;
    while (node) {
      if (matchesSelector(node, selector)) return node;
      node = node.parentNode;
    }
    return null;
  }

  querySelectorAll(selector) {
    const matches = [];
    walk(this, (node) => {
      if (node !== this && matchesSelector(node, selector)) {
        matches.push(node);
      }
    });
    return matches;
  }
}

function walk(node, visit) {
  visit(node);
  for (const child of node.children || []) {
    walk(child, visit);
  }
}

function matchesSelector(node, selector) {
  if (!(node instanceof FakeElement)) return false;
  if (selector.includes(",")) return selector.split(",").some((part) => matchesSelector(node, part.trim()));
  const dataRoleMatch = selector.match(/^\[data-role=['"]?([^'"\]]+)['"]?\]$/);
  if (dataRoleMatch) {
    return node.dataset.role === dataRoleMatch[1];
  }
  if (selector.startsWith(".")) {
    return node.className.split(/\s+/).includes(selector.slice(1));
  }
  return node.tagName.toLowerCase() === selector.toLowerCase();
}

function removeListener(listenersByType, type, listener) {
  const listeners = listenersByType.get(type) || [];
  const index = listeners.indexOf(listener);
  if (index !== -1) listeners.splice(index, 1);
}

function normalizeFakeEvent(event, target) {
  const eventObject = typeof event === "string" ? { type: event } : { ...event };
  eventObject.target ||= target;
  eventObject.defaultPrevented = false;
  const originalPreventDefault = eventObject.preventDefault;
  eventObject.preventDefault = () => {
    eventObject.defaultPrevented = true;
    originalPreventDefault?.();
  };
  eventObject.stopPropagation ||= () => {};
  return eventObject;
}

function toDatasetKey(name) {
  return name.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
}

// --- Self-repair (docs/FEATURE-overleaf-self-repair.md, Phase 5) -----------

function brokenItem(overrides = {}) {
  return editableItem({
    id: "theorem:corollary_a:0",
    label: "corollary_a",
    leanDeclarationName: "corollary_a",
    status: "invalid",
    breakage: {
      upstreamLabel: "main_theorem",
      upstreamDeclarationName: "main_theorem_v2",
      classificationKind: "renamed",
      renamedFrom: "main_theorem",
      renamedTo: "main_theorem_v2",
      via: "chat",
      editedAt: "2026-07-04T12:00:00.000Z",
      selfBroken: false,
      repair: { state: "offered" }
    },
    ...overrides
  });
}

test("Lean pane 'Repair with Lea' appears for a broken item and posts /lean-pane/repair/start", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: { ok: true, rootFile: "main.tex", items: [brokenItem()], diagnostics: [] }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();

  // attribution copy is shown with the offer
  assert.match(harness.bodyText(), /`main_theorem` was renamed to `main_theorem_v2`/);
  assert.equal(harness.hasButtonText("Repair with Lea"), true);

  harness.clickButtonText("Repair with Lea");
  await flushPromises();

  // no confirmation popup: the repair dispatches immediately
  assert.equal(harness.confirmCalls.length, 0);

  const startCall = harness.fetchCalls.find((call) => call.url.includes("/lean-pane/repair/start"));
  assert.ok(startCall, "expected a POST to /lean-pane/repair/start");
  const body = JSON.parse(startCall.options.body);
  assert.equal(body.targetLabel, "corollary_a");
  assert.equal(body.targetKind, "theorem");
});

test("no repair offer while the upstream item is itself broken (suppressed), with the redirecting copy", async () => {
  const item = brokenItem();
  item.breakage.repairSuppressed = "upstream_broken";
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: { ok: true, rootFile: "main.tex", items: [item], diagnostics: [] }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();

  assert.equal(harness.hasButtonText("Repair with Lea"), false);
});

test("post-save impact summary offers 'Repair all (N)' and posts the batch; the batch panel renders", async () => {
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: { ok: true, rootFile: "main.tex", items: [editableItem()], diagnostics: [] },
      editSave: {
        ok: true,
        unchanged: false,
        ownResult: { checkStatus: "ok", classification: { kind: "renamed", from: "main_theorem", to: "main_theorem_v2" } },
        dependentsImpact: [
          { targetLabel: "corollary_a", status: "invalid", attributed: true, busy: false, brokenByUpstream: { targetLabel: "main_theorem", renamed: true, via: "edit" } },
          { targetLabel: "corollary_b", status: "invalid", attributed: true, busy: false, brokenByUpstream: { targetLabel: "main_theorem", renamed: true, via: "edit" } }
        ]
      },
      repairAll: {
        ok: true,
        batchId: "batch-1",
        done: false,
        running: true,
        pausedOn: null,
        items: [
          { targetKind: "theorem", targetLabel: "corollary_a", state: "running", reason: null, runJobId: "repair-1" },
          { targetKind: "theorem", targetLabel: "corollary_b", state: "pending", reason: null, runJobId: null }
        ]
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickFirstPaneItem();
  harness.clickButtonText("Edit");
  await flushPromises();
  harness.setEditTextareaValue("theorem main_theorem_v2 : True := by\n  sorry\n");
  harness.clickButtonText("Save");
  await flushPromises();

  assert.equal(harness.hasButtonText("Repair all (2)"), true);
  harness.clickButtonText("Repair all (2)");
  await flushPromises();

  // no confirmation popup: the batch dispatches immediately
  assert.equal(harness.confirmCalls.length, 0);

  const batchCall = harness.fetchCalls.find((call) => call.url.includes("/lean-pane/repair/all"));
  assert.ok(batchCall, "expected a POST to /lean-pane/repair/all");
  const body = JSON.parse(batchCall.options.body);
  assert.deepEqual(body.items.map((i) => i.targetLabel), ["corollary_a", "corollary_b"]);

  // the live batch panel renders an ordered queue with the active ordinal
  assert.match(harness.bodyText(), /Repair allRepairing…0 \/ 2 complete/);
  assert.match(harness.bodyText(), /Current · 1 of 2●corollary_aRepairing…/);
  assert.match(harness.bodyText(), /Next○corollary_bQueued · position 2 of 2/);
});

// --- Stale-offer reconciliation (docs/PLAN-self-repair-stale-offers.md) ----

test("the reported case: 'Repair all' under the edited item disappears once its dependent is fixed elsewhere", async () => {
  // Live manifest truth, mutable: the dependent starts broken (as the save's
  // cascade left it) and is later fixed through another path (per-item
  // repair, manual edit -- the summary must not care which).
  let dependentFixed = false;
  const corollaryItem = () => ({
    id: "theorem:corollary_a:1",
    kind: "theorem",
    label: "corollary_a",
    sourceFile: "main.tex",
    naturalLanguageLatex: "B.",
    leanKind: "theorem",
    leanDeclarationName: "corollary_a",
    ...(dependentFixed
      ? { status: "valid" }
      : {
          status: "invalid",
          breakage: {
            upstreamLabel: "main_theorem",
            classificationKind: "signature",
            via: "edit",
            selfBroken: false,
            repair: { state: "offered" }
          }
        })
  });
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: () => ({ ok: true, rootFile: "main.tex", items: [editableItem(), corollaryItem()], diagnostics: [] }),
      editSave: {
        ok: true,
        unchanged: false,
        ownResult: { checkStatus: "ok", classification: { kind: "signature" } },
        dependentsImpact: [
          { targetLabel: "corollary_a", status: "invalid", attributed: true, busy: false, brokenByUpstream: { targetLabel: "main_theorem", renamed: false, via: "edit" } }
        ]
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickPaneItemHeaderText("main_theorem");
  harness.clickButtonText("Edit");
  await flushPromises();
  harness.setEditTextareaValue("theorem main_theorem (h : True) : True := by\n  sorry\n");
  harness.clickButtonText("Save");
  await flushPromises();

  // Snapshot and live truth agree: the offer is live.
  assert.match(harness.bodyText(), /1 downstream item affected: 1 broken\./);
  assert.equal(harness.hasButtonText("Repair all (1)"), true);

  // The dependent gets fixed through some other path; the next pane refresh
  // (here driven by a source change; in the reported flow, by the repair's
  // own polling) reconciles the frozen save snapshot against live truth.
  dependentFixed = true;
  await harness.loadVisibleTheorems({
    activeTex: "\\begin{theorem}\n% lea: formalize label=demo_theorem\nA theorem, revised.\n\\end{theorem}"
  });

  assert.equal(harness.hasButtonText("Repair all (1)"), false);
  assert.match(harness.bodyText(), /1 downstream item was affected by this edit -- all since fixed or re-verified\./);
  assert.match(harness.bodyText(), /corollary_a: broken by this edit -- since fixed\./);
});

test("a repair dispatch error renders only under the item it was dispatched for", async () => {
  const brokenPaneItem = (name) => editableItem({
    id: `theorem:${name}:0`,
    label: name,
    leanDeclarationName: name,
    status: "invalid",
    breakage: {
      upstreamLabel: "main_theorem",
      classificationKind: "signature",
      via: "edit",
      selfBroken: false,
      repair: { state: "offered" }
    }
  });
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      failRepairStart: "dispatch exploded",
      manifest: { ok: true, rootFile: "main.tex", items: [brokenPaneItem("corollary_x"), brokenPaneItem("corollary_y")], diagnostics: [] }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickPaneItemHeaderText("corollary_x");
  harness.clickButtonText("Repair with Lea");
  await flushPromises();

  // error shown under the item whose dispatch failed
  assert.match(harness.bodyText(), /dispatch exploded/);

  // collapse X, expand Y: Y carries its own offer but NOT X's error
  harness.clickPaneItemHeaderText("corollary_x");
  harness.clickPaneItemHeaderText("corollary_y");
  assert.equal(harness.hasButtonText("Repair with Lea"), true);
  assert.doesNotMatch(harness.bodyText(), /dispatch exploded/);
});

test("round 2: a dependent skipped as busy during the save joins 'Repair all' once its repair run ends broken", async () => {
  // Live truth over time: the dependent's earlier repair run is live during
  // the save (in-progress), then ends FAILED -- leaving it broken with a
  // failed-repair marker (the second-rename-while-repairing case).
  let repairEnded = false;
  const corollaryItem = () => ({
    id: "theorem:corollary_a:1",
    kind: "theorem",
    label: "corollary_a",
    sourceFile: "main.tex",
    naturalLanguageLatex: "B.",
    leanKind: "theorem",
    leanDeclarationName: "corollary_a",
    ...(repairEnded
      ? {
          status: "invalid",
          breakage: {
            upstreamLabel: "main_theorem",
            classificationKind: "renamed",
            renamedFrom: "main_theorem",
            renamedTo: "main_theorem_v2",
            via: "edit",
            selfBroken: false,
            repair: { state: "failed", failureReason: "verification failed after the upstream changed again" }
          }
        }
      : { status: "in-progress" })
  });
  const harness = createContentHarness(
    { status: "unformalized" },
    {},
    {
      locationPath: "/project/unknown",
      manifest: () => ({ ok: true, rootFile: "main.tex", items: [editableItem(), corollaryItem()], diagnostics: [] }),
      editSave: {
        ok: true,
        unchanged: false,
        ownResult: { checkStatus: "ok", classification: { kind: "renamed", from: "main_theorem", to: "main_theorem_v2" } },
        // the cascade skipped the dependent: a live run was in progress
        dependentsImpact: [
          { targetLabel: "corollary_a", status: "busy", attributed: true, busy: true, brokenByUpstream: null }
        ]
      }
    }
  );
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  harness.clickPaneItemHeaderText("main_theorem");
  harness.clickButtonText("Edit");
  await flushPromises();
  harness.setEditTextareaValue("theorem main_theorem_v2 : True := by\n  sorry\n");
  harness.clickButtonText("Save");
  await flushPromises();

  // While the dependent's run is still live: no offer, honest busy line.
  assert.equal(harness.hasButtonText("Repair all (1)"), false);
  assert.match(harness.bodyText(), /not re-checked yet .*-- repair in progress\./);

  // Its repair ends broken; the next refresh must PROMOTE it into the offer
  // (the bug: the snapshot-busy entry could never join stillBroken).
  repairEnded = true;
  await harness.loadVisibleTheorems({
    activeTex: "\\begin{theorem}\n% lea: formalize label=demo_theorem\nA theorem, revised.\n\\end{theorem}"
  });

  assert.match(harness.bodyText(), /1 downstream item affected: 1 broken\./);
  assert.match(harness.bodyText(), /corollary_a: was busy during this edit's re-check -- now broken\./);
  assert.equal(harness.hasButtonText("Repair all (1)"), true);
});


test("dependency wait explains the named blocker before expansion and exposes working recovery controls", async () => {
  const item = { id: "waiting-target", formalizationId: "waiting-form", kind: "theorem", label: "target", title: "Target theorem",
    status: "paused", sourceFile: "main.tex", sourceStartLine: 1, sourceEndLine: 3, naturalLanguageLatex: "True.",
    leanKind: "theorem", apiRunId: "waiting-run", stopReason: "dependency_wait", waitingForDependencies: ["L"],
    dependencies: [{ id: "L", ready: false, state: "failed", specification: { name: "Separation lemma", signature: ": True" },
      blocker: { detail: "An assumption is missing." } }],
    leaStatus: { assessment: null, freshness: "unknown", reporting_incomplete: true,
      activity: { status: "cancelled", stop_reason: "dependency_wait" }, run_id: "waiting-run" } };
  const harness = createContentHarness({ status: "paused", ...item }, {}, {
    manifest: { ok: true, rootFile: "main.tex", items: [item], diagnostics: [] }
  });
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  assert.match(harness.bodyText(), /Separation lemma needs attention.*An assumption is missing/);
  assert.equal(harness.hasButtonText("Resume"), false);
  harness.clickButtonText("View supporting result");
  await flushPromises();
  assert.equal(harness.countSelector(".ol-lean-live-status"), 1);
  assert.equal(harness.hasButtonText("Retry shared result"), true);
  assert.match(harness.bodyText(), /Assessment pending while supporting results are resolved/);
  assert.doesNotMatch(harness.bodyText(), /last published update.*retained|Source evidence changed|dependency wait/);
  harness.clickButtonText("Stop waiting");
  await flushPromises();
  const request = harness.fetchCalls.find(call => call.url.endsWith("/lean-pane/dependencies/wait"));
  assert.deepEqual(JSON.parse(request.options.body), { overleafProjectId: "project-1", runId: "waiting-run", action: "stop" });
});

test("a stopped dependency wait offers Continue when ready in the pane and editor popup", async () => {
  const status = { status: "paused", apiRunId: "waiting-run", stopReason: "dependency_wait",
    waitingForDependencies: ["L"], autoResumeEnabled: false,
    dependencyWait: { state: "stopped", autoResume: false, message: "Automatic continuation is stopped." } };
  const harness = createContentHarness(status, {}, { manifest: { ok: true, rootFile: "main.tex", diagnostics: [], items: [{
    ...status, id: "waiting-target", kind: "theorem", label: "target", sourceFile: "main.tex", naturalLanguageLatex: "True." }] } });
  await harness.loadStatusForVisibleTheorem();
  harness.openTargetPopover();
  await flushPromises();
  assert.equal(harness.hasButtonText("Continue when ready"), true);
  assert.equal(harness.hasButtonText("Resume"), false);
  harness.clickButtonText("Continue when ready");
  await flushPromises();
  const request = harness.fetchCalls.find(call => call.url.endsWith("/lean-pane/dependencies/wait"));
  assert.equal(JSON.parse(request.options.body).action, "continue");
});

test("live Lea Status renders ongoing findings, history and Pause even for stale source", async () => {
  const assessment = { confidence: "high", scope: "partial_artifact", summary: "Main step checked; boundary case remains.",
    confidence_reason: "The approach is preserved.", findings: [{ key: "boundary", severity: "warning", title: "Omitted boundary case",
      detail: "The LaTeX omits the endpoint.", lean_resolution: "applied", source_resolution: "open", correction: "Handled the endpoint explicitly." }],
    remaining_obligations: ["Author should update the LaTeX."] };
  const update = { id: "status-1", created_at: new Date().toISOString(), assessment, artifact_snapshot: { revision_token: "artifact-1", files: [] } };
  const item = { id: "status-target", formalizationId: "status-form", kind: "theorem", label: "target", title: "Target theorem", status: "stale",
    sourceFile: "main.tex", sourceStartLine: 1, sourceEndLine: 3, naturalLanguageLatex: "True.", leanKind: "theorem",
    leaStatus: { assessment, attention: "source_issue", freshness: "source_changed", activity: { status: "running" },
      run_id: "active-status-run", sequence: 1, latest_update: update } };
  const harness = createContentHarness({ status: "unformalized" }, {}, {
    manifest: { ok: true, rootFile: "main.tex", items: [item], diagnostics: [] },
    leaStatusHistory: { updates: [update], has_more: false, next_cursor: "next" }
  });
  await harness.loadVisibleTheorems();
  harness.clickPaneTrigger();
  await flushPromises();
  harness.clickPaneTreeRowText("main.tex");
  assert.match(harness.bodyText(), /High confidence · Partial artifact · Source issue · Source changed · Last assessed version/);
  harness.clickFirstPaneItem();
  assert.match(harness.bodyText(), /Main step checked; boundary case remains/);
  assert.match(harness.bodyText(), /Source issue: open/);
  assert.match(harness.bodyText(), /Lean correction \(applied\): Handled the endpoint explicitly/);
  harness.clickButtonText("Load updates");
  await flushPromises();
  assert.ok(harness.fetchCalls.some(call => call.url.includes("/lea-status/updates")));
  harness.clickButtonText("Pause");
  await flushPromises();
  const pause = harness.fetchCalls.find(call => call.url.includes("/lean-pane/chat/interrupt"));
  assert.equal(JSON.parse(pause.options.body).runId, "active-status-run");
});

function renameDisplayFixture() {
  const state = { busy: false, revision: "old", namespace: "Lea.TestProject", status: "formalized" };
  class EventSource {
    static instances = [];
    constructor() { this.listeners = new Map(); EventSource.instances.push(this); }
    addEventListener(type, fn) { this.listeners.set(type, fn); }
    close() {}
    emit(phase, projectId = "unknown", namespaceChanged = true) {
      this.listeners.get("project-identity-changed")?.({ data: JSON.stringify({ overleafProjectId: projectId, phase, namespaceChanged }) });
    }
  }
  const response = (body, status = 200) => ({ ok: status === 200, status, async json() { return body; } });
  const options = { locationPath: "/project/unknown", EventSource, fetchOverride: async (url) => {
    if (!/\/project\/(identity\?|graph\?)|\/statuses$|\/lean-pane\/manifest$/.test(url)) return;
    if (state.busy) return response({ error: "project_renaming", message: "Renaming project…" }, 409);
    if (url.includes("/identity?")) return response({ identity: {
      projectId: "p1", overleafProjectId: "unknown", slug: "unknown", projectName: "Test Project",
      namespace: state.namespace, namespaceRevision: state.revision, exists: true, hasRecordedProofs: true
    } });
    if (url.includes("/graph?")) return response({ exists: true, namespaceRevision: state.revision,
      nodes: [{ key: "demo", kind: "theorem", title: "Demo theorem", status: "proved", uses: [], lean_decl: `${state.namespace}.demo_theorem` }], edges: [] });
    if (url.endsWith("/manifest")) return response({ ok: true, namespaceRevision: state.revision, rootFile: "main.tex", diagnostics: [], items: [{
      id: "theorem:demo_theorem:0", kind: "theorem", label: "demo_theorem", title: "Demo theorem",
      status: state.status === "formalized" ? "valid" : "unformalized", sourceFile: "main.tex", sourceStartLine: 1,
      sourceEndLine: 4, documentOrder: 0, naturalLanguageLatex: "A theorem.", leanKind: "theorem",
      leanDeclarationName: "demo_theorem", leanArtifactContent: `namespace ${state.namespace}\n\ntheorem demo_theorem : True := by trivial\n\nend ${state.namespace}`
    }] });
    return response({ namespaceRevision: state.revision, statuses: { "theorem:demo_theorem": { status: state.status } } });
  } };
  return { state, options, EventSource, response };
}

test("rename display retains confirmed badges, counts and code until reconciliation succeeds", async () => {
  const f = renameDisplayFixture();
  const h = createContentHarness({}, {}, f.options);
  await h.loadVisibleTheorems();
  h.clickPaneTrigger();
  await flushPromises();
  h.clickPaneTreeRowText("main.tex");
  h.clickFirstPaneItem();
  await flushPromises();
  const before = h.paneText();
  assert.match(before, /Lea.TestProject/);
  assert.equal(h.countSelector(".ol-lean-status-formalized"), 1);
  f.state.busy = true;
  f.EventSource.instances[0].emit("started");
  await h.runTimersAt(1000);
  assert.match(h.bodyText(), /Renaming project…/);
  assert.equal(h.paneText(), before);
  assert.equal(h.countSelector(".ol-lean-status-formalized"), 1);
  assert.equal(h.pendingTimers().filter((delay) => delay === 1000).length, 1);
  f.state.busy = false;
  f.state.revision = "new";
  f.state.namespace = "Lea.NewNotes";
  f.EventSource.instances[0].emit("finished");
  await h.runTimersAt(1000);
  await h.runTimersAt(1000);
  assert.doesNotMatch(h.bodyText(), /Renaming project…/);
  assert.match(h.paneText(), /Lea.NewNotes/);
  assert.equal(h.countSelector(".ol-lean-status-formalized"), 1);
});

test("rename display shows loading on first load and pauses retries while hidden", async () => {
  const f = renameDisplayFixture();
  f.state.busy = true;
  const h = createContentHarness({}, {}, f.options);
  await h.loadVisibleTheorems();
  h.clickPaneTrigger();
  await flushPromises();
  assert.match(h.bodyText(), /Renaming project…/);
  assert.match(h.bodyText(), /Loading project inventory/);
  assert.equal(h.countSelector(".ol-lean-status-unformalized"), 0);
  await h.setVisibility(true);
  const count = h.fetchCalls.length;
  await h.runTimersAt(1000);
  assert.equal(h.fetchCalls.length, count);
  f.state.busy = false;
  f.state.revision = "new";
  await h.setVisibility(false);
  await h.runTimersAt(0);
  await h.runTimersAt(1000);
  assert.doesNotMatch(h.bodyText(), /Renaming project…/);
  assert.equal(h.countSelector(".ol-lean-status-formalized"), 1);
});

test("rename display recovers by polling after missed events and preserves real missing-file statuses", async () => {
  const f = renameDisplayFixture();
  const h = createContentHarness({}, {}, f.options);
  await h.loadVisibleTheorems();
  f.state.busy = true;
  await h.loadVisibleTheorems();
  assert.match(h.bodyText(), /Renaming project…/);
  f.state.busy = false;
  f.state.revision = "new";
  f.state.status = "unformalized";
  await h.runTimersAt(1000);
  await h.runTimersAt(1000);
  assert.doesNotMatch(h.bodyText(), /Renaming project…/);
  assert.equal(h.countSelector(".ol-lean-status-unformalized"), 1);
});

test("rename display discards delayed badge responses after rename events and navigation", async () => {
  const f = renameDisplayFixture();
  let release;
  let delayed = false;
  const normalFetch = f.options.fetchOverride;
  f.options.fetchOverride = async (url, options, calls) => {
    if (delayed && url.endsWith("/statuses")) return new Promise((resolve) => { release = () => resolve(f.response({
      namespaceRevision: "old", statuses: { "theorem:demo_theorem": { status: "unformalized" } }
    })); });
    return normalFetch(url, options, calls);
  };
  const h = createContentHarness({}, {}, f.options);
  await h.loadVisibleTheorems();
  delayed = true;
  await h.loadVisibleTheorems();
  assert.ok(release);
  f.EventSource.instances[0].emit("started");
  release();
  await flushPromises();
  assert.equal(h.countSelector(".ol-lean-status-formalized"), 1);
  delayed = false;
  f.state.revision = "new";
  f.EventSource.instances[0].emit("finished");
  await h.runTimersAt(1000);
  await h.runTimersAt(1000);
  delayed = true;
  await h.loadVisibleTheorems();
  h.window.location.pathname = "/project/another-project";
  release();
  await flushPromises();
  assert.equal(h.countSelector(".ol-lean-status-unformalized"), 0);
});

test("rename events synchronize two tabs and ignore unrelated projects", async () => {
  const f = renameDisplayFixture();
  const first = createContentHarness({}, {}, f.options);
  const second = createContentHarness({}, {}, f.options);
  await first.loadVisibleTheorems();
  await second.loadVisibleTheorems();
  for (const source of f.EventSource.instances) source.emit("started", "another-project");
  assert.doesNotMatch(first.bodyText() + second.bodyText(), /Renaming project…/);
  f.state.busy = true;
  for (const source of f.EventSource.instances) source.emit("started");
  assert.match(first.bodyText(), /Renaming project…/);
  assert.match(second.bodyText(), /Renaming project…/);
  f.state.busy = false;
  f.state.revision = "new";
  for (const source of f.EventSource.instances) source.emit("finished");
  for (const h of [first, second]) {
    await h.runTimersAt(1000);
    await h.runTimersAt(1000);
    assert.doesNotMatch(h.bodyText(), /Renaming project…/);
    assert.equal(h.countSelector(".ol-lean-status-formalized"), 1);
  }
});

test("rename display retains the populated Blueprint during retryable reads", async () => {
  const f = renameDisplayFixture();
  const h = createContentHarness({}, {}, f.options);
  const oldDocument = globalThis.document;
  globalThis.document = h.document;
  try {
    await h.loadVisibleTheorems();
    h.clickPaneTrigger();
    await flushPromises();
    h.clickButtonText("Blueprint");
    await flushPromises();
    assert.match(h.paneText(), /demotheorem/);
    const before = h.paneText();
    f.state.busy = true;
    f.EventSource.instances[0].emit("started");
    await h.runTimersAt(1000);
    assert.equal(h.paneText(), before);
    f.state.busy = false;
    f.state.revision = "new";
    f.EventSource.instances[0].emit("finished");
    await h.runTimersAt(1000);
    await h.runTimersAt(1000);
    assert.doesNotMatch(h.bodyText(), /Renaming project…/);
    assert.match(h.paneText(), /demotheorem/);
  } finally { globalThis.document = oldDocument; }
});

for (const failed of [false, true]) {
  test(`a delayed inventory ${failed ? "failure" : "response"} cannot replace the Blueprint view`, async () => {
    const fixture = renameDisplayFixture();
    const fetchNormal = fixture.options.fetchOverride;
    let delayed = false, release;
    fixture.options.fetchOverride = async (url, ...args) => {
      if (delayed && url.includes("/lean-pane/manifest")) {
        return new Promise((resolve, reject) => { release = () => failed
          ? reject(new Error("Old inventory unavailable"))
          : resolve(fixture.response({ items: [], namespaceRevision: "old" })); });
      }
      return fetchNormal(url, ...args);
    };
    const harness = createContentHarness({}, {}, fixture.options);
    const oldDocument = globalThis.document;
    globalThis.document = harness.document;
    try {
      await harness.loadVisibleTheorems();
      harness.clickPaneTrigger();
      await flushPromises();
      delayed = true;
      harness.clickButtonLabel("Refresh Lean pane");
      await flushPromises();
      assert.ok(release);
      harness.clickButtonText("Blueprint");
      await flushPromises();
      const before = harness.paneText();
      assert.match(before, /demotheorem/);
      release();
      await flushPromises();
      assert.equal(harness.paneText(), before);
      assert.equal(harness.countSelector(".ol-lean-project-pane-refresh-warning"), 0);
    } finally { globalThis.document = oldDocument; }
  });
}

for (const surface of ["manifest", "graph"]) {
  test(`rename display discards a delayed ${surface} instead of clearing the confirmed pane`, async () => {
    const f = renameDisplayFixture();
    const fetchNormal = f.options.fetchOverride;
    let delayed = false, release;
    f.options.fetchOverride = async (url, ...args) => {
      if (delayed && url.includes(surface === "manifest" ? "/lean-pane/manifest" : "/project/graph?")) {
        return new Promise(resolve => { release = () => resolve(f.response({ namespaceRevision: "old", items: [], nodes: [], edges: [] })); });
      }
      return fetchNormal(url, ...args);
    };
    const h = createContentHarness({}, {}, f.options);
    const oldDocument = globalThis.document;
    globalThis.document = h.document;
    try {
      await h.loadVisibleTheorems();
      h.clickPaneTrigger();
      await flushPromises();
      if (surface === "graph") { h.clickButtonText("Blueprint"); await flushPromises(); }
      const before = h.paneText();
      delayed = true;
      if (surface === "graph") h.clickButtonText("Refresh");
      else h.clickButtonLabel("Refresh Lean pane");
      await flushPromises();
      assert.ok(release);
      f.EventSource.instances[0].emit("started");
      release();
      await flushPromises();
      assert.equal(h.paneText(), before);
      assert.match(h.bodyText(), /Renaming project…/);
    } finally { globalThis.document = oldDocument; }
  });
}

test("display-only identity events refresh without a rename notice", async () => {
  const f = renameDisplayFixture();
  const h = createContentHarness({}, {}, f.options);
  await h.loadVisibleTheorems();
  f.EventSource.instances[0].emit("finished", "unknown", false);
  await h.runScheduledTimers();
  assert.doesNotMatch(h.bodyText(), /Renaming project…/);
  assert.equal(h.countSelector(".ol-lean-status-formalized"), 1);
});

test("rename reconciliation resumes immediately when the event stream reconnects", async () => {
  const f = renameDisplayFixture();
  const h = createContentHarness({}, {}, f.options);
  await h.loadVisibleTheorems();
  f.state.busy = true;
  await h.loadVisibleTheorems();
  f.state.busy = false;
  f.state.revision = "new";
  f.EventSource.instances[0].onopen();
  await h.runTimersAt(0);
  await h.runTimersAt(1000);
  assert.doesNotMatch(h.bodyText(), /Renaming project…/);
  assert.equal(h.countSelector(".ol-lean-status-formalized"), 1);
});

test("settled badges detect a complete rename when both push events were missed", async () => {
  const f = renameDisplayFixture();
  const h = createContentHarness({}, {}, f.options);
  await h.loadVisibleTheorems();
  f.state.revision = "new";
  f.state.namespace = "Lea.NewNotes";
  await h.runTimersAt(30000);
  assert.match(h.bodyText(), /Renaming project…/);
  assert.equal(h.countSelector(".ol-lean-status-formalized"), 1);
  await h.runTimersAt(1000);
  assert.doesNotMatch(h.bodyText(), /Renaming project…/);
  assert.equal(h.countSelector(".ol-lean-status-formalized"), 1);
});
