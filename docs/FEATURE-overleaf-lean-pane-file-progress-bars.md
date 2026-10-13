# Feature: Overleaf Lean Pane File Progress Bars

## Summary

Replace the file-level status chip in the Overleaf Lean pane with a proportional
progress bar that summarizes the Lea-marked content in that `.tex` file.

The pane already groups formalization targets by source file. Today, each file
row shows one aggregate status such as `missing stub`, `invalid`, `stale`, or
`valid`. That single label hides the distribution of work inside a file. A file
with one failed theorem and seven successful theorems can look just as urgent as
a file where every target failed.

The new file-row bar should show the composition directly:

- grey for unformalized content,
- red for failed formalizations,
- green for successful formalizations,
- yellow for sorry-stubbed content.

For example, if a file contains eight Lea-marked items and one failed to
formalize, one eighth of the bar should be red. If four of those eight items
have never been formalized, half of the bar should be grey. If six of eight have
successful Lean artifacts, three quarters of the bar should be green.

This is a presentation change inside the Overleaf extension Lean pane. It should
not change how formalization jobs run, how item statuses are computed, or the
shared backend architecture:

```text
Overleaf page -> Chrome extension -> companion (:31245) -> FastAPI adapter (:8001)
```

## Goal

Make each file row answer a project-review question at a glance:

```text
How much of this file is done, unfinished, stubbed, or broken?
```

The current aggregate status uses precedence. That is useful for warning users
that a file needs attention, but it loses proportion. The progress bar should
let users distinguish a mostly-complete file with one problem from a file that
has barely been formalized.

## Scope

This feature applies to file rows in the grouped Overleaf Lean pane introduced
by `FEATURE-overleaf-lean-pane-file-organization.md`.

The item cards inside each file should keep their existing status chips and
actions. Folder rows may use the same progress-bar summary in a later extension,
but V1 only requires file rows.

## Denominator

The denominator is the number of Lea-marked pane items in the source `.tex` file.

In V1, this is the same set of items already included in the Lean pane:

```text
theorem
lemma
proposition
corollary
definition
```

Only Lea-marked environments count. Ordinary unmarked theorem or definition
environments should not affect the bar.

Each item contributes exactly one unit to exactly one segment. Segment width is:

```text
segment item count / total Lea-marked item count in file
```

Examples:

- 4 unformalized items out of 8 -> 50% grey.
- 1 failed item out of 8 -> 12.5% red.
- 6 successful items out of 8 -> 75% green.
- 2 sorry-stubbed items out of 3 -> 66.67% yellow.

The implementation should use the item count as the source of truth, not source
line count, token count, generated Lean length, or theorem complexity.

## Status Buckets

File progress bars should map item statuses into four user-facing buckets.

### Success: Green

Green means Lea produced a successful terminal result for the item.

This bucket includes:

- `valid`
- `formalized` when received from companion/status payloads before pane mapping
- `defined`
- `disproved` / counterexample found

A counterexample or verified disproof remains semantically distinct at the item
level, but for the file progress bar it counts as successful work because Lea
completed the mathematical task with a verified result.

### Sorry Stubbed: Yellow

Yellow means the item has a generated Lean artifact that still relies on `sorry`
or `admit`.

This bucket includes:

- `sorry_stub`
- `stub-generated`

Yellow should not be counted as green even if the artifact currently typechecks,
because the mathematical work is intentionally incomplete.

### Failed: Red

Red means Lea attempted or checked the item and the current result failed.

This bucket includes:

- `failed`
- `invalid`
- `error`

If a status object exposes an `effectiveStatus`, the bar should use the current
visible pane status after that normalization, so item cards and file bars agree.

### Unformalized: Grey

Grey means there is Lea-marked content in the file but no current completed
formalization result for that item.

This bucket includes:

- `missing-stub`
- `unformalized`
- `unknown`
- `stale`
- missing or unrecognized statuses

`stale` counts as grey in the file progress bar because the available artifact
does not correspond to the current source text. The item card may still show the
more specific `stale` label and offer the existing re-formalization action.

## In-Progress Items

An in-progress item should not introduce a fifth persistent color in V1.

