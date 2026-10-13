# Plan — Self-Repair: Task the Agent on Edit-Induced Breakage

Implementation plan for `docs/FEATURE-overleaf-self-repair.md`: user-dispatched
agent repair of breakage caused by changes to existing formalizations, plus the
prerequisite detection gap — cascade verification after *agent-driven* changes
(chat mirror, re-formalize), which today exists only for manual edits.

## Status (updated 2026-07-04)

**All phases (0–6) implemented**, companion + extension suites green (316
tests; the one standing failure, `records targetSyntax on the job for
telemetry`, is a pre-existing flake reproducible on the untouched base branch
— a race between the test's deep-equal and the fire-and-forget `runLeaJob`,
unrelated to this work). Adapter untouched, as planned. The integration smoke
recipes in §5 (real prover, cascade-testing-guide extensions) have **not**
been run — they need a live Lean toolchain and model keys.

Deviations from the plan below, discovered during implementation:

- **Phase 3's jobKey-consumer audit resolved in favor of shared keys** (the
  plan's primary option): repair jobs share the target's `jobKey`, carry
  `mode: "repair"`, copy the display/identity fields of the job they
  supersede (declarationName, recordedProofPath, targetTextHash, …) **and
  mirror its broken-state fields** (`lastEditCheckStatus`/`lastEditBreakage`)
  — the repair job becomes the newest linked job the moment it exists, so
  mirroring is what keeps the chip override stable during/after the run. The
  masquerade risk is closed by terminal statuses `repaired`/`repair_failed`,
  which `findLatestJob`'s exact-match filters can never confuse with
  `formalized`/`failed`.
