# Feature: Batch Stub / Formalize — "Stub all" and "Formalize all"

## Summary

Shared supporting mathematics is coordinated alongside the document queue. See
[Project mathematics and shared dependencies](FEATURE-overleaf-shared-mathematics.md)
for canonical placement, library tools, saved dependency waits, and reuse semantics.

The Lean pane offers two project-level launchers above the item tree:

- **Stub all (N)** — generate a Lean `sorry`-stub for every un-stubbed theorem
  in the project.
- **Formalize all (N)** — run Lea to formalize every item that has no verified
  proof yet (unformalized theorems *and* definitions, existing `sorry`-stubs to
  complete, and broken/stale items to re-formalize), skipping only already-valid
  work.

These are the **batch versions** of the existing per-item `POST /stub` and
`POST /formalize` actions. They reuse the sequential, spend-cap-aware,
dependency-ordered dispatch pioneered by **batch repair**
(`docs/FEATURE-overleaf-self-repair.md`) — the same in-memory batch record,
progress panel, `repair-batch-updated` event, and `/lean-pane/repair/all/continue`
resume endpoint — distinguished by a new `operation` field
(`repair | stub | formalize`).

Stub generation uses the explicit `overleaf_stub` run purpose and its own
statement-only prompt and completion policy. The agent stops after a fresh Lean
check while retaining `sorry`; the adapter validates the named theorem's own
placeholder body and records an `answered` run, never a proved result. Definitions,
unrelated placeholders, stale checks, and ambiguous target declarations are rejected.
The companion waits for the current check observation after the run finishes;
publication lag keeps the job in progress rather than failing a compiled stub.
This wait is durable across worker restart and can be stopped with the batch.
Single-item and batch jobs persist the checked stub and a registry publication
retry before saving the derived project registry. A registry outage does not
change the stub outcome; publication resumes after worker restart without
rerunning the model. Batch items retain their job identity before run admission,
so Stop also interrupts a run whose start response arrives after cancellation.
Formalize all skips current certified proofs even when their derived artifact
index has not caught up. Retry publication resolves staging and canonical path
aliases before comparing imported source inputs, including newly required helpers.

## Why reuse the repair batch

A naive client-side loop firing N `/formalize` calls at once would bypass the
three properties that make the repair batch safe:

1. **Spend-cap pause/resume.** A cap reached mid-batch pauses on the current
   item (resumable via *Continue remaining* once the limit is raised) instead of
   failing it or blowing past the cap.
2. **Dependency ordering.** Formalizing `B` before the lemma `A` it `uses` would
   deny `B`'s run the recorded context of `A`. The batch topologically orders
   items by their `targetUses` so dependencies run first (the repair batch does
   the same over the project *import* graph; here we order over the batch-local
   uses graph, reusing `topologicalRepairOrder` with labels standing in for
   modules).
3. **One run at a time.** Sequential dispatch keeps N concurrent Lea jobs from
   hammering the adapter.

## Server (`apps/overleaf-extension/companion/server.mjs`)

- `POST /stub/all` → `handleStubAll`, `POST /formalize/all` → `handleFormalizeAll`.
  Both take `{ overleafProjectId, items: [<full target payload>] }` — the *full*
  per-target payload (`targetText`, `targetUses`, `sourceHash`, …), unlike repair,
  which addresses already-recorded artifacts by label.
- `startTargetBatch(payload, state, operation)` validates each item
  (`validateTargetPayload`; stub additionally rejects `definition` targets — a
  definition has no stub path), runs the standard preflight trio
  (`syncSharedSettingsFromAdapter` → `validateLeaRuntime` → `spendLimitReached`),
  orders by uses (`orderTargetsByUses`), and kicks off `runTargetBatch`.
- `runTargetBatch` dispatches each item through the existing single-item handler:
  - **stub** → `handleStub` (already blocks to completion). A failure is
    independent — the loop marks it failed and continues.
  - **formalize** → `handleFormalize` (returns as soon as the run starts;
    `awaitJobSettled` polls the job to its terminal `finishedAt`). A failure
    carries dependency semantics: items that transitively *use* the failed one
    are skipped, then the batch pauses for the user's continue/stop decision on
    the independent remainder — exactly the repair batch's behavior.
- The batch record, `repairBatchSnapshot` (now carrying `operation`), pruning,
  and the continue endpoint are shared. `resumeBatch` routes a continued batch to
  `runRepairBatch` or `runTargetBatch` by `operation`.

## Client (`apps/overleaf-extension/extension/`)

