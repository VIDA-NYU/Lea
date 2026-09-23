import assert from "node:assert/strict";
import test from "node:test";
import {
  buildLeanPaneManifest,
  hashLeanPaneSource
} from "../shared/leanPaneManifest.mjs";
import { parseTargets } from "../shared/theoremParser.mjs";

test("builds inventory from every marked tex file without requiring input/include", () => {
  const manifest = buildLeanPaneManifest({
    overleafProjectId: "project-1",
    activePath: "main.tex",
    files: [
      {
        path: "sections/defs.tex",
        content: [
          "\\begin{definition}[Locally finite family]\\label{def:locally-finite}",
          "% lea: define label=locally_finite_family",
          "A family $F$ is locally finite.",
          "\\end{definition}"
        ].join("\n")
      },
      {
        path: "main.tex",
        content: [
          "\\documentclass{article}",
          "\\begin{theorem}[Compactness criterion]\\label{thm:compactness}",
          "% lea: formalize label=compactness_criterion",
          "Every open cover has a finite subcover.",
          "\\end{theorem}",
          "\\input{sections/defs}"
        ].join("\n")
      },
      {
        path: "supp.tex",
        content: [
          "\\begin{lemma}\\label{lem:supp}",
          "% lea: formalize label=supplemental_lemma",
          "A supplemental result.",
          "\\end{lemma}"
        ].join("\n")
      }
    ]
  });

  assert.equal(manifest.rootFile, "");
  assert.equal(manifest.items.length, 3);
  assert.deepEqual(manifest.items.map((item) => item.label), ["compactness_criterion", "locally_finite_family", "supplemental_lemma"]);
  assert.deepEqual(manifest.items.map((item) => item.sourceFile), ["main.tex", "sections/defs.tex", "supp.tex"]);
  assert.deepEqual(manifest.items.map((item) => item.latexLabel), ["thm:compactness", "def:locally-finite", "lem:supp"]);
  assert.deepEqual(manifest.items.map((item) => item.documentOrder), [0, 1, 2]);
  assert.equal(manifest.items[0].leanDeclarationName, "compactness_criterion");
  assert.equal(manifest.items[1].leanKind, "def");
});

test("omits environments without Lea comment labels and preserves source metadata", () => {
  const source = [
    "\\begin{lemma}",
    "No label.",
    "\\end{lemma}",
    "\\begin{corollary}\\label{cor:main}",
    "% lea: formalize label=main_corollary",
    "A consequence of \\emph{the theorem}.",
    "\\end{corollary}"
  ].join("\n");
  const manifest = buildLeanPaneManifest({
    files: [{ path: "main.tex", content: source }]
  });

  assert.equal(manifest.items.length, 1);
  const [item] = manifest.items;
  assert.equal(item.kind, "corollary");
  assert.equal(item.label, "main_corollary");
  assert.equal(item.latexLabel, "cor:main");
  assert.equal(item.sourceFile, "main.tex");
  assert.equal(item.sourceStartLine, 4);
  assert.equal(item.sourceEndLine, 7);
  assert.equal(item.sourceStartOffset, source.indexOf("\\begin{corollary}"));
  assert.equal(item.sourceHash, hashLeanPaneSource("A consequence of \\emph{the theorem}."));
  assert.equal(item.naturalLanguageLatex, "A consequence of \\emph{the theorem}.");
  assert.equal(item.naturalLanguageRendered, "A consequence of the theorem.");
});

test("does not inventory LaTeX-labeled environments without Lea markers", () => {
  const manifest = buildLeanPaneManifest({
    files: [{
      path: "main.tex",
      content: [
        "\\begin{theorem}\\label{thm:plain}Plain theorem.\\end{theorem}",
        "\\begin{definition}\\label{def:plain}Plain definition.\\end{definition}"
      ].join("\n")
    }]
  });

  assert.equal(manifest.items.length, 0);
});

