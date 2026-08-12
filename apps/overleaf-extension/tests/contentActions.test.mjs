import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";

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
  assert.match(harness.bodyText(), /missing stub/);
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
  };
  const queuedManifestItem = {
    ...manifestItem,
    id: "theorem:queued_theorem:1",
    label: "queued_theorem",
    naturalLanguageRendered: "Another theorem.",
    naturalLanguageLatex: "Another theorem.",
    leanDeclarationName: "queued_theorem",
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
  assert.equal(harness.countSelector(".ol-lean-project-status-in-progress"), 2);
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
          { id: "t3", kind: "theorem", label: "t3", status: "disproved", sourceFile: "main.tex", documentOrder: 2, naturalLanguageLatex: "C.", leanKind: "theorem" },
          { id: "t4", kind: "theorem", label: "t4", status: "stub-generated", sourceFile: "main.tex", documentOrder: 3, naturalLanguageLatex: "D.", leanKind: "theorem" },
          { id: "t5", kind: "theorem", label: "t5", status: "invalid", sourceFile: "main.tex", documentOrder: 4, naturalLanguageLatex: "E.", leanKind: "theorem" },
          { id: "t6", kind: "theorem", label: "t6", status: "missing-stub", sourceFile: "main.tex", documentOrder: 5, naturalLanguageLatex: "F.", leanKind: "theorem", inProgress: true },
          { id: "t7", kind: "theorem", label: "t7", status: "stale", sourceFile: "main.tex", documentOrder: 6, naturalLanguageLatex: "G.", leanKind: "theorem" },
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
  assert.equal(progress.label, "main.tex: 8 Lea items, 3 successful, 1 sorry-stubbed, 1 failed, 1 out of date, 2 unformalized, 1 in progress.");
  assert.equal(progress.inProgress, true);
  assert.deepEqual(progress.segments.map((segment) => [segment.bucket, segment.count, segment.width]), [
    ["success", "3", "37.5%"],
    ["sorry-stubbed", "1", "12.5%"],
    ["failed", "1", "12.5%"],
    ["out-of-date", "1", "12.5%"],
    ["unformalized", "2", "25%"]
  ]);

  harness.clickPaneTreeRowText("main.tex");
  assert.match(harness.bodyText(), /Out of date.*LaTeX changed after this Lean artifact was generated/i);
});

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
  harness.clickFirstPaneItem();

  assert.equal(harness.hasButtonLabel("Copy stub"), true);
  assert.equal(harness.hasButtonLabel("Copy artifact"), true);
  assert.match(harness.bodyText(), /workspace\/proofs\/Main\.lean/);
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
  assert.ok(harness.countSelector(".ol-lean-project-math") >= 2);
  assert.equal(harness.countSelector(".ol-lean-project-math-sup"), 1);
  assert.ok(harness.countSelector(".ol-lean-project-lean-kw") >= 1);
  assert.ok(harness.countSelector(".ol-lean-project-lean-ty") >= 1);
  assert.ok(harness.countSelector(".ol-lean-project-lean-num") >= 1);
  assert.match(harness.bodyText(), /For every x ∈ ℝ, x2 ≥ 0\./);

  harness.clickFirstPaneItem();

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
  harness.clickFirstPaneItem();
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
  harness.clickFirstPaneItem();
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
  harness.clickFirstPaneItem();

  harness.clickButtonText("Formalize");
  await flushPromises();

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
    label: "Formalize all: 3 of 8 completed.",
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
  assert.match(queue.text, /Formalize allStopped1 complete · 3 stopped/);
  assert.deepEqual(queue.progress, {
    role: "progressbar",
    label: "Formalize all: 1 of 4 completed, 3 stopped.",
    min: "0",
    max: "4",
    now: "1"
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

function createContentHarness(statusInfo, theoremPatch = {}, options = {}) {
  const document = new FakeDocument();
  const timers = [];
  const fetchCalls = [];
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
    URL,
    TextEncoder,
    katex: options.katex,
    clearTimeout(id) {
      const index = timers.findIndex((timer) => timer.id === id);
      if (index !== -1) timers.splice(index, 1);
    },
    crypto: {
      subtle: {
        async digest() {
          return new ArrayBuffer(0);
        }
      }
    },
    document,
    fetch: async (url, fetchOptions) => {
      fetchCalls.push({ url: String(url), options: fetchOptions });
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
      return {
        ok: !failingRepairStart && !failingFormalize,
        status: failingRepairStart ? 400 : failingFormalize ? formalizeFailure.status || 400 : 200,
        async json() {
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
          if (String(url).endsWith("/settings") && options.companionSettings) {
            return options.companionSettings;
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
    setTimeout(callback) {
      const id = nextTimerId++;
      timers.push({ id, callback });
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
        sendMessage(_message, callback) {
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
        attentionCount: queue.querySelectorAll(".ol-lean-batch-queue-attention").length
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
  await new Promise((resolve) => setTimeout(resolve, 0));
  for (let i = 0; i < 4; i += 1) {
    await Promise.resolve();
  }
}

class FakeDocument {
  constructor() {
    this.body = new FakeElement("body");
    this.documentElement = new FakeElement("html");
    this.head = new FakeElement("head");
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
    return new FakeElement(tagName);
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

  focus() {}

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
      const results = popover.appendChild(new FakeElement("div"));
      results.className = "lea-model-picker-results";
      return;
    }
    if (html.includes("Extension Settings")) {
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
      const save = this.appendChild(new FakeElement("button"));
      save.dataset.role = "save-settings";
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
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  append(...children) {
    for (const child of children) this.appendChild(child);
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
