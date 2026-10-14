# Feature: Faithful Overleaf Formalization and Dual Lean/Lea Checks

**Planned replacement:** [Live Lea Status](FEATURE-overleaf-live-lea-status.md), with its [implementation plan](PLAN-overleaf-live-lea-status.md), replaces the separate evaluator with reporting during formalization. This document remains the record of the currently implemented behavior; the replacement is not yet implemented.

**Status:** V1 pilot implemented; live calibration and release review pending  
**Target release:** Pilot / V1  
**Last updated:** 2026-09-12  
**Primary surface:** Overleaf extension Lean pane  
**Affected systems:** Overleaf extension, companion server, FastAPI adapter, vendored Lea prover  
**Unaffected surface:** LeaChat's interactive behavior and system prompt

## Executive Summary

The Overleaf extension currently treats successful Lean compilation as the primary
signal that a formalization succeeded. Compilation is necessary, but it does not
establish that the generated Lean artifact accurately represents either:

1. the mathematical statement written in the LaTeX source, or
2. the proof strategy and reasoning used by the author.

This creates two product failures:

- Lea can produce a valid Lean proof using an approach unrelated to the proof in
  the paper, causing a compiler-approved artifact to misrepresent the author's
  mathematics.
- Lea can notice a defect or gap in the source argument, silently repair or
  reinterpret it in Lean, and leave the user unaware that the generated proof no
  longer follows the source.

This feature separates two independent questions that are currently collapsed into
one status:

- **Lean Check:** Does the current Lean artifact compile, and what is the state of
  the formalization run?
- **Lea Check:** Does the current Lean artifact faithfully correspond to the
  current LaTeX statement and, when supplied, the author's LaTeX proof?

Every completed Lea Check produces a persisted, user-visible report. An artifact
may therefore be `checked` by Lean while receiving a `warning` from Lea. This is an
expected and important state: it means the Lean artifact is technically valid but
may not represent the paper faithfully.

For V1, Lea Check runs when a formalization completes or pauses. Continuous checks
during long-running formalizations and one-click re-formalization using a Lea Check
report are follow-up work.

## Implementation Status

The V1 pilot implementation is present in the working tree as of 2026-09-12.
It includes:

- conservative same-file and explicit cross-file source-proof association, complete
  version-2 source bundles, and separate target-identity/evaluator-evidence hashes;
- a server-controlled `overleaf_solver` run purpose and dedicated
  `overleaf_faithful` prompt, without changing LeaChat's interactive prompt;
- independent Lean Check and Lea Check projections using exactly the status values
  specified below;
- structured stop reasons and recoverable `paused` behavior for turn, cost, timeout,
  and spend-cap stops, with retained artifacts and a Resume action;
- an isolated, zero-tool, read-only evaluator with one schema-correction attempt;
- SQLite-backed, revision-bound source/artifact/dependency snapshots, report history,
  idempotent automatic scheduling, evaluator usage accounting, and retry APIs;
- automatic checks after solver completion/pause and after manual edit, repair, and
  chat mutation entry points when an evaluable artifact/source bundle exists;
- dual status cards and an inspectable report showing matches, material findings,
  evidence, caveats, obligations, scope, confidence, limitations, recommended action,
  and exact evaluated revisions.

Deterministic validation is complete. Pilot release still requires the reviewed
live-model corpus and manual Overleaf scenarios described below; those checks incur
provider usage and require a real browser/project. The structured solver-finding
tool proposed in “Reliable solver findings” is not part of this first pass. V1 uses
the dedicated disclosure prompt plus the independent evaluator as its backstop, so
the strongest universal form of acceptance criterion 7 remains a calibration and
follow-up item rather than a guarantee established by unit tests. Historical reports
are durable and available through the adapter API; the initial card UI presents the
current report rather than a full history browser.

## Product Principle

For the Overleaf workflow, **faithful failure is preferable to unrelated success**.

When Lea cannot formalize the author's stated argument without materially changing
its meaning or method, it should preserve the mismatch, stop or pause honestly, and
explain the problem. It must not quietly substitute a different theorem, add an
unstated assumption, repair a mathematical gap, or use an unrelated proof strategy
merely to obtain a compiling artifact.

This principle is specific to translation and validation in the Overleaf workflow.
It must not change the general-purpose, collaborative behavior of LeaChat.

## Problem Statement

### Problem 1: Compiler success does not imply semantic fidelity

Lean verifies that a declaration is well typed. It does not verify that the
declaration is the intended translation of a LaTeX statement or that its proof
reflects the author's argument.

Examples of compiler-valid but product-invalid outcomes include:

- weakening or strengthening the translated theorem;
- changing a domain, quantifier, side condition, or hypothesis;
- proving a nearby Mathlib result rather than the stated result;
- replacing an elementary argument with an unrelated automation-heavy proof;
- omitting a key construction used by the author;
- introducing an assumption that only appears implicitly in Lea's interpretation;
- turning a claimed direct proof into a proof by contradiction when the method is
  itself part of what the author wants checked;
- silently correcting a false intermediate claim in the paper.

### Problem 2: Material discoveries are not reliably surfaced

Lea may encounter a defect, ambiguity, or missing justification, work around it in
the generated formalization, and never explain that change to the user. The final
status then conveys success while concealing information that would help the author
improve the paper.

### Problem 3: Recoverable stops are classified as errors

Turn limits, cost limits, and other resumable stops currently appear as errors in
some Overleaf flows. These are not necessarily failures of the proof, compiler, or
infrastructure. They are pauses in unfinished work and should be presented as such,
with an appropriate resume action.

## Goals

V1 must:

1. Separate compiler/run state from semantic-alignment state.
2. Display both states for every Lea-marked Overleaf item.
3. Introduce a dedicated Overleaf formalization prompt that prioritizes faithful
   translation of the supplied LaTeX argument.
4. Keep LeaChat's current interactive prompt and behavior unchanged.
5. Run an independent, read-only Lea Check at formalization completion and at
   recoverable pauses when an evaluable artifact exists.
6. Persist and display a structured report for every completed Lea Check,
   including approved checks and checks that encounter an error.
7. Make material discrepancies, silent repairs, ambiguities, and caveats visible.
8. Classify turn-limit and cost-limit stops as `paused`, not `error`.
9. Preserve proof bytes in git and metadata in SQLite, consistent with the current
   architecture.
10. Bind each Lea Check report to the exact LaTeX and Lean revisions it evaluated.

## Non-Goals for V1

V1 does not include:

- continuous or turn-by-turn Lea Checks during an active formalization;
- automatic rewriting of the Lean artifact in response to a Lea Check;
- automatic rewriting of the LaTeX source;
- one-click retry using the report as solver context;
- a proof of mathematical equivalence between arbitrary natural-language and Lean
  arguments;
