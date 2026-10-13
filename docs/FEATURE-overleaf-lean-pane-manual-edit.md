# Feature: Manual Lean Edits in the Overleaf Lean Pane

## Summary

Let a user edit the generated Lean artifact for a pane item directly, in place,
from the expanded item view in the Overleaf Lean pane. Today the only way to
change a formalization's Lean code is through the chat mirror (talking to Lea)
or by leaving Overleaf entirely and editing the recorded proof file by hand in
the standalone UI or on disk. A one-line fix — renaming a binder, tightening a
hypothesis, swapping a tactic — does not need a conversation.

This feature has two parts:

1. **The edit surface itself.** A user opens an item, edits its Lean text, saves,
   and gets a fast pass/fail verdict. This part is mostly *wiring*, not new
   product surface: the adapter already has a first-class, run-less "user edit"
   primitive (`POST /api/sessions/{id}/file`, design decisions D9/D11/D12 in
   `apps/lea-standalone/design/v2-architecture.md`) and a standalone fast-path
   `lean_check` (`POST /api/sessions/{id}/lean-check`, D2). The Overleaf
   companion has never called either — formalization there only ever runs
   through the autonomous agent. This feature is what makes the Overleaf lean
   pane use the same edit primitive the standalone canvas already has.

2. **Cross-target downstream-impact tracking.** This part is new. The
   standalone UI's edit primitive (D9) was designed around a single session
   editing its own file; it has no notion of *other* sessions whose generated
   Lean imports the file being edited. The Overleaf workflow, uniquely, already
   has cross-target dependencies — the `uses=` marker syntax
   (`docs/FEATURE-overleaf-inline-lea-tags.md` /
   `docs/FEATURE-overleaf-comment-marked-theorems.md`) lets one target's Lean
   import another's recorded declaration. A manual edit can silently break
   every target that imports the edited one. This feature adds the detection,
   verification, and surfacing of that breakage — see "Downstream Impact" below.

Architecture stays the existing one:

```text
Overleaf page -> Chrome extension -> companion (:31245) -> FastAPI adapter (:8001)
```

No new backend boundary, no separate Lean checking service. The companion gets
two or three new proxy endpoints; the adapter gets a small, additive extension
to an existing endpoint family.

## Current Behavior

- The expanded pane item view (`FEATURE-overleaf-lean-pane.md`) shows the
  generated Lean artifact **read-only**, plus "Go to source," "Formalize" /
  "Re-formalize," and copy actions.
- The only way to change a formalization's Lean code from Overleaf is the chat
  mirror (`FEATURE-overleaf-lean-pane-chat-mirror.md`), which drives a full
  autonomous agent run against the item's Lea session.
- The standalone UI's canvas (a different app, `apps/lea-standalone/src/`)
  already supports direct manual editing of a session's working file:
  - `POST /api/sessions/{id}/file` writes the edit to disk, commits it
    `author=user`, and inserts a `code_steps` row with `run_id = NULL`,
    `author = 'user'` (D9 — "user edits are first-class, run-less steps"). An
    optional note rides as a linked `edit_note` message (D11). The route also
    invalidates any prior SafeVerify verdict for the session
    (`store.set_session_safe_verify(session_id, None, None)`) — the closest
    existing precedent for "an edit invalidates a previously-trusted result,"
    today scoped to the session's own verdict only.
  - `POST /api/sessions/{id}/lean-check` runs the LSP-backed fast-path
    `lean_check` (`lea/tools.py:lean_check`, ~420× faster than a cold
    `lake env lean` thanks to the persistent LSP daemon, `lea/lsp_daemon.py`)
    against the working file and back-fills the verdict onto its latest
    `code_step`.
  - D12 ("diff-on-divergence") already tracks the last `author=agent` commit a
    session's agent saw, and injects `git diff <that-sha>..HEAD` the next time
    the agent runs in that session, so the agent picks up human edits made
    since its last turn.
- The companion's `leaApiClient.mjs` never calls either endpoint. The Overleaf
  extension has no edit affordance and no concept of "this edit may affect
  other targets."
- Cross-target dependencies exist today only in the *forward* direction, at
  generation time: a target marked `uses=other_label` has its formalize prompt
  resolve `other_label` to a previously recorded declaration and import it
  (`resolveUses` / `findImportedCurrentlyStubbedTheoremUses` /
  `parseLeanImports` in `companion/server.mjs`). There is no reverse index
  ("which targets import *this* declaration") and no mechanism that reacts
  when an already-imported declaration changes.
