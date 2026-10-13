# Plan — Manual-Edit Save Latency: Fast Save, Background Cascade

Fix for the reported latency problem on the lean pane's manual-edit flow
(`FEATURE-overleaf-lean-pane-manual-edit.md`, hardened by
`FEATURE-overleaf-self-repair.md` Phase 0):

> When I press save after a manual edit there is 1–2 minutes where the
> formalization is pending, as in a saving state. This is longer than is
> desirable.

Goal: **keep every verdict, attribution, and fail-closed behavior exactly as
it is today** — the cascade's correctness properties were all bug-driven and
must not regress — while (a) removing genuinely redundant work from the
verification pass and (b) taking the remaining work off the save request's
critical path.

File references are as of branch `self_heal` (post self-repair). Line numbers
drift; symbol names are the stable anchor.

## Status

Not started.

---

## Where the time goes, diagnosed

`handleLeanPaneEditSave` (`companion/server.mjs:1226`) runs the entire
verification pipeline **synchronously inside the one HTTP request**, and the
pane's Save button awaits that response end-to-end
(`extension/content.js:1144` — button disabled, textarea locked, "Saving…"
until the fetch resolves). The chain:

1. `loadEditableSessionFile` + `writeApiSessionFile` — read, write, git
   commit. Cheap (sub-second).
2. `runApiSessionLeanCheck` — the edited file's own verdict, warm-LSP path
   (`adapter routes/sessions.py: lean_check_session`). Fast **only if the
   daemon is warm** — see cost 2 below.
3. When `cascadeRequired(classification)` or the edit recovered a previously
   failing file: `runCascadeVerification` (`companion/cascadeVerify.mjs`),
   which is where the minutes live:
   - one real `lake build` of the edited module (fail-closed gate), then
   - **per dependent, serially**: another full `lake build`
     (`cascadeVerify.mjs`, the `rebuildApiSessionModule` call in the
     dependents loop) **plus** a warm-LSP `lean_check` whose only purpose is
     the `author:"cascade"` timeline entry (the very next comment says its
     verdict is deliberately not trusted).

Two compounding costs:

**Cost 1 — N + 1 serial `lake build` invocations.** `rebuild_module`
(`prover/lea/tools.py:217`) shells out `lake build <module>` once per call.
Each invocation pays lake startup and elaborates one Mathlib-importing file;
serially, N dependents ≈ N × (tens of seconds) even when nothing broke.

**Cost 2 — the "warm" checks are secretly cold.** Every successful rebuild
calls `lsp_daemon.mark_stale`, which restarts the LSP daemon (documented in
`routes/sessions.py: lean_check_session`'s docstring — the restart is load-
bearing for cascade correctness). So inside the cascade loop the sequence is
*rebuild (daemon restarts) → "warm" check (fresh daemon re-imports Mathlib)*,
per dependent: each timeline check is effectively a cold check. The same
mechanism means the **own check in step 2** is cold whenever any earlier save
ran a rebuild — the user pays a daemon warm-up at the top of every
cascade-triggering save.

Worked example — one edited item, two dependents, nothing actually broken:
1 (possibly cold) own check + 3 serial lake builds + 2 cold timeline checks,
all before the button un-sticks. That is the reported 1–2 minutes.

The client side is blameless-but-complicit: `content.js` has no rendering for
a partially-complete save, so the server has no fast response it *could*
return today.

## Design principle