- replacing Lean's kernel or `lean_check` as the authority on compilation;
- changing the existing human-approval checkmark described in
  `FEATURE-overleaf-human-approval.md`;
- changing the standalone LeaChat prompt, approval gate, or interaction model;
- introducing a new external service or a separate prover HTTP server;
- judging writing style, exposition quality, or mathematical novelty except where
  these prevent a reliable correspondence judgment.

## Relationship to Existing Features

This feature is additive to the existing architecture:

```text
Overleaf page -> Chrome extension -> companion (:31245) -> FastAPI adapter (:8001)
                                                     -> vendored prover in-process
```

The adapter remains the only backend on port 8001 and continues to import the
vendored prover as a library. No service is added on port 8000 or elsewhere.

This feature must coexist with:

- source freshness and stale-artifact detection;
- sorry stubs and transitive sorry-stub warnings;
- theorem and definition targets;
- verified counterexample/disproof results;
- manual Lean edits;
- chat-mirror changes;
- cascade verification and self-repair;
- browser-local human approval;
- batch stub/formalize/repair workflows.

The new Lea Check is not the same as the existing self-repair `needs_review`
outcome. `needs_review` should be migrated into the richer Lea Check/report model or
mapped to it during rollout; it must not survive as a third, competing semantic
status.

## Terminology

- **Formalization target:** A Lea-marked theorem, lemma, proposition, corollary, or
  definition in the Overleaf project.
- **Source statement:** The LaTeX statement attached to the formalization target.
- **Source proof:** The LaTeX proof text associated with the target, if present.
- **Source bundle:** The source statement, source proof, explicit `context`, declared
  `uses`, relevant source location, and the bounded surrounding excerpt supplied to
  the solver or evaluator.
- **Lean artifact:** The current generated or user-edited `.lean` file associated
  with the target.
- **Lean Check:** The compiler/run-lifecycle dimension of the target's status.
- **Lea Check:** A model-assisted comparison between one immutable source bundle
  revision and one immutable Lean artifact/dependency revision.
- **Material discrepancy:** A difference that could change the mathematical claim,
  the validity of the source argument, or the proof approach a mathematician would
  reasonably understand the paper to use.
- **Caveat:** A non-material difference worth disclosing but not sufficient to make
  the Lea Check a warning.
- **Solver finding:** An issue, ambiguity, or repair noticed by the formalization
  agent while it is constructing the Lean artifact.
- **Current report:** A Lea Check report whose source, artifact, and relevant local
  dependency revisions still match the current target.
- **Superseded report:** A historical report whose evaluated source or Lean revision
  is no longer current.

## User Stories

1. As a mathematician, I want to know whether Lean accepts the generated artifact
   independently of whether Lea believes it represents my paper.
2. As a mathematician, I want a warning when the generated Lean proof uses a
   materially different argument from the one I supplied.
3. As a mathematician, I want Lea to tell me when it found and repaired a problem in
   my source argument.
4. As a mathematician, I want an approved result to include caveats so that approval
   is not presented as more certain than it is.
5. As a mathematician, I want a capped run to appear paused and resumable, rather
   than failed.
6. As a supervisor, I want every judgment to be traceable to exact LaTeX and Lean
   revisions.
7. As a LeaChat user, I want the existing chat workflow to remain unchanged.

## V1 User Experience

Each formalization card in the Lean pane displays two explicitly labeled status
rows or chips:

```text
Lean Check   checked
Lea Check    warning
```

The labels must always be visible. Color alone must not distinguish the two checks
or their states.

The card also contains a collapsible **Lea Check report** section. The section is
shown once a Lea Check has started or a report exists. It must support all terminal
outcomes, not only warnings.

At a minimum, the collapsed header shows:

- Lea Check status;
- one-sentence summary;
- evaluation timestamp;
- whether the report is current or superseded;
- the number of material findings and caveats.

When expanded, the report shows:

1. overall assessment;
2. what matched;
3. material discrepancies;
4. issues or ambiguities found in the source;
5. solver repairs or reinterpretations, if any;
6. minor caveats;
7. cited LaTeX and Lean locations or excerpts;
8. recommended next action;
9. checker limitations and confidence;
10. the source/artifact revisions that were evaluated.

Warnings must be visually prominent. Approved reports remain inspectable and must
show their caveats; the UI must not reduce an approved report to a bare checkmark.

The compact in-document status badge may show a condensed dual-state summary. Its
popover must expose both full labels and link or scroll to the report in the Lean
pane.

## Status Model

Lean Check and Lea Check are independent dimensions. Neither status may overwrite,
hide, or be inferred from the other.

### Lean Check statuses

| Status | Definition | Entry conditions | Typical next states |
|---|---|---|---|
| `unformalized` | No meaningful Lean artifact exists for the current target. | No recorded declaration or artifact is available. | `stubbed`, `in-progress` |
| `stubbed` | A compiler-accepted declaration skeleton exists, but contains an intentional proof placeholder or is explicitly recorded as a stub. | Current artifact contains a recognized placeholder such as `sorry`, and its latest applicable Lean check is not failing. | `in-progress`, `paused`, `checked`, `error` |
| `in-progress` | A solver, edit verification, or compiler check affecting the current artifact is actively running. | A relevant active operation owns the target. | `stubbed`, `checked`, `paused`, `error` |
| `checked` | The exact current artifact is sorry-free and passed Lean. | The latest applicable `lean_check` for the current artifact revision returned success and the artifact satisfies final-proof rules. | `in-progress`, `paused`, `error` after a later change |
| `paused` | Formalization stopped for a recoverable reason before completing the desired artifact. | Turn cap, cost cap, user pause/interrupt with resumable state, recoverable timeout, or another explicitly resumable stop. | `in-progress`, `stubbed`, `checked`, `error` |
| `error` | The current artifact or checking operation encountered a non-recoverable failure requiring correction or intervention. | Compiler failure, malformed artifact, unrecoverable process/infrastructure failure, or a failed operation not classified as resumable. | `in-progress`, `stubbed`, `checked` |

#### Lean Check precedence

When multiple facts exist, derive the displayed state in this order:

1. active operation affecting the current target -> `in-progress`;
2. latest run ended recoverably without a completed checked artifact -> `paused`;
3. latest applicable compiler/infrastructure verdict is non-recoverable -> `error`;
4. no meaningful artifact -> `unformalized`;
5. current artifact is an accepted placeholder-bearing stub -> `stubbed`;
6. exact current artifact is sorry-free and compiler-approved -> `checked`.

If a run hits a cap after already producing and verifying a complete artifact in the
same turn, artifact truth wins and the Lean Check remains `checked`. The run history
may still record that the cap was reached.

`disproved`, `defined`, and similar concepts are **result kinds**, not Lean Check
statuses. A checked definition is `Lean Check: checked` with result kind `defined`.
A verified counterexample is `Lean Check: checked` with result kind `disproved`.

