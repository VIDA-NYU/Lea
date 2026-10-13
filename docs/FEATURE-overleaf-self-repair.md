# Feature: Self-Repair — Task the Agent on Edit-Induced Breakage

## Summary

When a user changes an existing formalization — by hand through the pane's
manual-edit surface, or conversationally through the chat mirror — the change
can break two things:

1. **The edited formalization itself** (the file no longer compiles), and
2. **Downstream formalizations** that import the edited declaration (a rename,
   a changed hypothesis, a changed `def` body, or simply an upstream file that
   no longer builds).

Today the product **detects and verifies** this breakage (the cascade pass in
`docs/FEATURE-overleaf-lean-pane-manual-edit.md`, implemented in
`handleLeanPaneEditSave` in `companion/server.mjs`) and **surfaces** it
("invalid — broken by an edit to `compactness_criterion`"), but deliberately
stops there: v1 of manual editing was "detect and verify, don't fix." Fixing a
broken dependent means the user manually drives "Re-formalize" or opens the
chat mirror per item, re-explaining context the system already knows.

This feature is the repair half — the "v2 (Roadmap): Let Lea Fix Downstream
Items" section of the manual-edit spec, promoted to a full specification and
broadened per the product brief:

- **Not limited to renames.** Any classified breaking change (rename, same-name
  signature change, `def`/`abbrev` body change, upstream compile failure)
  produces a repair offer. The classification machinery
  (`leanSignatureDiff.mjs: classifyEdit / cascadeRequired`) already
  distinguishes these; repair consumes that classification rather than
  re-deriving it.
- **Not limited to manual edits.** Chat-mirror runs and "Re-formalize" runs can
  change a recorded declaration just as surely as a manual edit can — and today
  those paths run **no cascade at all** (detection lives only inside
  `handleLeanPaneEditSave`). This spec closes that detection gap first, because
  repair is only trustworthy if every breakage path feeds it.
- **User-dispatched, never automatic.** The user is *offered* the option to
  task the agent with repair — per broken item or all at once. No repair run
  ever starts without an explicit user action. This mirrors the manual-edit
  spec's own acceptance criterion 9 ("no agent run is started automatically by
  a save") and keeps model spend under user control.
- **The agent of choice does the repairing.** A repair run is an ordinary
  autonomous run through the same adapter path everything else uses
  (`POST /api/runs`, D19), executed with the provider/model the user has
  configured (the model picker in the extension options page, backed by
  `packages/lea-model-catalog`). Repair is a new *prompt*, not a new *run
  type*.

Architecture stays the existing one:

```text
Overleaf page -> Chrome extension -> companion (:31245) -> FastAPI adapter (:8001)
```

No new backend boundary. The adapter needs **no new endpoints** for v1; the
companion gets a repair endpoint family and a factored-out cascade pass.

## Current Behavior (what exists, what's missing)

### Exists — detection & surfacing (manual-edit path only)

- `POST /lean-pane/edit/save` (`handleLeanPaneEditSave`, `companion/server.mjs`)
  writes the edit through the adapter's user-edit primitive
  (`POST /api/sessions/{id}/file`, D9), runs `lean_check`
  (`POST /api/sessions/{id}/lean-check`, D2), classifies the edit
  (`classifyEdit` over the pre/post declaration header), and — when
  `cascadeRequired(classification)` or the edit *recovered* a previously
  failing file — re-verifies every transitive dependent found by the
  reverse-import index (`leanDependencyGraph.mjs: dependentsOf /
  transitiveDependents / buildReverseImportIndex`).
- Each cascade re-check records a timeline entry with `author: "cascade"` and
  persists the real `lean_check` verdict onto the dependent's linked job
  (`recordEditCheckVerdict` → `job.lastEditCheckStatus`), which the pane's
  status chain honors ahead of every other status source
  (`getTheoremStatus` → `buildEditBrokenTheoremStatus`, `brokenByEdit: true`).
- The pane renders the impact: the edited item's save result lists
  `dependentsImpact[]`, broken dependents carry
  `brokenByUpstream: { targetLabel, editedAt, renamed }`, and the item chip
  reads "broken by this edit" / "renamed — reference must update"
  (`leanPaneView.mjs`, `content.js`).
