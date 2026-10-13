import assert from "node:assert/strict";
import test from "node:test";
import { parseTargetDocument, parseTargets } from "../shared/theoremParser.mjs";
import { stripLeaTargetText } from "../extension/targetParserCore.mjs";

const PACKAGE_LOADED = "\\usepackage{lea-tags}\n\\begin{document}\n";
const DOCUMENT_END = "\n\\end{document}";

function wrapDocument(body) {
  return `${PACKAGE_LOADED}${body}${DOCUMENT_END}`;
}

test("detects a tagged theorem inside a custom (non-allowlisted) environment", () => {
  const source = wrapDocument([
    "\\begin{claim}\\label{clm:foo}",
    "\\leatheorem{label=foo_claim, uses={bar}, context={Treat foo as a Nat predicate.}}",
    "Statement text.",
    "\\end{claim}"
  ].join("\n"));
  const result = parseTargetDocument(source);
  assert.equal(result.diagnostics.length, 0);
  assert.equal(result.targets.length, 1);
  const [target] = result.targets;
  assert.equal(target.targetKind, "theorem");
  assert.equal(target.targetLabel, "foo_claim");
  assert.deepEqual(target.targetUses, ["bar"]);
  assert.equal(target.targetContext, "Treat foo as a Nat predicate.");
  assert.equal(target.targetText, "Statement text.");
  assert.equal(target.latexEnvironment, "claim");
  assert.equal(target.latexLabel, "clm:foo");
  assert.equal(target.syntax, "tag");
});

test("each named wrapper implies its kind regardless of a conflicting kind= argument", () => {
  const cases = [
    ["leatheorem", "theorem"],
    ["lealemma", "theorem"],
    ["leaproposition", "theorem"],
    ["leacorollary", "theorem"],
    ["leadefinition", "definition"]
  ];
  for (const [command, expectedKind] of cases) {
    const source = wrapDocument([
      "\\begin{custom}",
      // A user mistakenly adding kind=... should not override the command name.
      `\\${command}{label=x, kind=bogus}`,
      "Body.",
      "\\end{custom}"
    ].join("\n"));
    const [target] = parseTargets(source);
    assert.equal(target.targetKind, expectedKind, `\\${command} should imply ${expectedKind}`);
  }
});

test("generic \\lea{...} reads kind= explicitly, defaulting to theorem", () => {
  const withKind = wrapDocument([
    "\\begin{fact}",
    "\\lea{kind=definition, label=even_nat, context={Use Nat parity.}}",
    "A natural number is even if there exists k with n = 2k.",
    "\\end{fact}"
  ].join("\n"));
  const [definitionTarget] = parseTargets(withKind);
  assert.equal(definitionTarget.targetKind, "definition");
  assert.equal(definitionTarget.targetLabel, "even_nat");

  const withoutKind = wrapDocument([
    "\\begin{fact}",
    "\\lea{label=default_kind}",
    "Body.",
    "\\end{fact}"
  ].join("\n"));
  const [theoremTarget] = parseTargets(withoutKind);
  assert.equal(theoremTarget.targetKind, "theorem");

  const badKind = parseTargetDocument(wrapDocument([
    "\\begin{fact}",
    "\\lea{kind=nonsense, label=x}",
    "Body.",
    "\\end{fact}"
  ].join("\n")));
  assert.equal(badKind.targets.length, 0);
  assert.equal(badKind.diagnostics[0].code, "unsupported_kind");
});

test("a tag with no enclosing environment is a missing_environment diagnostic", () => {
  const result = parseTargetDocument(wrapDocument("\\leatheorem{label=bare}\nFloating text."));
  assert.equal(result.targets.length, 0);
  assert.equal(result.diagnostics[0].code, "missing_environment");
  assert.match(result.diagnostics[0].message, /LaTeX environment/);
});