### Lea Check statuses

| Status | Definition | Entry conditions | Report behavior |
|---|---|---|---|
| `N/A` | There is no current source/artifact pair that can be evaluated. | No meaningful Lean artifact exists, or a source/artifact change invalidated the prior current report and no new check has started. | Historical superseded reports remain accessible. |
| `in-progress` | An alignment evaluator is actively comparing the current source and artifact revisions. | A checker run has started and has not settled. | Show progress and the evaluated revision identifiers. |
| `paused` | The alignment evaluator itself stopped recoverably. | Checker turn/cost cap, resumable timeout, or explicit interruption. | Preserve any partial report and offer resume when possible. |
| `warning` | Evaluation completed and found at least one material discrepancy or could not establish method fidelity from the supplied source evidence. | Valid structured report with one or more material findings. | Show all findings, evidence, caveats, and recommendations. |
| `error` | The check could not produce a trustworthy judgment. | Invalid checker output after retry, unavailable artifact/source, provider failure, schema failure, or non-recoverable evaluator failure. | Show a failure report explaining what was and was not evaluated. Never imply a proof mismatch merely from checker failure. |
| `approved` | Evaluation completed without a material discrepancy for the evaluated scope. | Valid structured report with zero material findings. | Show matched aspects, caveats, limitations, and confidence. |

#### Important interpretation rules

- `warning` describes the relationship between the source and artifact.
- `error` describes a failure of the evaluation process.
- `approved` is not a mathematical soundness guarantee. Lean remains authoritative
  for compilation, and the report must state its evaluated scope.
- `approved` does not mean “identical proof scripts.” Routine elaboration,
  bookkeeping, coercions, library calls, and low-level Lean steps may differ when
  they preserve the source argument's mathematical structure.
- A missing source proof is not an evaluator error. For a theorem whose method is
  meant to be checked, the evaluator returns `warning` with an
  `insufficient_source_evidence` finding if it can compare the statement but cannot
  determine whether the proof method is faithful.
- Definitions may receive `approved` based on statement/meaning alignment alone;
  they do not require a source proof body.
- An incomplete or compiler-failing Lean artifact may still be evaluated at a
  pause. The report must make clear that it judges the approach visible in the
  current snapshot, not a completed proof.

### Source freshness

Source freshness remains orthogonal to both checks.

When the LaTeX source changes after a report:

- the Lean Check continues to describe the current Lean artifact itself;
- the old Lea Check report becomes superseded;
- the current Lea Check becomes `N/A` until a new evaluation starts;
- the existing “out of date” source-freshness warning remains visible;
- re-formalization is offered as it is today.

When the Lean artifact or any semantics-relevant local dependency changes:

- the prior Lea Check report becomes superseded;
- a new applicable Lean compiler verdict must be obtained;
- the current Lea Check becomes `N/A` or `in-progress`, depending on whether a new
  evaluation has started.

Reports are never silently relabeled as applying to a new revision.

## Expected Combined States

| Lean Check | Lea Check | Interpretation |
|---|---|---|
| `unformalized` | `N/A` | Nothing exists to evaluate. |
| `stubbed` | `N/A` | A declaration skeleton exists, but V1 has not evaluated it. |
| `in-progress` | `N/A` | Formalization is underway and no current evaluation has started. |
| `checked` | `in-progress` | Lean accepts the artifact; fidelity evaluation is running. |
| `checked` | `approved` | The artifact compiles and no material mismatch was detected. |
| `checked` | `warning` | The artifact compiles but materially diverges from the source or lacks sufficient proof evidence. |
| `checked` | `error` | The artifact compiles, but semantic evaluation failed. |
| `paused` | `in-progress` | Solver work paused; the current snapshot is being evaluated. |
| `paused` | `approved` | Work is incomplete, but the approach visible so far aligns within the report's stated scope. |
| `paused` | `warning` | Work is incomplete and a material deviation is already visible. |
| `error` | `warning` | Lean currently rejects the artifact, and the evaluator also found a source/artifact mismatch. |
| any | `paused` | The Lea evaluator, rather than necessarily the solver, was interrupted recoverably. |

These combinations are intentional. The UI must not collapse them back into a
single “success/failure” badge.

## State Transitions

### Normal successful run

```text
Lean Check: unformalized -> in-progress -> checked
Lea Check:  N/A          -> N/A         -> in-progress -> approved | warning | error
```

### Turn or cost cap

```text
Lean Check: in-progress -> paused
Lea Check:  N/A         -> in-progress -> approved | warning | error | paused
```

### Source edit after approval

```text
Source freshness: current -> stale
Lean Check:       checked -> checked        (the artifact still compiles)
Lea Check:        approved -> N/A           (old report retained as superseded)
```

### Manual Lean edit

```text
Lean Check: checked -> in-progress -> checked | error
Lea Check:  approved/warning -> N/A -> in-progress -> approved | warning | error
```

## Capturing the LaTeX Proof Source

The current target model primarily captures the marked theorem or definition
statement plus a bounded surrounding excerpt. That is insufficient for reliable
proof-method comparison. V1 must add an explicit source-proof association.

### V1 association rules

For theorem-like targets, resolve a source proof in the following order:

1. **Explicit association:** A proof environment explicitly identifies the Lea
   target label using the syntax chosen during implementation. Explicit association
   is authoritative and is recommended when a document contains non-adjacent,
   multiple, or named proofs.
2. **Adjacent proof environment:** If there is no explicit association, attach the
   first `proof` environment following the marked theorem in the same file, provided
   no intervening marked theorem/definition or section-level boundary makes the
   association ambiguous.
3. **No reliable proof:** If neither rule produces a unique match, record
   `sourceProofStatus: "missing" | "ambiguous"` and do not infer a proof from
   unrelated surrounding prose.

The exact explicit LaTeX syntax is an implementation design decision, but it must:

- be stable under formatting and comments;
- refer to the Lea target label rather than DOM position;
- produce a parser diagnostic for duplicate or unresolved associations;
- avoid changing the typeset appearance by default;
- work across files in the mirrored Overleaf project.

Definitions normally have `sourceProofStatus: "not_applicable"`.

### Source bundle fields

The source bundle supplied to the evaluator must include:

- target kind;
- Lea target label and LaTeX label;
- complete source statement;
- associated proof body, when available;
- proof-association status and method;
- declared `uses` dependencies;
- explicit `context` guidance;
- source file and statement/proof line ranges;
- a bounded surrounding excerpt for notation and local context;
- relevant mirrored source paths;
- a deterministic source revision hash.

The source revision hash must cover the statement, associated proof, `uses`,
`context`, target kind, and proof-association identity. Whitespace normalization may
avoid invalidating reports for formatting-only changes, but must not erase
mathematically relevant LaTeX tokens.