While a formalization is running, the file row may show a lightweight animated
or striped overlay on the bar and keep the row accessible label updated with the
number of running items. For proportional accounting, an in-progress item should
remain in its last known bucket if it has one. If it has no prior completed
state, it should count as grey until the run settles.

This keeps the color vocabulary stable while still making live activity visible.

## Visual Behavior

The file row should contain:

- the existing disclosure control,
- the file name,
- the item count,
- the new progress bar.

The old single aggregate file status chip should be removed from file rows or
demoted to non-visible accessible text. Item-level status chips should remain.

The bar should have a fixed width or responsive width that does not cause file
row layout shift as statuses change. Segments should fill the full bar when the
file has at least one item.

Recommended segment order from left to right:

```text
green success | yellow sorry-stubbed | red failed | grey unformalized
```

The order should be stable across refreshes. Zero-count segments should not be
rendered visually, but their counts should still be available in computed data
for tests and accessible summaries.

The bar should use existing Overleaf extension color tokens where possible:

- green: existing success green,
- yellow: existing amber/yellow stub color,
- red: existing failure red,
- grey: existing muted line/status grey.

The bar must remain readable in narrow panes. On very small widths, it is better
for the bar to keep its minimum usable width and let file-name text truncate or
wrap than for one-item segments to become visually misleading.

## Labels and Accessibility

The bar should not rely on color alone.

Each progress bar should expose a concise accessible label such as:

```text
main.tex: 8 Lea items, 6 successful, 1 failed, 1 unformalized.
```

When yellow items exist:

```text
analysis.tex: 6 Lea items, 2 successful, 4 sorry-stubbed.
```

The visible row may show a compact text summary next to or inside the bar if it
fits without crowding, for example:

```text
6/8 done
```

The compact visible summary is optional in V1. The accessible summary is
required.

Segment titles or tooltips should include the bucket name, count, and percent:

```text
Failed: 1 of 8, 12.5%
```

## Data Model

The pane helper layer should compute a file progress summary from the same items
used to build the file tree.

Suggested shape:

```js
{
  total: 8,
  success: 3,
  sorryStubbed: 2,
  failed: 1,
  unformalized: 2,
  inProgress: 0
}
```

The tree node can keep the existing aggregate `status` field for compatibility
while the UI migrates, but new rendering should use the progress summary rather
than the precedence status. If the old aggregate status becomes unused after the
migration, it can be removed in a later cleanup.

## Edge Cases

A file row should not render when a file has zero Lea-pane items, matching the
current grouped-pane behavior.

If all items are unformalized, the bar should be entirely grey.

If all items are successful, the bar should be entirely green.

If a file has one item, the bar should be one full-width segment in that item's
bucket.

If a segment is mathematically nonzero but visually tiny, it should still be
represented. The tooltip and accessible label are the source of exact counts.

If a status is unknown to the mapper, count it as unformalized grey and keep the
item-level display fallback as `unknown`.

## Non-Goals

V1 should not add:

- file-level formalize-all actions,
- folder-level progress bars as a requirement,
- new item statuses in the companion or adapter,
- changes to formalization job scheduling,
- changes to the generated Lean artifact format,
- weighted progress by theorem length or proof complexity,
- persistence of historical progress after an item disappears from the source.

## Acceptance Criteria

- A file with 8 Lea-marked items, 4 `missing-stub`, 1 `invalid`, and 3 `valid`
  renders a bar that is 50% grey, 12.5% red, and 37.5% green.
- A file with 8 Lea-marked items, 6 successful terminal statuses, 1
  `sorry_stub`, and 1 `missing-stub` renders 75% green, 12.5% yellow, and 12.5%
  grey.
- A file with 3 Lea-marked items and 2 `stub-generated` items renders two thirds
  yellow.
- `defined` and `disproved` count toward the green success segment.
- `failed`, `invalid`, and `error` count toward the red segment.
- `stale`, `unknown`, and missing statuses count toward the grey segment.
- Expanding and collapsing file rows still works exactly as before.
- Background polling updates the bar proportions without collapsing expanded
  folders, files, or items.
- Item-level status chips and actions continue to show the precise item status.
- The progress bar has an accessible label with total count and bucket counts.
- Unit tests cover the status-to-bucket mapper and representative fractional
  summaries.