- Dependent→session attribution exists (`resolveDependentSession`), including
  the declaration-name fallback for dependents that were themselves renamed.
  This is exactly the resolution a repair run needs to know *which session* to
  run in.

### Missing — everything this spec adds

1. **No repair action anywhere.** A broken dependent's only affordances are
   the generic "Re-formalize" (which rebuilds from the `.tex` statement with
   no knowledge of *why* the item broke) and the chat mirror (where the user
   must explain the upstream change themselves).
2. **No cascade on agent-driven changes.** If the user asks the chat mirror
   "rename this theorem to `compactness_thm`", the agent does it, the run
   finishes — and no dependent is ever re-checked. Same for "Re-formalize"
   producing a different signature than the previous recording. The breakage
   is invisible until a dependent is next touched for an unrelated reason.
3. **No repair-shaped context handoff.** The system knows the upstream diff
   (git owns every version — D1; the adapter already computes
   diff-on-divergence context, D12 in `adapter/app/bridge.py:
   _divergence_context`), the classification (renamed vs. signature vs. def
   body vs. compile failure), the old→new name mapping, and the dependent's
   fresh compiler diagnostic. None of that reaches an agent today.

## Terminology

- **Breaking change** — a change to a recorded declaration classified by
  `classifyEdit` as anything other than "proof-body-only edit to a
  theorem/lemma," or a change that leaves the file itself failing
  `lean_check`.
- **Broken item** — a pane item whose latest recorded `lean_check` verdict is
  non-`ok` *and* which carries breakage attribution: either `brokenByEdit`
  (its own edit broke it) or `brokenByUpstream` (a cascade re-check after an
  upstream change broke it).
- **Repair run** — a user-dispatched autonomous run on a broken item's own Lea
  session whose prompt is the repair variant described below.
- **Repair set** — the ordered list of broken items produced by one upstream
  change, eligible for batch repair.

## Proposed Behavior

### Part 1 — Close the detection gap: cascade after agent runs

Prerequisite, not optional: repair offers must appear for chat-driven breakage
too, or the feature only covers half the brief.

- Factor the cascade block out of `handleLeanPaneEditSave` into a reusable
  `runCascadeVerification({ state, target, beforeContent, afterContent,
  ownCheckFailed })` in the companion. `handleLeanPaneEditSave` becomes its
  first caller, behavior-identical.
- When the companion starts any run that can rewrite an existing recorded
  declaration — a chat-mirror message run (`/lean-pane/chat/message`) or a
  re-formalize (`/formalize` on a target that already has a recorded proof) —
  snapshot the recorded file's current content on the job record
  (`job.preRunContent`, alongside the existing job bookkeeping in
  `runLeaJob`). This is a read of a file the companion already knows how to
  locate (`target.absolutePath`); no adapter involvement.
- When that job reaches a terminal status, read the file's final content,
  classify `preRunContent` → final content with the same `classifyEdit`, and
  if `cascadeRequired(...)` (or the run recovered a previously failing file),
  run `runCascadeVerification`. Dependents get the same `author: "cascade"`
  timeline rows, `lastEditCheckStatus` verdicts, and `brokenByUpstream`
  attribution the manual path produces today — with the attribution's
  `editedAt`/label pointing at the run's target, and a `via: "chat" |
  "formalize" | "edit"` field so the surfacing can say *how* the upstream item
  changed.
- The chat mirror surfaces the outcome inline: when a completed chat run
  triggered a cascade that broke N dependents, the mirror shows a system-style
  notice ("This change broke 2 downstream items: `compactness_corollary`,
  `heine_borel_application`") with the same repair affordances as the pane.
  The pane itself needs no new plumbing for this — broken dependents flow
  through the existing `lastEditCheckStatus` override into the existing chips.

Skip rule (matches the manual path's existing behavior): a dependent with an
active run is not cascade-checked mid-run; it is marked "not yet re-checked
(busy)" and re-checked when its run ends.

### Part 2 — The repair offer

Repair is offered wherever breakage is already surfaced, as an *action* next
to the existing *explanation*:

1. **Per-item.** A broken item's expanded pane view — which today shows the
   compiler diagnostic plus "broken by an edit to X" — gains a **"Repair with
   Lea"** button. Shown when:
   - the item carries `brokenByUpstream` (cascade-broken dependent), or
   - the item carries `brokenByEdit` (the user's own edit broke it — the
     "formalization that was altered" half of the brief), or
   - the item was flagged `renamed` (reference update needed even if the
     rename itself compiled).
2. **Batch.** The post-save / post-run impact summary — which today reports
   "N downstream item(s) broken" (`content.js` `brokenCount`) — gains a
   **"Repair all (N)"** action that dispatches the whole repair set
   sequentially (ordering below).
3. **Confirmation, with the agent named.** Both actions show a one-step
   confirmation before dispatch: which items will be repaired, what upstream
   change is being repaired against (label + classification, e.g. "renamed
   `compactness_criterion` → `compactness_thm`"), and **which agent will do
   it** — the currently configured provider/model from the options page
   (`options.js` `#lea-model`, greyed-out families without configured keys,
   same rendering rules as today). A "change model" link opens the existing
   options page; v1 does not add per-run model override plumbing (see
   Non-Goals). Each item's repair is one autonomous agent run and consumes
   model usage; the confirmation says so ("will start N agent runs").