## Faithfulness Rubric

The Lea evaluator must assess the following dimensions independently and include
each in its report.

### 1. Statement correspondence

Determine whether the Lean declaration preserves:

- the mathematical objects and their domains;
- hypotheses and side conditions;
- quantifier type, order, and scope;
- conclusions;
- equality, implication, equivalence, inequality, and negation direction;
- existence and uniqueness claims;
- finite/infinite, local/global, pointwise/uniform, and constructive/classical
  distinctions when mathematically relevant;
- definition parameters and returned meaning;
- intended dependency on previously declared items.

Any material strengthening, weakening, omission, or addition is a warning.

### 2. Proof-strategy correspondence

When a source proof is available, determine whether the Lean proof preserves the
author's central mathematical route. Relevant features include:

- principal construction or witness;
- induction variable and induction structure;
- case split or contradiction structure;
- reduction to an earlier theorem;
- key intermediate lemmas;
- direction of an equivalence proved first;
- use of compactness, continuity, algebraic manipulation, counting, extremality,
  approximation, or another domain-level method;
- dependencies the author explicitly invokes;
- treatment of exceptional or boundary cases.

The evaluator compares mathematical structure, not syntax. Lean-specific
elaboration steps, `simp`, coercion management, rewriting, and library lemma names
may differ without causing a warning when they implement the same argument.

### 3. Silent repair and reinterpretation

Identify whether the solver:

- corrected a false or malformed intermediate claim;
- supplied a missing assumption;
- chose one interpretation of ambiguous notation;
- filled a nontrivial logical gap;
- changed a definition or convention;
- used a stronger external result to bypass the source argument;
- replaced a failed author approach with an unrelated successful approach;
- omitted a source step because it was invalid rather than merely routine.

Material repairs produce warnings even when the resulting Lean artifact compiles.
Minor routine details may be caveats.

### 4. Coverage

Determine whether all material stages of the source argument appear in the Lean
artifact or are justified by cited library results. An omitted explanatory sentence
is not necessarily a discrepancy. An omitted mathematical obligation is.

### 5. Dependency fidelity

Determine whether the Lean artifact uses the declared or textually invoked
dependencies as intended. Using a standard library lemma for a routine fact is
normally acceptable. Bypassing the author's central cited result with a materially
different theorem is a warning unless the source explicitly permits alternatives.

### 6. Evaluation limitations

The evaluator must state limitations such as:

- missing or ambiguous source proof;
- incomplete Lean artifact;
- unresolved notation;
- truncated context;
- opaque automation whose mathematical route cannot be confidently reconstructed;
- unavailable imported source or dependency content.

Limitations must lower confidence and may produce a warning when they prevent the
core method-fidelity question from being answered.

## Materiality Rules

A finding is **material** when at least one of the following is true:

- it changes the proposition or definition being represented;
- it changes an assumption the result depends on;
- it conceals a mathematical error or nontrivial gap in the source;
- it replaces the central proof strategy with a substantially different strategy;
- it bypasses a source dependency that is central to the stated argument;
- it prevents the evaluator from determining whether the supplied proof method was
  followed;
- a reasonable author would want to revise either the paper or formalization after
  learning about it.

A finding is a **caveat** when it is worth disclosing but does not affect the claim
or central argument, for example:

- routine Lean bookkeeping omitted from the prose;
- harmless reordering of independent steps;
- use of a standard Mathlib lemma for an elementary substep;
- a classical reasoning instance that does not change the mathematical content;
- notation or representation choices that are equivalent in the stated context;
- low-confidence observations that do not undermine the overall comparison.

When uncertain whether a deviation is material, the evaluator should prefer a
warning with a clear uncertainty explanation over unsupported approval.

## Structured Lea Check Report

The evaluator must submit a schema-validated result. Free-form assistant text alone
is not a valid report.

Conceptual schema:

```json
{
  "schemaVersion": 1,
  "verdict": "approved | warning | error",
  "scope": "statement_and_proof | statement_only | partial_artifact",
  "summary": "One-sentence user-facing assessment.",
  "confidence": "high | medium | low",
  "dimensions": {
    "statement": "aligned | discrepancy | inconclusive | not_applicable",
    "proofStrategy": "aligned | discrepancy | inconclusive | not_applicable",
    "coverage": "aligned | discrepancy | inconclusive | not_applicable",
    "dependencies": "aligned | discrepancy | inconclusive | not_applicable"
  },
  "matches": [
    {
      "summary": "What corresponds faithfully.",
      "latexReference": { "file": "paper.tex", "startLine": 40, "endLine": 45 },
      "leanReference": { "path": "Lea/Project/result.lean", "startLine": 10, "endLine": 18 }
    }
  ],
  "findings": [
    {
      "id": "stable-report-local-id",
      "severity": "warning | caveat",
      "category": "statement_mismatch | assumption_change | quantifier_change | strategy_substitution | silent_repair | omitted_step | dependency_bypass | ambiguity | insufficient_source_evidence | incomplete_artifact | other",
      "title": "Short finding title",
      "explanation": "Why this matters to the author.",
      "latexReference": { "file": "paper.tex", "startLine": 40, "endLine": 45, "excerpt": "..." },
      "leanReference": { "path": "Lea/Project/result.lean", "startLine": 10, "endLine": 18, "excerpt": "..." },
      "recommendedAction": "Concrete next step."
    }
  ],
  "solverFindings": [
    {
      "severity": "warning | caveat | error",
      "category": "source_issue | ambiguity | repair | interpretation | unprovable_as_written | other",
      "summary": "Issue reported by the solver itself.",
      "actionTaken": "What the solver did, or null if it stopped."
    }
  ],
  "limitations": ["..."],
  "recommendedNextAction": "...",
  "evaluatedRevision": {
    "sourceHash": "...",
    "artifactCommit": "...",
    "artifactContentHash": "...",
    "dependencyHash": "..."
  }
}
```

Validation rules:

- `approved` requires zero `severity: warning` findings.
- `warning` requires at least one `severity: warning` finding.
- `error` must explain why no trustworthy verdict was produced.
- Every material finding should contain both LaTeX and Lean evidence when such
  evidence exists. Missing evidence must be explained.
- References must point into the evaluated snapshots, not mutable live content.
- Excerpts must be bounded in length.
- Unknown category values are rejected rather than silently discarded.
- If output validation fails, the evaluator receives at most one correction attempt.
  A second failure produces `Lea Check: error` with a generated failure report.

## Solver Behavior and Prompt Contract

### Dedicated Overleaf prompt

Add a new prompt variant for Overleaf formalization rather than modifying the
existing `default` or `interactive` prompts globally.

Recommended variants:

- `interactive`: existing LeaChat collaborator; unchanged;
- `default`: existing general autoformalizer/CLI behavior; unchanged;
- `overleaf_formalization`: new source-faithful autonomous translator;
- `overleaf_alignment_check`: new read-only evaluator.

