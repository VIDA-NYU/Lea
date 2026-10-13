# Plan — Overleaf Lean Pane Improvements

Suggested improvements to the project-wide Lean pane (branch `lean_pane_overleaf`,
commits `e56ff37` + `6a88ac5`, not yet merged into `main`). Each item is tagged
with an expected **difficulty**, **reasoning**, and a **classification**:

- **Implementation adjustment** — a change to how the existing V1 feature is built;
  no change to the agreed product behavior.
- **Feature-spec adjustment** — a change to *what* the feature does or how its
  behavior is defined, requiring a product/scope decision (often with
  implementation behind it).

Grounding: the pane is built from three layers —
`shared/leanPaneManifest.mjs` (pure parser), the companion's
`handleLeanPaneManifest` / `enrichLeanPaneItem` in `companion/server.mjs`, and the
extension UI in `extension/content.js` + `content.css`. See
`docs/FEATURE-overleaf-lean-pane.md` for the V1 spec (read-only; source
navigation and formalization actions are explicitly deferred to later versions).

## Status (updated 2026-06-26)

- **Done:** items 1–12. The seven implementation adjustments (1–7), the two
  status-taxonomy spec adjustments (8 disproved≠failed, 9 defined≠proved), and the
  two V2 roadmap features (11 source navigation, 12 formalize from pane) are
  implemented with tests; the overleaf-extension suite is green.
- **Item 9 resolution:** the pane keeps its artifact-lifecycle vocabulary and adds
  distinct `defined`, `disproved` (chip label "counterexample"), and `in-progress`
  states (rather than adopting the full product run-status vocabulary).
