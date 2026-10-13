# Feature: Overleaf Definition Formalization Tags

## Summary

Add an invisible Overleaf annotation for definition blocks that tells Lea to
formalize a mathematical definition instead of proving a theorem.

Authors should be able to write ordinary LaTeX definition environments and add a
non-rendering Lea comment:

```tex
\begin{definition}\label{def:even}
% lea: define label=even_nat context={Use Nat parity, not Int parity.}
A natural number $n$ is even if there exists $k$ such that $n = 2k$.
\end{definition}
```

The Overleaf extension should detect the marked definition, show it as a Lea
formalization target, and send it to the companion with a definition-specific
target kind. The companion should ask Lea to create the appropriate Lean
declaration, not to prove a theorem.

## Goal

Let mathematicians formalize vocabulary before proving results.

Definitions are often the right first step in a formalization project. The
current Overleaf flow is theorem-shaped: the parser, payloads, prompts, statuses,
and UI copy all assume that a target is a theorem whose final success condition
is a completed proof. That makes definitions awkward even though they are
essential mathematical objects.

This feature should make definition formalization a first-class Overleaf
workflow while preserving the same architecture:

```text
Overleaf page -> Chrome extension -> companion (:31245) -> FastAPI adapter (:8001)
```

There should be no separate prover server or new backend boundary. The companion
still starts autonomous adapter runs, and the adapter still drives the vendored
prover in-process.

## Current Behavior

The Overleaf extension currently formalizes labeled theorem-like targets. The
legacy syntax is a custom command:

```tex
\theorem[label=my_theorem_name]{
  Every finite tree has at least two leaves.
}
```

There is also a newer comment-marker design for ordinary theorem environments:

```tex
\begin{theorem}
% lea: formalize label=tree_has_two_leaves
Every finite tree has at least two leaves.
\end{theorem}
```

Both workflows are still theorem-oriented. They send fields such as
`theoremLabel`, `theoremText`, `theoremUses`, and `theoremContext` to the
companion, and the companion prompt asks Lea to complete a proof.

## Proposed Behavior

The extension should support a separate marker command for definitions:

```tex
% lea: define label=<lean_identifier> uses={...} context={...}
```

When the parser finds this marker inside a supported definition-like
environment, it should treat the enclosing environment as a definition target.

Initial supported environment:

```tex
\begin{definition} ... \end{definition}
```

Possible later environments:

```tex
\begin{notation} ... \end{notation}
\begin{convention} ... \end{convention}
\begin{remark} ... \end{remark}
```

Unmarked definition environments should be ignored.

## Marker Syntax

Recommended single-line form:

```tex
% lea: define label=even_nat uses={natural_number_convention} context={Use Nat, not Int.}
```

Recommended multiline form:

```tex
% lea: define
% lea: label=Subadditive
% lea: uses={real_sequence}
% lea: context={Represent this as a predicate on functions Nat -> Real.}
```

The parser should also accept the more general form:

```tex
% lea: formalize kind=definition label=Subadditive
```

The documented default should be `% lea: define ...` because it is concise and
clear to mathematicians.

## Metadata Fields

### `label`

Required stable identifier for the definition inside Lea.

The label should be a valid Lean identifier:

```text
[A-Za-z_][A-Za-z0-9_]*
```

Invalid labels should produce a clear UI error rather than silently hiding the
target.

### `uses`

Optional list of earlier Lea labels this definition depends on:

```tex
% lea: define label=Subadditive uses={real_sequence}
```

Values should refer to Overleaf/Lea labels, not raw Lean names. They may point to
earlier definitions or earlier theorems, as long as the companion can resolve
them to checked Lean artifacts.

### `context`

Optional natural-language guidance passed to Lea:

```tex
% lea: define label=even_nat context={Use Exists k, n = 2 * k.}
```

This field is especially important for definitions because mathematical prose
can map to different Lean representations. The user may want to guide Lea toward
a `def`, `structure`, `class`, `abbrev`, notation, or a predicate.

### `kind`

Optional when using `% lea: formalize ...`.

For this feature, the supported value is:

```text
definition
```

`% lea: define ...` should be treated as sugar for
`% lea: formalize kind=definition ...`.

## Block Extraction

Given this source:

```tex
\begin{definition}\label{def:subadditive}
% lea: define label=Subadditive context={Use a predicate over Nat -> Real.}
A sequence $a_n$ is subadditive if $a_{m+n} \le a_m + a_n$ for all $m,n$.
\end{definition}
```

The extension should pass the enclosing definition body to the companion,
excluding Lea marker comments and the LaTeX label.

Useful target shape:

```json
{
  "targetKind": "definition",
  "targetLabel": "Subadditive",
  "targetText": "A sequence $a_n$ is subadditive if $a_{m+n} \\le a_m + a_n$ for all $m,n$.",
  "targetUses": [],
  "targetContext": "Use a predicate over Nat -> Real.",
  "latexEnvironment": "definition",
  "latexLabel": "def:subadditive",
  "sourceHash": "..."
}
```

For a transitional implementation, the companion may continue accepting
`theoremLabel`, `theoremText`, `theoremUses`, and `theoremContext`, but the
request should include `targetKind: "definition"` so the companion can choose the
definition prompt.

## Companion Behavior