The adapter currently maps every autonomous run to `default`. That mapping must be
refined using a server-controlled run purpose. The client must not be allowed to
inject arbitrary system-prompt text.

### Overleaf solver requirements

The `overleaf_formalization` prompt must instruct the solver to:

1. Treat the source statement and proof as the authoritative mathematical contract.
2. Preserve the theorem/definition statement exactly in meaning.
3. Follow the supplied proof's central strategy when a proof is present.
4. Use Lean-specific scaffolding as needed without substituting a different central
   argument.
5. Stop and report when the source method cannot be faithfully formalized.
6. Prefer a partial, accurately aligned artifact and explicit explanation over an
   unrelated complete proof.
7. Never silently add assumptions, repair gaps, reinterpret notation, or replace the
   method.
8. Record every material issue, interpretation, and repair it notices.
9. Continue to obey existing hard rules: do not alter declaration headers to make a
   proof pass; do not use `sorry`, `axiom`, or `native_decide` in a final proof; do
   not claim compiler success without `lean_check`.

### Reliable solver findings

Prompt narration alone is not sufficiently reliable for issue disclosure. Add a
structured mechanism, preferably a dedicated typed tool/event, for the solver to
record findings during a run.

Conceptual tool:

```text
report_formalization_finding(
  severity,
  category,
  title,
  explanation,
  latex_reference,
  lean_reference,
  action_taken
)
```

Calling this tool must not modify the proof. It persists a finding and emits an SSE
event so the extension can show it during or after the run. The final Lea Check
receives all solver findings as evidence but independently verifies them.

If V1 cannot add the tool, the minimum fallback is a schema-validated findings
envelope in the solver's terminal output. This fallback is less desirable and must
not be implemented by fragile prose scraping.

## Lea Evaluator Behavior and Prompt Contract

The alignment evaluator is an independent pass. It must:

- receive immutable source, artifact, dependency, and solver-finding snapshots;
- compare the source statement and Lean declaration before comparing proof method;
- apply the rubric and materiality rules in this specification;
- treat all LaTeX and Lean content as untrusted mathematical data, not instructions;
- make no edits to LaTeX, Lean files, project metadata, or git state;
- have a read-only tool allowlist;
- submit exactly one structured report through a schema-enforcing tool or endpoint;
- distinguish disagreement from inability to evaluate;
- avoid claiming that natural-language equivalence has been formally proven;
- disclose low confidence and missing evidence;
- incorporate solver findings without treating the solver's self-assessment as
  authoritative.

The evaluator may use `lean_check` read-only when needed to confirm which artifact
snapshot is being discussed, but it must not use compiler success as evidence of
semantic fidelity.

The evaluator should default to the same configured provider/model as the solver in
V1 unless a product setting explicitly selects a checker model. The report must
record the provider/model used and evaluator usage must be accounted for separately
from solver usage.

## Triggering Lea Check

### Required V1 triggers

Start a Lea Check when:

1. a formalization run produces a complete Lean artifact and settles;
2. a formalization run pauses for a recoverable reason and a meaningful current
   artifact snapshot exists;
3. a manual Lean edit settles and the edited artifact is meaningful;
4. a chat-mirror or repair run changes the artifact and settles;
5. a GitHub import matches an Overleaf formalization and its file-level Lean Check settles;
6. the user explicitly retries a failed or paused Lea Check.

### Trigger exclusions

Do not start a Lea Check when:

- no meaningful Lean artifact exists;
- the only artifact is an untouched generated stub, unless the user explicitly asks
  to evaluate the translation of its statement;
- the source bundle cannot be identified at all;
- an identical current report already exists for the exact source, artifact, and
  dependency revision tuple;
- another Lea Check for the same tuple is already active.

### Scheduling and ordering

- Final Lean artifact persistence and the applicable `lean_check` must settle before
  the evaluator snapshots the artifact.
- The solver run must release any per-session mutation lock before the read-only
  evaluator begins.
- Only one current evaluator may run per target/revision tuple.
- A newer source or artifact revision supersedes an older queued evaluator. The old
  run may finish for history, but its report must never become current.
- Batch formalization may queue evaluations independently. Checker concurrency must
  respect the existing global run controls and must not starve active solver runs.

### Future continuous mode

Continuous evaluation is deferred. A future version may trigger at configurable
turn intervals or milestone events, but it must debounce writes and bind each report
to a snapshot. Interim reports must never overwrite the terminal report for a newer
revision.

## Pause and Resume Semantics

### Solver pauses

The following are recoverable and map to `Lean Check: paused` when no completed
checked artifact was produced:

- maximum turn count reached;
- maximum per-run cost reached;
- cumulative spend cap reached;
- user-requested stop where the session and artifact can be resumed;
- recoverable job timeout;
- transient provider interruption with preserved session state, if explicitly
  classified as resumable.

Authentication failures, malformed configuration, unrecoverable process crashes,
and compiler failures caused by the current artifact remain `error` unless a
specific recovery classification applies.

The paused UI must show:

- why the run paused;
- current turn and configured turn limit when applicable;
- relevant cost/cap information when available;
- whether an artifact snapshot was saved;
- a **Resume formalization** action.

If a cumulative cost cap caused the pause, Resume must explain that the cap must be
raised or disabled before the run can proceed. Pressing Resume without available
budget must not create another immediately failing run.

Resuming starts a new adapter run in the same Lea session, preserving transcript and
current git-backed artifact state. It does not mutate the previous run record.

### Evaluator pauses

A checker-specific cap or interruption maps to `Lea Check: paused`, not Lean Check.
The UI offers **Resume Lea Check** when continuation is possible, otherwise **Retry
Lea Check**. Any partial structured findings remain visible and are clearly labeled
partial.

## Persistence and Source of Truth

### Architectural rules

- Git continues to own proof content.
- SQLite owns runs, evaluation metadata, findings, reports, and revision links.
- Session status remains derived; do not add a mutable session-status column.
- Companion `jobs.json` may cache or mirror information needed for Overleaf UI
  continuity, but it must not become the authoritative report store.
- The current Lean Check is derived from active-run, artifact, and latest applicable
  compiler facts.
- The current Lea Check is derived from the latest evaluation record matching the
  current revision tuple.

### Lea Check record

Add an immutable evaluation record concept, tentatively `alignment_checks`, with at
least:

- evaluation ID;
- project, session, formalization, target kind, and target label;
- optional solver run ID;
- evaluator run ID;
- source hash;
- artifact git commit/SHA and content hash;
- local dependency revision hash;
- lifecycle status (`running`, `paused`, `completed`, `failed`);
- verdict (`approved`, `warning`, or `error`) when terminal;
- structured report JSON and schema version;
- provider and model;
- input/output tokens and cost, or a link to the ordinary usage records;
- created, started, paused, and completed timestamps;
- terminal reason/error code and detail;
- supersession relationship when known.

