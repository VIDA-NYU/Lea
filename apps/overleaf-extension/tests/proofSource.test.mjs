import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import test from "node:test";
import { associateProofSources, buildFormalizationSourceBundle } from "../shared/proofSource.mjs";
import { parseTargets } from "../shared/theoremParser.mjs";
import { sourceHashInputs } from "../extension/sourceIdentityCore.mjs";

test("version-2 source hashes match the cross-language golden fixture", async () => {
  const fixture = JSON.parse(await fs.readFile(new URL("./fixtures/sourceIdentityV2.json", import.meta.url), "utf8"));
  const hash = (value) => createHash("sha256").update(JSON.stringify(value)).digest("hex");
  const { evidence, identity } = sourceHashInputs(fixture.bundle);
  assert.equal(hash(evidence), fixture.bundleHash);
  assert.equal(hash(identity), fixture.sourceIdentityHash);
});

function targetsIn(content, sourceFile = "main.tex") {
  return parseTargets(content).map((target) => ({ ...target, sourceFile }));
}

test("associates an adjacent proof with the preceding target", () => {
  const content = String.raw`\begin{theorem}
% lea: formalize label=main_result
Every even integer is divisible by two.
\end{theorem}
\begin{proof}
Write the integer as $2k$.
\end{proof}`;
  const result = associateProofSources({ targets: targetsIn(content), files: [{ path: "main.tex", content }] });
  assert.equal(result.targets[0].proofAssociation.status, "associated");
  assert.equal(result.targets[0].proofAssociation.method, "adjacent");
  assert.match(result.targets[0].sourceProof, /2k/);
});

test("an explicit proof-for marker can associate a proof across files", () => {
  const statement = String.raw`\begin{theorem}
% lea: formalize label=main_result
Statement.
\end{theorem}`;
  const proof = String.raw`\begin{proof}
% lea: proof-for=main_result
The source argument.
\end{proof}`;
  const result = associateProofSources({
    targets: targetsIn(statement, "statement.tex"),
    files: [{ path: "statement.tex", content: statement }, { path: "proofs.tex", content: proof }]
  });
  assert.equal(result.targets[0].proofAssociation.method, "explicit");
  assert.equal(result.targets[0].proofAssociation.sourceFile, "proofs.tex");
  assert.doesNotMatch(result.targets[0].sourceProof, /proof-for/);
});

test("a generated target can associate a proof by its unique LaTeX label", () => {
  const statement = String.raw`\begin{theorem}\label{thm:base}
% lea: formalize
Statement.
\end{theorem}`;
  const proof = String.raw`\begin{proof}
% lea: proof-for={thm:base}
The source argument.
\end{proof}`;
  const result = associateProofSources({
    targets: targetsIn(statement, "statement.tex"),
    files: [{ path: "statement.tex", content: statement }, { path: "proofs.tex", content: proof }]
  });
  assert.equal(result.targets[0].proofAssociation.method, "explicit");
  assert.equal(result.targets[0].sourceProof, "The source argument.");
});

test("duplicate LaTeX labels cannot claim an explicit proof", () => {
  const statement = String.raw`\begin{theorem}\label{thm:duplicate}
% lea: formalize
Statement.
\end{theorem}`;
  const proof = String.raw`\begin{proof}
% lea: proof-for={thm:duplicate}
An argument.
\end{proof}`;
  const result = associateProofSources({
    targets: [
      ...targetsIn(statement, "a.tex"),
      ...targetsIn(statement, "b.tex")
    ],
    files: [
      { path: "a.tex", content: statement },
      { path: "b.tex", content: statement },
      { path: "proof.tex", content: proof }
    ]
  });
  assert.deepEqual(result.targets.map((target) => target.proofAssociation.status), ["missing", "missing"]);
  assert.ok(result.diagnostics.some((diagnostic) => diagnostic.code === "unresolved_proof_association"));
});