test("source hashes match the formalize target parser for the same block", () => {
  // Guards the staleness mechanism: the pane's item.sourceHash and the formalize
  // path's target.sourceHash must hash byte-identical text, or every formalized
  // item would render permanently stale (or never stale).
  const source = [
    "\\documentclass{article}",
    "\\begin{theorem}[Compactness criterion]\\label{thm:c}",
    "% lea: formalize label=compactness_criterion uses={finite_subcover}",
    "Every open cover of a compact space has a finite subcover.",
    "\\end{theorem}",
    "\\begin{definition}\\label{def:lf}",
    "% lea: define label=locally_finite",
    "A family is \\emph{locally finite} if every point has a neighborhood",
    "meeting finitely many members.",
    "\\end{definition}"
  ].join("\n");

  const manifest = buildLeanPaneManifest({ files: [{ path: "main.tex", content: source }] });
  const targets = parseTargets(source);

  assert.equal(manifest.items.length, 2);
  for (const item of manifest.items) {
    const target = targets.find((candidate) => candidate.targetLabel === item.label);
    assert.ok(target, `expected a formalize target for ${item.label}`);
    assert.equal(item.sourceHash, target.sourceHash);
  }
});

test("carries marker metadata so pane items can be formalized", () => {
  const manifest = buildLeanPaneManifest({
    files: [{
      path: "main.tex",
      content: [
        "\\begin{theorem}\\label{thm:c}",
        "% lea: formalize label=compactness uses={finite_subcover} context={Start from the cover.}",
        "Every open cover has a finite subcover.",
        "\\end{theorem}",
        // A bare \\label with no Lea marker is not a formalize target (and is omitted).
        "\\begin{lemma}\\label{lem:plain}Plain.\\end{lemma}"
      ].join("\n")
    }]
  });

  assert.equal(manifest.items.length, 1);
  const [item] = manifest.items;
  assert.equal(item.formalizable, true);
  assert.deepEqual(item.targetUses, ["finite_subcover"]);
  assert.equal(item.targetContext, "Start from the cover.");
});

test("label-free theorem and definition appear as runnable pane items", () => {
  const source = [
    "\\begin{theorem}\\label{thm:base}",
    "% lea: formalize",
    "Base statement.",
    "\\end{theorem}",
    "\\begin{definition}",
    "% lea: define",
    "A useful definition.",
    "\\end{definition}"
  ].join("\n");
  const manifest = buildLeanPaneManifest({ files: [{ path: "main.tex", content: source }] });
  assert.equal(manifest.items.length, 2);
  assert.deepEqual(manifest.items.map((item) => item.formalizable), [true, true]);
  assert.deepEqual(manifest.items.map((item) => item.labelSource), ["generated", "generated"]);
  assert.equal(manifest.items[0].title, "thm:base");
  assert.equal(manifest.items[1].title, "A useful definition.");
  assert.deepEqual(manifest.diagnostics, []);
});

test("marks items with a malformed marker as not formalizable", () => {
  const manifest = buildLeanPaneManifest({
    files: [{
      path: "main.tex",
      content: [
        // `define` marker inside a theorem environment is an environment_mismatch:
        // the pane still lists the item, but it is not a runnable formalize target.
        "\\begin{theorem}\\label{thm:x}",
        "% lea: define label=mismatch",
        "A statement.",
        "\\end{theorem}"
      ].join("\n")
    }]
  });

  assert.equal(manifest.items.length, 1);
  assert.equal(manifest.items[0].formalizable, false);
  assert.deepEqual(manifest.items[0].targetUses, []);
});

test("makes ids unique for duplicate kind+label items", () => {
  const manifest = buildLeanPaneManifest({
    files: [{
      path: "main.tex",
      content: [
        "\\begin{theorem}\\label{thm:a}\n% lea: formalize label=dup\nA.\\end{theorem}",
        "\\begin{theorem}\\label{thm:b}\n% lea: formalize label=dup\nB.\\end{theorem}"
      ].join("\n")
    }]
  });

  assert.equal(manifest.items.length, 2);
  assert.notEqual(manifest.items[0].id, manifest.items[1].id);
});

