import assert from "node:assert/strict";
import test from "node:test";
import {
  aggregatePaneStatus,
  buildPaneUseRelationships,
  buildLeanPaneTree,
  canEditPaneItem,
  canFormalizePaneItem,
  canStubPaneItem,
  canViewPaneItemInLeaUi,
  canRepairPaneItem,
  capitalize,
  formatBreakageAttribution,
  formatDependentOutcome,
  formatDependentsImpact,
  formatLiteLatexText,
  formatRepairOutcome,
  formatLiteMath,
  formatPaneStatus,
  githubImportMatchedTargets,
  githubImportMatchedTargetKeys,
  hasInProgressItems,
  highlightLeanLine,
  overlayActiveTex,
  paneItemActions,
  paneItemToEditTarget,
  paneItemToFormalizeTarget,
  paneItemToGithubImportTarget,
  paneProgressBucketForItem,
  paneProgressSegments,
  deriveShareControls,
  filenameFromContentDisposition,
  formatPaneProgressLabel,
  parsePaneLatex,
  reconcileDependentsImpact,
  shouldRefetchLeanPaneFiles,
  stillBrokenDependents,
  stubbableItems,
  formalizableItems,
  summarizePaneProgress,
  treeAncestorIdsForFile
} from "../extension/leanPaneView.mjs";

test("formatPaneStatus maps known statuses and falls back to unknown", () => {
  assert.equal(formatPaneStatus("missing-stub"), "missing stub");
  assert.equal(formatPaneStatus("stub-generated"), "stub generated");
  assert.equal(formatPaneStatus("valid"), "valid");
  assert.equal(formatPaneStatus("defined"), "defined");
  assert.equal(formatPaneStatus("disproved"), "counterexample");
  assert.equal(formatPaneStatus("in-progress"), "in progress");
  assert.equal(formatPaneStatus("stale"), "out of date");
  assert.equal(formatPaneStatus("mixed"), "mixed");
  assert.equal(formatPaneStatus("nonsense"), "unknown");
  assert.equal(formatPaneStatus(undefined), "unknown");
});

test("capitalize uppercases the first character only", () => {
  assert.equal(capitalize("theorem"), "Theorem");
  assert.equal(capitalize(""), "");
  assert.equal(capitalize(undefined), "");
});

test("shouldRefetchLeanPaneFiles forces a fetch on explicit request, empty cache, or project switch", () => {
  const lastFiles = [{ path: "main.tex", content: "x" }];
  assert.equal(shouldRefetchLeanPaneFiles({ forceFetch: true, lastFiles, lastProjectId: "p", projectId: "p" }), true);
  assert.equal(shouldRefetchLeanPaneFiles({ forceFetch: false, lastFiles: null, lastProjectId: "p", projectId: "p" }), true);
  assert.equal(shouldRefetchLeanPaneFiles({ forceFetch: false, lastFiles: [], lastProjectId: "p", projectId: "p" }), true);
  assert.equal(shouldRefetchLeanPaneFiles({ forceFetch: false, lastFiles, lastProjectId: "p", projectId: "q" }), true);
});

test("shouldRefetchLeanPaneFiles reuses the cache for same-project edits", () => {
  const lastFiles = [{ path: "main.tex", content: "x" }];
  assert.equal(shouldRefetchLeanPaneFiles({ forceFetch: false, lastFiles, lastProjectId: "p", projectId: "p" }), false);
});

test("overlayActiveTex overrides existing files only and never invents paths", () => {
  const files = [{ path: "main.tex", content: "old" }, { path: "intro.tex", content: "keep" }];
  overlayActiveTex(files, "/main.tex", "new");
  assert.equal(files.find((f) => f.path === "main.tex").content, "new");
  assert.equal(files.find((f) => f.path === "intro.tex").content, "keep");

  overlayActiveTex(files, "missing.tex", "ghost");
  assert.equal(files.length, 2);

  overlayActiveTex(files, "", "noop");
  assert.equal(files.find((f) => f.path === "main.tex").content, "new");
});

test("hasInProgressItems detects any in-progress item", () => {
  assert.equal(hasInProgressItems([{ inProgress: false }, { inProgress: true }]), true);
  assert.equal(hasInProgressItems([{ inProgress: false }]), false);
  assert.equal(hasInProgressItems([]), false);
  assert.equal(hasInProgressItems(undefined), false);
});

test("canFormalizePaneItem keeps reruns available for settled items and blocks active runs", () => {
  const base = { formalizable: true, inProgress: false, status: "missing-stub" };
  assert.equal(canFormalizePaneItem(base), true);
  assert.equal(canFormalizePaneItem({ ...base, status: "stale" }), true);
  assert.equal(canFormalizePaneItem({ ...base, status: "invalid" }), true);
  // Terminal-good states remain explicitly re-formalizable.
  assert.equal(canFormalizePaneItem({ ...base, status: "valid" }), true);
  assert.equal(canFormalizePaneItem({ ...base, status: "defined" }), true);
  assert.equal(canFormalizePaneItem({ ...base, status: "disproved" }), true);
  assert.equal(canFormalizePaneItem({ ...base, inProgress: true }), false);
  assert.equal(canFormalizePaneItem({ ...base, status: "in-progress" }), false);
  // A malformed marker (no valid target) is not formalizable.
  assert.equal(canFormalizePaneItem({ ...base, formalizable: false }), false);
  assert.equal(canFormalizePaneItem(undefined), false);
});

test("canStubPaneItem offers sorry-stubbing only for an unformalized theorem", () => {
  const base = { formalizable: true, inProgress: false, status: "missing-stub", leanKind: "theorem" };
  assert.equal(canStubPaneItem(base), true);
  // Definitions always get a full body from Formalize; there is no stub form.
  assert.equal(canStubPaneItem({ ...base, leanKind: "def" }), false);
  // Once any artifact exists (stub or proof), Formalize is the right action.
  assert.equal(canStubPaneItem({ ...base, status: "stub-generated" }), false);
  assert.equal(canStubPaneItem({ ...base, status: "valid" }), false);
  assert.equal(canStubPaneItem({ ...base, status: "stale" }), false);
  assert.equal(canStubPaneItem({ ...base, inProgress: true }), false);
  assert.equal(canStubPaneItem({ ...base, formalizable: false }), false);
  assert.equal(canStubPaneItem(undefined), false);
});