- **The statement guard uses the post-run classification plus a
  rename-substituted header comparison** (the plan's suggested mitigation),
  and its verdict is `lastRepair.state = "needs_review"` on the job,
  surfaced as `item.repairNeedsReview` — separate from `breakage`, because a
  needs-review repair *compiles* and therefore has no breakage left to hang
  metadata on.
- **A chat-record race the plan missed:** `handleChatMessage` rewrites
  `state.chatSessions[targetKey]` *after* the run starts, which would clobber
  a fast run's `lastRunImpact`. Fixed by clearing the previous impact
  *before* the run and preserving `lastRunImpact` on the post-start rewrite;
  `finishChatRunCascade` also creates the record if the run finished first.
- **`resolveRepairContext` gained kind-agnostic fallback resolution**
  (`resolveDependentSession`) because batch callers know a dependent's label
  but not its kind — the plan's `repair/all` payload shape implied but never
  stated this.
- **Confirmation UI is `window.confirm`** with `formatRepairConfirmation`'s
  text (items, upstream change, model label, run count) rather than an inline
  panel — one native dialog, zero re-render state, used identically by the
  pane button, the post-save summary, and the chat notice.
- Batch state also carries a `running` flag (loop re-entry guard) and
  `handleLeanPaneRepairStatus` is POST-shaped like every other companion
  endpoint.

## 1. Grounding — what's there today, confirmed by reading the code

Each fact below removes a design decision this plan would otherwise make from
scratch. Line numbers are as of branch `self_heal` (commit `b1270db`).

1. **The cascade pass is richer than the feature docs describe, and it lives
   inline in one handler.** `handleLeanPaneEditSave`
   (`companion/server.mjs:1140-1435`) contains the whole pipeline: pre-edit
   snapshot (`loadEditableSessionFile`), write + own-check, classification
   (`classifyEdit`), recovery detection (`recoveredFromFailure`, `:1237`), and
   then a cascade block (`:1240-1428`) that the factored-out version **must
   reproduce exactly**, because three of its behaviors postdate the original
   manual-edit spec and were bug-driven:
   - a forced rebuild of the edited module before any dependent is checked
     (`rebuildApiSessionModule`, `:1262`), with a **fail-closed** branch when
     that rebuild fails (every dependent marked `unknown`, verdicts written as
     `error`, `:1264-1305`);
   - per-dependent verdicts from `rebuildApiSessionModule` (a real
     `lake build`), *not* the warm LSP check — the warm check is called only as
     an `author:"cascade"` timeline entry whose verdict is deliberately not
     trusted (`:1332-1367`);
   - a transitive-breakage fixpoint that propagates "invalid" down the
     recorded import graph even when a second-hop dependent's own check
     spuriously passed against a stale `.olean` (`:1385-1427`).
   Any "reuse the cascade" phrasing in the feature spec means *this* block,
   fixpoint and all.

2. **Chat runs are companion-driven end-to-end, and the terminal-state hook
   already exists but is discarded.** `startChatRun` (`server.mjs:1455`) calls
   `runApiProofJob` (the SSE driver in `leaApiClient.mjs`) and returns as soon
   as `onRunStarted` fires; the promise for the *whole run* (`run`) resolves at
   terminal state but is currently only consulted for start-failure
   (`run.then(...)` used as a fallback settle). Post-chat-run cascade needs no
   new plumbing to *know* a run ended — it needs a continuation chained onto
   that existing promise. Chat runs are **not** jobs: they live in
   `state.chatSessions` keyed by `targetKey` (`handleChatMessage`,
   `:810-870`), so anything a chat-triggered cascade wants to persist must go
   through the target's *linked job* (see fact 4) or a new chat-side record.

3. **Formalize/re-formalize runs are jobs with a clean terminal seam.**
   `handleFormalize` (`server.mjs:203`) creates a job via `createLeaJob`
   (`:2459` — note jobs already carry a `mode` field, `"formalization"` today,
   which is the natural discriminator for a `"repair"` mode) and fire-and-forgets
   `runLeaJob` (`:3008`), which ends by calling `applyProofOutcomeToJob`. The
   post-run classification hook for re-formalize belongs at the end of
   `runLeaJob`, after the outcome is applied (so `job.recordedProofPath` /
   `job.declarationName` reflect what the run actually produced).

4. **Breakage attribution is only half-persisted today.** The chip override is
   persisted: `recordEditCheckVerdict` (`server.mjs:1442`) writes
   `job.lastEditCheckStatus` / `lastEditCheckDetail` / `lastEditedAt` into
   `state.jobs` (flushed to `jobs.json` via `writeJson(state.jobsPath, ...)`),
   and `getTheoremStatus` honors it ahead of every other status source
   (`:3972` → `buildEditBrokenTheoremStatus`, `:4149`, `brokenByEdit: true`).
   But the **upstream attribution** — *which* item's change broke this one,
   whether it was a rename, `brokenByUpstream` — exists only in the transient
   `dependentsImpact[]` response of one save call. After a manifest refresh,
   a broken dependent's chip shows a bare "invalid" with the check detail
   text. The feature spec requires repair offers to be **re-derivable** after
   refresh/restart, so attribution must start being persisted on the job
   (Phase 2). This is the single biggest gap between "what the cascade knows"
   and "what a later repair dispatch can know."

5. **The manifest item shape doesn't carry breakage metadata to the
   extension.** `enrichLeanPaneItem` (`server.mjs:3761`) maps a status object
   to a fixed pane-item shape; `brokenByEdit` is set on the status
   (`:4155`) but nothing in `extension/` reads it (grep: only
   `server.mjs:4155`). The Repair button's gating predicate needs a new field
   plumbed through the manifest mapping — additive, but it must be done
   deliberately, not assumed present.

6. **"Agent of choice" is already a settled, catalog-backed setting.**
   `state.settings.leaModel`, normalized via `normalizeLeaModelId` against
   `LEA_MODEL_BY_ID` (from `packages/lea-model-catalog`), set by the options
   page (`handleUpdateLeaSettings`, `server.mjs:1502-1560`; env fallback
   `LEA_MODEL`, `:91`), and *already recorded per-job* (`createLeaJob` stamps
   `modelInfo`, `:2468`). The repair confirmation dialog reads this; no new
   model plumbing exists in v1 (per the feature spec's Non-Goals, the adapter's
   `RunRequest` gains no `model` field).

7. **Busy-skip and preflight conventions are established and reusable.**
   `findActiveJob(jobs, jobKey)` (`server.mjs:2371`) is the one busy signal —
   both the cascade (`:1274`, `:1326`) and `handleFormalize` (`:234`) use it.
   Run preflight is `syncSharedSettingsFromAdapter` + `validateLeaRuntime` +
   `spendLimitReached` (`:218-226`, same trio in `handleChatMessage`). Repair
   dispatch copies this trio verbatim, per item for batches (a cap can be hit
   mid-batch).

8. **A real cascade test fixture already exists.** `docs/cascade-testing-fixture.tex`
   / `docs/cascade-testing-guide.tex` (the compiled guide walks breakage
   recipes; the fixpoint comment at `server.mjs:1391` cites "Recipe 4"). The
   integration smoke tests below extend those recipes rather than inventing a
   new fixture.

---

## 2. Work breakdown

### Phase 0 — Factor the cascade out of `handleLeanPaneEditSave` (pure refactor)

No behavior change; every existing test stays green. This phase exists so
Phases 1 and 3 have something callable.

**New module: `apps/overleaf-extension/companion/cascadeVerify.mjs`** (kept
importable without the HTTP server, like `leanDependencyGraph.mjs`), exporting:

- `async function runCascadeVerification({ state, deps, upstream })` where
  - `deps` bundles the adapter-call context (`fetchImpl`, `baseUrl`, `apiKey`)
    plus the injected helpers the block already uses
    (`rebuildApiSessionModule`, `runApiSessionLeanCheck`, `dependentsOf`,
    `resolveDependentSession`, `recordEditCheckVerdict`,
    `summarizeDependentFile`, `parseLeanImports`) — injected, not imported,
    so the module unit-tests with the same fake-`fetchImpl`/stub pattern
    `leanPaneEdit.test.mjs` already uses and `server.mjs` keeps owning its
    helpers;
  - `upstream` is the new attribution descriptor threaded through everywhere
    `{ targetLabel, renamed }` is built today (`:1380`, `:1413-1417`):
    ```ts
    type UpstreamChange = {
      overleafProjectId: string;
      targetLabel: string;          // pane label of the changed item
      effectiveName: string;        // current Lean declaration name
      classification: EditClassification;  // from classifyEdit
      via: "edit" | "chat" | "formalize" | "repair";
      editedAt: string;             // ISO timestamp
      sessionId: string;            // upstream session (for the rebuild step)
      path: string;                 // upstream file path (for the rebuild step)
      namespace: string;
      moduleName: string;
    };
    ```
  - returns `{ dependentsImpact, jobsChanged }` — the caller persists
    `state.jobs` once, exactly as today (`:1430-1432`).
- The moved code is the block at `server.mjs:1239-1428` **verbatim in
  behavior**: recovery/`cascadeRequired` gating stays in the *caller* (it
  depends on caller-local state like `wasPreviouslyFailing`); the rebuild,
  fail-closed, busy-skip, per-dependent build-verdict, `author:"cascade"`
  timeline entry, and the transitive fixpoint all move.
- `brokenByUpstream` entries gain `via: upstream.via` and
  `editedAt: upstream.editedAt` (additive fields; `formatDependentOutcome` in
  `leanPaneView.mjs` ignores unknown fields, confirmed by its current shape).

**`handleLeanPaneEditSave`** shrinks to: resolve → snapshot → write → check →
classify → (gate) → `runCascadeVerification({ ..., upstream: { via: "edit",
... } })` → persist → respond. Response shape unchanged.

**Tests: `apps/overleaf-extension/tests/cascadeVerify.test.mjs`** — port the
cascade-specific cases out of `leanPaneEdit.test.mjs` (which keeps its
handler-level coverage): rebuild-fails ⇒ all-unknown fail-closed; busy
dependent skipped in both branches; broken dependent gets `invalid` +
persisted verdict + `author:"cascade"` timeline call; second-hop fixpoint
propagation flips a spuriously-"reverified" entry to `invalid` with
`viaModule`; `via`/`editedAt` present on every `brokenByUpstream`.

### Phase 1 — Post-run cascade for agent-driven changes (chat + re-formalize)

Closes the detection gap (feature spec Part 1). Two call sites, one shared
helper.

**Shared: `snapshotPreRunState` / `classifyRunOutcome`** (in
`cascadeVerify.mjs` or `server.mjs`, decide by size):

- At run start, for a target that already has a recorded artifact
  (`linkedJob?.recordedProofPath` truthy), capture
  `{ beforeHeader: parseDeclarationHeader(content, effectiveName), moduleName,
  namespace, path, wasPreviouslyFailing }`. **Persist the parsed header (small)
  on the job / chat record; keep full `before.content` in memory only** — the
  header is all `classifyEdit` needs, full content is only nice-to-have for a
  future richer diff, and `jobs.json` should not accrete whole Lean files.
  A companion restart mid-run therefore loses only the optional diff, not the
  classification — matching the feature spec's documented restart limitation
  but strictly smaller.
- At terminal state: re-read the file (same `loadEditableSessionFile` /
  `fetchApiSessionDetail` path the save flow uses), `parseDeclarationHeader`
  the after-side, `classifyEdit({ before, after, expectedName,
  ownCheckFailed })` with `ownCheckFailed` taken from the run's recorded
  verdict (the adapter back-fills `check_status` onto the final `code_step`,
  D6 — read it from session detail rather than re-checking), then gate on
  `cascadeRequired(classification) || recoveredFromFailure` and call
  `runCascadeVerification` with `via: "chat"` or `"formalize"`.
- Renames get the same `linkedJob.declarationName` refresh the manual path
  does (`server.mjs:1214-1217`) — an agent can rename too, and every reader
  of `declarationName` has the same stale-cache bug otherwise.

**Call site A — chat:** in `startChatRun`, extend the existing
`run.then(...)` continuation: after the start-failure settle logic, `await`
the post-run classification + cascade (wrapped in try/catch that logs and
never throws — a cascade failure must not poison the settled chat response).
The pre-run snapshot is taken in `handleChatMessage` before `startChatRun`
(it's the last place with the resolved target + session), passed through.
Store the outcome on a new `state.chatSessions[targetKey].lastRunImpact =
{ classification, dependentsImpact, finishedAt }` (persisted via the existing
`persistChatSessions`), and expose it in `handleChatSession` /
`handleChatPoll` responses (additive field on `toChatSessionResponse`'s
output, assembled server-side) so the mirror can render the "this change
broke N downstream items" notice and clear it on next message.

**Call site B — formalize:** at the end of `runLeaJob` (after
`applyProofOutcomeToJob`), same classification + cascade with
`via: "formalize"`. Only when the job *started with* a recorded artifact
(the re-formalize case; a first formalization has no "before" and no
dependents that could have elaborated against it — `dependentsOf` on a
brand-new module is empty anyway, so this is an optimization and a
clarity guard, not a correctness requirement). Note `cleanupPreviousRunArtifacts`
(`:265`) may have *deleted* the old file before the run: snapshot in
`handleFormalize` **before** the cleanup call, not inside `runLeaJob`.

**Tests:** `leanPaneChat.test.mjs` — a chat run (fake `fetchImpl` driving
`runApiProofJob` to terminal) whose final file has a changed header triggers
cascade calls and records `lastRunImpact`; a proof-only chat outcome does not;
cascade explosion doesn't reject the chat response. `companion.test.mjs` —
re-formalize with changed signature cascades; first-formalize doesn't
classify; snapshot survives the cleanup path.

### Phase 2 — Persist breakage attribution; plumb it to the pane

The repair offer must be re-derivable after refresh/restart (grounding
fact 4/5).

- **Job-side:** wherever the cascade (Phase 0 module) writes an `error`
  verdict on a dependent's linked job, also write
  `job.lastEditBreakage = { upstreamLabel, upstreamDeclarationName,
  classificationKind, renamedTo?, via, editedAt }`; clear it wherever a
  non-error verdict is recorded (`recordEditCheckVerdict` is the single
  choke point for both — extend its signature with an optional `breakage`
  argument rather than adding a second mutation site). The *edited/changed
  item itself* gets the same treatment on its own failed check
  (`brokenByEdit` case): `lastEditBreakage` with `upstreamLabel === its own
  label`, so one field drives both offer variants.
- **Manifest-side:** `buildEditBrokenTheoremStatus` (`server.mjs:4149`)
  already receives the linked job — add the persisted breakage to the status
  object; `enrichLeanPaneItem` (`:3761`) passes a new `item.breakage`
  field through to the extension (shape mirrors `BrokenByUpstream` in the
  feature spec, plus `repair: { state, runId?, failureReason? }` filled by
  Phase 3/4). Confirm during implementation which exact mapping function
  whitelists item fields, and extend it there — do not bypass it.
- **Suppression rule** (feature spec edge case): when the *upstream* item's
  own file currently fails to compile (`classificationKind ===
  "own-check-failed"` on the upstream's own job), dependents' `breakage`
  entries are marked `repairSuppressed: "upstream_broken"` at manifest-build
  time (computed, not stored — derivable from the upstream job's
  `lastEditCheckStatus`), so the UI offers repair on the upstream item only.

**Tests:** `companion.test.mjs` / `leanPaneManifest.test.mjs` — breakage
persists across a simulated restart (re-read `jobs.json` fixture) and appears
on the manifest item; a passing re-check clears it; suppression flag computed
when upstream is itself broken.

### Phase 3 — The repair run: prompt, job mode, single-item endpoint

**Prompt: `buildRepairPrompt` in `companion/chatPrompt.mjs`** (pure,
side-effect-free, tested like `buildChatPrompt`). Inputs per feature spec
Part 4: target identity block (reuse the same lines `buildChatPrompt` emits
for first messages — factor the preamble into a shared helper rather than
duplicating), upstream change block (label, classification kind, old/new
headers, explicit rename mapping "X was renamed to Y — renamed, not
removed"), the dependent's current `lastEditCheckDetail` diagnostic, the
done-definition (compiles via `lean_check`; statement semantically unchanged
except mechanical renamed references; no `sorry`/`admit`/`axiom`), and the
stop condition (report unprovable-as-stated instead of altering the
statement; the report must clearly state *why*).

**Dispatch: repair runs are jobs**, `mode: "repair"` (the field exists,
grounding fact 3). New `startRepairJob({ state, item, breakage })` in
`server.mjs`:

1. Preflight trio (fact 7) + re-validate the offer: resolve the item's
   session (`resolveEditSession`, both kinds — the `resolveDependentSession`
   pattern); if `lastEditBreakage` is gone or the latest verdict is `ok`,
   return `{ alreadyFixed: true }` (feature spec: no-op, not a run); if
   `findActiveJob` hits, return the existing busy response shape.
2. `createLeaJob(...)` with `mode: "repair"`, the item's own `jobKey` (so
   every existing busy check composes), `repairOf` = the persisted breakage
   descriptor, and **no** `cleanupPreviousRunArtifacts` / stub-reuse logic —
   a repair edits the existing recorded file in the existing session; deleting
   it would destroy exactly the context the repair needs.
3. Run via a `runLeaRepairJob` sibling of `runLeaJob`: `runApiProofJobForJob`
   with the repair prompt against the item's **existing session** (this is
   the one structural difference from formalize jobs, which may create
   sessions — repair must pass `sessionId` and fail loudly if absent, the
   `resolveEditSession` no-create rule).
4. Verify the outcome itself — do not trust run completion: rebuild + check
   the item's module (`rebuildApiSessionModule`, the same authoritative
   verdict source the cascade uses), then `recordEditCheckVerdict` — an `ok`
   clears `lastEditBreakage` (Phase 2's choke point does this for free), a
   failure writes `breakage.repair = { state: "failed", failureReason }`
   with the agent's final message as the reason.
5. Post-run cascade with `via: "repair"` (Phase 1's helper — a repair that
   touched its own header cascades like any other change; normally a no-op).
6. **Statement-guard check:** compare pre-repair vs post-repair headers of
   the *item's own* declaration (`parseDeclarationHeader` both sides). A
   changed header is allowed only when `repairOf.classificationKind ===
   "renamed"` *and* the change is confined to renamed identifiers — v1
   implements the tractable approximation: header changed ⇒ flag the repair
   outcome as `needs_review` (surfaced in the UI as "repaired, but the
   statement changed — review required") rather than silently accepting or
   hard-failing. Record the decision in the job for the timeline.

**Endpoint:** `POST /lean-pane/repair/start` → `handleLeanPaneRepairStart`
(payload: `{ overleafProjectId, targetKind, targetLabel }` — the breakage
descriptor is read from the *persisted* job state, not trusted from the
client), registered in the dispatch block alongside `/lean-pane/edit/*`
(`server.mjs:1875-1885`). Response: `buildJobResponse(...)` in-progress shape,
so the extension's existing job polling drives progress unmodified.

**Tests:** `chatPrompt.test`-style unit tests for `buildRepairPrompt`
(rename vs signature vs def-body variants; stop-condition text present);
`leanPaneRepair.test.mjs` for `startRepairJob` — already-fixed no-op, busy
conflict, missing-session loud failure, `ok` outcome clears breakage,
failed outcome records `failureReason`, header-change ⇒ `needs_review`,
spend-cap preflight rejection.

### Phase 4 — Batch repair: ordering, sequencing, skip rules

**`POST /lean-pane/repair/all`** → `handleLeanPaneRepairAll` (payload:
`{ overleafProjectId, items: [{ targetKind, targetLabel }] }`):

1. Re-derive each item's persisted breakage; drop already-fixed/busy items
   into the response's `skipped[]` up front.
2. **Topological order:** reuse `listProjectProofFiles` +
   `parseLeanImports` (`leanDependencyGraph.mjs`) to build the *forward*
   import relation among the repair set's modules; sort so an item runs only
   after every repair-set item it transitively imports. New pure function
   `topologicalRepairOrder(items, importsByModule)` in
   `leanDependencyGraph.mjs` (visited-set DFS; acyclic by Lean construction,
   defensive cycle guard returns insertion order + a flag rather than
   looping).
3. Dispatch sequentially: an async loop that awaits each `startRepairJob`'s
   *terminal* outcome (Phase 3 step 4) before the next. On failure, mark
   every not-yet-run item whose imports transitively include the failed
   module as `skipped: "depends_on_failed"` and **pause** — the batch record
   (below) carries `pausedOn`, and the extension renders continue/stop.
   `POST /lean-pane/repair/all/continue` resumes past the pause (repairs the
   remaining non-skipped items); the batch is abandoned implicitly if never
   continued.
4. **Batch state:** `state.repairBatches[batchId] = { items: [{ label, state,
   runJobId }], pausedOn, createdAt }`, in-memory only (a restart mid-batch
   leaves per-item truth in jobs/breakage records, which is the durable
   layer; the batch is just orchestration). Exposed via the `repair/all`
   response and a `GET`-shaped `POST /lean-pane/repair/status` for the
   extension's polling.
5. Per-item preflight repeats the spend-cap check (fact 7) — a cap reached
   mid-batch pauses with `pausedOn: { reason: "max_spend" }` rather than
   failing the item.

**Tests:** `leanPaneRepair.test.mjs` — order respects imports (B before C
when C imports B); failure of B skips C but continues an independent D only
after user continue; mid-batch spend-cap pause; batch status reflects
per-item progression.

### Phase 5 — Extension UI

All rendering additions follow the existing pure-helper-in-`leanPaneView.mjs`
+ wiring-in-`content.js` split.

**`leanPaneView.mjs`:**
- `canRepairPaneItem(item)` — `Boolean(item.breakage) &&
  !item.breakage.repairSuppressed && item.status !== "in-progress"`.
- `formatBreakageAttribution(breakage)` — the chip-adjacent line: "broken by
  an edit to X" / "broken — X was renamed to Y (via chat)" / "your edit broke
  this item", from `via` + `classificationKind` (extends the copy
  `formatDependentOutcome` already established; renamed stays distinct per
  the manual-edit spec's acceptance criterion 8).
- `formatRepairConfirmation({ items, breakage, modelLabel })` — the
  confirmation body: item list, upstream change summary, "will start N agent
  run(s) with <modelLabel>". `modelLabel` comes from the manifest/settings
  payload the pane already receives (`leaModel` + `LEA_MODEL_BY_ID` label —
  confirm which existing settings-shaped response the pane holds and extend
  it if the label isn't already client-visible).
- `formatRepairOutcome(breakage.repair)` — running / repaired / failed
  (+ reason) / needs_review / skipped lines.

**`content.js`:**
- `renderRepairButton(item)` in the expanded-item actions block
  (`:783-789`, next to `renderEditButton`), gated by `canRepairPaneItem`;
  click → confirmation (native `confirm`-style inline panel, same pattern as
  the edit surface's save/cancel — no new modal framework) → `POST
  /lean-pane/repair/start` → item enters the existing in-progress rendering
  via the normal manifest refresh (`scheduleLeanPaneRefresh`, `:493`).
- "Repair all (N broken)" action appended to
  `renderLeanPaneEditImpactSummary` (`:973`) when `brokenCount > 0`, and to
  the chat mirror's new post-run impact notice; both call
  `/lean-pane/repair/all` after the same confirmation panel, then poll
  `/lean-pane/repair/status` alongside the existing refresh loop; the pause
  state renders continue/stop buttons wired to `repair/all/continue` /
  simply dismissing.
- Chat mirror: render `lastRunImpact` (Phase 1) as a system-style notice row
  in the transcript area with the same per-dependent outcome lines
  (`formatDependentOutcome`) and the batch-repair affordance.

**`content.css`:** `.ol-lean-project-repair-*` classes following the existing
`ol-lean-project-*` convention; no new visual language.

**Tests:** `leanPaneView.test.mjs` — the four new pure helpers, including
suppression and needs_review copy. `contentActions.test.mjs` — repair button
gating; confirm-then-post payload shape; no network on cancel; "Repair all"
appears only with `brokenCount > 0`.

### Phase 6 — Docs

- `FEATURE-overleaf-lean-pane-manual-edit.md`: mark the "v2 (Roadmap)"
  section as superseded-by/implemented-in `FEATURE-overleaf-self-repair.md`.
- `FEATURE-overleaf-self-repair.md`: reconcile the spec with implementation
  realities discovered here — most notably that the cascade uses
  build-verdicts + fixpoint propagation (grounding fact 1), that `repairOf`
  persists as `lastEditBreakage` on jobs, and the `needs_review`
  statement-guard outcome (Phase 3 step 6), which the spec's acceptance
  criterion 6 describes more loosely.
- This plan: add a Status section once work starts (convention:
  `PLAN-overleaf-lean-pane-manual-edit.md`).
- `docs/cascade-testing-guide.tex`: add repair recipes (see §5).

---

## 3. Suggested sequencing

1. **Phase 0** (cascade factor-out) — pure refactor, green suite before and
   after; everything else calls it.
2. **Phase 3's prompt builder** (`buildRepairPrompt`) in parallel — pure
   function, no dependencies.
3. **Phase 2** (persist attribution) before Phase 1 — Phase 1's cascade calls
   want to write attribution through the Phase 2 choke point from day one,
   otherwise chat-broken dependents are un-repairable until a later migration
   of transient state.
4. **Phase 1** (post-run cascade) — the detection gap; after this lands, the
   product *detects* everything the feature covers, with repair still absent.
   This is a shippable intermediate state.
5. **Phase 3** (single-item repair) — the core deliverable.
6. **Phase 4** (batch) — pure orchestration over Phase 3.
7. **Phase 5** (UI) last, thinnest, per the established pattern.
8. **Phase 6** (docs) as each convention finalizes.

## 4. Edge cases to handle (with the phase that owns each)

- **Upstream edited again while a repair runs** → nothing special: the
  repair's verdict comes from a rebuild against the *current* tree (Phase 3
  step 4), so a stale repair surfaces as `failed`, never a false `repaired`.
  The new edit's cascade sees the repair job via `findActiveJob` (shared
  `jobKey`) and marks the item busy — composition confirmed by a test, not
  assumed.
- **Item fixed manually between offer and dispatch** → Phase 3 step 1's
  re-validation (`alreadyFixed` no-op).
- **Diamond / two upstream breaks on one dependent** → last-cascade-wins on
  `lastEditBreakage` (same rule the verdict already has); repair correctness
  is attribution-independent (rebuild is the arbiter).
- **Upstream itself doesn't compile** → Phase 2's `repairSuppressed`
  derivation; dependents' offers return once the recovery cascade
  (`recoveredFromFailure`, existing) clears them.
- **Dependent with no linked job / no session** (`attributed: false` in
  today's cascade, `server.mjs:1318-1325`) → no place to persist breakage, so
  **no repair offer**; the impact line already says a manual re-check is
  needed. Recorded as a known limitation in Phase 6's spec reconciliation —
  fixing it means inventing job-less attribution storage, out of scope.
- **Companion restart** → per-item breakage + verdicts survive (`jobs.json`);
  in-flight run classification loses only the optional content diff (Phase
  1's header-persistence decision); an in-flight *batch* dies but every
  completed/failed item's truth persists and offers re-derive.
- **Repair job vs. status derivation** → a `mode: "repair"` job with
  `finalStatus: "failed"` must not make `getTheoremStatus` re-derive the item
  as a failed *formalization* (wrong copy, wrong affordances). Audit every
  `findLatestJob(jobs, key, status)` consumer for mode-sensitivity in Phase 3
  — this is the plan's riskiest integration point (see §6).
- **Interrupt** → repair jobs get interrupt for free if and only if the
  existing interrupt path keys off jobs/`apiRunId` — confirm
  `handleChatInterrupt`-vs-job-interrupt coverage for `mode: "repair"` during
  Phase 3 and wire the pane's existing interrupt affordance to it.

## 5. Testing & verification

- Unit suites per phase, listed inline: `cascadeVerify.test.mjs` (new),
  `leanPaneRepair.test.mjs` (new), additions to `leanPaneChat.test.mjs`,
  `companion.test.mjs`, `leanPaneManifest.test.mjs`, `leanPaneView.test.mjs`,
  `contentActions.test.mjs`, `chatPrompt` tests. Full suite:
  `npm test -w apps/overleaf-extension`.
- **Adapter:** no adapter code change is expected anywhere in this plan
  (grounding facts 3/6 — runs, sessions, lean-check, rebuild all exist). If
  implementation discovers otherwise, that's a plan deviation to record, not
  to absorb silently.
- **Integration smoke (manual, against the real prover), extending the
  cascade-testing-guide recipes:**
  1. *Rename repair:* rename `A` via the **chat mirror** (not manual edit —
     this exercises Phase 1 + 3 together); confirm dependents `B`, `C` break
     with `via: "chat"` attribution, then "Repair all" fixes both in
     topological order (`B` before `C`) with two clicks total, and both chips
     clear only after real rebuild verdicts.
  2. *Hypothesis-change repair:* strengthen a hypothesis on `A` such that
     `B`'s proof genuinely needs rework but remains provable — confirm the
     repair run fixes `B` without touching `B`'s statement (header-guard
     silent).
  3. *Unprovable-as-stated:* remove a hypothesis `B`'s statement genuinely
     needs — confirm the repair ends `failed` with the agent's explanation
     surfaced, statement untouched.
  4. *Own-edit repair:* break `A` itself with a manual edit; confirm the
     repair offer appears on `A`, dependents' offers are suppressed, and
     repairing `A` triggers the recovery cascade that clears them.
  5. *Mid-batch failure:* three-item chain where the middle repair is made to
     fail — confirm the downstream item is skipped with reason and the
     independent item still runs after "continue".
- Regression: recipes 1–4 of the existing cascade guide (manual-edit paths)
  behave identically after Phase 0's refactor — run them immediately after
  Phase 0, before any new behavior lands on top.

## 6. Open risks

- **(High) `mode: "repair"` jobs interacting with status re-derivation.**
  `getTheoremStatus` and its helpers (`findLatestJob*`, `buildJobResponse`,
  `getLatestMappedJobStatus`, `readLeanPaneArtifactFromSession`) all consume
  jobs by `jobKey` with no mode awareness today. Sharing the target's
  `jobKey` buys the busy logic for free but risks a failed repair
  masquerading as a failed formalization in half a dozen readers. Phase 3
  must start with a read-through of every `jobKey` consumer; if the audit
  gets hairy, the fallback design is a *separate* job key namespace
  (`repair:<jobKey>`) plus one explicit busy-check addition — less free
  composition, far smaller blast radius. Decide there, with the code open,
  not here.
- **(Medium) The chat post-run hook's failure isolation.** `startChatRun`'s
  promise chain currently settles the HTTP response; the Phase 1
  continuation runs *after* settle, unawaited by any caller. An unhandled
  rejection there is a process-level event in Node. The continuation must be
  fully try/caught with logging, and the test suite must include a
  cascade-throws case.
- **(Medium) Statement-guard approximation (Phase 3 step 6).** "Header
  changed ⇒ needs_review" will flag legitimate rename-reference updates
  inside the dependent's own binders (the dependent's header can mention the
  renamed upstream identifier). Acceptable v1 noise (a false `needs_review`
  is a review, not a wrong verdict), but write the guard so the rename case
  compares headers *after* applying the known old→new substitution, which
  removes the most common false positive cheaply.
- **(Medium) Repair prompt efficacy.** Whether the agent reliably repairs
  from the injected context (vs. wandering into re-proving from scratch) is
  an empirical question the smoke recipes answer late. De-risk early: once
  `buildRepairPrompt` exists (step 2 of sequencing), hand-run its output
  against a real broken dependent via the standalone UI before building
  Phases 3–5 on top of it.
- **(Low) `lastRunImpact` growth in `chat-sessions.json`.** One record per
  target, overwritten per run — bounded, but confirm `persistChatSessions`
  isn't called on a hot path where the added payload matters.
- **(Low) Batch UX during long runs.** Each repair is a full agent run
  (minutes); a paused-or-running batch across pane reloads relies on
  `repair/status` polling + persisted per-item truth. If the in-memory batch
  record proves too fragile in practice, promote `repairBatches` to a JSON
  file next to `jobs.json` — the design leaves that a one-line storage swap.