Nothing is dispatched automatically. Declining the offer leaves everything
exactly as v1 leaves it today: broken chips with attribution, and the
existing "Re-formalize" / chat-mirror escape hatches untouched.

### Part 3 — The repair run

A repair run is an ordinary autonomous run, differing only in prompt and
bookkeeping:

- **Session:** the broken item's own Lea session, resolved exactly as cascade
  attribution resolves it today (`resolveDependentSession`, including the
  declaration-name fallback). If the item has no session (recorded file but
  its session was reset), fall back to the same resolve-or-create path the
  chat mirror uses; if even that fails, the item is reported "cannot repair —
  no session" rather than silently skipped.
- **Dispatch:** `startApiRun` / the existing `runLeaJob` machinery with
  `autonomous: true` (D19 — no approval gate, non-interactive prompt variant),
  the project slug/origin fields the Overleaf path always sends, and the
  repair prompt as the message. Live progress uses the pane's existing
  in-progress job polling; the item shows the existing `in-progress` state
  while repairing.
- **Bookkeeping:** the job record carries
  `repairOf: { targetLabel, classification, editedAt, via }` so the timeline
  and any later debugging can answer "why did this run happen." The run
  itself is recorded adapter-side like any other run (sessions, usage,
  `code_steps` with `author='agent'`) — no schema change; provenance stays
  derivable, per the "don't store derived state" rule.
- **Interrupt:** the pane's existing interrupt affordance applies to a repair
  run like any other run.

### Part 4 — Repair prompt construction

New `buildRepairPrompt(...)` in `companion/chatPrompt.mjs`, alongside
`buildChatPrompt`. The design goal: the agent starts with **"what changed"
and "what broke"** already in hand — the two things D12's diff-on-divergence
precedent proved are worth injecting — instead of re-deriving either. Inputs,
all already computed by the time the offer is shown:

1. **The dependent's identity and contract.** Its `.tex` statement (the
   source of truth for what it must say), its recorded Lean file path, and
   the standing guardrail: *the dependent's own theorem statement must not be
   weakened or changed to make the proof go through* (the vendored prover's
   own `DESIGN.md` rule). The one sanctioned statement-adjacent change is
   mechanical reference updating (imports, identifier names) when upstream
   was renamed.