test("canViewPaneItemInLeaUi requires a target identity and a real run or artifact", () => {
  const base = { label: "thm:main", status: "valid" };
  assert.equal(canViewPaneItemInLeaUi(base), true);
  assert.equal(canViewPaneItemInLeaUi({ ...base, status: "defined" }), true);
  assert.equal(canViewPaneItemInLeaUi({ ...base, status: "disproved" }), true);
  assert.equal(canViewPaneItemInLeaUi({ ...base, status: "needs-review" }), false);
  assert.equal(canViewPaneItemInLeaUi({ ...base, status: "in-progress" }), true);
  assert.equal(canViewPaneItemInLeaUi({ ...base, status: "stub-generated" }), true);
  assert.equal(canViewPaneItemInLeaUi({ ...base, status: "stale" }), true);
  assert.equal(canViewPaneItemInLeaUi({ ...base, status: "invalid" }), true);
  // Never-formalized or indeterminate items must not appear viewable.
  assert.equal(canViewPaneItemInLeaUi({ ...base, status: "missing-stub" }), false);
  assert.equal(canViewPaneItemInLeaUi({ ...base, status: "unknown" }), false);
  assert.equal(canViewPaneItemInLeaUi({ ...base, status: "error" }), false);
  // Without a declaration name or label the session can't be resolved.
  assert.equal(canViewPaneItemInLeaUi({ status: "valid" }), false);
  assert.equal(canViewPaneItemInLeaUi(undefined), false);
});

test("paneItemActions puts Formalize primary with Stub in the overflow for an unformalized theorem", () => {
  const item = { formalizable: true, inProgress: false, status: "missing-stub", leanKind: "theorem", label: "thm:main" };
  const actions = paneItemActions(item);
  assert.deepEqual(actions.primary, { id: "formalize", label: "Formalize" });
  assert.deepEqual(actions.rail.map((action) => action.id), ["go-to-source", "chat"]);
  assert.deepEqual(actions.overflow.map((action) => action.id), ["stub"]);
});

test("paneItemActions promotes Repair over Re-formalize on a broken item and keeps both reachable", () => {
  const item = {
    formalizable: true,
    inProgress: false,
    status: "invalid",
    leanKind: "theorem",
    label: "thm:dep",
    leanDeclarationName: "dep_theorem",
    leanArtifactContent: "theorem dep_theorem : True := by trivial",
    breakage: { upstreamLabel: "upstream", via: "edit" }
  };
  const actions = paneItemActions(item);
  assert.equal(actions.primary.id, "repair");
  // Repair takes the primary slot, but Re-formalize and Edit stay reachable.
  assert.deepEqual(actions.overflow.map((action) => action.label), ["Re-formalize", "Edit"]);
  assert.deepEqual(actions.rail.map((action) => action.id), ["go-to-source", "chat", "view-in-lea"]);
});

test("paneItemActions keeps Re-formalize primary on a settled valid item", () => {
  const item = {
    formalizable: true,
    inProgress: false,
    status: "valid",
    leanKind: "theorem",
    label: "thm:main",
    leanDeclarationName: "main_theorem",
    leanArtifactContent: "theorem main_theorem : True := by trivial"
  };
  const actions = paneItemActions(item);
  assert.deepEqual(actions.primary, { id: "formalize", label: "Re-formalize" });
  assert.deepEqual(actions.rail.map((action) => action.id), ["go-to-source", "chat", "view-in-lea"]);
  assert.deepEqual(actions.overflow.map((action) => action.id), ["edit"]);
});

test("paneItemActions suppresses state-changing actions while the item is being edited or running", () => {
  const broken = {
    formalizable: true,
    inProgress: false,
    status: "invalid",
    label: "thm:x",
    leanArtifactContent: "theorem x : True := by trivial",
    breakage: { upstreamLabel: "up", via: "edit" }
  };
  const whileEditing = paneItemActions(broken, { editing: true });
  assert.equal(whileEditing.primary, null);
  assert.deepEqual(whileEditing.overflow, []);

  const running = paneItemActions({ formalizable: true, inProgress: true, status: "in-progress", label: "thm:x" });
  assert.equal(running.primary, null);
  assert.deepEqual(running.overflow, []);
  assert.deepEqual(running.rail.map((action) => action.id), ["go-to-source", "chat", "view-in-lea"]);
});

test("paneItemToFormalizeTarget shapes the /formalize payload from a pane item", () => {
  const target = paneItemToFormalizeTarget({
    leanKind: "def",
    leanDeclarationName: "even_nat",
    label: "even_nat",
    naturalLanguageLatex: "A natural number is even...",
    targetUses: ["parity"],
    targetContext: "Use Nat parity.",
    sourceFile: "sections/parity.tex",
    sourceStartLine: 14,
    sourceEndLine: 19,
    sourceHash: "source-hash",
    syntax: "comment"
  });
  assert.deepEqual(target, {
    targetKind: "definition",
    targetLabel: "even_nat",
    targetText: "A natural number is even...",
    targetUses: ["parity"],
    targetContext: "Use Nat parity.",
    sourceFile: "sections/parity.tex",
    sourceStartLine: 14,
    sourceEndLine: 19,
    sourceHash: "source-hash",
    syntax: "comment"
  });

  const theorem = paneItemToFormalizeTarget({ leanKind: "theorem", label: "thm", naturalLanguageLatex: "X" });
  assert.equal(theorem.targetKind, "theorem");
  assert.deepEqual(theorem.targetUses, []);
  assert.equal(theorem.targetContext, "");
});

