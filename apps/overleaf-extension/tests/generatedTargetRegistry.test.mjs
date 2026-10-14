import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { reconcileGeneratedItems } from "../companion/generatedTargetRegistry.mjs";
import { handleLeanPaneManifest, handleResolveTargets } from "../companion/server.mjs";

const theorem = (statement, label = "") => [
  `\\begin{theorem}${label ? `\\label{${label}}` : ""}`,
  "% lea: formalize",
  statement,
  "\\end{theorem}"
].join("\n");

test("editor and pane resolve the same generated ID and retain it after an edit and restart", async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "lea-generated-label-"));
  const generatedTargetsPath = path.join(dir, "generated-targets.json");
  const state = { settings: {}, jobs: {}, generatedTargetsPath, generatedTargets: { version: 1, projects: {} } };
  const source = theorem("Every even square is even.", "thm:even-square");
  const live = await handleResolveTargets({ overleafProjectId: "project-1", sourceFile: "main.tex", source }, state);
  const label = live.body.targets[0].targetLabel;
  assert.match(label, /^lea_auto_/);
  const pane = await handleLeanPaneManifest({
    overleafProjectId: "project-1", files: [{ path: "main.tex", content: source }]
  }, state);
  assert.equal(pane.body.items[0].label, label);
  assert.equal(pane.body.items[0].formalizable, true);

  const restarted = {
    settings: {}, jobs: {}, generatedTargetsPath,
    generatedTargets: JSON.parse(await fs.readFile(generatedTargetsPath, "utf8"))
  };
  const edited = theorem("Every even square remains even after rewriting.", "thm:even-square");
  const after = await handleResolveTargets({ overleafProjectId: "project-1", sourceFile: "main.tex", source: edited }, restarted);
  assert.equal(after.body.targets[0].targetLabel, label);
});

test("inserting a new label-free theorem does not transfer the earlier theorem's ID", () => {
  const registry = { version: 1, projects: {} };
  const old = [{
    label: "lea_auto_theorem_1", labelSource: "generated", targetKind: "theorem",
    sourceFile: "main.tex", sourceStartLine: 1, sourceHash: "old",
    naturalLanguageLatex: "Every even square is even."
  }];
  const initial = reconcileGeneratedItems(old, registry, "project-1");
  const after = reconcileGeneratedItems([
    { ...old[0], sourceHash: "new", naturalLanguageLatex: "All primes have a property." },
    { ...old[0], label: "lea_auto_theorem_2", sourceStartLine: 5 }
  ], registry, "project-1");
  assert.equal(after.items[1].label, initial.items[0].label);
  assert.notEqual(after.items[0].label, initial.items[0].label);
});

test("a small edit without a LaTeX label keeps the generated ID", () => {
  const registry = { version: 1, projects: {} };
  const original = {
    label: "lea_auto_theorem_old", labelSource: "generated", targetKind: "theorem",
    sourceFile: "main.tex", sourceStartLine: 3, sourceHash: "before",
    naturalLanguageLatex: "Every even natural number has an even square."
  };
  const first = reconcileGeneratedItems([original], registry, "project-1").items[0];
  const edited = reconcileGeneratedItems([{
    ...original, sourceHash: "after", sourceStartLine: 4,
    naturalLanguageLatex: "Every even natural number has an even cube."
  }], registry, "project-1").items[0];
  assert.equal(edited.label, first.label);
});

test("a complete rewrite at the same sole marker retains the ID for stale status", () => {
  const registry = { version: 1, projects: {} };
  const original = {
    label: "lea_auto_theorem_old", labelSource: "generated", targetKind: "theorem",
    sourceFile: "main.tex", sourceStartLine: 3, sourceHash: "before",
    naturalLanguageLatex: "Every even natural number has an even square."
  };
  const first = reconcileGeneratedItems([original], registry, "project-1").items[0];
  const rewritten = reconcileGeneratedItems([{
    ...original, sourceHash: "after", naturalLanguageLatex: "A completely different proposition."
  }], registry, "project-1").items[0];
  assert.equal(rewritten.label, first.label);
});