Reports are immutable after completion. A corrected or retried evaluation creates a
new record.

### Revision identity

A report is current only when all applicable components match:

```text
(sourceHash, artifactContentHash, artifactCommit, dependencyHash)
```

`dependencyHash` should cover semantics-relevant transitive project-local imports,
using the same general revision-awareness already used by human approval. This
prevents an approved report from remaining current when an unfolded definition or
imported theorem changes underneath the target.

## API and Event Contract

Exact route names may follow existing adapter conventions, but the logical contract
must provide the following operations.

### Run purpose

Extend run creation with a server-validated purpose enum such as:

```text
chat
overleaf_formalization
overleaf_repair
overleaf_alignment_check
```

The purpose selects a server-owned prompt/tool policy. `autonomous: true` continues
to control the approval gate; it must no longer be the only signal selecting the
generic `default` prompt.

### Check operations

- Start an alignment check for a target and explicit revision tuple.
- Read the current check for a target.
- List historical/superseded checks for a target.
- Resume or retry a paused/failed checker run.
- Read the structured report.

The start operation must be idempotent for an exact revision tuple while a current
check is active or already complete.

### Events

Use internal event names that avoid confusion with the existing compiler
`lean_check`, for example:

- `alignment_check_started`;
- `alignment_check_progress`;
- `alignment_finding_recorded`;
- `alignment_check_paused`;
- `alignment_check_completed`;
- `alignment_check_failed`;
- `alignment_check_superseded`.

Each event includes target identity, evaluation ID, revision tuple, timestamp, and
only the fields appropriate to that event. Terminal events contain or link to the
validated report.

The extension may receive these through its existing polling/event mechanisms. A
page refresh must reconstruct the same current state from persisted data.

### Manifest shape

Each Lean-pane item should expose two structured blocks rather than one ambiguous
status string:

```json
{
  "leanCheck": {
    "status": "unformalized | stubbed | in-progress | checked | paused | error",
    "reasonCode": "max_turns | max_spend | compiler_error | ...",
    "message": "...",
    "checkedArtifactHash": "...",
    "resumable": true
  },
  "leaCheck": {
    "status": "N/A | in-progress | paused | warning | error | approved",
    "evaluationId": "...",
    "summary": "...",
    "warningCount": 1,
    "caveatCount": 2,
    "current": true,
    "reportAvailable": true
  },
  "resultKind": "proved | defined | disproved | null",
  "sourceFreshness": "current | stale | unknown"
}
```

During migration, legacy `status` may remain as a compatibility projection, but new
UI code must consume `leanCheck` and `leaCheck`. The projection must be documented
and removed after all extension surfaces migrate.

## UI Requirements

### Formalization card

Every item card must:

- render Lean Check and Lea Check as separately labeled controls;
- display a tooltip or explanation for each status;
- render the result kind separately where applicable;
- preserve source-freshness and transitive-stub warnings;
- show pause reason and resume actions;
- provide a collapsible Lea Check report;
- keep historical reports accessible without confusing them with the current one;
- show when a report is based on an incomplete artifact;
- expose cited source and Lean locations with navigation when possible.

### Report presentation

Recommended section order:

1. **Assessment** — verdict, scope, confidence, and summary.
2. **What agrees** — concise matched aspects.
3. **Warnings** — material discrepancies, first.
4. **Issues Lea found while formalizing** — solver findings and actions taken.
5. **Caveats** — accepted minor deviations.
6. **Evidence** — linked LaTeX and Lean references.
7. **Recommended next step**.
8. **Technical details** — model, timestamps, revision hashes, limitations.

If a solver finding says Lea repaired a source issue, the UI must say so explicitly;
it must not euphemize the repair as ordinary progress.

### Accessibility

- Do not rely on green/amber/red alone.
- Status updates use appropriate live-region behavior without repeatedly announcing
  polling refreshes.
- The report toggle is keyboard accessible and exposes `aria-expanded`.
- Warning/caveat icons have text alternatives.
- Source/Lean evidence links have descriptive accessible labels.
- Focus moves predictably after Resume or Retry actions.

## Errors and Edge Cases

### No source proof

- Statement comparison still runs.
- Proof-strategy comparison is `inconclusive`.
- For theorem-method validation, the final status is `warning` with
  `insufficient_source_evidence`.
- The report recommends explicitly associating a proof environment.

### Ambiguous proof association

Do not guess. Treat it like missing proof evidence, cite the candidate locations,
and return a warning unless the target is a definition.

### Artifact compiles but statement differs

Lean Check is `checked`; Lea Check is `warning`.

### Artifact does not compile but follows the source so far

Lean Check is `error` or `paused`, depending on terminal reason. Lea Check may be
`approved` for `scope: partial_artifact` only when the report clearly limits the
claim to the visible approach. Low confidence or missing core steps should instead
produce a warning.

### Solver silently fixes a source error

The solver records a `repair` or `source_issue` finding. The evaluator independently
classifies the difference. The expected final state is usually Lean `checked`, Lea
`warning`.

### Evaluator output is malformed

Attempt one schema-repair turn. If it remains malformed, record Lea `error` with a
failure report. Do not transform malformed prose into approval.

### Source changes while evaluation runs

Allow the evaluation to settle for its original immutable snapshot, mark its report
superseded, and do not present it as current. Queue a new check only after the new
artifact/source state is stable and otherwise eligible.

### Lean changes while evaluation runs

Apply the same immutable-snapshot behavior. The evaluator must never read a mixture
of old and new file contents.

### Restored artifact after failed retry

Bind the check to the artifact actually restored on disk and its originating commit,
not to the failed retry's intended output. Preserve the failed attempt in history.

### Counterexample/disproof result

Lean Check may be `checked` with result kind `disproved`. Lea Check evaluates whether
the counterexample or negation corresponds to the source and whether it accurately
communicates that the original theorem was not proved.

### Stubbed dependencies

Keep the existing transitive-stub warning. Lea Check must list stubbed dependencies
as limitations or findings when they affect confidence, but it must not relabel a
compiler-accepted target as compiler-checked if current product rules require a
sorry-free dependency closure for that claim.

### Checker provider/cost failure

Only Lea Check becomes `paused` or `error`. A previously obtained Lean `checked`
status remains intact.

## Retry with Report Context: Follow-Up Design

This is not required for V1, but V1 persistence and report schemas must support it.

A future **Re-formalize using report** action should:

1. require explicit user action;
2. show which report and artifact revision will be used;
3. create a new solver run in the same target session;
4. include the selected structured warnings and caveats as context;
5. retain the previous artifact, run, and report for comparison;
6. instruct the solver to address the report while preserving the source statement
   and method;