The user pressing Save is waiting for exactly one answer: **"did *my* edit
compile?"** Everything else — dependent re-verification — is already
architected as *derived truth that flows through job records and manifest
refreshes* (verdicts persist via `recordEditCheckVerdict`; chips read
`lastEditCheckStatus`; impact summaries reconcile against live state on every
render, per `PLAN-self-repair-stale-offers.md`'s rule). Nothing in that
machinery requires the cascade's results to arrive in the save response — the
synchronous design predates it. So:

> The save response ends when the user's own answer is known. Dependent
> verification is a tracked background pass whose results arrive through the
> exact channels that already deliver them on every other refresh — and that
> pass itself should do the minimum real work (one batch build on the happy
> path, per-module attribution only on failure).

Phases 1.x are pure cost removal (no UX or API-shape change, independently
shippable). Phase 2 is the decoupling. Phase 0 keeps us honest.

---

## Phase 0 — Instrument the save path

**Where:** `handleLeanPaneEditSave` + `runCascadeVerification` (companion),
gated behind the companion's existing debug logging.

**Approach.** Wrap each step (write, own check, upstream rebuild, each
dependent build, each timeline check) with duration logging, emitted as one
structured line per save. Every claim in the diagnosis above becomes a
measured number before and after each phase lands; Phase 3's go/no-go
decision reads directly off it.

**Tests.** None beyond "doesn't break the suite" — logging only.

**Risk.** None.

## Phase 1.1 — Stop double-checking dependents (verdict passthrough)

**Where:** adapter `routes/sessions.py: lean_check_session` +
`companion/cascadeVerify.mjs` dependents loop.

**Approach.** The per-dependent `runApiSessionLeanCheck` exists only to
record the `author:"cascade"` timeline code_step; its LSP verdict is
explicitly untrusted (the `lake build` verdict wins, per the inline comment).
Yet it costs a full — post-`mark_stale`, effectively cold — check per
dependent. Let the caller supply the verdict it already has:

- Extend the adapter's `PathRequest` for `lean-check` with an optional
  `verdict: { status, detail }`. When present together with `author`, **skip
  `interface_check` entirely** and record the new code_step with the supplied
  verdict. No verdict → behavior unchanged.
- The cascade passes the dependent's `lake build` result as the verdict.

This removes Cost 2's per-dependent cold check outright, and makes the
timeline row *more* honest than today: the recorded step now carries the same
authoritative build verdict the chip shows, instead of a warm-LSP opinion
that can disagree with it.

**Tests.** Adapter pytest (`test_routes_sessions`-family): verdict supplied →
step recorded with that status/detail, `interface_check` not called (patch
it); no verdict → unchanged path. Companion `leanPaneEdit.test.mjs`: the fake
adapter asserts the cascade's timeline call carries the build verdict.

**Risk.** Minimal. Additive request field; one new branch at an existing
recording site.

## Phase 1.2 — One `lake build` for the whole cascade (batch rebuild)

**Where:** adapter — new `rebuild` capability accepting multiple paths;
`prover/lea/tools.py: rebuild_module`; `companion/cascadeVerify.mjs`.

**Approach.** `lake build A B C` builds all targets in one invocation: one
lake startup, shared work deduplicated, independent targets parallelized by
lake itself (safe — one lake process, unlike concurrent invocations against
the shared workspace, which is why the loop is serial today).

- `tools.py`: a multi-module variant of `rebuild_module` (same lake-root and
  module-name derivation per path, one `lake build <mods...>` subprocess, one
  `mark_stale`). Surface through `lea/interface.py` and a batch shape on the
  adapter's rebuild route (`paths: [...]` alongside today's `path`).
  Dependents belong to different sessions but share one Lake workspace; the
  batch call runs against the upstream session, while per-dependent timeline
  steps keep their own sessions via Phase 1.1's passthrough.
- Cascade logic becomes two-tier:
  - **Happy path:** one batch build of `[edited module, ...all dependents]`.
    `ok` → every dependent verdict is `ok`; record timeline steps via 1.1;
    done. N+1 serial builds collapse to 1.
  - **Failure path:** batch build fails → fall back to *exactly today's*
    sequence — upstream module alone first (preserving the fail-closed
    "can't verify" discrimination when the edited module itself doesn't
    build), then per-dependent serial builds for precise attribution. The
    slow path is paid only when something actually broke, which is when the
    user needs the per-item detail anyway.
- Transitive propagation, busy-skip, and no-session handling are untouched —
  they operate on the per-dependent verdicts, however produced.