2. **What changed upstream.** The upstream declaration's label, the
   classification (`renamed` / `signature-changed` / `def-body-changed` /
   `compile-broken` / `recovered`), the old and new declaration headers
   (`parseDeclarationHeader` output for both sides), and for renames the
   explicit old→new mapping ("`compactness_criterion` is now
   `compactness_thm`; it was renamed, not removed"). Optionally the full
   upstream file diff — git has it (every write is a commit, D1), and the
   companion snapshotted `preRunContent` for the agent-driven path.
3. **What broke.** The dependent's fresh `lean_check` diagnostic from the
   cascade pass — the actual compiler error, not a guess.
4. **What "done" means.** The dependent's file compiles again
   (`lean_check` ok), its statement is semantically unchanged (modulo renamed
   upstream identifiers), and no `sorry`/`admit`/`axiom` was introduced —
   the same final-proof rules every run already operates under.
5. **The stop condition.** If the upstream change makes the dependent's
   statement *unprovable as stated* (e.g. a hypothesis the dependent's proof
   genuinely needed was removed upstream), the agent must stop and report
   that conclusion rather than alter the statement — surfaced to the user as
   a failed repair with the agent's explanation, which is precisely the
   information the user needs to decide whether to revise the `.tex`.

### Part 5 — Batch ordering and post-repair verification

- **Topological order.** Dependents can depend on each other (`C` imports `B`
  imports the edited `A`). "Repair all" sorts the repair set so that an item
  is repaired only after every broken item it transitively imports has been
  repaired — the import graph is already available
  (`buildReverseImportIndex`; Lean's import graph is acyclic, so a topological
  sort always exists). Repairing `C` before `B` would have `C` fail on `B`'s
  still-broken import and waste a run.
- **Sequential, with early-exit option.** One run at a time, reusing the
  pane's existing per-item progress polling. If an item's repair fails, the
  batch pauses and asks: continue with the remaining items (those not
  importing the failed one can still succeed) or stop. Items downstream of a
  failed repair are auto-skipped with "skipped — depends on failed repair of
  `B`", not attempted.
- **Re-verify after each repair.** A repair run edits a file that may itself
  have dependents, so each completed repair run flows through the same
  Part-1 post-run cascade as any other run. Normally the repair changes only
  a proof body (no further cascade); if the agent had to touch the
  dependent's own header (rename-reference updates don't, but agents can
  surprise), the cascade catches it instead of trusting it.
- **Success clears attribution.** A dependent whose post-repair `lean_check`
  is ok has its `lastEditCheckStatus` set to `"ok"` by the existing verdict
  recording, which already clears the `brokenByEdit`/`brokenByUpstream`
  override and returns the chip to the normal status chain. No new
  state-clearing mechanism.

### What the user sees, end to end

> User renames `compactness_criterion` → `compactness_thm` via the chat
> mirror. The run finishes; the post-run cascade re-checks 3 dependents;
> 2 fail. The chat mirror and the pane both show: "This change broke 2
> downstream items." The user clicks **Repair all (2)**, confirms
> ("2 agent runs with `gpt-5.2`"), and watches `compactness_corollary`
> then `heine_borel_application` go in-progress → valid. Both chips clear.
> Total user actions: two clicks.

## Data Model Additions

All additive, all on existing structures; nothing stored that can drift from
the derivable truth.

```ts
// On the companion job record (in-memory/state jobs, not the adapter DB):
type RepairJobFields = {
  preRunContent?: string;          // snapshot at run start, for post-run classification
  repairOf?: {
    targetLabel: string;           // the upstream item whose change is being repaired
    classification: string;        // renamed | signature-changed | def-body-changed | compile-broken
    renamedTo?: string;            // present for renames
    editedAt: string;
    via: "edit" | "chat" | "formalize";
  };
};

// Extending the existing brokenByUpstream metadata the pane already renders:
type BrokenByUpstream = {
  targetLabel: string;
  editedAt: string;
  renamed?: { from: string; to: string };
  via?: "edit" | "chat" | "formalize";   // NEW: how upstream changed
  repair?: {                             // NEW: repair lifecycle for this breakage
    state: "offered" | "running" | "repaired" | "failed" | "skipped";
    runId?: string;
    failureReason?: string;              // agent's stop-condition explanation
  };
};
```

Adapter-side: **no schema change.** Repair runs are ordinary runs;
`code_steps.author` stays `'agent' | 'user' | 'cascade'` as already
conventioned; session status stays derived from the latest `code_step`
verdict.

## Companion API Surface (new)

```text
POST /lean-pane/repair/start
  { overleafProjectId, targetKind, targetLabel,
    upstream: { targetLabel, classification, renamedTo?, editedAt, via } }
  -> resolve the broken item's session, build the repair prompt,
     start one autonomous run; returns the same job-tracking shape
     /formalize returns so the pane's existing polling drives progress.

POST /lean-pane/repair/all
  { overleafProjectId, upstream: {...}, items: [{ targetKind, targetLabel }] }
  -> topologically sort, dispatch sequentially, stream/poll per-item progress
     through the existing job state; returns the ordered plan immediately.

(Existing /lean-pane/chat/interrupt-style interruption applies per run.)
```

