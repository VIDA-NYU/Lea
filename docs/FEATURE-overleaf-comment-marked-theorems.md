# Feature: Comment-Marked Theorem Block Detection

## Summary

Replace the current Lea-specific `\theorem[label=...]{...}` wrapper with a
non-rendering LaTeX comment marker that tells the Overleaf extension which
ordinary theorem-like block should be formalized.

Instead of requiring authors to rewrite theorem statements into a custom command,
authors can keep their existing LaTeX structure:

```tex
\begin{theorem}\label{thm:even-square}
% lea: formalize label=even_square uses={even_def} context={Use the parity definition first.}
If $n$ is even, then $n^2$ is even.
\end{theorem}
```

The extension detects the `lea:` comment, finds the enclosing LaTeX block, and
sends that block's theorem text to the companion service.

## Goal

Make Lea formalization opt-in through lightweight annotations instead of custom
document syntax.

The author should be able to use Lea without changing the mathematical document
structure, redefining theorem commands, or introducing rendering-specific macros.
The marker acts as an annotation layer over ordinary LaTeX.

## Current Behavior

The Overleaf extension currently detects Lea targets through a custom command:

```tex
\theorem[label=my_theorem_name]{
  Every finite tree has at least two leaves.
}
```

The command carries all Lea metadata in its optional argument:

```tex
\theorem[
  label=main_bound,
  uses={auxiliary_bound, monotonicity_lemma},
  context={Start from auxiliary_bound, then apply monotonicity_lemma.}
]{
  The desired main bound holds.
}
```

This works for controlled beta examples, but it forces users to wrap statements
in Lea-specific syntax and define or redefine `\theorem` in the document
preamble.

## Proposed Behavior

The extension should scan the active `.tex` source for Lea marker comments:

```tex
% lea: formalize label=main_bound
```

When it finds a marker, it should determine the smallest enclosing theorem-like
environment and treat that environment as the formalization target.

Examples of initially supported environments:

```tex
\begin{theorem} ... \end{theorem}
\begin{lemma} ... \end{lemma}
\begin{proposition} ... \end{proposition}
\begin{corollary} ... \end{corollary}
```

Unmarked theorem-like environments should be ignored.

## Marker Syntax

Recommended single-line form:

```tex
% lea: formalize label=my_theorem uses={prior_theorem} context={Hint text}
```

Recommended multiline form:

```tex
% lea: formalize
% lea: label=main_bound
% lea: uses={aux_bound, mono_lemma}
% lea: context={Apply aux_bound, then use monotonicity.}
```

The parser should treat adjacent `lea:` comment lines inside the same block as
metadata for the same target.

## Metadata Fields

### `label`

Deprecated optional stable identifier for the theorem inside Lea. When omitted,
Lea assigns and persists an internal identifier. An existing LaTeX `\label{...}`
can serve as a readable reference in `uses={...}` or `proof-for={...}`.

The label should remain a valid Lean identifier:

```text
[A-Za-z_][A-Za-z0-9_]*
```

Invalid labels should produce a clear UI error rather than making the theorem
silently disappear.

### `uses`

Optional list of earlier Lea theorem labels this theorem depends on:

```tex
% lea: formalize label=main_bound uses={aux_bound, mono_lemma}
```

Values should keep the same meaning as today: Overleaf/Lea labels, not raw Lean
declaration names.

### `context`

Optional natural-language guidance passed to Lea:

```tex
% lea: formalize label=even_square context={Use the parity definition first.}
```

The parser should preserve commas and bracketed Lean hints inside braced context
values.

## Label Inference

The primary path should be an explicit Lea label:

```tex
% lea: formalize label=even_square
```

As a later convenience, if no explicit Lea label is present, the parser could
infer one from a LaTeX `\label{...}` inside the enclosing block:

```tex
\begin{theorem}\label{thm:main-bound}
% lea: formalize
...
\end{theorem}
```

Possible handling:

- Normalize `thm:main-bound` to `thm_main_bound`.
- Or reject it with a clear message asking the user to add `label=...`.

The explicit `label=...` should always win over inferred labels.

## Block Extraction

Given this source:

```tex
\begin{lemma}\label{lem:helper}
% lea: formalize label=helper_lemma
Let $G$ be a finite group. Then ...
\end{lemma}
```

The extension should pass the enclosing lemma statement to Lea, excluding the Lea
marker comment itself.

Useful payload shape:

```json
{
  "label": "helper_lemma",
  "kind": "lemma",
  "latexLabel": "lem:helper",
  "text": "Let $G$ be a finite group. Then ...",
  "uses": [],
  "context": ""
}
```

Including `kind` and `latexLabel` is optional for compatibility, but useful for
prompt quality and diagnostics.

## Detection Rules

The parser should:

1. Detect `% lea: formalize` comments in `.tex` source.
2. Associate each marker with the smallest enclosing theorem-like environment.
3. Parse adjacent Lea metadata comments in that block.
4. Remove Lea marker comments from the theorem text sent to Lea.
5. Preserve the original source range so the extension can place UI badges near
   the marked theorem.
6. Ignore unmarked theorem-like environments.
7. Report malformed markers or invalid labels clearly.

## Compatibility

The existing `\theorem[label=...]{...}` syntax can remain temporarily as a
compatibility path while the new comment-marker syntax becomes the documented
default.

Documentation should steer new users toward comment markers because they are:

- LaTeX-safe.
- Invisible in rendered PDFs.
- Compatible with existing theorem environments.
- Easier to add to existing Overleaf documents.
- Easier to search with plain text tools.

## Acceptance Criteria

1. A theorem-like environment containing `% lea: formalize ...` appears in the
   extension UI as a formalizable target.
2. The extension extracts the full enclosing theorem block.
3. The theorem text sent to Lea excludes Lea marker comments.
4. `label`, `uses`, and `context` preserve their current semantics.
5. Invalid or missing labels produce a clear UI error.
6. Multiple marked theorem blocks in the same file are detected independently.
7. Unmarked theorem environments are ignored.
8. The old custom `\theorem[...] {...}` syntax either remains as a temporary
   compatibility path or is explicitly deprecated in docs.

## Future Extensions

The comment namespace leaves room for additional commands:

```tex
% lea: ignore
% lea: stub label=my_theorem
% lea: formalize mode=autonomous
```

These are not required for the first version, but the parser design should avoid
closing off this direction.