- All targets in one Overleaf project share a single git repo and Lean
  namespace (`Lea.<ProjectSlug>`, D24 in the standalone design doc): each
  target's session resolves to that shared repo, and each target's recorded
  proof is its own file under `workspace/proofs/Lea.<ProjectSlug>/...`. This is
  what makes a project-wide reverse-dependency scan tractable — it is a scan
  over one repo's files, not a cross-repo problem.

## A Lean fact this feature leans on

`theorem`/`lemma` declarations are **proof-irrelevant**: Lean only cares that a
declaration with a given name and a given *type* exists and type-checks.
Nothing downstream can observe *how* a theorem was proved. So:

- Editing a theorem/lemma's **proof body** without changing its statement
  (its name and type) can never break a downstream file that imports it.
- Editing a theorem/lemma's **statement** (name, binders, hypotheses,
  conclusion — anything that changes its type) can break every downstream use,
  because the type downstream code was elaborated against no longer matches.
- `def`/`abbrev` declarations are different: their **value**, not just their
  type, can be definitionally unfolded by downstream proofs (`simp [foo]`,
  `unfold foo`, `decide`, defeq checks, computation). A body-only edit to a
  `def` can break downstream proofs even though its type signature is
  unchanged.

This feature treats those as two different risk tiers rather than guessing:
**verify, don't guess**, using the same fast `lean_check` the standalone canvas
already relies on for instant feedback.

## Proposed Behavior: The Edit Surface

### Where editing happens

The expanded item view gains an "Edit" affordance next to the existing
read-only Lean artifact view (and next to the existing copy action). Activating
it swaps the read-only `<pre>` for an editable text area (full CodeMirror/Lean
syntax highlighting is a nice-to-have, not required for v1 — the pane already
has a lightweight Lean token highlighter in `leanPaneView.mjs`
(`highlightLeanLine`) that can be reused for display, with a plain `<textarea>`
underneath for v1 editing).

Editing is only offered for items that already have a recorded Lean artifact
(`stub-generated`, `valid`, `invalid`, `stale`, or a prior `error`/`disproved`
state). `missing-stub` items have nothing to edit — "Formalize" is still the
right action there.

### Save flow

1. The extension posts the edited content to a new companion endpoint,
   `POST /lean-pane/edit/save`, with the same target-identity shape the chat
   mirror already uses (`paneItemToChatTarget` in `leanPaneManifest.mjs`) plus
   the new content and an optional note:

   ```ts
   type LeanPaneEditSaveRequest = {
     overleafProjectId: string;
     targetKind: "theorem" | "definition";
     targetLabel: string;       // Lean declaration name
     content: string;           // the full edited Lean file
     note?: string;             // optional, rides as an edit_note (D11)
   };
   ```

2. The companion resolves (or creates, mirroring the chat-mirror resolution
   path) the Lea session backing that target, exactly as `/lean-pane/chat/*`
   does today.
3. The companion calls the adapter's existing
   `POST /api/sessions/{id}/file` with the resolved file path and content.
   This is the same call the standalone canvas makes — no adapter change is
   needed for this step. A no-op save (identical content) short-circuits, same
   as today.
4. The companion calls `POST /api/sessions/{id}/lean-check` to get an
   immediate verdict on the edited file, same fast LSP path the agent's own
   `lean_check` tool uses.
5. The companion records the edit's **before** content's declaration header
   (see "Signature-change detection" below) so it can classify the edit, then
   runs the downstream-impact pass described in the next section.
6. The companion returns a combined result: the item's own new status, plus a
   downstream-impact summary, in one response — so the pane can update the
   edited item and any affected items together without a second round trip.

### What the user sees immediately

- The item's status updates from its prior state to `valid` / `defined` (own
  check passed) or `invalid` (own check failed, with the `lean_check`
  diagnostic shown the same way a failed formalize run shows one today).