Adapter: no new endpoints. `POST /api/runs` (with `autonomous: true`) and the
existing session/file/lean-check family cover everything.

## Edge Cases

- **Upstream is edited again while repairs are running.** The new edit's own
  cascade pass sees busy dependents, marks them "busy," and re-checks them
  when their repair run ends — the existing skip rule composes correctly. The
  in-flight repair run may now be repairing against a stale upstream; its
  post-run `lean_check` (against the *current* tree) is the arbiter, so a
  stale repair that no longer compiles surfaces as a failed repair, never as
  a false "repaired."
- **Two upstream edits break the same dependent (diamond).** The dependent
  carries the most recent `brokenByUpstream` attribution (last cascade wins,
  as today); a repair prompt is built from whatever attribution is current,
  and the repair's success condition is compilation, which is
  attribution-independent — so a repair dispatched from a stale offer still
  either genuinely fixes the item or honestly fails.
- **The broken item's session has an active run** (user opened the chat
  mirror on it meanwhile). The repair offer for that item is disabled with
  "busy" until the run ends — same rule as cascade checking.
- **Repair of a `def` dependent that is itself imported further downstream.**
  Covered by Part 5's re-verify: the repair run's own post-run cascade
  re-checks *its* dependents.
- **The user manually fixes a broken dependent before dispatching repair.**
  The manual edit's save flow records an `ok` verdict, the broken attribution
  clears, and any still-visible repair offer for that item is dropped
  (offers are re-validated against current `lastEditCheckStatus` at dispatch
  time — dispatching a repair for an item that now compiles is a no-op
  response, not a run).
- **Upstream compile failure (`compile-broken` classification).** Repairing
  dependents is pointless while their import can't build. The repair offer
  for dependents is suppressed in this case; the offer shown is on the
  *edited item itself* ("Repair with Lea" on the item the user broke), and
  dependents are re-checked by the recovery cascade once it compiles again —
  which already exists (`recoveredFromFailure` path).
- **No configured API key for the selected model.** The confirmation surfaces
  the same provider-key status the options page renders; dispatch is blocked
  with a pointer to options, not a failed run.
- **Companion restart between breakage and repair.** `brokenByUpstream`
  attribution and verdicts live on the persisted job state and adapter DB
  (as today); the repair *offer* is re-derivable from them, so it survives
  restart. `preRunContent` snapshots are only needed during a live run and
  may be lost on restart — a run that terminates while the companion is down
  misses its post-run cascade; the next edit/save or manual re-check of any
  item recovers ground truth. Documented limitation, not silently wrong
  state.

## Non-Goals (this feature)

- **Auto-repair without user dispatch.** Even for "obviously mechanical"
  rename fixes. The brief is explicit that this is an offered option; spend
  and write-access stay user-controlled.
- **Per-repair model override.** v1 uses the configured model ("agent of
  choice" = the existing options-page picker). Adding a `model` field to the
  adapter's `RunRequest` for per-run override is a clean additive follow-on
  if wanted, but it is adapter surface this feature doesn't need.
- **Parallel batch repair.** Sequential is correct-by-ordering and matches
  the pane's one-run-at-a-time job model; parallelism is an optimization
  with real interleaving hazards (two repairs writing one shared repo).
- **Repairing `.tex` statements.** If the correct resolution is "the
  downstream *statement* must change," that is a user decision in the
  Overleaf editor; the agent's stop-condition report is the input to it.
- **Standalone-UI coverage.** The standalone project graph
  (`adapter/app/graph.py`, blueprint `uses` edges, D28/D29) has the same
  breakage shape and the adapter-side repair vehicle is identical
  (autonomous run + repair prompt), but the standalone canvas has no cascade
  detection yet at all — that's a separate feature that should reuse Part 3/4
  of this spec, tracked separately.
- **Persistent/cached dependency graph.** Recompute-on-demand stays, per the
  manual-edit spec.

## Implementation Notes (as built — see PLAN-overleaf-self-repair.md's Status)

Reconciliations between this spec and the shipped implementation:

- **The cascade is richer than described above.** The reused pipeline
  (`companion/cascadeVerify.mjs`) rebuilds the changed module first and fails
  closed when that rebuild fails, takes per-dependent verdicts from a real
  `lake build` (the warm LSP check is only an `author:"cascade"` timeline
  entry), and propagates breakage transitively to a fixpoint. These behaviors
  predate this feature (bug-driven hardening of the manual-edit cascade) and
  are preserved for every `via`.
- **`repairOf` persists as `job.lastEditBreakage`** (written/cleared through
  `recordEditCheckVerdict`, the single verdict choke point), plus the
  upstream declaration's before/after headers so repair prompts survive
  companion restarts. The pane reads it as `item.breakage` with computed
  `selfBroken`/`repairSuppressed`/`repair` fields.
- **Acceptance criterion 6's statement guard is an approximation with a
  review outcome**: a compiling repair whose declaration header changed
  beyond the known upstream rename substitution ends as
  `lastRepair.state = "needs_review"` (surfaced as `item.repairNeedsReview`)
  rather than being silently accepted or hard-failed.
- **Repair jobs share the target's `jobKey`** (`mode: "repair"`, terminal
  statuses `repaired`/`repair_failed` invisible to formalize-status lookups),
  so all existing busy/skip logic composes unchanged.
