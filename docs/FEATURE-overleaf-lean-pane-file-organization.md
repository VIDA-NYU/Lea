# Feature: Overleaf Lean Pane File Organization

## Summary

Organize the Overleaf Lean pane by source `.tex` file instead of showing every
formalization target in one flat list.

Version 1 should present a compact mini filesystem inside the existing
extension-owned Lean pane. A user should be able to expand the entry for
`main.tex` and see the theorem, lemma, proposition, corollary, and definition
work Lea has done inside that file. Projects with paths such as
`sections/intro.tex` should render nested folder rows so the pane feels like a
small project browser rather than a long undifferentiated feed.

The feature is organizational only. It should not change formalization behavior,
source navigation, generated Lean artifacts, status computation, or the existing
architecture:

```text
Overleaf page -> Chrome extension -> companion (:31245) -> FastAPI adapter (:8001)
```

## Goal

Make the project-wide Lean pane easier to scan once a document contains more
than a handful of formalized statements.

The current pane already gives users a project-level inventory of Lea-marked
mathematical environments. However, all items appear together. That makes it
hard to answer basic project-navigation questions:

- Which `.tex` file did this formalization come from?
- What work has Lea done in `main.tex`?
- Which section file contains stale or invalid formalizations?
- Where should I expand next when reviewing a large Overleaf project?

Grouping by `.tex` file should preserve the existing item-level workflow while
adding enough structure to support project review.

## Proposed Behavior

The Lean pane should render a collapsible tree of folders and `.tex` files.

Only `.tex` files that contain at least one Lea-pane item should appear in the
tree. A project file with no Lea-marked theorem or definition work should be
hidden in version 1, even if it exists in the Overleaf project archive.

The detected root file, usually `main.tex`, should be expanded by default when
the pane first opens. Other file rows should start collapsed unless the user
expands them.

For nested project paths, the pane should render folder rows:

```text
main.tex
sections/
  intro.tex
  definitions.tex
appendix/
  extra_examples.tex
```

Folder rows should expand and collapse. File rows should also expand and
collapse. Expanding a file row reveals the existing item cards for that file in
their source/document order.

File rows are not navigation actions in version 1. Clicking a file row should
only expand or collapse it. Source navigation remains item-level through the
existing "Go to source" action.

## Included Items

The file tree should group the same items the existing Lean pane inventories:

```text
theorem
lemma
proposition
corollary
definition
```

The existing marker rule still applies. An environment appears only when it has
a Lea marker comment such as:

```tex
% lea: formalize label=compactness_criterion
```

or:

```tex
% lea: define label=locally_finite_family
```

Unmarked LaTeX environments should remain omitted.

## File Row Content

Each file row should show:

- the file name or path segment appropriate to its tree location,
- the number of Lea items in that file,
- an aggregate status for those items.

The root file should be visually identifiable when possible, for example with a
small `root` label or equivalent secondary metadata.

The aggregate file status should help users find active or problematic files
quickly. Recommended precedence:

1. `in progress` if any child item is currently running.
2. `error` or `invalid` if any child item failed.
3. `stale` if any child item is stale.
4. `missing stub` or `stub generated` if work exists but is incomplete.
5. `valid` or `defined` when all child items are successful terminal states.
6. `mixed` when several terminal categories apply and no more specific status
   is accurate.

File-level status is informational only. Version 1 should not introduce
file-level "formalize all", "copy all", or "go to file" actions.

## Item Behavior

Item cards inside a file group should behave exactly as they do in the current
flat pane.

Each item should continue to show:

- mathematical item kind,
- optional title or inferred display name,
- Lea label,
- current item status,
- lightly rendered natural-language content,
- generated Lean stub when available,
- missing-stub text when no Lean stub exists.

Expanding an item should continue to show:

- source file path,
- source range when available,
- Lean declaration name when known,
- generated Lean artifact when available,
- "Go to source",
- "Formalize" or "Re-formalize" when the item is actionable,
- copy actions for stub and artifact content when available.

Grouping should not alter the item ids, duplicate-label handling, staleness
behavior, or formalize-from-pane payload.

## Navigation and State

Folder and file expansion state should persist while the pane remains open and
should survive background refreshes and polling updates.

Item expansion state should remain independent of file expansion state. If an
expanded item is inside a collapsed file, the item may remain logically expanded
and should appear expanded again when the file is reopened.

Refresh should not unexpectedly collapse the tree unless:

- the Overleaf project changes,
- the source file disappears from the manifest,
- the item no longer exists,
- or the user closes and reopens the pane.

The pane status line should summarize the grouped inventory. Example:

```text
8 labeled items across 3 .tex files from main.tex.
```

When no items exist, the pane should keep the current empty-state behavior:

```text
No labeled theorem, lemma, proposition, corollary, or definition environments found.
```

## Accessibility

Folder rows, file rows, and item headers should use normal disclosure semantics.
They should communicate expanded/collapsed state to assistive technology and be
operable by keyboard.

The visible hierarchy should be understandable without relying only on color.
Indentation, disclosure controls, labels, and item counts should carry the
structure.

## Non-Goals

Version 1 should not add:

- batch formalization at the file or folder level,
- a full Overleaf file explorer replacement,
- source opening from file rows,
- filters or search inside the pane,
- drag-and-drop organization,
- persistence of expansion state across browser sessions,
- changes to the companion/adapter architecture.

## Acceptance Criteria

- A single-file project with Lea work shows one expanded root file row containing
  all current item cards.
- A project with `main.tex` and `sections/defs.tex` shows `main.tex` expanded by
  default and `sections/defs.tex` nested under a `sections` folder.
- `.tex` files with no Lea-marked items do not appear.
- Items remain ordered within each file according to the existing document/source
  order.
- Aggregate file status updates when child item statuses change.
- Background polling during an in-progress formalization updates item and file
  statuses without collapsing expanded folders or files.
- Existing item actions still work from inside grouped files.
- Duplicate labels across files remain visible and distinguishable by their file
  grouping.
- The empty project state remains clear and does not render an empty tree.