test("inventories a tag-marked item in a custom (non-allowlisted) environment", () => {
  const manifest = buildLeanPaneManifest({
    files: [{
      path: "main.tex",
      content: [
        "\\usepackage{lea-tags}",
        "\\begin{document}",
        "\\begin{claim}\\label{clm:foo}",
        "\\leatheorem{label=foo_claim, uses={bar}, context={hint}}",
        "Statement text.",
        "\\end{claim}",
        "\\begin{fact}",
        "\\leadefinition{label=even_nat}",
        "A definition body.",
        "\\end{fact}",
        "\\end{document}"
      ].join("\n")
    }]
  });

  assert.equal(manifest.items.length, 2);
  const [claim, fact] = manifest.items;
  assert.equal(claim.kind, "claim");
  assert.equal(claim.label, "foo_claim");
  assert.equal(claim.leanKind, "theorem");
  assert.equal(claim.formalizable, true);
  assert.deepEqual(claim.targetUses, ["bar"]);
  assert.equal(claim.targetContext, "hint");

  assert.equal(fact.kind, "fact");
  assert.equal(fact.label, "even_nat");
  assert.equal(fact.leanKind, "def");
  assert.equal(fact.formalizable, true);
});

test("tag-marked and comment-marked items in allowlisted environments are equivalent", () => {
  const manifestFor = (marker) => buildLeanPaneManifest({
    files: [{
      path: "main.tex",
      content: [
        "\\usepackage{lea-tags}",
        "\\begin{document}",
        "\\begin{theorem}\\label{thm:x}",
        marker,
        "A statement.",
        "\\end{theorem}",
        "\\end{document}"
      ].join("\n")
    }]
  });

  const tagManifest = manifestFor("\\leatheorem{label=x, uses={y}, context={hint}}");
  const commentManifest = manifestFor("% lea: formalize label=x uses={y} context={hint}");

  for (const key of ["label", "kind", "leanKind", "formalizable", "targetUses", "targetContext", "naturalLanguageLatex"]) {
    assert.deepEqual(tagManifest.items[0][key], commentManifest.items[0][key], `expected ${key} to match`);
  }
});

test("a malformed tag still surfaces as a non-formalizable pane item", () => {
  const manifest = buildLeanPaneManifest({
    files: [{
      path: "main.tex",
      content: [
        "\\begin{claim}\\label{clm:x}",
        // kind=bogus is invalid, but the label is still loosely recoverable.
        "\\lea{kind=bogus, label=mismatch}",
        "A statement.",
        "\\end{claim}"
      ].join("\n")
    }]
  });

  assert.equal(manifest.items.length, 1);
  assert.equal(manifest.items[0].label, "mismatch");
  assert.equal(manifest.items[0].formalizable, false);
});

test("inventories a standalone (body-argument) tag with no enclosing environment", () => {
  const manifest = buildLeanPaneManifest({
    files: [{
      path: "main.tex",
      content: [
        "\\usepackage{lea-tags}",
        "\\begin{document}",
        "\\leatheorem{label=pythagorean, uses={right_triangle}}",
        "{In a right triangle, the square of the hypotenuse equals the sum of the",
        "squares of the other two sides.}",
        "",
        "\\begin{claim}\\label{clm:foo}",
        "\\leatheorem{label=foo_claim}",
        "Environment-based statement.",
        "\\end{claim}",
        "\\end{document}"
      ].join("\n")
    }]
  });

  assert.equal(manifest.items.length, 2);
  // Source order, not "environment items first": the standalone tag appears
  // earlier in the file than the claim environment, so it must come first.
  const [standalone, claim] = manifest.items;
  assert.equal(standalone.label, "pythagorean");
  assert.equal(standalone.kind, "leatheorem");
  assert.equal(standalone.leanKind, "theorem");
  assert.equal(standalone.formalizable, true);
  assert.deepEqual(standalone.targetUses, ["right_triangle"]);
  assert.equal(
    standalone.naturalLanguageLatex,
    "In a right triangle, the square of the hypotenuse equals the sum of the\nsquares of the other two sides."
  );
  assert.equal(standalone.documentOrder, 0);

  assert.equal(claim.label, "foo_claim");
  assert.equal(claim.documentOrder, 1);
});