- `content.js`: `renderLeanPaneBatchActions` renders the two buttons above the
  tree, each present only when it has eligible work and hidden while a batch panel
  is already showing (one batch surface at a time). `stubAllTheorems` /
  `formalizeAllItems` gather the eligible items, flush the `.tex` mirror, POST the
  full target set, and drive the **same** batch panel + polling the repair batch
  uses. The panel wording is generalized by `operation`.
- `leanPaneView.mjs`: `stubbableItems` / `formalizableItems` select exactly the
  items whose own per-item buttons would offer the action, delegating to the
  existing `canStubPaneItem` / `canFormalizePaneItem` predicates (which honor
  `formalizable` and skip in-progress runs and already-valid work).
  `formatRepairOutcome` gained the `stubbed` / `formalized` / `disproved` states
  and operation-aware verbs.

## Stopping a batch mid-flight

A running batch is interruptible. While it is actively working the progress
panel shows a **Stop** button; clicking it `POST`s to
`/lean-pane/repair/all/cancel` (`handleBatchCancel`, shared by all operations,
including repair), which:

1. sets a `cancelRequested` flag both runners poll between items (they `break`
   at the next iteration boundary), and
2. **interrupts the item currently mid-run** via `interruptApiRun` — every run
   type records `job.apiRunId` at run start through the shared
   `runLeaProofJobForJob` driver, so the running entry's job carries the id the
   adapter needs to abort. This is best-effort: if the interrupt fails the batch
   still stops, just after the current item finishes on its own.

The currently-running item is recorded as `canceled`; any not-yet-started items
become `canceled` too (`finalizeCanceledBatch`), and the batch reads as `done`
so polling stops and the panel is dismissible. An actively-looping batch settles
itself in its own `finally`; a paused/idle one is settled inline by the handler.
The snapshot exposes `canceled` and a transient `stopping` (cancel requested,
not yet settled — the panel shows "Stopping...").

Already-completed items are **not** rolled back — a `sorry`-stub or verified
proof produced before Stop was pressed is kept. Stop halts further work; it does
not undo finished work.

## Eligibility

| Button        | Eligible items                                                                 |
|---------------|--------------------------------------------------------------------------------|
| Stub all      | un-stubbed **theorems** (`missing-stub`, not a `def`, not in progress)          |
| Formalize all | anything **not yet verified**: `missing-stub`, `stub-generated`, `stale`, `invalid`, `unknown`, `error` (theorems and definitions); never a valid/defined/disproved proof or an in-progress run |

## Tests

- `apps/lea-standalone/adapter/tests/test_stub_workflow.py` — immutable source
  admission, the real agent/tool/bridge completion path, named declaration and
  statement checks, single/batch SQL jobs, and registry publication after a new
  process and worker epoch. Only model and compiler responses are scripted.
- `apps/lea-standalone/prover/tests/agent/test_stub_workflow.py` — a fresh checked
  placeholder finishes in stub mode while ordinary proof completion still rejects
  `sorry`.
- `tests/stubWorkflow.test.mjs` — stub-to-formalization reuse, declaration/body
  validation, stale checks, sequential mixed outcomes, cancellation during both
  admission and execution, delayed check observations and their restart recovery,
  and persisted publication retries.
- `apps/lea-standalone/adapter/tests/test_reformalization_cache.py` — bounded
  cancellation polling during dependency scans, compiler-input invalidation,
  and real Lean cache reuse.
- `apps/lea-standalone/adapter/tests/test_reformalization_operations.py` — real
  Lean publication through aliased data paths, importing consumers, and new
  required helper modules while preserving unrelated declarations.
- `tests/leanPaneBatch.test.mjs` — request-validation guards (empty/missing
  items, missing project id, malformed label, definition rejected from stub),
  `orderTargetsByUses` dependency ordering, and cancel behavior (unknown batch
  404; an idle/paused batch settles to `canceled`/`done`; a running batch reports
  `stopping` and defers settlement to its loop).
- `tests/leanPaneView.test.mjs` — `stubbableItems` / `formalizableItems`
  selection and operation-aware `formatRepairOutcome`.
- The shared batch loop (snapshot, pause, continue) is covered by
  `tests/leanPaneRepair.test.mjs`; the per-item run pipeline by
  `tests/companion.test.mjs`.

## Live validation (2026-10-19)

The production SQL/API/companion workflow was exercised in an isolated project
with the configured `gpt-5.6-sol` model and Lean `v4.29.0`. Stub all generated and
checked three theorem stubs: natural-number addition by zero, a dependent theorem
applying that result twice, and a rational square identity. Per-item Formalize
followed by Formalize all completed the proofs, skipped the already certified
target, and produced a ready batch report. The three generated statements were
unchanged, all final target checks were current, and compiler trust evidence
contained no `sorryAx`. A combined Lake build succeeded.