**Tests.** Adapter pytest: batch route builds once, `mark_stale` once,
per-path validation errors surface per-path. Companion `leanPaneEdit` /
`companion.test.mjs`: happy path performs one rebuild call and marks all
dependents `reverified`; a failing batch falls back and produces
byte-identical `dependentsImpact` to today's fixtures (reuse the existing
cascade tests as the oracle).

**Risk.** Moderate — this is the one phase that touches verdict *production*.
Contained by the fallback being today's exact code path, and by the oracle
tests. Lake's multi-target failure output is not parsed at all (we only need
ok/not-ok for the batch; attribution comes from the fallback).

## Phase 1.3 — Pre-warm the LSP daemon while the user types

**Where:** adapter — a non-recording warm-up primitive;
`handleLeanPaneEditStart` (`companion/server.mjs:1180`).

**Approach.** The own check at save time is cold whenever a prior rebuild
restarted the daemon (Cost 2). The user telegraphs an upcoming check by
opening the editor — warm the daemon then, so Mathlib re-imports overlap with
typing instead of following the Save click:

- Adapter: `record: false` option on `lean-check` (run `interface_check`,
  persist nothing, back-fill nothing) — a pure warm-up must not mutate
  code_step verdicts as a side effect of merely opening an editor.
- Companion: `handleLeanPaneEditStart` fires it fire-and-forget (no await, no
  effect on the start response; failures logged and swallowed).

**Tests.** Adapter pytest: `record: false` runs the check, writes no step and
back-fills nothing. Companion `leanPaneEdit.test.mjs`: edit-start triggers
the warm-up call; a hanging/failing warm-up does not delay or fail the start
response.

**Risk.** Low. Worst case is a wasted warm-up (user cancels the edit) — the
daemon was going to be warmed by the next check anyway.

## Phase 2 — Decouple the cascade from the save response

**Where:** `handleLeanPaneEditSave` + new cascade-runner state
(`companion/server.mjs`), `handleLeanPaneManifest`, dispatch gates
(`resolveEditSession`, repair dispatch, chat dispatch); client
`content.js` save flow + `leanPaneView.mjs` reconciliation.

**Approach.**

*Server — return early, run the pass in the background:*

- The save handler completes write → own check → classification → own-verdict
  recording (`recordEditCheckVerdict` + `lastRepair` clearing + rename
  bookkeeping) → **persist jobs → respond**. When a cascade is required, the
  response's `dependentsImpact` lists the pending set (from `dependentsOf` +
  `summarizeDependentFile`, already in hand) with `status: "checking"`, plus
  `cascadePending: true`.