test("a malformed tag (unbalanced argument) is a malformed_tag diagnostic", () => {
  const result = parseTargetDocument(wrapDocument([
    "\\begin{theorem}",
    "\\leatheorem{label=foo",
    "A.",
    "\\end{theorem}"
  ].join("\n")));
  assert.equal(result.targets.length, 0);
  assert.equal(result.diagnostics.some((d) => d.code === "malformed_tag"), true);
});

test("a comment marker and a tag in the same environment is a cross-syntax duplicate_marker", () => {
  const result = parseTargetDocument(wrapDocument([
    "\\begin{theorem}\\label{thm:x}",
    "% lea: formalize label=foo",
    "\\leatheorem{label=foo2}",
    "A.",
    "\\end{theorem}"
  ].join("\n")));
  assert.equal(result.targets.length, 0);
  assert.equal(result.diagnostics.some((d) => d.code === "duplicate_marker"), true);
});

test("a tag in an allowlisted environment behaves identically to the comment-marker path", () => {
  const tagSource = wrapDocument([
    "\\begin{theorem}\\label{thm:even-square}",
    "\\leatheorem{label=even_square, uses={even_def}, context={Use the parity definition first.}}",
    "If $n$ is even, then $n^2$ is even.",
    "\\end{theorem}"
  ].join("\n"));
  const [tagTarget] = parseTargets(tagSource);

  const commentSource = [
    "\\begin{theorem}\\label{thm:even-square}",
    "% lea: formalize label=even_square uses={even_def} context={Use the parity definition first.}",
    "If $n$ is even, then $n^2$ is even.",
    "\\end{theorem}"
  ].join("\n");
  const [commentTarget] = parseTargets(commentSource);

  for (const key of ["targetKind", "targetLabel", "targetText", "targetUses", "targetContext", "latexEnvironment", "latexLabel"]) {
    assert.deepEqual(tagTarget[key], commentTarget[key], `expected ${key} to match between tag and comment syntax`);
  }
  assert.equal(tagTarget.syntax, "tag");
  assert.equal(commentTarget.syntax, "comment");
});

test("reports tag_package_not_loaded when no usepackage/definition is present, and suppresses it when one is", () => {
  const unloaded = parseTargetDocument([
    "\\begin{claim}",
    "\\leatheorem{label=foo}",
    "A.",
    "\\end{claim}"
  ].join("\n"));
  assert.equal(unloaded.diagnostics.some((d) => d.code === "tag_package_not_loaded"), true);

  const viaUsepackage = parseTargetDocument(wrapDocument([
    "\\begin{claim}",
    "\\leatheorem{label=foo}",
    "A.",
    "\\end{claim}"
  ].join("\n")));
  assert.equal(viaUsepackage.diagnostics.some((d) => d.code === "tag_package_not_loaded"), false);

  const viaInlineFallback = parseTargetDocument([
    "\\RequirePackage{xparse}",
    "\\NewDocumentCommand{\\lea}{m}{}",
    "\\NewDocumentCommand{\\leatheorem}{m}{}",
    "\\begin{document}",
    "\\begin{claim}",
    "\\leatheorem{label=foo}",
    "A.",
    "\\end{claim}",
    "\\end{document}"
  ].join("\n"));
  assert.equal(viaInlineFallback.diagnostics.some((d) => d.code === "tag_package_not_loaded"), false);
});

test("does not false-positive on the package's own \\NewDocumentCommand{\\lea}{m}{} definition", () => {
  // Regression: \NewDocumentCommand{\lea}{m}{} contains the literal substring
  // "\lea}", which earlier matched as a malformed tag invocation in the preamble.
  const source = [
    "\\NeedsTeXFormat{LaTeX2e}",
    "\\ProvidesPackage{lea-tags}[2026/07/01 v0.1 Lea inline formalization tags]",
    "\\RequirePackage{xparse}",
    "\\NewDocumentCommand{\\lea}{m}{}",
    "\\NewDocumentCommand{\\leatheorem}{m}{}",
    "\\NewDocumentCommand{\\lealemma}{m}{}",
    "\\NewDocumentCommand{\\leaproposition}{m}{}",
    "\\NewDocumentCommand{\\leacorollary}{m}{}",
    "\\NewDocumentCommand{\\leadefinition}{m}{}",
    "\\begin{document}",
    "\\begin{theorem}",
    "\\leatheorem{label=foo}",
    "A.",
    "\\end{theorem}",
    "\\end{document}"
  ].join("\n");
  const result = parseTargetDocument(source);
  assert.equal(result.diagnostics.length, 0);
  assert.equal(result.targets.length, 1);
  assert.equal(result.targets[0].targetLabel, "foo");
});