The companion should validate definition targets with the same basic guarantees
as theorem targets:

1. The Overleaf project id is present.
2. The target label is a valid Lean identifier.
3. Each `uses` label is a valid Lean identifier.
4. The target text is non-empty.
5. `sourceHash`, when present, matches the normalized target text.

The companion should resolve `uses` against prior checked artifacts. Definition
targets should be allowed to depend on prior definition targets and prior theorem
targets.

The job key should include the project slug, target kind, and label so a
definition and theorem with the same label cannot collide:

```text
<projectSlug>:definition:<targetLabel>
```

The job record should eventually use target-neutral fields:

```json
{
  "mode": "formalization",
  "targetKind": "definition",
  "targetLabel": "Subadditive",
  "targetTextHash": "...",
  "targetUses": [],
  "targetContext": ""
}
```

## Lea Prompt Behavior

Definition mode should use a distinct prompt. It should not reuse theorem proof
language with small substitutions.

Suggested prompt shape:

```text
Formalize the Overleaf definition labeled <label> in project <projectSlug>.
This target is a definition, not a theorem.

Create the appropriate Lean declaration or small group of declarations for the
mathematical concept described below. Do not create a fake theorem just to
satisfy a proof workflow.

If the prose naturally maps to a predicate, prefer a named `def`.
If it introduces bundled data and properties, consider `structure` or `class`.
If it introduces a shorthand, consider `abbrev` or notation.
Use the declaration name <label> for the primary declaration unless the text
explicitly specifies a better Lean name.

The final Lean file must compile with no sorry/admit.
Use the Lea project context to choose the project namespace and proof path.
Do not edit the project markdown during formalization; Lea will record the final
result after the declaration compiles.

Overleaf definition text:
<targetText>

Formalization guidance:
<targetContext>
```

The final artifact may contain helper declarations, but one primary declaration
must be identifiable for the Overleaf label.

## UI Behavior

The extension should avoid theorem-specific copy for definition targets.

Suggested action labels:

```text
Formalize definition
Regenerate definition
View in Lea UI
```

Suggested status copy:

```text
unformalized
formalizing
formalized
failed
```

If the UI later introduces target-specific statuses, `defined` is acceptable for
successful definition targets. Until then, `formalized` is acceptable because it
applies to both theorem and definition workflows.

Avoid copy such as:

```text
Proof complete
Theorem proved
Generate proof
```

## Detection Rules

The parser should:

1. Detect `% lea: define` comments in `.tex` source.
2. Detect `% lea: formalize kind=definition` comments as an alias.
3. Associate the marker with the smallest enclosing supported definition-like
   environment.
4. Parse adjacent Lea metadata comments in that block.
5. Remove Lea marker comments from the text sent to Lea.
6. Remove the LaTeX `\label{...}` from the text sent to Lea.
7. Preserve source ranges so editor badges can be placed near the definition.
8. Report malformed markers or invalid labels clearly.
9. Ignore unmarked definition environments.

## Compatibility

Existing theorem workflows should continue to work:

```tex
\theorem[label=foo]{...}
```

```tex
\begin{theorem}
% lea: formalize label=foo
...
\end{theorem}
```

Definition support should not change the meaning of theorem markers. If a
theorem-like environment contains `% lea: define`, the parser should report a
clear environment mismatch. If a definition environment contains
`% lea: formalize` without `kind=definition`, the parser should either reject it
with a clear message or default it to a definition target only if that behavior
is documented.

## Implementation Notes

The parser should move toward target-neutral names such as `parseTargets`,
`targetKind`, `targetLabel`, and `targetText`. The current theorem-specific names
can remain during migration, but new definition behavior should avoid deepening
the theorem-only API shape.

The repo currently has two relevant parser surfaces:

- `apps/overleaf-extension/shared/theoremParser.mjs`
- `apps/overleaf-extension/extension/pageBridge.js`

The definition marker should not be implemented twice with diverging logic. The
injected page bridge and the shared tests should agree on the same target model,
diagnostics, ranges, and extracted text.

## Acceptance Criteria

1. A `definition` environment containing `% lea: define label=foo` appears in the
   extension UI as a formalizable definition target.
2. The marker is invisible in the rendered PDF because it is a LaTeX comment.
3. The extracted target text excludes Lea marker comments and LaTeX labels.
4. `label`, `uses`, and `context` preserve the same semantics as theorem targets.
5. Invalid labels, missing labels, and environment mismatches produce clear UI
   diagnostics.
6. Starting the action sends `targetKind: "definition"` to the companion.
7. The companion uses a definition-specific Lea prompt.
8. Lea is instructed to generate checked Lean declarations, not theorem proofs.
9. Definition targets can depend on earlier formalized definitions and theorems.
10. A successful definition target is reusable by later theorem `uses={...}`.
11. Existing theorem formalization behavior remains unchanged.
12. Tests cover parser detection, diagnostics, payload shape, prompt text, status
    display, and dependency resolution.

## Future Extensions

The marker namespace can support more mathematical target kinds:

```tex
% lea: notation label=...
% lea: example label=...
% lea: axiom label=...
% lea: convention label=...
```

These are not required for the first definition-tag release. The parser and
payload design should leave room for them by modeling all Overleaf formalization
items as typed targets rather than only as theorems.