test("paneItemToGithubImportTarget keeps the stable label and current declaration separate", () => {
  assert.deepEqual(
    paneItemToGithubImportTarget({
      leanKind: "theorem",
      label: "stable_marker",
      leanDeclarationName: "renamed_theorem",
      naturalLanguageLatex: "Every x has P(x).",
      sourceHash: "source-sha",
    }),
    {
      targetKind: "theorem",
      targetLabel: "stable_marker",
      declarationName: "renamed_theorem",
      displayTitle: "renamed_theorem",
      statement: "Every x has P(x).",
      sourceHash: "source-sha",
    },
  );
});

test("githubImportMatchedTargetKeys locks only matched declarations in importable files", () => {
  const targets = [
    { targetKind: "theorem", targetLabel: "stable_marker", declarationName: "renamed_theorem" },
    { targetKind: "theorem", targetLabel: "conflict_marker", declarationName: "conflict_theorem" },
    { targetKind: "definition", targetLabel: "fallback_marker", declarationName: "fallback_definition" },
  ];
  const preview = {
    plan: {
      files: [
        {
          disposition: "add",
          destination_path: "Stable.lean",
          declarations: [{ match: {
            origin_key: "project-1:theorem:stable_marker",
            declaration_name: "renamed_theorem",
          } }],
        },
        {
          disposition: "path_conflict",
          destination_path: "Conflict.lean",
          declarations: [{ match: {
            origin_key: "project-1:theorem:conflict_marker",
            declaration_name: "conflict_theorem",
          } }],
        },
        {
          disposition: "already_present",
          destination_path: "Fallback.lean",
          declarations: [{ match: {
            origin_key: null,
            declaration_name: "fallback_definition",
          } }],
        },
      ],
    },
  };

  assert.deepEqual(
    githubImportMatchedTargetKeys(preview, targets).sort(),
    ["definition:fallback_marker", "theorem:stable_marker"],
  );
  assert.deepEqual(
    githubImportMatchedTargets(preview, targets).map((target) => ({
      key: target.key,
      displayTitle: target.displayTitle,
      destinationPath: target.destinationPath,
    })),
    [
      { key: "theorem:stable_marker", displayTitle: "renamed_theorem", destinationPath: "Stable.lean" },
      { key: "definition:fallback_marker", displayTitle: "fallback_definition", destinationPath: "Fallback.lean" },
    ],
  );
});

test("buildLeanPaneTree groups files into a compact source tree", () => {
  const tree = buildLeanPaneTree([
    { id: "defs", sourceFile: "sections/defs.tex", documentOrder: 2, status: "missing-stub" },
    { id: "main", sourceFile: "main.tex", documentOrder: 0, status: "valid" },
    { id: "intro", sourceFile: "sections/intro.tex", documentOrder: 1, status: "stale" }
  ]);

  assert.deepEqual(tree.children.map((node) => node.name), ["main.tex", "sections"]);
  assert.equal(tree.children[0].type, "file");
  assert.equal(tree.children[0].itemCount, 1);
  assert.equal(tree.children[1].type, "folder");
  assert.deepEqual(tree.children[1].children.map((node) => node.path), ["sections/intro.tex", "sections/defs.tex"]);
  assert.deepEqual(tree.files.map((node) => node.path), ["main.tex", "sections/intro.tex", "sections/defs.tex"]);
});

test("buildLeanPaneTree omits files with no manifest items and preserves item order", () => {
  const tree = buildLeanPaneTree([
    { id: "b", sourceFile: "main.tex", documentOrder: 2, status: "valid" },
    { id: "a", sourceFile: "main.tex", documentOrder: 1, status: "valid" }
  ]);

  assert.equal(tree.files.length, 1);
  assert.deepEqual(tree.files[0].items.map((item) => item.id), ["a", "b"]);
});

test("buildPaneUseRelationships resolves ordered uses and derives reverse edges by stable marker label", () => {
  const base = {
    id: "theorem:base:0",
    label: "base",
    leanDeclarationName: "base_after_manual_rename",
    status: "valid",
    targetUses: []
  };
  const middle = {
    id: "theorem:middle:1",
    label: "middle",
    status: "stub-generated",
    targetUses: ["base"]
  };
  const result = {
    id: "theorem:result:2",
    label: "result",
    status: "invalid",
    targetUses: ["middle", "outside_inventory"]
  };

  const relationships = buildPaneUseRelationships([base, middle, result]);
  assert.deepEqual(
    relationships.usesByItem.get(result).map((relationship) => ({
      label: relationship.label,
      itemId: relationship.item?.id || null,
      status: relationship.status,
      resolution: relationship.resolution
    })),
    [
      { label: "middle", itemId: middle.id, status: "stub-generated", resolution: "resolved" },
      { label: "outside_inventory", itemId: null, status: "unknown", resolution: "not-in-inventory" }
    ]
  );
  assert.deepEqual(
    relationships.usedByItem.get(base).map((relationship) => relationship.label),
    ["middle"]
  );
  assert.deepEqual(
    relationships.usedByItem.get(middle).map((relationship) => relationship.label),
    ["result"]
  );
});

test("buildPaneUseRelationships leaves duplicate labels ambiguous and handles cycles without traversal", () => {
  const duplicateA = { id: "a", label: "duplicate", status: "valid", targetUses: [] };
  const duplicateB = { id: "b", label: "duplicate", status: "invalid", targetUses: [] };
  const source = { id: "source", label: "source", status: "valid", targetUses: ["duplicate"] };
  const cycleA = { id: "cycle-a", label: "cycle_a", status: "valid", targetUses: ["cycle_b"] };
  const cycleB = { id: "cycle-b", label: "cycle_b", status: "valid", targetUses: ["cycle_a", "cycle_b"] };

  const relationships = buildPaneUseRelationships([duplicateA, duplicateB, source, cycleA, cycleB]);
  const ambiguous = relationships.usesByItem.get(source)[0];
  assert.equal(ambiguous.resolution, "ambiguous");
  assert.equal(ambiguous.item, null);
  assert.deepEqual(relationships.usedByItem.get(duplicateA), []);
  assert.deepEqual(relationships.usedByItem.get(duplicateB), []);
  assert.deepEqual(
    relationships.usesByItem.get(cycleB).map((relationship) => relationship.item.id),
    ["cycle-a", "cycle-b"]
  );
  assert.deepEqual(
    relationships.usedByItem.get(cycleB).map((relationship) => relationship.item.id),
    ["cycle-a", "cycle-b"]
  );
});