test("flags an unusual enclosing environment without blocking the target", () => {
  const result = parseTargetDocument(wrapDocument([
    "\\begin{figure}",
    "\\leatheorem{label=oops}",
    "Caption-ish text.",
    "\\end{figure}"
  ].join("\n")));
  assert.equal(result.targets.length, 1);
  assert.equal(result.diagnostics.some((d) => d.code === "suspicious_environment"), true);
});

test("strips tag calls (including nested braces in context=) from target text", () => {
  const body = "\\leatheorem{label=foo, context={Use $f(x)=\\{1,2\\}$ as a hint.}}\nActual statement.";
  assert.equal(stripLeaTargetText(body), "Actual statement.");
});

test("ignores unmarked custom environments (tags are still opt-in)", () => {
  assert.deepEqual(parseTargets(wrapDocument("\\begin{claim}\nUnmarked.\n\\end{claim}")), []);
});

// --- Standalone form: \leatheorem{...}{Statement...} needs no enclosing
// environment at all. See docs/FEATURE-overleaf-inline-lea-tags.md
// ("Standalone form") -- this is the direct answer to "does the tag have to
// be inside a block?": with a body argument, no.

test("a standalone tag (body as second argument) needs no enclosing environment", () => {
  const source = wrapDocument(
    "\\leatheorem{label=pythagorean, uses={right_triangle}}{If $n$ is even, then $n^2$ is even.}"
  );
  const result = parseTargetDocument(source);
  assert.equal(result.diagnostics.length, 0);
  assert.equal(result.targets.length, 1);
  const [target] = result.targets;
  assert.equal(target.targetLabel, "pythagorean");
  assert.deepEqual(target.targetUses, ["right_triangle"]);
  assert.equal(target.targetText, "If $n$ is even, then $n^2$ is even.");
  assert.equal(target.latexEnvironment, "leatheorem");
  assert.equal(target.syntax, "tag");
});

test("a standalone tag's body argument can span multiple lines and paragraphs", () => {
  const source = wrapDocument([
    "\\leatheorem{label=multi_line}",
    "{In a right triangle, the square of the hypotenuse equals the sum of the",
    "squares of the other two sides.}"
  ].join("\n"));
  const [target] = parseTargets(source);
  assert.equal(
    target.targetText,
    "In a right triangle, the square of the hypotenuse equals the sum of the\nsquares of the other two sides."
  );
});

test("the generic \\lea{...}{...} standalone form respects kind=", () => {
  const source = wrapDocument(
    "\\lea{kind=definition, label=even_nat}{A natural number is even if there exists k with n = 2k.}"
  );
  const [target] = parseTargets(source);
  assert.equal(target.targetKind, "definition");
  assert.equal(target.latexEnvironment, "lea");
});

test("a standalone tag with an unterminated body argument is a malformed_tag diagnostic", () => {
  const result = parseTargetDocument(wrapDocument("\\leatheorem{label=foo}{unterminated body"));
  assert.equal(result.targets.length, 0);
  assert.equal(result.diagnostics.some((d) => d.code === "malformed_tag"), true);
});

test("a standalone tag strips a nested \\label{...} from its own body", () => {
  const source = wrapDocument("\\leatheorem{label=foo}{\\label{thm:foo} Actual statement.}");
  const [target] = parseTargets(source);
  assert.equal(target.targetText, "Actual statement.");
  assert.equal(target.latexLabel, "thm:foo");
});