- **Known limitation:** a broken dependent with no linked job (jobs.json
  reset since it was recorded) has nowhere to persist attribution, so it gets
  the "may be affected — re-check manually" impact line but **no repair
  offer**, as anticipated in the plan's edge cases.
- **Snapshots are historical records; affordances derive from live truth**
  (`PLAN-self-repair-stale-offers.md`, fixed post-ship): the post-save impact
  summary and the chat mirror's post-run notice are frozen records of what a
  change did *at the time*. Their per-item lines keep that history, but their
  counts and "Repair all (N)" offers are reconciled against current truth on
  every render/poll (`reconcileDependentsImpact` client-side against the live
  manifest; `annotateLastRunImpact` server-side against job records) — a
  dependent fixed through any path drops out of the offer, with a "— since
  fixed" correction on its line. Any future pane surface that copies breakage
  state must follow the same rule: render the copy as history, derive actions
  from live state, and when live state can't be confirmed, fail toward
  keeping the offer (a stale dispatch is a server-validated no-op).

## Acceptance Criteria

1. A manual edit that breaks downstream items produces, alongside today's
   breakage surfacing, a per-item "Repair with Lea" action and a
   "Repair all (N)" action. Neither starts a run without explicit user
   confirmation.
2. A chat-mirror run or re-formalize run that changes a recorded
   declaration's header (or breaks/recovers its compilation) triggers the
   same cascade verification as a manual edit, with `brokenByUpstream`
   attribution identifying the upstream item and `via` identifying the path.
3. An item broken by the user's own edit (`brokenByEdit`) also carries a
   "Repair with Lea" offer — repair covers the altered formalization itself,
   not only downstream items.
4. The repair confirmation names the items to be repaired, the upstream
   change (including rename old→new when applicable), the configured
   provider/model that will run, and the number of agent runs to be started.
5. A repair run executes on the broken item's own session as an autonomous
   run through `POST /api/runs`; its prompt contains the upstream
   classification, old/new declaration headers, rename mapping when
   applicable, and the dependent's actual `lean_check` diagnostic.
6. A repair run never changes the broken item's own theorem statement except
   for mechanical renamed-reference updates; a repair the agent cannot
   complete under that constraint ends as a failed repair carrying the
   agent's explanation, surfaced on the item.
7. "Repair all" processes items in topological import order; items
   transitively importing a failed repair are skipped with an explicit
   skipped-reason, not attempted.
8. A successful repair flows through the standard verdict recording so the
   item's broken attribution clears and its chip returns to the normal
   status chain — verified by `lean_check`, not assumed from run completion.
9. A completed repair run is itself cascade-verified (its own dependents are
   re-checked) like any other change.
10. Every repair run is attributable after the fact: the job record carries
    `repairOf`, and the adapter records the run/session/usage exactly like
    existing runs — no derived state is stored.
11. Repair offers respect busy sessions, missing API keys, and
    already-fixed items (no-op instead of a wasted run) as specified in
    Edge Cases.
12. All existing v1 behavior — detection, cascade verification, breakage
    surfacing, "Re-formalize," chat mirror — is unchanged for users who never
    click a repair action.