- `runCascadeVerification` then runs in the background, **unchanged
  internally** (including Phase 1's improvements). Verdicts land on job
  records per dependent exactly as today; jobs are persisted on completion.
  The completed impact list is stashed on the companion's persisted state
  keyed by the upstream `jobKey` (the same pattern as the chat mirror's
  `lastRunImpact`), replacing the pending entry.
- **Single flight.** One cascade runs at a time, globally — the Lake
  workspace is shared, and concurrent `lake build` invocations against it are
  exactly what the serial loop exists to avoid. A cascade triggered while one
  is running (another save, a post-run cascade from chat/re-formalize)
  queues.

*Server — busy semantics (the race the synchronous design got for free):*

- While a cascade is running or queued, its upstream item **and every item in
  its pending set** count as busy: an in-memory `state.activeCascades`
  registry (jobKey → { upstreamLabel, startedAt, pending: [...] }) consulted
  by the same gates that check `findActiveJob` today. A save/repair/chat/
  re-formalize dispatch on a covered item gets the existing 409/"busy"
  treatment, with the message naming the cascade ("still re-checking
  dependents of X"). The cascade's own skip rule (don't check a dependent
  with an active job) already composes from the other direction.
- v1 blocks a second save on the same item while its cascade runs;
  superseding (cancel + restart with the newer content) is a later
  refinement, noted in Non-goals.

*Server — restart safety (fail closed, as ever):*

- Today an interrupted save loses the cascade too (the whole request dies);
  the exposure isn't new, but backgrounding makes it likelier to go
  unnoticed. Persist a small `pendingCascade` record (the `upstream`
  descriptor + pending set) to the jobs file when a cascade starts; clear on
  completion. On companion boot, a leftover record **re-runs the cascade**
  (verification is idempotent, and Phase 1.2 makes the happy-path re-run one
  build); if the re-run cannot start (adapter down), mark the pending
  dependents unconfirmed with the same fail-closed verdict text the
  rebuild-failure path uses. Never leave a chip reading pre-edit "valid" for
  an item the cascade meant to check but didn't.

*Client — render the pass, don't wait for it:*

- The Save button resolves on the fast response: editor closes, own chip
  updates, the impact summary renders "re-checking N downstream item(s)…"
  from the `"checking"` entries.
- Results arrive through the pane's existing refresh machinery — verdicts are
  already served per item from job records on every manifest response. The
  manifest handler additionally surfaces `activeCascade` (per covered item,
  so chips/offers can show "re-checking…" and suppress dispatch client-side
  too) and the completed impact list for the upstream item. The pane polls at
  its existing in-progress cadence while a cascade covers any visible item,
  dropping back when none does.
- `reconcileDependentsImpact` (`leanPaneView.mjs`) learns the `"checking"`
  state: a pending entry upgrades to the live per-item truth once the manifest
  shows a fresh verdict, exactly the stale-offers rule ("render the copy as
  history, derive current-state claims from live truth") extended one state
  earlier in the lifecycle. Repair offers never render from a `"checking"`
  entry — an offer requires a landed verdict.

*Free rider:* the post-run cascade for chat/re-formalize/repair runs
(`runPostRunCascade`) flows through the same background runner and busy
registry, so agent-run completion also stops paying the cascade on its
critical path — same feature, no extra design.

**Tests.**
- `leanPaneEdit.test.mjs` / `companion.test.mjs`: save responds before any
  rebuild call is made (fake-adapter call ordering); verdicts land on job
  records after completion; busy gates reject dispatch on covered items
  mid-cascade and release on completion; queued second cascade runs after the
  first; boot with a leftover `pendingCascade` re-runs it / fails closed.
- `leanPaneView.test.mjs`: `reconcileDependentsImpact` upgrades `"checking"`
  entries from live items; no repair offer from a checking entry.
- `contentActions.test.mjs`: end-to-end — save returns fast, summary shows
  "re-checking…", a later manifest response carrying the landed verdicts
  flips the summary to today's final rendering (broken counts, offers), byte-
  compatible with the existing post-save fixtures.

**Risk.** Highest of the plan — concurrency where there was none. Contained
by: cascade internals unchanged (Phase 1 already re-verified them); busy
registry reuses the existing gate sites rather than new locking; single-
flight serialization preserves the workspace's one-writer property; restart
handling fails closed. The stale-offers reconciliation layer was built for
exactly this shape of eventual consistency.

## Phase 3 (conditional) — Async own check

**Only if Phase 0's numbers, measured after 1.3, show the own warm check
still dominating the save response.** Return after the write with
`checkStatus: "pending"`; deliver the verdict through the same
`lastEditCheckStatus` override via polling. Deliberately deferred: instant
"did my edit compile" feedback is the one thing worth a few synchronous
seconds, and pre-warming should keep it to that. Classification (and thus
cascade triggering) would need to move behind the async check, so this is a
real re-sequencing — not worth it on spec, only on evidence.

---

## What must not change (invariants, restated as a checklist)

1. Every dependent verdict comes from a real `lake build`, never the warm
   LSP check (batch `ok` ⇒ per-module `ok` is sound: `lake build A B C`
   succeeds only if every target built).
2. Upstream rebuild failure fails **closed**: dependents read unconfirmed/
   broken, never stale-valid. (Phase 1.2's fallback discriminates this case
   exactly as today; Phase 2's restart path extends it to interrupted
   cascades.)
3. Transitive propagation to a fixpoint over the recorded import graph.
4. Breakage attribution (`brokenByUpstream`, `lastEditBreakage`,
   before/after headers) and the repair-offer lifecycle, byte-for-byte.
5. Busy dependents are skipped, and (new, but implied by the old synchronous
   behavior) nothing dispatches onto an item the cascade hasn't finished
   with.
6. Acceptance criterion 9 of the manual-edit spec: no agent run starts from
   a save.
7. Timeline: every re-checked dependent still gets its `author:"cascade"`
   code_step — now carrying the build verdict (an improvement, not a
   change of contract).

## Suggested sequencing

1. **Phase 0** — measure; confirms the diagnosis and baselines the wins.
2. **Phase 1.1** — smallest change, likely the largest single win (kills the
   per-dependent cold checks).
3. **Phase 1.3** — trivial, independent; fixes the own check's cold start.
4. **Phase 1.2** — batch build; verify against the oracle tests.
5. **Phase 2** — the UX decoupling, landing on an already-fast cascade
   (which also shrinks the busy windows and restart-replay cost it has to
   manage).
6. Docs: update `FEATURE-overleaf-lean-pane-manual-edit.md` /
   `FEATURE-overleaf-self-repair.md` implementation notes (save-response
   contract, `"checking"` state, single-flight rule).

## Acceptance criteria

1. Saving a proof-body-only edit (no cascade) responds in roughly the cost of
   one warm LSP check; no `lake build` on the request path.
2. Saving a cascade-triggering edit responds in the same bound; the pane
   shows the edited item's own verdict immediately and "re-checking N…" for
   dependents, which resolve to today's exact final states via polling with
   no user action.
3. The happy-path cascade (nothing broke / recovery re-verification) performs
   exactly one `lake build` invocation regardless of dependent count.
4. When breakage exists, per-dependent verdicts, attribution, propagation,
   and repair offers are identical to the current implementation's for the
   same inputs (existing cascade tests pass unchanged against the fallback
   path).
5. No dispatch (save, chat, re-formalize, repair) can race a live cascade on
   a covered item; the rejection names the in-flight verification.
6. A companion restart mid-cascade never leaves a covered item reading its
   pre-edit status: the cascade re-runs on boot or the item reads
   unconfirmed.
7. Timeline code_steps for cascade re-checks carry the build verdict.
8. Phase 0's instrumentation shows the before/after step timings in the
   companion log for any save.

## Non-goals

- **Superseding an in-flight cascade** with a newer save on the same item
  (v1 blocks with 409; supersede is a refinement once the queue exists).
- **Parallel `lake build` invocations** against the shared workspace — batch
  targets in one invocation instead, and let lake parallelize internally.
- **Caching the dependency graph** — recompute-on-demand stays, per the
  manual-edit spec.
- **Async own check** — Phase 3, evidence-gated.
- **Standalone-UI cascade** — still tracked separately (self-repair spec
  Non-goals).

## Open risks

- **(Medium) Batch-build semantics across lake/toolchain versions.** The
  plan never parses multi-target failure output (ok/not-ok only, fallback
  for attribution), which minimizes surface — but "one invocation, N
  targets, one `mark_stale`" should be smoke-tested against the pinned
  toolchain before trusting the happy path.
- **(Medium) Busy-registry coverage.** The gates that must consult
  `activeCascades` are enumerable (`resolveEditSession`, repair dispatch,
  chat dispatch, re-formalize) but live in different handlers; a missed gate
  reintroduces a race the synchronous design masked. Mitigate with one shared
  helper and a test per dispatch path.
- **(Low) Pending-set identity across renames.** The busy registry and
  `"checking"` reconciliation key items the same way cascade attribution
  already does (declaration-name convention, refreshed by rename
  bookkeeping); a rename landing mid-cascade can briefly mis-key one refresh,
  self-correcting on the next — same accepted exposure as stale-offers
  Fix 1.
- **(Low) Polling load.** The elevated in-progress cadence now also covers
  cascade windows; bounded by single-flight and the pane's existing cadence
  caps.