test("a tag with no body argument still requires an enclosing environment, with a message mentioning both options", () => {
  const result = parseTargetDocument(wrapDocument("\\leatheorem{label=bare}\nFloating text."));
  assert.equal(result.targets.length, 0);
  assert.equal(result.diagnostics[0].code, "missing_environment");
  assert.match(result.diagnostics[0].message, /LaTeX environment/);
  assert.match(result.diagnostics[0].message, /second argument/);
});

test("standalone-tag detection matches real TeX argument scanning, verified against pdflatex separately: " +
  "whitespace (including a blank line) between metadata and an unrelated brace group is absorbed as the body", () => {
  // This mirrors the exact fixture compiled with pdflatex during development
  // (see docs/PLAN-overleaf-inline-lea-tags.md) -- documenting the behavior
  // here as a test, not just prose, so a future change can't silently drift
  // from what Overleaf will actually compile.
  const source = wrapDocument([
    "\\leatheorem{label=foo}",
    "",
    "{\\bf This stray group has nothing but whitespace before it.}"
  ].join("\n"));
  const [target] = parseTargets(source);
  assert.equal(target.targetText, "\\bf This stray group has nothing but whitespace before it.");
});

test("text between a tag and a later brace group prevents accidental absorption", () => {
  const source = wrapDocument([
    "\\leatheorem{label=foo}",
    "Some ordinary prose follows on the next line, not a body argument.",
    "{\\bf This bold group should not be swallowed as foo's body.}"
  ].join("\n"));
  const result = parseTargetDocument(source);
  // No body argument was found (prose intervened), and there's no enclosing
  // environment either, so this is a missing_environment diagnostic -- an
  // honest failure, not a silent misattribution of the bold group's text.
  assert.equal(result.targets.length, 0);
  assert.equal(result.diagnostics[0].code, "missing_environment");
});

// --- Code-block form: \begin{leacode}{...}...\end{leacode} renders its body
// as a Lean listing, and that same code is sent to Lea verbatim as the target
// text -- exactly like every other tag's statement, just typeset as code. See
// docs/FEATURE-overleaf-inline-lea-tags.md ("Code-block form").

test("a leacode block is a formalizable theorem target whose body is the raw code", () => {
  const source = wrapDocument([
    "\\begin{leacode}{label=add_zero, uses={my_lemma}, context={Use simp.}}",
    "theorem add_zero (n : Nat) : n + 0 = n := by",
    "  simp",
    "\\end{leacode}"
  ].join("\n"));
  const result = parseTargetDocument(source);
  assert.equal(result.diagnostics.length, 0);
  assert.equal(result.targets.length, 1);
  const [target] = result.targets;
  assert.equal(target.targetKind, "theorem");
  assert.equal(target.targetLabel, "add_zero");
  assert.deepEqual(target.targetUses, ["my_lemma"]);
  assert.equal(target.targetContext, "Use simp.");
  assert.equal(target.targetText, "theorem add_zero (n : Nat) : n + 0 = n := by\n  simp");
  assert.equal(target.latexEnvironment, "leacode");
  assert.equal(target.syntax, "leacode");
});

test("a leacode block preserves Lean's catcode-hostile characters verbatim (_ \\ { } ->)", () => {
  const code = "theorem foo {p : Nat -> Prop} : \\forall x, p x_0 = p x_0 := by rfl";
  const source = wrapDocument([
    "\\begin{leacode}{label=foo}",
    code,
    "\\end{leacode}"
  ].join("\n"));
  const [target] = parseTargets(source);
  assert.equal(target.targetText, code);
});

test("leacode honors kind= like the generic \\lea{...}, defaulting to theorem", () => {
  const asDefinition = wrapDocument([
    "\\begin{leacode}{kind=definition, label=Even}",
    "def Even (n : Nat) : Prop := \\exists k, n = 2 * k",
    "\\end{leacode}"
  ].join("\n"));
  const [defTarget] = parseTargets(asDefinition);
  assert.equal(defTarget.targetKind, "definition");

  const asTheorem = wrapDocument([
    "\\begin{leacode}{label=default_kind}",
    "theorem default_kind : True := trivial",
    "\\end{leacode}"
  ].join("\n"));
  const [thmTarget] = parseTargets(asTheorem);
  assert.equal(thmTarget.targetKind, "theorem");
});

