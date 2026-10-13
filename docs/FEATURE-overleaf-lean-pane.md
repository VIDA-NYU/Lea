# Feature: Project-Wide Overleaf Lean Pane

## Summary

Add a project-wide Lean pane to the Overleaf extension. The pane should act as
an alternative document view next to the PDF preview: instead of showing the
rendered PDF, it shows the labeled theorem and definition inventory for the
whole project, together with the Lean stubs and generated Lean artifacts
associated with those items.

The target product experience is that a user can choose a Lean view in the same
general way they can choose to view the PDF.

**Decision (2026-06-26):** a true PDF-preview sibling tab inside Overleaf's own UI
is **not** pursued — Overleaf exposes no stable surface for it. The pane ships as an
extension-owned floating panel, and that is the accepted V1 surface, not a stopgap.
It should still read as a project preview rather than a cramped inline widget, but
no further Overleaf-tab integration is planned.

Version 1 began read-only. As of 2026-06-26 the pane also supports **source
navigation** ("Go to source" jumps the editor to an item's block) and
**formalize-from-pane** (start a run for an actionable item, reusing the existing
`/formalize` flow with live status polling). See
`PLAN-overleaf-lean-pane-improvements.md` items 11–12.

## Goal

Let a user inspect all Lean-relevant mathematical content in an Overleaf project
without clicking through individual source blocks.

The current Overleaf workflow is target-oriented: the extension detects specific
formalization targets and the companion starts autonomous adapter runs for those
targets. That is useful for proving one item at a time, but it does not give the
user a project-level view of what Lea knows about the document.

The Lean pane should make the Overleaf integration feel project-oriented:

- Scan the whole Overleaf project.
- List labeled mathematical environments in rendered document order.
- Show missing Lean artifacts as first-class states.
- Show the natural-language statement together with the Lean stub.
- Let users expand an item to inspect its theorem-specific generated Lean
  artifact.

The architecture should remain the existing one:

```text
Overleaf page -> Chrome extension -> companion (:31245) -> FastAPI adapter (:8001)
```

There should be no separate prover HTTP service or new backend boundary. The
companion may produce the pane manifest, and the adapter still drives the
vendored prover in-process when formalization runs are eventually added.

## Current Behavior

The Overleaf extension currently focuses on formalizing individual targets from
the source document. Existing and proposed target detection mechanisms include:

```tex
\theorem[label=my_theorem_name]{
  Every finite tree has at least two leaves.
}
```

and comment-marked ordinary theorem environments:

```tex
\begin{theorem}\label{thm:even-square}
% lea: formalize label=even_square
If $n$ is even, then $n^2$ is even.
\end{theorem}
```

These flows do not provide a project-wide Lean preview. A user cannot currently
open a single pane that shows all labeled theorem and definition environments,
their generated Lean stubs, and their generated Lean artifacts.

## Proposed Behavior

The extension should expose a Lean pane for the current Overleaf project. The
pane should be available as a second document preview option alongside, or
equivalent to, the PDF preview.

The pane should list all Lea-marked mathematical environments across the whole
project (see "Included Items" for the marker rule). Items should appear in the
order their natural-language statements would appear in the rendered document.

Each item should show:

- Mathematical item kind.
- LaTeX label.
- Optional title or inferred display name.
- Lightly rendered natural-language content.
- Current Lean status.
- Generated Lean stub when available.
- A missing-stub message when no Lean has been generated.

Each item should be expandable. The expanded view should show the
theorem-specific generated Lean artifact associated with that item.

## Included Items

Version 1 should include **Lea-marked** instances of:

```text
theorem
lemma
proposition
corollary
definition
```

**Decision (2026-06-26):** an environment appears in the pane only when it carries a
Lea marker comment (`% lea: formalize …` / `% lea: define …`). An ordinary
theorem/definition environment that is merely `\label`-ed but has no Lea marker is
**omitted** — the pane is a marked-target inventory, not a catalogue of every
labeled environment in the document.

Definitions should be treated distinctly from proof-bearing statements. A
definition should be associated with a Lean `def`, not with a theorem-shaped
proof obligation.

Possible later item kinds:

```text
example
remark
exercise
custom theorem environments
```

## Pane Layout

A compact theorem entry could look like:

```text
Theorem: Compactness criterion
Label: thm:compactness
Status: Stub generated

[Lightly rendered natural-language statement]

Lean stub:
theorem compactness_criterion ... := by
  sorry
```

A definition entry with no generated Lean could look like:

```text
Definition: Locally finite family
Label: def:locally-finite
Status: Missing stub

[Lightly rendered natural-language definition]

No Lean stub has been generated yet.
```

When expanded, an entry should show:

- The theorem-specific generated Lean artifact.
- Source file path.
- Source range, if available.
- Lean declaration name, if known.
- Copy action for the Lean stub, when present.
- Copy action for the full generated artifact, when present.
- Status and stale-state information, when available.

The pane should include loading, empty, and error states.

## Statuses

Recommended version 1 statuses:

```text
missing-stub
stub-generated
valid
invalid
stale
unknown
error
```

`missing-stub` means a labeled mathematical environment was detected, but no
Lean stub or generated artifact is currently associated with it.

`stale` means the LaTeX source for the labeled item changed after the Lean
artifact was generated. It is displayed to users as **out of date**: the item
card uses an amber warning callout, keeps Re-formalize as its primary action,
and remains linked to the existing Lean artifact because freshness and Lean
validity are separate facts.

The in-editor badge over the LaTeX marker must derive freshness from the same
artifact provenance and source hash as the pane. A stale item therefore reads
**out of date** on both surfaces; neither surface may continue to claim that
the current LaTeX is formalized. The companion status response retains the
artifact lifecycle in `status` and reports `sourceFreshness` separately as
`current`, `stale`, or `unknown`.

Freshness covers all formalization-relevant marker inputs, not only the theorem
body. Changing `uses={...}` or `context={...}` on a `% lea:` activation comment
or inline Lea tag makes the artifact out of date even when the natural-language
statement is unchanged. Marker formatting and whitespace-only changes remain
normalized away.

## Ordering

Items should be ordered according to rendered document order, matching the order
in which the corresponding natural-language statements would appear in the PDF.

The implementation should not simply sort files alphabetically. A best-effort
version 1 ordering can be:

1. Determine the Overleaf root document.
2. Walk `\input` and `\include` references in source order.
3. Extract labeled items from each visited file in source order.
4. Assign a monotonically increasing `documentOrder` value.

If exact PDF order is not available in version 1, this root-document expansion
order is the expected fallback.

## Natural-Language Rendering

The pane should show natural-language theorem and definition text in a lightly
rendered form.

The rendering does not need to match the PDF perfectly, but it should be more
readable than raw LaTeX. Inline math should remain intelligible, and common
LaTeX commands should be displayed in a way that is useful for browsing.

The underlying raw LaTeX should still be preserved in the data model for source
hashing, artifact generation, diagnostics, and future re-rendering.

## Project Manifest

The feature should use an explicit Lean pane manifest rather than deriving all
generated artifacts from labels and paths alone.

LaTeX labels are the primary identity anchor, but a manifest gives the extension
and companion enough information for:

- Missing-stub states.
- Stale detection.
- Artifact lookup.
- Rendered ordering.
- Future jump-to-source behavior.
- Future formalize-one and formalize-all actions.

Useful item shape:

```ts
type LeanPaneItem = {
  id: string;
  label: string;
  kind: "theorem" | "lemma" | "proposition" | "corollary" | "definition";
  title?: string;

  documentOrder: number;

  sourceFile: string;
  sourceStartLine?: number;
  sourceEndLine?: number;
  sourceStartOffset?: number;
  sourceEndOffset?: number;
  sourceHash: string;

  naturalLanguageLatex: string;
  naturalLanguageRendered: string;

  leanKind: "theorem" | "def";
  leanDeclarationName?: string;
  leanStub?: string;
  leanArtifactPath?: string;
  leanArtifactContent?: string;

  status:
    | "missing-stub"
    | "stub-generated"
    | "valid"
    | "invalid"
    | "stale"
    | "unknown"
    | "error";

  generatedFromSourceHash?: string;
  lastGeneratedAt?: string;
};
```

The manifest can be produced by the companion and consumed by the extension UI.
It may be persisted or regenerated depending on the companion's current project
state model, but the UI should consume a single normalized shape.

## Auto-Refresh

The Lean pane should refresh automatically when project LaTeX source changes.

Expected behavior:

- Added labeled theorem or definition environments appear in the pane.
- Removed labeled environments disappear from the pane.
- Changed natural-language text updates in the pane.
- Generated Lean associated with changed source is marked stale when source
  hashing is available.

Refreshes should be debounced so ordinary typing does not cause excessive
project scans or companion requests.

Recommended behavior:

- Use a short debounce for local UI updates.
- Use a longer debounce or save-driven trigger for more expensive companion
  synchronization.
- Preserve expanded/collapsed state where possible across refreshes.

## Version 2: Source Navigation

A later version should let a user activate a Lean-pane item and navigate the
Overleaf LaTeX editor to the source theorem or definition.

Recommended interaction:

- Single click selects the Lean-pane item.
- Double click on the entry header jumps to the source environment.
- An explicit `Open source` button or icon performs the same action.
- The action is keyboard accessible.

The explicit button is recommended because double-click behavior is easy to miss
and less accessible.

Version 1 should already preserve the source metadata needed for this feature:

- Source file.
- Line range when available.
- Character or byte offset range when available.
- LaTeX label.

Possible later reverse-sync behavior: when the cursor is inside a theorem or
definition in the LaTeX editor, the corresponding Lean-pane item is highlighted.

## Version 2: Formalization Actions

Later versions should add actions such as:

- Formalize this item.
- Formalize all missing stubs.
- Re-run stale items.
- Show proof/check logs.
- Show latest proof artifact.
- Compare stale source against the source used for generation.
- Filter by missing, stale, valid, and invalid states.

Definitions should not be presented as proof jobs, though they may still support
generation, validation, stale detection, and artifact inspection.

## Compatibility

The new pane should not break existing Overleaf extension behavior.

The extension may continue to support current inline controls, comment-marked
formalization targets, and any legacy `\theorem[label=...]` compatibility path.
The Lean pane is a project-level browsing and inspection surface over the same
underlying project data.

## Acceptance Criteria

1. A user can open a Lean pane or equivalent Lean project view from Overleaf.
2. The view scans the whole Overleaf project, not just the active source file.
3. Only Lea-marked theorem and definition-like environments appear (a bare
   `\label` without a `% lea:` marker is omitted).
4. Marked items with no generated Lean still appear with `missing-stub` status.
5. Items are ordered in rendered document order as closely as possible.
6. Natural-language content is shown in a lightly rendered form.
7. Available Lean stubs are shown inline.
8. Expanding an item shows the theorem-specific generated Lean artifact.
9. Definitions are displayed as definitions and associated with Lean `def`s.
10. The pane auto-refreshes when project LaTeX source changes.
11. Each item includes source metadata suitable for future jump-to-source.
12. Existing extension behavior remains intact.
