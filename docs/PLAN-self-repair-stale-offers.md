# Plan — Self-Repair Bugfix: Stale Repair Offers (Snapshot Reconciliation)

Fixes for the class of error reported against the self-repair feature
(`FEATURE-overleaf-self-repair.md` / `PLAN-overleaf-self-repair.md`):

> Theorem A used in the formalization of theorem B. Theorem A renamed. The
> repair button under theorem B was clicked and B was fixed. The "Repair all"
> button under theorem A remained, even though all 1 of the downstream
> theorems had already been fixed.

All file references are `apps/overleaf-extension/extension/content.js` unless
stated otherwise. Line numbers are as of branch `self_heal` after the
self-repair implementation landed.

## Status (updated 2026-07-04)

**All four fixes implemented**, suite green (324 of 325 tests passing; the
one standing failure remains the pre-existing `records targetSyntax` flake
documented in `PLAN-overleaf-self-repair.md`'s Status). Deviations from the
plan below:

- Fix 1 also exports `stillBrokenDependents` (the currently-broken filter)
  from `leanPaneView.mjs` rather than inlining it, since the summary needs it
  in two places (heading counts and the offer) and tests want it directly.
- Fix 1's since-fixed/repairing suffixes live inside `formatDependentOutcome`
  itself (gated to lines that claimed a problem, so "still valid" lines are
  never suffixed) rather than in the caller — the chat notice reuses the same
  renderer, so both surfaces get identical copy for free.
- Fix 2's poll path (`handleChatPoll`) recovers the project slug from the
  chat-record key (`<slug>:<kind>:<label>`); `slugProjectId` is idempotent,
  so feeding the slug back through dependent resolution is safe — verified
  before relying on it.
- The end-to-end test for the reported case drives the reconciling refresh
  via a source-change signal (the harness's pane refresh trigger); in the
  real flow the refresh comes from the repair's own polling. Noted because
  the pane deliberately does not refresh on identical source re-posts.

## Round 2 (2026-07-05): the reconciliation was one-directional

**Reported case.** A renamed → B broken → B's per-item repair dispatched.
*While that repair ran*, A was renamed again. The second save's cascade
correctly skipped B as busy ("not yet re-checked"); B's repair then finished
and FAILED verification (it had adopted rename 1; the tree now had rename 2).
Result: B invalid with a per-item repair offer — but A's summary never
offered "Repair all", showed a stale "not re-checked yet (a Lea run is
already in progress)" line, and B's failure read "Repair failed:
*Acknowledged and repaired… compiles with no errors*" under a stale
"renamed to `X`" attribution.

**Why Fix 1 missed it.** `stillBrokenDependents` still gated the offer's
*inclusion* on the snapshot's `brokenByUpstream` field: reconciliation could
only *remove* offers (`sinceFixed`/`nowRepairing`), never *add* one. A
snapshot-busy entry (`brokenByUpstream: null`) could never join the offer no
matter what live truth said — the fix violated its own principle in the
upgrade direction. Two adjacent members of the same class surfaced with it:
the failed repair re-recorded the *dispatched* breakage descriptor (stale
rename mapping → wrong pane copy and a wrong next repair prompt), and
`readRepairFailureReason` quoted the agent's *success* prose because the
verify-rebuild failure happened after the run the agent saw.

**Fixes (implemented, suite green — 329/330, same standing flake):**

1. **Bidirectional reconciliation.** `reconcileDependentsImpact` now also
   annotates `nowBroken` (live item carries un-suppressed breakage), and
   `stillBrokenDependents` decides from live truth alone for matched entries
   — in both directions — falling back to the snapshot only for unmatched
   ones. Stale busy lines are *corrected*, not suffixed ("was busy during
   this edit's re-check — now broken"), since "a run is in progress" is a
   current-state claim. The chat notice routes through the same predicate
   (`stillBroken === true` ⇒ `nowBroken`).
2. **Honest failure reason.** When the repair run's own final check passed
   but the verifying rebuild failed, the reason now states that verification
   against the current project failed because the upstream changed again
   (naming the current identifier), instead of quoting the agent's success
   report.
3. **Attribution refresh on failed verify.** The failure path compares the
   dispatched descriptor's `upstreamDeclarationName` with the upstream's
   *current* `declarationName` (kept fresh by rename bookkeeping on every
   path) and, when they diverge, records an updated rename descriptor
   (what-this-file-references → current name) — so the pane copy and the
   next repair prompt carry the right mapping.

**Still open (known, deliberate).** The cascade's busy-skip promise
("re-checked when its run ends") has no active mechanism; correctness is
recovered passively — the busy run's own terminal verification writes the
verdict, and this round's reconciliation surfaces it. A dependent whose busy
run ends in a state its own verification doesn't cover (e.g. interrupted
runs) stays un-re-checked until the next edit/cascade touches it. If that
bites, the fix is an explicit deferred-cascade queue drained on job
completion — noted here so it starts from a diagnosis, not a report.

---

## The reported case, diagnosed

Two different data sources render repair affordances, and only one of them is
live:

- **B's per-item "Repair with Lea" button** is derived from `item.breakage`,
  which the companion recomputes from persisted job truth
  (`job.lastEditBreakage`, cleared by any passing verdict through
  `recordEditCheckVerdict`) on **every manifest refresh**. When B was
  repaired, the next refresh dropped `item.breakage` and the button
  disappeared. Correct.
- **A's post-save impact summary** — the "1 downstream item affected:
  1 broken" note with its per-dependent outcome lines and the
  "Repair all (1)" button — renders from `leanPaneEditLastResult`
  (`content.js:79`), a **client-side snapshot of the save response**, frozen
  at save time. It is cleared only when a new edit is opened on that item
  (`openLeanPaneEdit`) or the pane state resets. The pane re-renders it on
  every manifest refresh, but re-renders the *same frozen object*: nothing
  reconciles the snapshot against the live per-item state it summarizes. So
  after B was fixed through any other path, A's summary kept asserting
  "1 broken" and kept offering "Repair all (1)".

One mitigating fact, for severity framing: clicking the stale button wastes
nothing. Repair dispatch re-validates every item against persisted truth at
dispatch time (`resolveRepairContext` → `alreadyFixed`), so the stale batch
resolves to `skipped: already_fixed` with zero agent runs. The bug is a lying
UI — a standing offer (and standing "broken" outcome lines) for work that no
longer exists — not wasted spend. That guard is also why the fix below is
purely presentational: the authoritative layer already behaves correctly.

## The class

**Snapshot copies of breakage truth that render *affordances* (or
current-state claims) without re-deriving from the live truth.** This is the
UI-layer echo of the repo-wide rule the backend already follows ("don't store
derived state" — session status is derived, never stored): breakage truth
lives in job records and is surfaced per item via `item.breakage`; every
other copy is derived state that can go stale the moment any other path
(per-item repair, manual edit, chat run, batch, recovery cascade) changes the
underlying truth.

Design principle for every fix below, stated once:

> A snapshot may be rendered as a **historical record** ("this save broke B
> at the time"), but anything that claims **current** state ("B is broken")
> or offers an **action** ("Repair all (N)") must be derived — at render time
> or server-side at serve time — from the live truth, and must disappear or
> re-count when that truth changes.

## Inventory — all members of the class, audited

| # | Surface | Storage | Stale how | Fix |
|---|---------|---------|-----------|-----|
| 1 | Post-save impact summary + "Repair all (N)" under the edited item (**the reported bug**) | `leanPaneEditLastResult` (client) | Frozen at save; survives until next edit of that item | Reconcile against live manifest at render time (Fix 1) |
| 2 | Chat mirror post-run notice ("This change broke N…" + Repair all) | `chatSessions[key].lastRunImpact` (companion, persisted) | Frozen at run end; survives until the next chat message on that target | Annotate with live per-dependent state at serve time (Fix 2) |
| 3 | `repairNeedsReview` flag on an item | `job.lastRepair` on the latest finished job | Manual edits are run-less (no new job), so after the user reviews *and manually edits* the item, the newest finished job is still the repair job and the flag never clears | Clear `lastRepair` on a subsequent user edit save (Fix 3) |
| 4 | `leanPaneRepairError` line | module-global in content.js, rendered inside **every** item's breakage block | A dispatch failure for one item renders under all broken items, and is cleared only by the next dispatch | Scope to the item that failed; clear on refresh/success (Fix 4) |

Audited and **not** in the class: the batch progress panel (live-polled via
`/lean-pane/repair/status`, explicit Dismiss), the pre-save dependents
preview (transient, discarded on save/cancel), and per-item breakage chips
(already live). The chat notice's *transcript* content is canonical history
and untouched — only the notice's current-state claims and button are in
scope.

---

## Fix 1 — Reconcile the post-save impact summary (the reported bug)

**Where:** `renderLeanPaneEditImpactSummary` (`content.js`) + a new pure
helper in `extension/leanPaneView.mjs`.

**Approach.** The summary is rendered inside `renderLeanPaneManifest`, which
already re-runs on every manifest refresh with fresh `item.breakage` on every
item — the live truth is *in hand* at render time; the function just doesn't
look at it. Add:

- `leanPaneView.reconcileDependentsImpact(dependents, manifestItems)` — pure,
  returns the snapshot entries annotated with a current-state verdict per
  dependent:
  - Match a dependent to its manifest item by declaration name
    (`dependent.targetLabel` ↔ `item.leanDeclarationName || item.label`) —
    the same identity convention the cascade's own attribution uses, and one
    that survives renames because both sides are refreshed by the existing
    rename bookkeeping.
  - `sinceFixed: true` when the matched item exists and **positively reads
    fixed**: no `item.breakage` and status not `invalid`/`unknown`/
    `in-progress`.
  - `nowRepairing: true` when the matched item's `breakage.repair.state ===
    "running"` or status is `in-progress` (B being repaired right now should
    read "repairing…", not "broken").
  - Unmatched dependents (item not in the current manifest — e.g. its `.tex`
    isn't in the loaded doc set) keep their snapshot state unchanged: the
    offer stays, because it cannot be *confirmed* fixed, and a stale click is
    a server-validated no-op anyway. Fail toward offering, never toward
    hiding an offer for something still broken.
- In `renderLeanPaneEditImpactSummary`:
  - Per-dependent lines: a `sinceFixed` entry renders the historical outcome
    plus a state correction — `"corollary_a: broken by this edit — since
    fixed."` (append, don't rewrite history); `nowRepairing` renders
    `"…— repair in progress."`.
  - The heading's counts and the "Repair all (N)" button are computed from
    **currently-broken** entries only (`brokenByUpstream && !sinceFixed &&
    !nowRepairing`). N reaches 0 → no button. All entries fixed → heading
    becomes "N downstream item(s) were affected by this edit — all since
    fixed or re-verified."

Because the pane already re-renders on every refresh, this makes the summary
self-healing with zero new polling: repair B anywhere, and A's summary
corrects itself on the next refresh tick.

**Tests.**
- `leanPaneView.test.mjs`: `reconcileDependentsImpact` — fixed match,
  still-broken match, repairing match, unmatched dependent passthrough,
  rename-identity match (dependent label matches `leanDeclarationName` after
  the item's own rename).
- `contentActions.test.mjs` (the harness's function-valued `manifest` option
  already supports call-counted responses): save breaks 1 dependent →
  summary shows "Repair all (1)"; next manifest response returns the
  dependent with no `breakage`, status `valid` → summary shows "since
  fixed", **no** Repair-all button — the reported scenario end-to-end.

**Risk.** Low. Render-time only; no state shape changes; the dispatch-time
server guard remains the backstop for anything reconciliation misses.

## Fix 2 — Annotate the chat notice server-side

**Where:** `handleChatSession` / `handleChatPoll`
(`companion/server.mjs`), plus the notice renderer
(`renderChatRunImpactNotice`, `content.js`).

**Approach.** Unlike Fix 1's snapshot, `lastRunImpact` flows through the
companion on every poll, and the live truth (job records) lives server-side —
so reconcile at serve time, where it's authoritative, rather than trusting
the client to hold a fresh manifest while the chat panel is open:

- When attaching `lastRunImpact` to a session/poll response, annotate each
  `dependentsImpact` entry with `stillBroken`: resolve the dependent
  (`resolveDependentSession`, the existing kind-agnostic path) and read
  `linkedJob.lastEditCheckStatus === "error" && linkedJob.lastEditBreakage`.
  Busy dependents (`activeJob`) annotate `nowRepairing`/busy instead. The
  stored record is **not** rewritten — it stays the historical fact of what
  that run broke; the annotation is computed per response (same
  computed-not-stored choice as `repairSuppressed`).
- `renderChatRunImpactNotice` mirrors Fix 1's rendering rules: counts and the
  Repair-all button from `stillBroken` entries only; fixed entries get the
  "— since fixed" suffix; all-fixed collapses the heading to past tense with
  no button.

**Tests.** `companion.test.mjs`: after the existing chat-rename-cascade test
records `lastRunImpact`, flip the dependent's job verdict to ok (as a repair
or manual fix would) → the next `handleChatSession`/`handleChatPoll` response
carries `stillBroken: false` on that entry while the stored record still says
what broke. `contentActions`-side: notice renders no Repair-all when nothing
is `stillBroken`.

**Risk.** Low. Per-entry job lookups are in-memory map scans, pane-scale.

## Fix 3 — Clear `lastRepair` on a subsequent user edit

**Where:** `handleLeanPaneEditSave` (`companion/server.mjs`).

**Approach.** `repairNeedsReview` exists to say "an agent changed this
statement; a human should look." A manual edit save *is* the human looking —
and it changes the file the flag was about, so the flag's referent is gone
either way. After the save's verdict recording, `delete
linkedJob.lastRepair`. (Re-formalize and repair runs already supersede it
naturally by creating a newer job.) This also keeps the failed-repair line
(`breakage.repair.state === "failed"`, sourced from the same `lastRepair`)
from outliving a manual fix in the window before the verdict clears the
breakage.

**Tests.** `leanPaneEdit.test.mjs`: a job carrying `lastRepair: {state:
"needs_review"}` loses it on the next successful save; manifest no longer
carries `repairNeedsReview`.

**Risk.** Minimal; one field deletion at an existing mutation point.

## Fix 4 — Scope the repair dispatch error to its item

**Where:** `content.js` (`leanPaneRepairError`, `renderLeanPaneBreakage`).

**Approach.** Replace the global string with `{ itemKey, message }` (itemKey
= the target label the dispatch was for, or `"batch"`); render it only inside
the matching item's breakage block (batch errors render in the batch panel);
clear it on any successful dispatch and on the next manifest refresh that
shows the item repaired. Cosmetic member of the class, included so the audit
table is fully addressed.

**Tests.** `contentActions.test.mjs`: a failed dispatch for item X renders
the error under X only, with a second broken item Y showing none.

---

## Suggested sequencing

1. **Fix 1** — the reported bug; lands the reconciliation helper the others'
   rendering rules reuse.
2. **Fix 2** — same rendering rules, server-side annotation; independent of
   Fix 1's code but consistent with its copy.
3. **Fixes 3 and 4** — small, independent.
4. Docs: append these to `FEATURE-overleaf-self-repair.md`'s Implementation
   Notes ("snapshots are historical records; affordances derive from live
   truth") so the next surface added to the pane inherits the principle
   rather than the bug.

## Acceptance criteria

1. The reported case exactly: rename A, repair B via its per-item button →
   on the next pane refresh, A's summary shows B as since-fixed and offers no
   "Repair all". No user action (re-edit, pane reopen) is required.
2. The same holds when B is fixed by a manual edit, by chat, or by the
   recovery cascade after A is edited again — reconciliation keys off live
   truth, not off *how* the fix happened.
3. The chat notice's Repair-all count reflects only still-broken dependents
   at poll time, while the transcript/history of what the run broke is
   unchanged.
4. A dependent that cannot be matched to live state keeps its offer (fail
   toward offering), and clicking it remains a server-validated no-op.
5. `repairNeedsReview` clears on the next manual edit save of the item.
6. A repair dispatch error renders only under the item (or batch panel) it
   belongs to.
7. Historical facts are never rewritten: outcome lines keep saying what broke
   and why; only current-state claims and affordances are reconciled.

## Open risks

- **(Low) Identity matching across renames.** Fix 1 matches by declaration
  name on both sides; both are refreshed by the existing rename bookkeeping,
  but a dependent renamed *while* A's summary is on screen could briefly fail
  to match — which by the fail-toward-offering rule leaves a stale offer, the
  pre-fix behavior, until the next refresh. Acceptable; noted so a report of
  "offer lingered one refresh after I renamed the dependent" has a known
  cause.
- **(Low) Fix 2 cost.** Per-poll job scans per dependent; pane-scale today.
  If chat polling ever gets hot, annotate only when `lastRunImpact` exists
  (already the case) and cap at the impact list's size (already bounded).