test("does not infer adjacency across an intervening target", () => {
  const content = String.raw`\begin{theorem}
% lea: formalize label=first
First.
\end{theorem}
\begin{theorem}
% lea: formalize label=second
Second.
\end{theorem}
\begin{proof}Second proof.\end{proof}`;
  const result = associateProofSources({ targets: targetsIn(content), files: [{ path: "main.tex", content }] });
  assert.equal(result.targets[0].proofAssociation.status, "missing");
  assert.equal(result.targets[1].proofAssociation.status, "associated");
});

test("does not infer adjacency across an unmarked statement environment", () => {
  const content = String.raw`\begin{theorem}
% lea: formalize label=first
First.
\end{theorem}
\begin{theorem}
An unmarked intervening theorem.
\end{theorem}
\begin{proof}Proof of the intervening theorem.\end{proof}`;
  const result = associateProofSources({ targets: targetsIn(content), files: [{ path: "main.tex", content }] });
  assert.equal(result.targets[0].proofAssociation.status, "missing");
});

test("reports multiple explicit proof associations as ambiguous", () => {
  const statement = String.raw`\begin{theorem}
% lea: formalize label=main_result
Statement.
\end{theorem}`;
  const proof = (value) => String.raw`\begin{proof}
% lea: proof-for=main_result
${value}
\end{proof}`;
  const result = associateProofSources({
    targets: targetsIn(statement),
    files: [
      { path: "main.tex", content: statement },
      { path: "a.tex", content: proof("A") },
      { path: "b.tex", content: proof("B") }
    ]
  });
  assert.equal(result.targets[0].proofAssociation.status, "ambiguous");
  assert.equal(result.targets[0].sourceProof, "");
});

test("source bundle hash changes when only the proof changes", () => {
  const base = {
    targetLabel: "main_result",
    targetKind: "theorem",
    targetText: "Statement.",
    sourceFile: "main.tex",
    proofAssociation: { status: "associated", method: "adjacent", sourceFile: "main.tex" }
  };
  const first = buildFormalizationSourceBundle({ ...base, sourceProof: "Argument A." });
  const second = buildFormalizationSourceBundle({ ...base, sourceProof: "Argument B." });
  assert.notEqual(first.bundleHash, second.bundleHash);
});

test("evaluator evidence changes do not make the target source identity stale", () => {
  const target = {
    targetLabel: "main_result",
    targetKind: "theorem",
    targetText: "Statement.",
    sourceFile: "main.tex",
    sourceProof: "The argument.",
    proofAssociation: { status: "associated", method: "adjacent", sourceFile: "main.tex" }
  };
  const first = buildFormalizationSourceBundle(target, {
    relevantSource: [{ kind: "context", path: "main.tex", content: "First excerpt." }],
    mirror: { revision: "a".repeat(64), mirroredFileCount: 1, verified: true }
  });
  const second = buildFormalizationSourceBundle(target, {
    relevantSource: [{ kind: "context", path: "main.tex", content: "Updated excerpt." }],
    mirror: { revision: "b".repeat(64), mirroredFileCount: 2, verified: true }
  });

  assert.notEqual(first.bundleHash, second.bundleHash);
  assert.equal(first.sourceIdentityHash, second.sourceIdentityHash);
});

test("definitions do not acquire a following proof environment", () => {
  const source = String.raw`\begin{definition}
% lea: define label=base
A base point.\end{definition}
\begin{proof}This prose belongs elsewhere.\end{proof}`;
  const target = parseTargets(source)[0];
  const result = associateProofSources({
    targets: [{ ...target, sourceFile: "main.tex" }],
    files: [{ path: "main.tex", content: source }]
  });
  assert.equal(result.targets[0].proofAssociation.status, "not_applicable");
  assert.equal(result.diagnostics.length, 0);
});

test("reports an explicit proof association that names no target", () => {
  const source = String.raw`\begin{proof}
% lea: proof-for=missing_target
Proof text.
\end{proof}`;
  const result = associateProofSources({ targets: [], files: [{ path: "proofs.tex", content: source }] });
  assert.equal(result.diagnostics[0].code, "unresolved_proof_association");
  assert.equal(result.diagnostics[0].label, "missing_target");
});