test("a leacode block needs no enclosing environment and never flags suspicious/mismatch", () => {
  const source = wrapDocument([
    "\\begin{figure}",
    "\\begin{leacode}{label=inside_figure}",
    "theorem inside_figure : True := trivial",
    "\\end{leacode}",
    "\\end{figure}"
  ].join("\n"));
  const result = parseTargetDocument(source);
  // Its own body span is the target -- the enclosing figure is irrelevant, so
  // no suspicious_environment (that warning is tag-only) and no mismatch.
  assert.equal(result.targets.length, 1);
  assert.equal(result.diagnostics.length, 0);
  assert.equal(result.targets[0].latexEnvironment, "leacode");
});

test("a leacode block with an unterminated \\end is a malformed_tag diagnostic", () => {
  const result = parseTargetDocument(wrapDocument([
    "\\begin{leacode}{label=foo}",
    "theorem foo : True := trivial"
  ].join("\n")));
  assert.equal(result.targets.length, 0);
  assert.equal(result.diagnostics.some((d) => d.code === "malformed_tag"), true);
});

test("a leacode block without its required metadata argument is malformed", () => {
  const result = parseTargetDocument(wrapDocument([
    "\\begin{leacode}",
    "theorem foo : True := trivial",
    "\\end{leacode}"
  ].join("\n")));
  assert.equal(result.targets.length, 0);
  assert.equal(result.diagnostics.some((d) => d.code === "malformed_tag"), true);
});

test("empty metadata on a leacode block generates a label", () => {
  const result = parseTargetDocument(wrapDocument([
    "\\begin{leacode}{}",
    "theorem foo : True := trivial",
    "\\end{leacode}"
  ].join("\n")));
  assert.equal(result.targets.length, 1);
  assert.equal(result.targets[0].labelSource, "generated");
  assert.equal(result.diagnostics.some((d) => d.code === "missing_label"), false);
});

test("label-free inline and standalone tags are valid targets", () => {
  const result = parseTargetDocument(wrapDocument([
    "\\begin{claim}\\label{clm:first}",
    "\\leatheorem{}",
    "First statement.",
    "\\end{claim}",
    "\\leadefinition{}{Second statement.}"
  ].join("\n")));
  assert.equal(result.targets.length, 2);
  assert.deepEqual(result.targets.map((target) => target.labelSource), ["generated", "generated"]);
  assert.deepEqual(result.targets.map((target) => target.targetKind), ["theorem", "definition"]);
});

test("leacode triggers tag_package_not_loaded, and an inline lstnewenvironment suppresses it", () => {
  const unloaded = parseTargetDocument([
    "\\begin{document}",
    "\\begin{leacode}{label=foo}",
    "theorem foo : True := trivial",
    "\\end{leacode}",
    "\\end{document}"
  ].join("\n"));
  assert.equal(unloaded.diagnostics.some((d) => d.code === "tag_package_not_loaded"), true);

  const inlineDefined = parseTargetDocument([
    "\\usepackage{listings}",
    "\\lstnewenvironment{leacode}[1]{}{}",
    "\\begin{document}",
    "\\begin{leacode}{label=foo}",
    "theorem foo : True := trivial",
    "\\end{leacode}",
    "\\end{document}"
  ].join("\n"));
  assert.equal(inlineDefined.diagnostics.some((d) => d.code === "tag_package_not_loaded"), false);
});

test("a leacode block shown inside a verbatim documentation example is not a real target", () => {
  const source = wrapDocument([
    "\\begin{verbatim}",
    "\\begin{leacode}{label=example_only}",
    "theorem example_only : True := trivial",
    "\\end{leacode}",
    "\\end{verbatim}"
  ].join("\n"));
  assert.deepEqual(parseTargets(source), []);
});