- The edit is recorded as a `code_step` with `author = 'user'`, visible in the
  same provenance trail the standalone canvas and chat-mirror runs already
  produce — so "what changed and who/what changed it" stays answerable from
  one place (D9's existing model, just now reachable from Overleaf too).
- **v1 explicitly does not auto-trigger an agent run on save**, even though the
  standalone canvas has that as an opt-out default behavior (D10,
  "explain-edit triggers run"). The whole point of this feature is letting a
  user make a small fix *without* invoking the agent. If the edit leaves the
  file unable to compile, the user sees that immediately (step 4) and can keep
  editing, revert, or fall back to chat — the existing "Re-formalize" /
  chat-mirror affordances are unaffected and still available as escape hatches.

## Proposed Behavior: Downstream Impact

This is the part that does not exist anywhere in the product today, including
the standalone UI's own canvas editing.

### Reverse-dependency index

The companion already knows, for any *new* target being generated, which
already-recorded declarations it imports (`findImportedCurrentlyStubbedTheoremUses`,
`parseLeanImports` over `import` lines). This feature adds the mirror image: for
a given declaration being edited, which *other* recorded files in the same
project import it.

```ts
type LeanPaneDependents = {
  moduleName: string;            // the edited declaration's Lean module
  dependents: Array<{
    targetKind: "theorem" | "definition";
    targetLabel: string;
    moduleName: string;
    sourceFile: string;          // .tex source, for display/navigation
    relativePath: string;        // recorded .lean proof path
  }>;
};
```

Computed by walking every recorded `.lean` file under
`workspace/proofs/Lea.<ProjectSlug>/` in the project's shared repo (the same
repo every target in the project already shares, D24), parsing its `import`
lines with the existing `parseLeanImports`, and keeping any file whose imports
include the edited declaration's module name. Lean's import graph is acyclic by
construction, so this scan terminates and needs no cycle handling — but it must
be **transitive**: if `C` imports `B` and `B` imports `A`, an edit to `A` must
walk through `B` to reach `C`. v1 computes this on demand at save time (project
sizes here are pane-scale — dozens of targets, not thousands — so a full
project scan per edit is cheap and always fresh, unlike a cached index that
could drift).

This is computed **before** save (to show the user what they're about to
affect) and **after** save (to act on it). The pre-save version only needs the
*unedited* file's module name, so it can be shown as soon as the edit surface
opens, not just at save time:

> Editing `compactness_criterion` may affect 2 downstream item(s):
> `compactness_corollary`, `heine_borel_application`.

### Signature-change detection

On save, the companion compares the edited declaration's **header** —
everything from `theorem`/`lemma`/`def` through the `:=` that starts the proof
— between the pre-edit and post-edit content. This is a structural comparison
(name, binders, type), not a byte-diff of the whole file, so reordering
whitespace or reformatting the proof body doesn't register as a signature
change. The classification:

| Edit kind | Detection | Downstream risk |
|---|---|---|
| Proof-body-only edit to a `theorem`/`lemma` | header unchanged | **None** (proof irrelevance) — dependents are not re-checked |
| Signature edit to a `theorem`/`lemma` (renamed binder, changed hypothesis, changed conclusion, renamed declaration) | header changed | **High** — every dependent is re-checked |
| Any edit to a `def`/`abbrev` body or signature | def/abbrev kind | **Possible** — every dependent is re-checked (proof irrelevance does not apply to definitional unfolding) |
| Edit that makes the file itself fail to compile | own `lean_check` fails | **High** — every dependent that imports it can no longer build; re-checked |