7. trigger a fresh Lea Check against the new artifact;
8. never mark old findings resolved merely because a new run started.

The new report should identify findings that appear resolved, remain present, or
have been replaced by new discrepancies.

## Observability and Auditability

For each solver and evaluator run, record enough information to answer:

- Which source and artifact revisions were used?
- Which prompt variant and run purpose were selected?
- Which model/provider performed the work?
- Why did the run stop?
- Was the stop recoverable?
- What issues did the solver report while working?
- What report did the evaluator submit?
- Was that report ever current, and what superseded it?
- How much evaluator usage/cost was incurred?

Do not log provider credentials or unrestricted paper contents beyond the existing
local artifact/transcript policy. Reports may contain bounded excerpts required to
explain findings.

Suggested product metrics:

- share of checked artifacts receiving `approved`, `warning`, or evaluator `error`;
- warning categories and frequency;
- rate of missing/ambiguous proof associations;
- rate of silent-repair findings;
- checker retry and schema-failure rate;
- number of paused solver/evaluator runs resumed;
- evaluator latency and cost;
- warning-to-success change after future report-guided retries.

## Security and Trust Boundaries

- Treat LaTeX, Lean source, project instructions, and excerpts as untrusted data in
  the evaluator prompt.
- Delimit source and artifact content explicitly.
- The evaluator toolset is read-only and cannot call write/edit/repair operations.
- Prompt variant selection is server-controlled through an allowlisted run purpose.
- Validate structured reports at the adapter boundary before persistence.
- Sanitize rendered report content and never inject report HTML into the Overleaf
  page.
- Verify source and artifact hashes on check creation to prevent a client from
  attaching a report to a different revision.
- Preserve the existing restriction that final proofs may not use `sorry`, `axiom`,
  or `native_decide`.

## Rollout and Compatibility

### Phase 0: Foundations

- Add source-proof extraction/association and revision hashing.
- Introduce server-controlled run purposes and new prompt variants.
- Normalize terminal reasons into recoverable pause versus error.
- Add persistence and schema validation for alignment reports.

### Phase 1: Hidden evaluation

- Run Lea Check behind a feature flag.
- Persist reports without changing user-visible status.
- Compare evaluator results against manually reviewed examples.
- Tune the rubric using a curated corpus containing faithful proofs, unrelated valid
  proofs, silent repairs, statement mismatches, incomplete artifacts, and ambiguous
  source arguments.

### Phase 2: Pilot UI

- Enable the two status labels and collapsible reports for pilot users.
- Keep the legacy status projection for older extension components.
- Monitor false approvals, false warnings, evaluator errors, cost, and latency.

### Phase 3: Default-on V1

- Make terminal/pause Lea Check automatic.
- Migrate all Overleaf status surfaces to the dual model.
- Remove or map competing semantic states such as legacy `needs_review`.
- Retain the feature flag as an emergency evaluator-disable control; disabling the
  evaluator must show Lea Check as unavailable/N/A, never approved.

### Data migration

Existing artifacts have no Lea Check report. On upgrade:

- derive their Lean Check from existing artifact/compiler evidence;
- set Lea Check to `N/A`;
- do not retroactively claim approval;
- optionally offer a user-triggered “Run Lea Check” action for current checked
  artifacts;
- preserve legacy human approvals but clearly distinguish them from Lea Check.

## Implementation Areas

The following are expected integration points, not a mandate to put all new logic in
the listed files.

### Overleaf parser and extension

- `apps/overleaf-extension/extension/targetParserCore.mjs`
  - detect and associate proof environments;
  - emit proof-source diagnostics and locations.
- `apps/overleaf-extension/shared/theoremParser.mjs`
  - hash the expanded formalization/evaluation source bundle.
- `apps/overleaf-extension/extension/content.js`
  - send proof-source context;
  - render dual status controls, report, pause reasons, and resume actions.
- `apps/overleaf-extension/extension/leanPaneView.mjs`
  - add pure dual-status/report presentation helpers and summary logic.
- `apps/overleaf-extension/extension/content.css`
  - style the accessible dual-status and report surfaces.

### Companion

- `apps/overleaf-extension/companion/server.mjs`
  - carry source-proof context;
  - derive/map Lean Check status;
  - trigger and expose alignment checks;
  - map recoverable terminal conditions to paused;
  - enrich pane manifests with both checks.
- `apps/overleaf-extension/companion/jobStore.mjs`
  - retain compatibility bookkeeping while keeping SQLite authoritative.
- `apps/overleaf-extension/companion/leaApiClient.mjs`
  - handle evaluator lifecycle, events, reports, and terminal reasons.
- A focused new module should own alignment-check payload construction and report
  validation instead of further enlarging `server.mjs`.

### Adapter

- `apps/lea-standalone/adapter/app/bridge.py`
  - select prompt/tool policy from run purpose;
  - stream structured solver findings and alignment events;
  - preserve paused terminal reasons;
  - run the evaluator without mutation tools.
- `apps/lea-standalone/adapter/app/db.py`, `store.py`, and migrations
  - persist immutable evaluation/report records and revision links.
- `apps/lea-standalone/adapter/app/routes/formalizations.py` or a focused route
  module
  - expose create/read/history/resume operations.

### Vendored prover

- `apps/lea-standalone/prover/lea/prompt.py`
  - add `overleaf_formalization` and `overleaf_alignment_check` variants while
    leaving `default` and `interactive` unchanged.
- `apps/lea-standalone/prover/lea/events.py`
  - add typed finding/report events if the structured-tool approach is implemented.
- Tool registry/configuration
  - add the solver finding/report tool;
  - enforce the evaluator's read-only allowlist and structured submission tool.

## Testing Strategy

### Parser tests

- adjacent proof environment is associated with the correct target;
- explicit association overrides adjacency;
- non-adjacent explicit proof association works across mirrored files;
- duplicate and unresolved associations produce diagnostics;
- an intervening theorem or section boundary prevents an unsafe implicit match;
- comments, verbatim blocks, nested environments, and formatting do not create false
  associations;
- source revision changes when proof content changes;
- whitespace-only changes follow the chosen normalization policy.

### Status unit tests

- every Lean Check status has a deterministic derivation;
- every Lea Check status maps from evaluator lifecycle/verdict correctly;
- turn and cost caps map to Lean `paused`;
- a completed artifact produced on the cap boundary remains Lean `checked`;
- checker caps affect Lea Check only;
- source/artifact/dependency changes supersede the prior report;
- legacy `valid`, `defined`, `disproved`, `sorry_stub`, `failed`, `in_progress`,
  and `needs_review` inputs migrate predictably.

### Report validation tests

- approved reports with warning findings are rejected;
- warning reports without warning findings are rejected;
- malformed references and unknown categories are rejected;
- one schema-repair attempt is allowed;
- a second malformed result becomes Lea `error`;
- error reports never masquerade as proof discrepancies;
- excerpts are bounded and rendered safely.