test("aggregatePaneStatus applies file status precedence", () => {
  assert.equal(aggregatePaneStatus([{ status: "valid" }, { status: "in-progress" }]), "in-progress");
  assert.equal(aggregatePaneStatus([{ status: "valid" }, { status: "error" }, { status: "invalid" }]), "error");
  assert.equal(aggregatePaneStatus([{ status: "valid" }, { status: "invalid" }]), "invalid");
  assert.equal(aggregatePaneStatus([{ status: "valid" }, { status: "stale" }]), "stale");
  assert.equal(aggregatePaneStatus([{ status: "stub-generated" }, { status: "missing-stub" }]), "missing-stub");
  assert.equal(aggregatePaneStatus([{ status: "stub-generated" }]), "stub-generated");
  assert.equal(aggregatePaneStatus([{ status: "valid" }, { status: "valid" }]), "valid");
  assert.equal(aggregatePaneStatus([{ status: "defined" }, { status: "defined" }]), "defined");
  assert.equal(aggregatePaneStatus([{ status: "valid" }, { status: "defined" }, { status: "disproved" }]), "mixed");
});

test("paneProgressBucketForItem maps pane and companion statuses to progress buckets", () => {
  for (const status of ["valid", "formalized", "defined", "disproved"]) {
    assert.equal(paneProgressBucketForItem({ status }), "success");
  }
  for (const status of ["stub-generated", "sorry_stub"]) {
    assert.equal(paneProgressBucketForItem({ status }), "sorryStubbed");
  }
  for (const status of ["failed", "invalid", "error"]) {
    assert.equal(paneProgressBucketForItem({ status }), "failed");
  }
  assert.equal(paneProgressBucketForItem({ status: "stale" }), "outOfDate");
  for (const status of ["missing-stub", "unformalized", "unknown", "in-progress", undefined, "new-weird-status"]) {
    assert.equal(paneProgressBucketForItem({ status }), "unformalized");
  }
});

test("summarizePaneProgress counts representative fractional file summaries", () => {
  assert.deepEqual(summarizePaneProgress([
    { status: "missing-stub" },
    { status: "missing-stub" },
    { status: "missing-stub" },
    { status: "missing-stub" },
    { status: "invalid" },
    { status: "valid" },
    { status: "valid" },
    { status: "valid" }
  ]), {
    total: 8,
    success: 3,
    sorryStubbed: 0,
    failed: 1,
    outOfDate: 0,
    unformalized: 4,
    inProgress: 0
  });

  assert.deepEqual(summarizePaneProgress([
    { status: "valid" },
    { status: "defined" },
    { status: "disproved" },
    { status: "formalized" },
    { status: "valid" },
    { status: "defined" },
    { status: "sorry_stub" },
    { status: "missing-stub" }
  ]), {
    total: 8,
    success: 6,
    sorryStubbed: 1,
    failed: 0,
    outOfDate: 0,
    unformalized: 1,
    inProgress: 0
  });

  assert.deepEqual(summarizePaneProgress([
    { status: "stub-generated" },
    { status: "stub-generated" },
    { status: "unknown" }
  ]), {
    total: 3,
    success: 0,
    sorryStubbed: 2,
    failed: 0,
    outOfDate: 0,
    unformalized: 1,
    inProgress: 0
  });
});

test("paneProgressSegments returns ordered nonzero segments with percentages", () => {
  assert.deepEqual(paneProgressSegments({
    total: 8,
    success: 6,
    sorryStubbed: 1,
    failed: 0,
    unformalized: 1,
    inProgress: 0
  }).map((segment) => [segment.id, segment.count, segment.percent, segment.title]), [
    ["success", 6, 75, "Successful: 6 of 8, 75%"],
    ["sorry-stubbed", 1, 12.5, "Sorry-stubbed: 1 of 8, 12.5%"],
    ["unformalized", 1, 12.5, "Unformalized: 1 of 8, 12.5%"]
  ]);

  assert.deepEqual(paneProgressSegments({
    total: 3,
    success: 0,
    sorryStubbed: 2,
    failed: 0,
    unformalized: 1,
    inProgress: 0
  }).map((segment) => [segment.id, segment.count, segment.percent, segment.title]), [
    ["sorry-stubbed", 2, 66.66666666666666, "Sorry-stubbed: 2 of 3, 66.7%"],
    ["unformalized", 1, 33.33333333333333, "Unformalized: 1 of 3, 33.3%"]
  ]);
});

test("pane progress gives stale items their own out-of-date segment", () => {
  const summary = summarizePaneProgress([
    { status: "valid" },
    { status: "stale" },
    { status: "missing-stub" }
  ]);
  assert.equal(summary.outOfDate, 1);
  assert.equal(summary.unformalized, 1);
  assert.deepEqual(
    paneProgressSegments(summary).map((segment) => [segment.id, segment.count]),
    [["success", 1], ["out-of-date", 1], ["unformalized", 1]]
  );
  assert.equal(
    formatPaneProgressLabel("main.tex", summary),
    "main.tex: 3 Lea items, 1 successful, 1 out of date, 1 unformalized."
  );
});