- **Item 11:** "Go to source" posts `OL_LEAN_NAVIGATE`; pageBridge selects/scrolls
  the CodeMirror range (same-file is precise; a different file is opened
  best-effort via Overleaf's IDE API, then selected).
- **Item 12:** the manifest now carries marker metadata (`formalizable`,
  `targetUses`, `targetContext`); a "Formalize"/"Re-formalize" action on actionable
  items reuses the existing `/formalize` flow, then live polling (item 4) takes over.
- **Decided, no code change:** item 10 — do **not** show unmarked environments
  (the pane stays a marked-target inventory; already the implemented behavior, with
  a test) and item 13 — **abandon** full Overleaf-tab integration and keep the
  extension-owned floating pane as the accepted V1 surface. Both recorded in
  `FEATURE-overleaf-lean-pane.md`.
- **All plan items (1–13) are now resolved.**

---

## Implementation adjustments

### 1. Verify and align the staleness hashes — **Medium** — *highest-value fix*

Staleness is detected by comparing `item.sourceHash` (pane side) against
`job.targetTextHash` (formalize side):

- Pane: `hashLeanPaneSource(naturalLanguageLatex)` — sha256 of `normalizeLeanPaneText`
  applied to the environment body with the `% lea:` marker line and `\label{}`
  stripped.
- Formalize: `hashTargetText(targetText)` — sha256 of `normalizeTargetText` applied
  to `targetText`, which is extracted by a **separate** code path.

The two normalizers are equivalent (`\s+`→single space, then trim), but the
*inputs* are produced independently. If they are not byte-identical, every
formalized item shows **permanently stale** (or never stale), silently breaking the
feature. This is the top correctness risk.

**Fix:** introduce one shared canonicalization helper used by both the manifest
parser and the formalize path, and add a test that runs a single block through both
extractions and asserts the hashes match.

### 2. Parallelize per-item enrichment — **Low/Medium**

`handleLeanPaneManifest` enriches items sequentially:

```js
for (const item of manifest.items) {
  items.push(await enrichLeanPaneItem({ item, state, overleafProjectId }));
}
```

Each `enrichLeanPaneItem` does filesystem reads plus a possible
`fetchApiSessionDetail` network round-trip. For N items this is N sequential waits.

**Fix:** run enrichment with `Promise.all` (optionally bounded with a small
concurrency limit), turning the wall-clock cost from O(N) into roughly O(1) for
typical project sizes.

### 3. Stop re-downloading the whole project on every keystroke — **Low/Medium**

An active-file edit triggers
`scheduleLeanPaneRefresh → refreshLeanPaneNow({ forceFetch: true })`, and
`forceFetch: true` forces `collectProjectTexFiles` (a full project fetch) instead of
the cheap `overlayActiveTex` path that already exists. So while the pane is open,
editing re-downloads the entire project roughly every 1.5s.

**Fix:** use the overlay path for edit-driven refreshes (the live active-editor text
is already available); reserve a full fetch for the manual refresh button and for
project switches.

### 4. Live refresh while a run is in progress — **Medium**

The pane only refreshes on an edit or a manual refresh click. A formalization run
that completes in the background (for example, one started from the standalone UI)
leaves the pane stale until the user manually refreshes.

**Fix:** poll the manifest while any item is `unknown`/in-progress, and stop polling
once everything settles. This becomes more important alongside item 12
(formalize-from-pane actions).

### 5. Fix duplicate-label id collisions — **Low**

Item identity is `item.id = ${kind}:${label}`. The manifest already reports duplicate
labels as a `duplicate_label` diagnostic but still emits multiple items sharing the
same id. In the UI that means expanding one duplicate expands all of them, and
`card.dataset.itemId` collides.

**Fix:** disambiguate the id (e.g. append `documentOrder` or the source path).

### 6. Extract and unit-test the pane's content.js logic — **Medium**

The manifest parser and the companion endpoint are well covered (the branch is green
at 91 overleaf tests), but the render/refresh/overlay/debounce logic lives inside the
`content.js` IIFE and is untested.

**Fix:** pull the pure pieces — file overlay, status formatting, manifest→DOM
shaping — into a small importable module and cover them the way
`leanPaneManifest.mjs` and the companion handlers already are.

### 7. Accessibility, focus, and scroll polish — **Low**

Minor but cheap UX gaps: Escape closes the target popover but not the pane; opening
the pane does no focus management; and `renderLeanPaneManifest` does a full
`replaceChildren` on each refresh, dropping the user's scroll position and any
transient focus.

**Fix:** bind Escape to `closeLeanPane`, move focus into the pane on open, and
preserve scroll position across refreshes (or diff/patch cards instead of rebuilding).

---

## Feature-spec adjustments

### 8. Distinct "disproved / counterexample" status — **Low/Medium** — *also a consistency bug*

`mapLeanPaneStatus` maps `disproved` → `invalid`, lumping a successful disproof
together with `failed`. This contradicts the counterexample feature's core
principle (`docs/FEATURE-counterexample-workflows.md`): a counterexample/disproof is
a *successful* mathematical result, not a failure. The counterexample work
deliberately distinguishes disproof everywhere status is shown — except here.

**Decision + fix:** add a dedicated pane state (and chip styling) for
disproved/counterexample outcomes, distinct from `invalid`. This is a spec change
(new status in the pane's vocabulary) with implementation behind it.

### 9. Align the pane's status taxonomy with the rest of the product — **Low/Medium**

The pane defines its own status set
(`missing-stub` / `stub-generated` / `valid` / `invalid` / `stale` / `unknown`),
separate from the UI's (`formalized` / `defined` / `disproved` / `sorry_stub` /
`in_progress`). One consequence: a successfully *defined* definition and a *proved*
theorem both collapse to `valid`, erasing the definition-vs-proof distinction the
def_feature work just introduced (`docs/FEATURE-overleaf-definition-tags.md`).

**Decision:** choose a single canonical status vocabulary shared across surfaces, or
explicitly document why the pane needs its own. This subsumes item 8.

### 10. Decide whether to show unmarked labeled environments — **Medium** (mostly design)

The manifest deliberately omits theorem-like environments that lack a `% lea:`
marker — there is even a test asserting this ("does not inventory LaTeX-labeled
environments without Lea markers"). But the V1 spec's stated goal is to let a user
"inspect **all** Lean-relevant mathematical content in an Overleaf project." Showing
unmarked environments as "not a Lea target yet" candidates would better match that
project-overview ambition.

**Decision:** confirm whether the pane is a marked-target inventory or a true
project overview, before building more on top of the current assumption.

**Resolved (2026-06-26):** the pane is a marked-target inventory — unmarked
environments are **not** shown. This is the already-implemented behavior, so no code
change is needed; clarified in `FEATURE-overleaf-lean-pane.md` (the spec previously
read ambiguously as "all labeled environments").

### 11. Source navigation — **Medium** — *specced V2*

Clicking an item should jump to its block in the Overleaf editor. The manifest
already carries `sourceStartOffset` / `sourceStartLine` / `sourceEndLine`, so the
remaining work is mostly in `pageBridge.js`: scroll/select the CodeMirror range. The
V1 spec explicitly lists source navigation for "later versions."

**Implemented (2026-06-26):** a "Go to source" button posts `OL_LEAN_NAVIGATE`;
`pageBridge.js` selects + `scrollIntoView`s the block in the active CodeMirror view.
Navigation is **anchored on the marker text** (the `% lea: … label=<name>` comment,
then `\label{…}`) so it survives byte-offset drift and path-format quirks, falling
back to offsets. An unknown/unmatched active-doc path is treated as "navigate in the
current view".

**Cross-file navigation:** a known *different* file is opened through Overleaf's
current editor tabs or accessible file-tree UI, including expanding nested folders
one render at a time. Older Overleaf builds can still use the private
`fileTreeManager` / `editorManager` API when it is present. The bridge then **polls
until that doc is actually active** (~3s budget) and verifies the marker/LaTeX-label
anchor before applying that file's offsets. If the file can't be resolved/opened,
it reports `OL_LEAN_NAVIGATE_RESULT { ok:false }` and the pane tells the user to open
the file manually (it never mis-selects an unrelated range from the wrong file).
Covered by `pageBridge.test.mjs` (same-file anchor/offset/clamp, current-UI tab and
nested-tree navigation, legacy-API navigation, and cross-file failure) and
`contentActions.test.mjs` (message posted).

### 12. Formalize-from-pane actions — **Medium/High** — *specced V2*

Let users trigger an autonomous formalization run for a missing or stale item
directly from the pane. Higher effort because it crosses the
pane → companion (:31245) → adapter (:8001) boundary and needs run-state feedback in
the pane (which ties into item 4, live refresh).

**Implemented (2026-06-26):** the manifest now carries `formalizable` /
`targetUses` / `targetContext` (recovered by re-running the formalize-path parser),
so a "Formalize"/"Re-formalize" action on actionable items reuses the existing
`/formalize` flow and then refreshes; live polling (item 4) reflects progress until
the run settles. Malformed markers are not offered as targets. Covered by
`leanPaneView.test.mjs`, `leanPaneManifest.test.mjs`, and `contentActions.test.mjs`.

### 13. True PDF-sibling tab vs. accept the floating aside — **High**

The pane is currently a floating `<aside>`. The spec wants it to "feel like a project
preview mode" alongside the PDF preview, while conceding that an extension-owned
equivalent is acceptable. Pursuing real Overleaf-tab integration means working
against a fragile, unstable DOM with no public API.

**Decision:** invest in tab-style integration, or formally accept the floating pane
as the intended V1 surface and record that in the spec.

**Resolved (2026-06-26):** accept the floating pane as the intended V1 surface; no
full Overleaf-tab integration will be pursued. Recorded in
`FEATURE-overleaf-lean-pane.md`. No code change needed.

---

## Suggested sequencing

1. **Correctness and cost first:** item 1 (hash alignment), item 3 (stop full
   re-downloads), item 2 (parallel enrichment).
2. **Status semantics:** items 8 and 9 (disproved/counterexample state and taxonomy
   alignment).
3. **V2 roadmap:** item 11 (source navigation) → item 12 (formalize from pane),
   with item 4 (live refresh) folded in.
4. **Decided and recorded (2026-06-26):** item 10 (marked-target inventory only)
   and item 13 (keep the floating pane; no Overleaf-tab integration) — both
   no-code-change decisions captured in `FEATURE-overleaf-lean-pane.md`.