The "renamed declaration" case is a special case of a signature change: if the
edit renames `compactness_criterion` to `compactness_thm`, every dependent's
`import`/reference to the old name is now broken. The companion detects this by
comparing the declaration name parsed from the header, not just the type, and
flags it distinctly in the impact summary ("declaration renamed — dependents
must update their reference, this is not auto-fixable by re-checking alone").

### Cascade verification (v1: detect and verify, don't fix)

When an edit falls into a "downstream risk" row above, the companion does not
*guess* that dependents are broken — it **verifies**, using the same fast
`lean_check` path the edit itself just used:

1. For each transitive dependent file found in the reverse-dependency index,
   call the adapter's `lean-check` path against that file's *current, unedited*
   content (no changes are made to dependent files in v1).
2. Record the result as a new `code_steps` row on the dependent's own session,
   with `run_id = NULL` and a new `author` value, `'cascade'` (extending the
   existing `'agent' | 'user'` enum — additive, no migration since the schema
   is rebuild-on-change per the adapter's stated no-ALTER policy). The row's
   `summary` names the upstream edit, e.g. `"Re-checked after edit to
   compactness_criterion"`, so the provenance trail explains *why* a status
   changed without the user having touched that file.
3. The dependent's pane status updates to reflect the **real** outcome:
   - Still compiles → status is unaffected; nothing alarming shown beyond a
     transient "re-verified, still valid" note.
   - No longer compiles → status becomes `invalid`, with the new
     `lean_check` diagnostic shown exactly like a failed formalize/edit would
     show one, plus a visible link back to the upstream edit that caused it
     (`brokenByLabel: "compactness_criterion"`).
4. If a dependent fails to even resolve (its imported module no longer exists —
   the rename case), the diagnostic says so explicitly rather than surfacing a
   raw Lean "unknown identifier" error.

Cascade verification is bounded by the reverse-dependency index, so its cost is
proportional to the number of actual dependents, and each check is the same
sub-second LSP-backed call the rest of the product already uses — this does
not invoke the agent and does not consume model usage/cost.

### What this does not do in v1

Per the brief: v1 stops at **detection, verification, and clear surfacing**.
It does not attempt to repair a broken dependent. The user is told exactly
what broke and why (own edit vs. cascade-verified break vs. rename), and the
existing "Re-formalize" / chat-mirror affordances remain the way to fix a
dependent — now pre-populated with useful context (see v2 below for how that
context gets richer).

## v2 (Roadmap): Let Lea Fix Downstream Items

> **Superseded.** This roadmap section became a full feature of its own:
> `FEATURE-overleaf-self-repair.md` (implemented per
> `PLAN-overleaf-self-repair.md`). It delivers everything sketched below —
> repair runs with injected upstream-diff + failure context, a batch "fix all
> N" action, and rename-aware repair prompts — plus post-run cascade
> detection for chat/re-formalize changes that this spec did not cover. The
> text below is kept for the original design rationale only.

Once v1's detection is trusted, a natural next step is an action on each
cascade-broken dependent: "Have Lea fix this." This reuses machinery that
already exists rather than inventing a repair flow from scratch:

- D12 ("diff-on-divergence") already injects `git diff <last-known-sha>..HEAD`
  into an agent's context when it resumes a session after a human edit
  happened elsewhere. The same mechanism generalizes directly: when starting a
  repair run on a cascade-broken dependent, inject the **upstream** edit's
  diff (`git diff <old-sha>..<new-sha>` on the edited declaration's file) plus
  the dependent's own fresh `lean_check` failure, so the agent starts with
  both "what changed" and "what broke" instead of re-deriving either.
- The existing autonomous-run path (`/formalize`, same one "Re-formalize"
  already uses) is the vehicle — v2 is mostly about prompt construction
  (`buildLeaPrompt`/`chatPrompt.mjs`) for a "repair" variant, not a new run
  type.
- A "fix all N downstream items" batch action can sequence these one at a
  time, reusing the pane's existing in-progress polling (item 4 in
  `PLAN-overleaf-lean-pane-improvements.md`) to show live progress per item.
- Renamed declarations need one more piece in v2: a "this dependent still
  refers to the old name" pre-check, so the repair prompt can tell the agent
  the identifier was renamed rather than removed.

v2 is not part of this spec's acceptance criteria; it is recorded here so v1's
data model (the `brokenByLabel` pointer, the cascade `code_steps` rows, the
diff-on-divergence precedent) is built in a shape that does not need to be
reworked to support it.

## Status Vocabulary

This feature does not introduce a new pane status taxonomy (the existing
taxonomy — `missing-stub` / `stub-generated` / `valid` / `defined` / `disproved`
/ `invalid` / `stale` / `in-progress` / `unknown` / `error` / `mixed` — already
covers "this item's own check result"). It adds two pieces of *metadata* that
ride alongside a status rather than replacing it:

```ts
type LeanPaneEditMetadata = {
  lastEditedByUser?: boolean;        // most recent code_step has author='user'
  brokenByUpstream?: {               // present only when a cascade re-check
    targetLabel: string;             //   found this item now invalid
    editedAt: string;
  };
  affectsDependents?: Array<{        // present on the edited item itself,
    targetLabel: string;             //   pre- and post-save, when nonempty
    sourceFile: string;
  }>;
};
```

`brokenByUpstream` is what lets the pane render "invalid — broken by an edit to
`compactness_criterion`" instead of a bare `invalid`, and is what distinguishes
a cascade-verified break from an ordinary failed proof attempt. Aligning the
pane's status taxonomy with the rest of the product end-to-end is a pre-existing
open item (item 9, `PLAN-overleaf-lean-pane-improvements.md`) and is out of
scope here — this feature works with the taxonomy as it stands today.

## Companion API Surface (new)

```text
POST /lean-pane/edit/start
  -> resolve-or-create the session for a pane item (mirrors /lean-pane/chat/*),
     return current file content + the pre-save dependents list.

POST /lean-pane/edit/save
  -> write + commit (adapter /file), lean_check (adapter /lean-check),
     classify the edit, run cascade verification over dependents,
     return { ownResult, dependentsImpact[] }.
```

No new adapter endpoints are required for the edit-surface half of this
feature — it is additive use of `/api/sessions/{id}/file` and
`/api/sessions/{id}/lean-check`, which already exist and are already exercised
by the standalone UI's test suite (`test_routes_sessions.py`). `code_steps.author`
is a free-text column (`'agent' | 'user'` by convention, not an enforced SQL
`CHECK`), so recording cascade re-checks with `author = 'cascade'` needs no
schema migration — just a documented third convention value, consistent with
the "agent/user/cascade" attribution model this feature relies on for
provenance.

## Edge Cases

- **Editing while a downstream item has a run in progress.** The cascade check
  should not race a live agent run on a dependent. v1: skip cascade
  verification for a dependent whose session has an active run, and surface it
  as "not yet re-checked (busy)" rather than silently stale or silently wrong.
- **Editing while another browser tab has the same item open.** Same
  last-write-wins behavior the standalone canvas already has via `commit_write`
  — no new conflict handling is introduced by this feature.
- **A dependent with no recorded Lean at all yet** (still `missing-stub`,
  declared `uses=` but never formalized) is not a dependent in the reverse
  index — there is no recorded file to import the edited module yet, so there
  is nothing to cascade-check. It is unaffected by definition.
- **Diamond dependencies** (`C` depends on both `A` and `B`, both edited in
  quick succession) — each edit triggers its own cascade pass; `C` may be
  re-checked twice in a short window. Acceptable for v1 given the cost of each
  check; not something to special-case.
- **An edit that both changes the signature and breaks compilation.** Surfaced
  as a single `invalid` outcome on the edited item; the cascade still runs
  (signature-changed and own-check-failed both trigger it independently, so
  this is not a missed case).

## Non-Goals (this feature)

- Rich Lean editing (autocomplete, inline diagnostics-as-you-type, goal state
  display). v1 is a plain text area with the pane's existing static syntax
  highlighting.
- Auto-fixing downstream items (v2, above).
- Maintaining a persistent/cached dependency graph across sessions — v1
  recomputes on demand.
- Editing a `.tex` source statement from the pane (separate, existing surface:
  the Overleaf editor itself).
- Changing how `uses=` targets are *declared* at formalize time — this feature
  only adds detection for edits to already-recorded declarations.

## Acceptance Criteria

1. An item with a recorded Lean artifact can be opened for editing from the
   expanded pane view; an item with no recorded artifact cannot.
2. Saving an edit writes it through the adapter's existing user-edit primitive
   (`author=user` code_step, no model turn) and immediately runs `lean_check`,
   updating the item's status from the result.
3. A proof-body-only edit to a `theorem`/`lemma` never triggers a cascade
   re-check of dependents.
4. A signature edit to a `theorem`/`lemma`, any edit to a `def`/`abbrev`, or an
   edit that breaks the file's own compilation triggers a cascade re-check of
   every transitive dependent recorded in the project.
5. A cascade re-check uses real `lean_check` verification, not a heuristic
   guess, and only changes a dependent's displayed status when the check
   result actually changed.
6. A dependent broken by a cascade re-check shows which upstream edit caused
   it, distinguishable from an ordinary failed proof attempt.
7. The pre-save edit view shows the list of items that would be affected by a
   signature-level change before the user saves.
8. A renamed declaration is flagged distinctly from a same-name signature
   change in the impact summary.
9. No agent run is started automatically by a save in v1.
10. Existing pane behavior (read-only viewing, "Go to source," "Formalize" /
    "Re-formalize," chat mirror) is unaffected for items that are never
    manually edited.