test("formatPaneProgressLabel omits zero-count buckets and includes running count", () => {
  assert.equal(formatPaneProgressLabel("main.tex", {
    total: 8,
    success: 6,
    sorryStubbed: 0,
    failed: 1,
    unformalized: 1,
    inProgress: 0
  }), "main.tex: 8 Lea items, 6 successful, 1 failed, 1 unformalized.");

  assert.equal(formatPaneProgressLabel("analysis.tex", {
    total: 6,
    success: 2,
    sorryStubbed: 4,
    failed: 0,
    unformalized: 0,
    inProgress: 1
  }), "analysis.tex: 6 Lea items, 2 successful, 4 sorry-stubbed, 1 in progress.");
});

test("treeAncestorIdsForFile returns expandable folder and file ids", () => {
  assert.deepEqual(treeAncestorIdsForFile("sections/defs.tex"), ["folder:sections", "file:sections/defs.tex"]);
  assert.deepEqual(treeAncestorIdsForFile("/main.tex"), ["file:main.tex"]);
});

test("parsePaneLatex splits inline and display math delimiters", () => {
  assert.deepEqual(parsePaneLatex("Let $x \\in \\mathbb{R}$ and \\[x^2 \\ge 0\\]."), [
    { type: "text", text: "Let " },
    { type: "math", text: "x \\in \\mathbb{R}", display: false },
    { type: "text", text: " and " },
    { type: "math", text: "x^2 \\ge 0", display: true },
    { type: "text", text: "." }
  ]);

  assert.deepEqual(parsePaneLatex("Use $$a_n\\to 0$$ now."), [
    { type: "text", text: "Use " },
    { type: "math", text: "a_n\\to 0", display: true },
    { type: "text", text: " now." }
  ]);

  assert.deepEqual(parsePaneLatex([
    "Before.",
    "\\begin{align}",
    "a &\\triangleq b \\\\",
    "c &= d",
    "\\end{align}",
    "After."
  ].join("\n")), [
    { type: "text", text: "Before.\n" },
    {
      type: "math",
      text: "\\begin{align}\na &\\triangleq b \\\\\nc &= d\n\\end{align}",
      display: true
    },
    { type: "text", text: "\nAfter." }
  ]);
});

test("parsePaneLatex leaves unmatched delimiters as readable text", () => {
  assert.deepEqual(parsePaneLatex("Cost is \\$5 and $unfinished."), [
    { type: "text", text: "Cost is \\$5 and $unfinished." }
  ]);
});

test("formatLiteMath prettifies common theorem math without dependencies", () => {
  assert.deepEqual(formatLiteMath("\\forall x \\in \\mathbb{R}, x^2 \\ge 0"), [
    { type: "text", text: "∀ x ∈ ℝ, x" },
    { type: "sup", text: "2" },
    { type: "text", text: " ≥ 0" }
  ]);

  assert.deepEqual(formatLiteMath("a_{n+1} \\to \\alpha"), [
    { type: "text", text: "a" },
    { type: "sub", text: "n+1" },
    { type: "text", text: " → α" }
  ]);

  assert.deepEqual(formatLiteMath("f(x) \\triangleq x^2"), [
    { type: "text", text: "f(x) ≜ x" },
    { type: "sup", text: "2" }
  ]);
});

test("formatLiteMath keeps unknown commands visible as fallback text", () => {
  assert.deepEqual(formatLiteMath("\\Spec R \\subseteq X"), [
    { type: "text", text: "\\Spec R ⊆ X" }
  ]);
});

test("formatLiteLatexText styles common prose and preserves unknown commands", () => {
  assert.deepEqual(
    formatLiteLatexText("A \\emph{locally \\textbf{finite}} family~uses \\Spec and \\eqref{eq:key}."),
    [
      { type: "text", text: "A ", marks: [] },
      { type: "text", text: "locally ", marks: ["em"] },
      { type: "text", text: "finite", marks: ["em", "strong"] },
      { type: "text", text: " family\u00a0uses \\Spec and ", marks: [] },
      { type: "text", text: "(eq:key)", marks: ["ref"] },
      { type: "text", text: ".", marks: [] }
    ]
  );

  assert.deepEqual(formatLiteLatexText("\\label{hidden}Cost: \\$5 \\& 10\\%."), [
    { type: "text", text: "Cost: $5 & 10%.", marks: [] }
  ]);
});

test("canEditPaneItem requires a recorded artifact", () => {
  assert.equal(canEditPaneItem({ leanArtifactContent: "theorem foo : True := by trivial" }), true);
  assert.equal(canEditPaneItem({ leanArtifactContent: "" }), false);
  assert.equal(canEditPaneItem({}), false);
  assert.equal(canEditPaneItem(null), false);
});

test("paneItemToEditTarget shapes a theorem/definition target from a pane item", () => {
  assert.deepEqual(
    paneItemToEditTarget({ leanKind: "theorem", leanDeclarationName: "compactness_criterion" }, "project-1"),
    { overleafProjectId: "project-1", targetKind: "theorem", targetLabel: "compactness_criterion" }
  );
  assert.deepEqual(
    paneItemToEditTarget({ leanKind: "def", label: "locally_finite_family" }, "project-1"),
    { overleafProjectId: "project-1", targetKind: "definition", targetLabel: "locally_finite_family" }
  );
});

test("formatDependentsImpact summarizes an empty or non-empty dependents list", () => {
  assert.equal(formatDependentsImpact([]), "");
  assert.equal(formatDependentsImpact(null), "");
  assert.equal(
    formatDependentsImpact([{ targetLabel: "corollary_a" }]),
    "Editing this may affect 1 downstream item: corollary_a."
  );
  assert.equal(
    formatDependentsImpact([{ targetLabel: "corollary_a" }, { targetLabel: "corollary_b" }]),
    "Editing this may affect 2 downstream items: corollary_a, corollary_b."
  );
});