test("reports duplicate labels without dropping items", () => {
  const manifest = buildLeanPaneManifest({
    files: [
      {
        path: "main.tex",
        content: [
          "\\begin{theorem}\\label{thm:a}\n% lea: formalize label=dup\nA.\\end{theorem}",
          "\\begin{lemma}\\label{lem:b}\n% lea: formalize label=dup\nB.\\end{lemma}"
        ].join("\n")
      }
    ]
  });

  assert.equal(manifest.items.length, 2);
  assert.equal(manifest.diagnostics[0].code, "duplicate_label");
  assert.equal(manifest.diagnostics[0].label, "dup");
});

// Regression test for a real false positive: a documentation file showing
// "% lea: ..." syntax examples inside a \begin{verbatim} block had that
// block picked up as its own phantom target (kind "verbatim"), using the
// first label found in its illustrative text -- colliding with the real,
// nested theorem the example was describing. LaTeX itself never treats "%"
// as a comment character inside verbatim-like environments, so a marker
// found there can never be real; these environments must be excluded
// outright, not merely produce an extra (duplicate-labeled) item.
test("a % lea: line inside a verbatim-like environment is never treated as a marker", () => {
  const manifest = buildLeanPaneManifest({
    files: [
      {
        path: "main.tex",
        content: [
          "\\begin{verbatim}",
          "\\begin{theorem}\\label{thm:example}",
          "% lea: formalize label=compactness_criterion",
          "Example text shown for documentation purposes only.",
          "\\end{theorem}",
          "\\end{verbatim}",
          "",
          "\\begin{theorem}\\label{thm:real}",
          "% lea: formalize label=compactness_criterion",
          "The real statement.",
          "\\end{theorem}"
        ].join("\n")
      }
    ]
  });

  // Only the real, non-verbatim theorem becomes an item -- no phantom
  // "verbatim" item, and therefore no duplicate_label diagnostic either.
  assert.equal(manifest.items.length, 1);
  assert.equal(manifest.items[0].kind, "theorem");
  assert.equal(manifest.items[0].label, "compactness_criterion");
  assert.deepEqual(manifest.diagnostics, []);
});

test("lstlisting, minted, and comment environments are excluded the same way", () => {
  for (const env of ["lstlisting", "minted", "comment", "verbatim*", "Verbatim"]) {
    const manifest = buildLeanPaneManifest({
      files: [
        {
          path: "main.tex",
          content: [
            `\\begin{${env}}`,
            "% lea: formalize label=should_not_appear",
            `\\end{${env}}`
          ].join("\n")
        }
      ]
    });
    assert.equal(manifest.items.length, 0, `expected no items for \\begin{${env}}`);
  }
});

// Regression test for a follow-on real false positive, found live after the
// fix above: excluding verbatim-like environments from *becoming* a target
// stopped that, but a legitimately real *outer* environment (here
// \begin{enumerate}, e.g. a numbered recipe step in a documentation file)
// that merely *contains* a nested verbatim example was still promoted to its
// own phantom item -- because extractLeaMarkerLabel searched a plain
// substring of the ORIGINAL content for that environment's body, which still
// literally contained the nested example's "% lea: ..." line. The fix
// (searching a masked slice instead) must leave the real theorem inside the
// enumerate step untouched while making the enumerate itself produce no item
// at all.
test("a real outer environment containing a nested verbatim marker example is not itself promoted to an item", () => {
  const manifest = buildLeanPaneManifest({
    files: [
      {
        path: "main.tex",
        content: [
          "\\begin{enumerate}",
          "  \\item Open the item, click Edit, replace with:",
          "\\begin{verbatim}",
          "\\begin{theorem}\\label{thm:example}",
          "% lea: formalize label=uses_locally_finite_computationally",
          "\\end{theorem}",
          "\\end{verbatim}",
          "  \\item Save.",
          "\\end{enumerate}",
          "",
          "\\begin{theorem}\\label{thm:real}",
          "% lea: formalize label=compactness_criterion",
          "The real statement.",
          "\\end{theorem}"
        ].join("\n")
      }
    ]
  });

  assert.equal(manifest.items.length, 1);
  assert.equal(manifest.items[0].kind, "theorem");
  assert.equal(manifest.items[0].label, "compactness_criterion");
  assert.deepEqual(manifest.diagnostics, []);
});