### Prompt contract tests

- Overleaf runs select `overleaf_formalization`;
- evaluator runs select `overleaf_alignment_check`;
- LeaChat runs remain `interactive` and byte-for-byte unaffected where practical;
- generic autonomous/CLI behavior remains `default`;
- evaluator tools contain no write/edit capability;
- source/Lean content is delimited as untrusted data;
- the solver prompt states the faithful-failure rule and issue-reporting obligation.

### Adapter/companion integration tests

- checked artifact triggers one idempotent Lea Check;
- matched GitHub imports trigger one idempotent Lea Check per formalization, including
  multiple matched declarations that share one compiler-checked Lean file;
- paused run with an artifact triggers a Lea Check;
- paused run without an artifact leaves Lea `N/A`;
- manual edit, chat, repair, and re-formalization invalidate/recheck correctly;
- a refresh reconstructs the same dual status and report;
- an evaluator settling after source drift is stored as superseded;
- reports bind to the exact git/content/dependency hashes;
- evaluator usage is recorded separately;
- restored artifacts after failed retries receive correct provenance.

### UI tests

- both labels appear on every formalization card;
- all combined states render without collapsing to a single status;
- reports render for approved, warning, paused, and error outcomes;
- warning and caveat counts are accurate;
- report expand/collapse and evidence navigation are keyboard accessible;
- stale reports are clearly marked and cannot appear current;
- Resume formalization and Resume/Retry Lea Check invoke the correct operation;
- human approval remains visually and semantically distinct.

### Evaluation corpus

Before default-on rollout, maintain a reviewed corpus containing at least:

- same statement and same proof strategy expressed differently in Lean;
- same statement proved by an unrelated valid method;
- weakened and strengthened statements;
- added and removed hypotheses;
- quantifier/domain changes;
- a source proof with a genuine gap silently repaired in Lean;
- an ambiguous notation choice;
- missing and ambiguously associated source proofs;
- incomplete but faithful artifacts;
- opaque automation that hides the proof route;
- definitions;
- counterexamples/disproofs;
- dependent proofs whose imported definitions changed.

False approval is the highest-severity evaluation defect. Pilot review should be
biased toward measuring and reducing false approvals before reducing noisy warnings.

## Acceptance Criteria

V1 is complete when all of the following are true:

1. Every Lea-marked item in the Lean pane displays separately labeled Lean Check and
   Lea Check states.
2. Lean Check uses exactly the six states `unformalized`, `stubbed`, `in-progress`,
   `checked`, `paused`, and `error`.
3. Lea Check uses exactly the six states `N/A`, `in-progress`, `paused`, `warning`,
   `error`, and `approved`.
4. A compiler-valid artifact can simultaneously display Lean `checked` and Lea
   `warning`.
5. Turn and cost limits produce Lean `paused`, unless a complete artifact was already
   verified, and expose a usable resume path.
6. The Overleaf solver uses a dedicated faithful-translation prompt. LeaChat's
   interactive prompt and behavior remain unchanged.
7. The solver cannot silently repair or reinterpret source mathematics without
   recording a user-visible finding.
8. A completed or paused solver run with an evaluable artifact automatically starts
   one idempotent Lea Check for that exact revision tuple.
9. The evaluator is read-only and cannot change Lean, LaTeX, git, or project metadata.
10. Every completed Lea Check produces a schema-valid report visible from the
    formalization card, including approved and error outcomes.
11. Warning reports identify each material discrepancy, explain its significance,
    and cite the relevant LaTeX and Lean evidence when available.
12. Approved reports list caveats, limitations, evaluated scope, and confidence.
13. Missing or ambiguous theorem-proof association cannot produce unqualified
    method-fidelity approval.
14. Reports are bound to exact source, artifact, and dependency revisions.
15. Changing source, artifact, or semantics-relevant local dependencies supersedes
    the old report and prevents it from appearing current.
16. Existing proof bytes remain git-backed; evaluation metadata and reports are
    persisted in SQLite; no stored session-status field is introduced.
17. Definitions and disproof/counterexample results retain their distinct result
    kinds without expanding the Lean Check status taxonomy.
18. Existing source-freshness, human-approval, cascade, self-repair, and batch
    behaviors continue to work.
19. Evaluator failures never downgrade or erase a valid Lean compiler result.
20. A page refresh reconstructs the same dual statuses and current report from
    persisted state.

## V1 Priority Breakdown

### P0: Required for pilot

- dual status model and UI;
- paused mapping and resume affordance for turn/cost caps;
- dedicated Overleaf solver prompt;
- source-proof association and hashing;
- terminal/pause-triggered read-only evaluator;
- structured persisted reports;
- report staleness/supersession;
- solver finding disclosure;
- tests protecting LeaChat from prompt regressions.

### P1: Recommended immediately after pilot

- user-triggered Lea Check for upgraded legacy artifacts;
- report comparison across revisions;
- dedicated checker model setting;
- improved evidence navigation into Overleaf and Lean panes;
- report-guided re-formalization.

### P2: Future

- interval or milestone-based continuous checks;
- incremental evaluation of only changed proof regions;
- project-level semantic consistency reports;
- calibrated checker ensembles or adjudication for low-confidence cases.

## Decisions Recorded by This Specification

1. Compiler validity and semantic fidelity are independent statuses.
2. The user-facing names are **Lean Check** and **Lea Check**.
3. V1 checks at completion and recoverable pauses, not continuously.
4. Every completed evaluation produces a report, including approval.
5. Approval may contain caveats but never material warning findings.
6. Checker failure is `error`; proof/source mismatch is `warning`.
7. Missing proof evidence is disclosed and cannot yield unqualified method approval.
8. Overleaf receives dedicated solver/evaluator prompts; LeaChat remains unchanged.
9. Evaluators are read-only.
10. Reports are immutable and revision-bound.
11. Turn/cost caps are pauses, not proof errors.
12. Report-guided retry is intentionally deferred but supported by the V1 data model.

## Remaining Product Decisions

These choices do not block the core architecture but should be settled before UI and
parser implementation is finalized:

1. Exact LaTeX syntax for explicit proof-to-target association.
2. Whether a warning report auto-expands the first time it appears.
3. Whether pilot users can disable automatic Lea Check per project.
4. Whether V1 uses the solver model for evaluation or exposes a separate checker
   model immediately.
5. The evaluator's default cost/turn budget and priority relative to solver runs.
6. Whether user interruption always counts as resumable or can be explicitly
   abandoned.
7. How long superseded reports remain in the default UI before moving behind a
   history control.
8. Whether `approved` with `scope: partial_artifact` is allowed in the pilot or
   conservatively converted to `warning` until evaluator calibration is mature.