test("formatDependentOutcome distinguishes broken/renamed/busy/unattributed/still-valid outcomes", () => {
  assert.equal(
    formatDependentOutcome({ targetLabel: "a", busy: true }),
    "a: not re-checked yet (a Lea run is already in progress for it)."
  );
  assert.equal(
    formatDependentOutcome({ targetLabel: "a", brokenByUpstream: { renamed: true } }),
    "a: broken -- the declaration it referred to was renamed."
  );
  assert.equal(
    formatDependentOutcome({ targetLabel: "a", brokenByUpstream: { renamed: false } }),
    "a: broken by this edit."
  );
  assert.equal(
    formatDependentOutcome({ targetLabel: "a", attributed: false }),
    "a: may be affected, but no recorded session was found to re-check it."
  );
  assert.equal(
    formatDependentOutcome({ targetLabel: "a", attributed: true, brokenByUpstream: null }),
    "a: re-checked, still valid."
  );
  assert.equal(formatDependentOutcome(null), "");
});

// Regression test for a real bug caught live: a dependent that was never
// actually re-checked (its upstream module's rebuild failed, or the
// per-dependent check call itself errored) must not render the same text as
// a genuinely successful recheck -- `status: "unknown"` has to take priority
// over the "attributed, not busy, not broken -> still valid" default.
test("formatDependentOutcome distinguishes 'could not verify' from a genuine still-valid recheck", () => {
  assert.equal(
    formatDependentOutcome({
      targetLabel: "epsilon_two",
      status: "unknown",
      attributed: true,
      busy: false,
      brokenByUpstream: null,
      checkDetail: "error: lake build failed for Lea.Proj.epsilon_one (exit 1): ..."
    }),
    "epsilon_two: could not be verified (error: lake build failed for Lea.Proj.epsilon_one (exit 1): ...) -- treat as unconfirmed, not as valid."
  );
  // Same outcome without a detail message still reads as "unconfirmed", not "valid".
  assert.equal(
    formatDependentOutcome({ targetLabel: "epsilon_two", status: "unknown", attributed: true, busy: false, brokenByUpstream: null }),
    "epsilon_two: could not be verified -- treat as unconfirmed, not as valid."
  );
  // A genuine successful recheck (status "reverified", not "unknown") is unaffected.
  assert.equal(
    formatDependentOutcome({ targetLabel: "a", status: "reverified", attributed: true, busy: false, brokenByUpstream: null }),
    "a: re-checked, still valid."
  );
});

test("highlightLeanLine marks Lean keywords, comments, strings, numbers, and types", () => {
  const spans = highlightLeanLine('theorem foo : Nat := 2 -- "note"');
  assert.equal(spans.find((span) => span.text === "theorem")?.cls, "kw");
  assert.equal(spans.find((span) => span.text === "Nat")?.cls, "ty");
  assert.equal(spans.find((span) => span.text === "2")?.cls, "num");
  assert.equal(spans.find((span) => span.text.startsWith("--"))?.cls, "com");

  const stringSpans = highlightLeanLine('open "Mathlib"');
  assert.equal(stringSpans.find((span) => span.text === '"Mathlib"')?.cls, "str");
});

test("deriveShareControls gates Save on a changed draft and Push on remote+token", () => {
  // No Lea project yet: everything off, with the formalize-first hint.
  const missing = deriveShareControls({ exists: false, remoteUrl: null, tokenConfigured: true });
  assert.equal(missing.canSave, false);
  assert.equal(missing.canPush, false);
  assert.match(missing.hint, /formalize a theorem first/i);

  // Project, no remote: Save enables once a draft is typed; Push stays off.
  const untouched = deriveShareControls({ exists: true, remoteUrl: null, tokenConfigured: true });
  assert.equal(untouched.canSave, false);
  assert.match(untouched.hint, /Save a GitHub remote/);
  const drafted = deriveShareControls({
    exists: true, remoteUrl: null, draftRemote: "https://github.com/me/doc", tokenConfigured: true
  });
  assert.equal(drafted.canSave, true);
  assert.equal(drafted.canPush, false);

  // Saved remote + token: Push on; Save off until the draft diverges from saved.
  const ready = deriveShareControls({
    exists: true, remoteUrl: "https://github.com/me/doc", tokenConfigured: true
  });
  assert.equal(ready.canSave, false);
  assert.equal(ready.canPush, true);
  assert.equal(ready.hint, "");

  // Saved remote, no token: Push off with the settings hint.
  const tokenless = deriveShareControls({
    exists: true, remoteUrl: "https://github.com/me/doc", tokenConfigured: false
  });
  assert.equal(tokenless.canPush, false);
  assert.match(tokenless.hint, /GitHub token in Settings/);

  // A request in flight disables both.
  const busy = deriveShareControls({
    exists: true, remoteUrl: "https://github.com/me/doc",
    draftRemote: "https://github.com/me/other", tokenConfigured: true, busy: true
  });
  assert.equal(busy.canSave, false);
  assert.equal(busy.canPush, false);
});

test("filenameFromContentDisposition extracts the quoted filename or falls back", () => {
  assert.equal(
    filenameFromContentDisposition('attachment; filename="doc-1.zip"'),
    "doc-1.zip"
  );
  assert.equal(filenameFromContentDisposition(null, "lean-project.zip"), "lean-project.zip");
  assert.equal(filenameFromContentDisposition("attachment", "x.zip"), "x.zip");
});

// --- Self-repair helpers (docs/FEATURE-overleaf-self-repair.md, Phase 5) ---

test("canRepairPaneItem gates on breakage, suppression, in-progress, and a running repair", () => {
  const breakage = { upstreamLabel: "a", repair: { state: "offered" } };
  assert.equal(canRepairPaneItem({ status: "invalid", breakage }), true);
  assert.equal(canRepairPaneItem({ status: "invalid" }), false);
  assert.equal(canRepairPaneItem({ status: "invalid", breakage: { ...breakage, repairSuppressed: "upstream_broken" } }), false);
  assert.equal(canRepairPaneItem({ status: "in-progress", breakage }), false);
  assert.equal(canRepairPaneItem({ status: "invalid", breakage: { ...breakage, repair: { state: "running" } } }), false);
  // a previously failed repair can be retried
  assert.equal(canRepairPaneItem({ status: "invalid", breakage: { ...breakage, repair: { state: "failed" } } }), true);
});