test("two identical unlabeled statements keep distinct IDs across refreshes", () => {
  const registry = { version: 1, projects: {} };
  const items = [1, 2].map((number) => ({
    label: `lea_auto_theorem_${number}`, labelSource: "generated", targetKind: "theorem",
    sourceFile: "main.tex", sourceStartLine: number * 5, sourceHash: "same",
    naturalLanguageLatex: "The same statement."
  }));
  const first = reconcileGeneratedItems(items, registry, "project-1").items.map((item) => item.label);
  const second = reconcileGeneratedItems(items, registry, "project-1").items.map((item) => item.label);
  assert.notEqual(first[0], first[1]);
  assert.deepEqual(second, first);
});

test("copying a statement to another file does not steal the original ID", () => {
  const registry = { version: 1, projects: {} };
  const original = {
    label: "lea_auto_theorem_original", labelSource: "generated", targetKind: "theorem",
    sourceFile: "z.tex", sourceStartLine: 1, sourceHash: "same",
    naturalLanguageLatex: "The same statement."
  };
  const id = reconcileGeneratedItems([original], registry, "project-1").items[0].label;
  const copied = { ...original, label: "lea_auto_theorem_copy", sourceFile: "a.tex" };
  const items = reconcileGeneratedItems([copied, original], registry, "project-1").items;
  assert.equal(items[1].label, id);
  assert.notEqual(items[0].label, id);
});

test("explicit labels keep their value and reserve it against generated collisions", () => {
  const registry = { version: 1, projects: {} };
  const result = reconcileGeneratedItems([
    { label: "lea_auto_theorem_1", labelSource: "explicit" },
    {
      label: "lea_auto_theorem_1", labelSource: "generated", targetKind: "theorem",
      sourceFile: "main.tex", sourceStartLine: 3, sourceHash: "h", naturalLanguageLatex: "A."
    }
  ], registry, "project-1");
  assert.equal(result.items[0].label, "lea_auto_theorem_1");
  assert.equal(result.items[1].label, "lea_auto_theorem_1_2");
});

test("historical explicit job labels are reserved for compatibility", () => {
  const result = reconcileGeneratedItems([{
    label: "lea_auto_theorem_1", labelSource: "generated", targetKind: "theorem",
    sourceFile: "main.tex", sourceStartLine: 1, sourceHash: "h", naturalLanguageLatex: "A."
  }], { version: 1, projects: {} }, "project-1", {
    reservedLabels: ["lea_auto_theorem_1"]
  });
  assert.equal(result.items[0].label, "lea_auto_theorem_1_2");
});

test("uses can resolve a unique LaTeX label while legacy Lea references remain unchanged", () => {
  const registry = { version: 1, projects: {} };
  const items = [
    {
      label: "lea_auto_theorem_1", labelSource: "generated", targetKind: "theorem",
      sourceFile: "main.tex", sourceStartLine: 1, sourceHash: "h1",
      latexLabel: "thm:base", naturalLanguageLatex: "Base result."
    },
    {
      label: "result", labelSource: "explicit", targetKind: "theorem",
      sourceFile: "main.tex", sourceStartLine: 5, sourceHash: "h2",
      targetUses: ["thm:base", "older_result"], naturalLanguageLatex: "Result."
    }
  ];
  const result = reconcileGeneratedItems(items, registry, "project-1");
  assert.deepEqual(result.items[1].targetUses, [result.items[0].label, "older_result"]);
});

test("live target resolution finds a generated dependency in another TeX file", async () => {
  const state = { generatedTargets: { version: 1, projects: {} } };
  const base = theorem("Base result.", "thm:base");
  const dependent = [
    "\\begin{theorem}",
    "% lea: formalize label=result uses={thm:base}",
    "Dependent result.",
    "\\end{theorem}"
  ].join("\n");
  const response = await handleResolveTargets({
    overleafProjectId: "project-1", sourceFile: "dependent.tex", source: dependent,
    files: [
      { path: "base.tex", content: base },
      { path: "dependent.tex", content: dependent }
    ]
  }, state);
  const baseId = state.generatedTargets.projects["project-1"].entries[0].id;
  assert.equal(response.body.targets[0].targetLabel, "result");
  assert.deepEqual(response.body.targets[0].targetUses, [baseId]);
});