test("formatBreakageAttribution distinguishes self-break, rename, suppression, and generic upstream breaks", () => {
  assert.equal(
    formatBreakageAttribution({ selfBroken: true, via: "edit" }),
    "Broken by a manual edit to this item."
  );
  assert.match(
    formatBreakageAttribution({ classificationKind: "renamed", renamedFrom: "a", renamedTo: "b", via: "chat" }),
    /`a` was renamed to `b` \(via a chat request\)\. This item still refers to the old name\./
  );
  assert.match(
    formatBreakageAttribution({ upstreamLabel: "a", repairSuppressed: "upstream_broken", via: "formalize" }),
    /repair a first/i
  );
  assert.equal(
    formatBreakageAttribution({ upstreamLabel: "a", classificationKind: "signature", via: "formalize" }),
    "Broken by a re-formalization to a."
  );
});

test("formatRepairOutcome covers every batch item state", () => {
  assert.equal(formatRepairOutcome({ targetLabel: "b", state: "pending" }), "b: waiting.");
  assert.equal(formatRepairOutcome({ targetLabel: "b", state: "running" }), "b: repairing...");
  assert.equal(formatRepairOutcome({ targetLabel: "b", state: "repaired" }), "b: repaired and verified.");
  assert.match(formatRepairOutcome({ targetLabel: "b", state: "needs_review" }), /review required/);
  assert.match(formatRepairOutcome({ targetLabel: "b", state: "failed", reason: "unprovable" }), /repair failed -- unprovable/);
  assert.match(
    formatRepairOutcome({ targetLabel: "c", state: "skipped", reason: "depends_on_failed:b" }),
    /skipped -- depends on failed repair of b\./
  );
  assert.match(formatRepairOutcome({ targetLabel: "c", state: "skipped", reason: "already_fixed" }), /already compiles/);
});

test("formatRepairOutcome is operation-aware for stub and formalize batches", () => {
  // Running verb + terminal vocabulary track the batch operation.
  assert.equal(formatRepairOutcome({ targetLabel: "t", state: "running" }, "stub"), "t: stubbing...");
  assert.equal(formatRepairOutcome({ targetLabel: "t", state: "running" }, "formalize"), "t: formalizing...");
  assert.equal(formatRepairOutcome({ targetLabel: "t", state: "stubbed" }, "stub"), "t: stub created.");
  assert.equal(formatRepairOutcome({ targetLabel: "t", state: "formalized" }, "formalize"), "t: formalized and verified.");
  assert.equal(formatRepairOutcome({ targetLabel: "t", state: "disproved" }, "formalize"), "t: counterexample found.");
  assert.match(formatRepairOutcome({ targetLabel: "t", state: "failed", reason: "no key" }, "stub"), /stub failed -- no key/);
  assert.match(formatRepairOutcome({ targetLabel: "t", state: "failed" }, "formalize"), /formalization failed/);
  assert.match(
    formatRepairOutcome({ targetLabel: "c", state: "skipped", reason: "depends_on_failed:b" }, "formalize"),
    /skipped -- depends on failed formalization of b\./
  );
  // A stopped batch marks its unsettled items canceled, in any operation.
  assert.equal(formatRepairOutcome({ targetLabel: "t", state: "canceled" }, "stub"), "t: stopped.");
  assert.equal(formatRepairOutcome({ targetLabel: "t", state: "canceled" }, "formalize"), "t: stopped.");
});

test("stubbableItems / formalizableItems skip completed work in project-level batches", () => {
  const items = [
    { targetLabel: "fresh_thm", status: "missing-stub", leanKind: "theorem", formalizable: true },
    { targetLabel: "fresh_def", status: "missing-stub", leanKind: "def", formalizable: true },
    { targetLabel: "stubbed_thm", status: "stub-generated", leanKind: "theorem", formalizable: true },
    { targetLabel: "broken_thm", status: "invalid", leanKind: "theorem", formalizable: true },
    { targetLabel: "done_thm", status: "valid", leanKind: "theorem", formalizable: true },
    { targetLabel: "running_thm", status: "missing-stub", leanKind: "theorem", formalizable: true, inProgress: true },
    { targetLabel: "unmarked", status: "missing-stub", leanKind: "theorem", formalizable: false }
  ];
  // Stub: un-stubbed THEOREMS only -- no definitions, no already-stubbed, none
  // in progress, none without a valid marker.
  assert.deepEqual(stubbableItems(items).map((i) => i.targetLabel), ["fresh_thm"]);
  // Formalize all: everything not yet verified -- fresh items (incl.
  // definitions), sorry-stubs to complete, and broken items to re-formalize.
  // A valid proof remains individually rerunnable but is not restarted in bulk.
  assert.deepEqual(
    formalizableItems(items).map((i) => i.targetLabel),
    ["fresh_thm", "fresh_def", "stubbed_thm", "broken_thm"]
  );
});

// --- Stale-offer reconciliation (docs/PLAN-self-repair-stale-offers.md) ----

test("reconcileDependentsImpact: fixed, still-broken, repairing, and unmatched dependents", () => {
  const dependents = [
    { targetLabel: "fixed_one", brokenByUpstream: { targetLabel: "a" } },
    { targetLabel: "still_broken", brokenByUpstream: { targetLabel: "a" } },
    { targetLabel: "repairing_now", brokenByUpstream: { targetLabel: "a" } },
    { targetLabel: "not_in_manifest", brokenByUpstream: { targetLabel: "a" } }
  ];
  const items = [
    { leanDeclarationName: "fixed_one", status: "valid" },
    { leanDeclarationName: "still_broken", status: "invalid", breakage: { upstreamLabel: "a" } },
    { leanDeclarationName: "repairing_now", status: "invalid", breakage: { upstreamLabel: "a", repair: { state: "running" } } }
  ];
  const out = reconcileDependentsImpact(dependents, items);
  const byLabel = Object.fromEntries(out.map((d) => [d.targetLabel, d]));
  assert.deepEqual(
    [byLabel.fixed_one.sinceFixed, byLabel.fixed_one.nowRepairing, byLabel.fixed_one.matched],
    [true, false, true]
  );
  assert.deepEqual(
    [byLabel.still_broken.sinceFixed, byLabel.still_broken.nowRepairing],
    [false, false]
  );
  assert.deepEqual(
    [byLabel.repairing_now.sinceFixed, byLabel.repairing_now.nowRepairing],
    [false, true]
  );
  // unmatched: snapshot state kept, offer stays (fail toward offering)
  assert.deepEqual(
    [byLabel.not_in_manifest.sinceFixed, byLabel.not_in_manifest.matched],
    [false, false]
  );
});

test("reconcileDependentsImpact: an invalid item with no breakage is NOT counted as fixed, and label matching also works", () => {
  const out = reconcileDependentsImpact(
    [{ targetLabel: "broken_other_way", brokenByUpstream: { targetLabel: "a" } }],
    [{ label: "broken_other_way", status: "invalid" }] // matched by label, still invalid
  );
  assert.equal(out[0].matched, true);
  assert.equal(out[0].sinceFixed, false);
});

test("stillBrokenDependents keeps only currently-broken entries", () => {
  const out = stillBrokenDependents([
    { targetLabel: "a", brokenByUpstream: {}, sinceFixed: false, nowRepairing: false },
    { targetLabel: "b", brokenByUpstream: {}, sinceFixed: true, nowRepairing: false },
    { targetLabel: "c", brokenByUpstream: {}, sinceFixed: false, nowRepairing: true },
    { targetLabel: "d", brokenByUpstream: null, sinceFixed: false, nowRepairing: false }
  ]);
  assert.deepEqual(out.map((d) => d.targetLabel), ["a"]);
});

test("formatDependentOutcome appends since-fixed / repair-in-progress only to lines that claimed a problem", () => {
  assert.equal(
    formatDependentOutcome({ targetLabel: "b", brokenByUpstream: { renamed: false }, sinceFixed: true }),
    "b: broken by this edit -- since fixed."
  );
  assert.equal(
    formatDependentOutcome({ targetLabel: "b", brokenByUpstream: { renamed: false }, nowRepairing: true }),
    "b: broken by this edit -- repair in progress."
  );
  assert.match(
    formatDependentOutcome({ targetLabel: "b", busy: true, sinceFixed: true }),
    /not re-checked yet.*-- since fixed\.$/
  );
  // history is never rewritten on lines that claimed no problem
  assert.equal(
    formatDependentOutcome({ targetLabel: "b", status: "reverified", attributed: true, sinceFixed: true }),
    "b: re-checked, still valid."
  );
});

// --- Round 2: bidirectional reconciliation (the busy-skipped dependent) ----

test("reconcileDependentsImpact upgrades a snapshot-busy entry to nowBroken when live truth says so", () => {
  const dependents = [
    { targetLabel: "busy_then_broken", busy: true, brokenByUpstream: null },
    { targetLabel: "busy_then_fixed", busy: true, brokenByUpstream: null },
    { targetLabel: "busy_still_running", busy: true, brokenByUpstream: null },
    { targetLabel: "busy_but_suppressed", busy: true, brokenByUpstream: null }
  ];
  const items = [
    { leanDeclarationName: "busy_then_broken", status: "invalid", breakage: { upstreamLabel: "a", repair: { state: "failed" } } },
    { leanDeclarationName: "busy_then_fixed", status: "valid" },
    { leanDeclarationName: "busy_still_running", status: "in-progress", breakage: { upstreamLabel: "a", repair: { state: "running" } } },
    { leanDeclarationName: "busy_but_suppressed", status: "invalid", breakage: { upstreamLabel: "a", repairSuppressed: "upstream_broken" } }
  ];
  const byLabel = Object.fromEntries(reconcileDependentsImpact(dependents, items).map((d) => [d.targetLabel, d]));
  assert.equal(byLabel.busy_then_broken.nowBroken, true);
  assert.equal(byLabel.busy_then_fixed.nowBroken, false);
  assert.equal(byLabel.busy_then_fixed.sinceFixed, true);
  assert.equal(byLabel.busy_still_running.nowBroken, false);
  assert.equal(byLabel.busy_still_running.nowRepairing, true);
  assert.equal(byLabel.busy_but_suppressed.nowBroken, false); // offering it would be a server 409
});

test("stillBrokenDependents decides from live truth for matched entries, in BOTH directions", () => {
  const out = stillBrokenDependents([
    // snapshot busy, live broken -> offered (the reported round-2 case)
    { targetLabel: "a", busy: true, brokenByUpstream: null, matched: true, nowBroken: true, sinceFixed: false, nowRepairing: false },
    // snapshot broken, live fixed -> not offered (round 1)
    { targetLabel: "b", brokenByUpstream: {}, matched: true, nowBroken: false, sinceFixed: true, nowRepairing: false },
    // snapshot broken, live still broken -> offered
    { targetLabel: "c", brokenByUpstream: {}, matched: true, nowBroken: true, sinceFixed: false, nowRepairing: false },
    // unmatched snapshot-broken -> offered (fail toward offering)
    { targetLabel: "d", brokenByUpstream: {}, matched: false, nowBroken: false, sinceFixed: false, nowRepairing: false }
  ]);
  assert.deepEqual(out.map((d) => d.targetLabel), ["a", "c", "d"]);
});

test("formatDependentOutcome corrects a stale busy line when the item is now broken", () => {
  assert.equal(
    formatDependentOutcome({ targetLabel: "b", busy: true, nowBroken: true }),
    "b: was busy during this edit's re-check -- now broken."
  );
  assert.equal(
    formatDependentOutcome({ targetLabel: "b", status: "reverified", attributed: true, nowBroken: true }),
    "b: re-checked, still valid -- now broken."
  );
});
