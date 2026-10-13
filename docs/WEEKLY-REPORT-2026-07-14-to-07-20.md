# Weekly Work Report — Jul 14 – Jul 20, 2026

**Author:** Daniel Arturi (`darturi`)
**Repository:** `lea-ecosystem` monorepo
**Branch:** `gen_repair`
**Branches:** `gen_repair` (features/cleanup) · `one-click-install`,
`phase-2-companion-in-container`, `sandbox-dist`, `sandbox-ci` (distribution)
**Scope:** the legacy status-engine retirement that closed out Phase 4; a new
project-level **batch Stub / Formalize** capability (with mid-flight Stop) built
on the repair batch's machinery; and a **one-click install** strand that
publishes a prebuilt image so users install nothing but Docker.

Last week (Jul 7–13) hardened the Overleaf workflow and stood up a *single
source of truth* for proof state behind a feature flag, dual-writing the new
ledger alongside the old five-source heuristic during a bake period. This week
had three strands. Two were on the `gen_repair` branch: it **collected on that
bake period** — deleting the legacy engine now that the ledger had proven it
agreed — and **spent the resulting simplicity** on a feature users had asked
for: acting on the whole project at once instead of one theorem at a time. The
third, on separate branches, was **distribution** — turning install from a
clone-and-build-Mathlib ordeal into a Docker pull, and dry-running that pipeline
in a sandbox ahead of any public release.

---

## 1. Retiring the legacy status engine (closes Phase 4.3 / 4.4)

The prior report listed "flip the status engine default to `ledger`" as the
last open item of the hardening plan, done in principle on Jul 13 but with the
legacy code still physically present as a fallback. This week that code came
out. In `companion/server.mjs` the following were deleted outright:

- `statusEngine()` and `getTheoremStatusLegacy()` — the old five-source
  status derivation,
- `identifyLeaArtifact()` / `selectLeaArtifactCandidate()` and the
  before/after marker plumbing (`markerKey`, `proofFileTouchedAfter`,
  `beforeMarkers` threaded through `applyProofOutcomeToJob`) — the markdown-diff
  artifact-identification heuristic,
- the constellation of per-source readers (`getCurrentTheoremProofStatus(es)`,
  `getLeaProjectTheoremStatus`, `getLeaDirectProofStatus`,
  `getLatestMappedJobStatus`, `findImportedCurrentlyStubbedTheoremUses`).

`getTheoremStatusLedger` was promoted to the sole `getTheoremStatus`, and
`resolveProofOutcome` / `applyProofOutcomeToJob` lost their `beforeMarkers` and
`artifactError` parameters — identity now comes from the adapter's artifact
index, so the companion no longer needs to guess which file a run touched by
diffing directory listings. Net effect on the single largest source file: a
substantial deletion (~780 lines churned in the snapshot commit, net ~1,000
lines removed across the tree), and `companion.test.mjs` was reworked in step to
drop the legacy-engine cases and pin the ledger path.

**Why this was the right sequencing.** The deletion was deliberately *not* done
at cutover time. The value of the dual-write/diff period was that the ledger had
to be observed agreeing with the legacy engine on live runs before the legacy
engine could be removed. Only after that confidence was banked did the fallback
become dead weight — at which point keeping it was a *liability* (two code paths
that can drift, the exact anti-pattern Phase 4 existed to kill). This is the
"gate the final deletion behind a bake period" discipline from the hardening
plan reaching its natural end.

## 2. Batch Stub-all / Formalize-all

The Lean pane already had per-item **Stub** and **Formalize** buttons. This week
added two project-level launchers above the item tree:

- **Stub all (N)** — generate a Lean `sorry`-stub for every un-stubbed theorem.
- **Formalize all (N)** — run Lea on every item with no verified proof yet
  (unformalized theorems and definitions, existing stubs to complete, and
  broken/stale items to re-formalize), skipping only already-valid work.

Full design is in [`docs/FEATURE-overleaf-batch-stub-formalize.md`](FEATURE-overleaf-batch-stub-formalize.md).
The load-bearing decisions:

**Reuse the repair batch rather than loop on the client.** The obvious
implementation — have the extension fire N `/formalize` calls — was rejected
because it would bypass the three safety properties the existing *batch repair*
machinery already guarantees. So instead the new operations ride the same
in-memory batch record, progress panel, `repair-batch-updated` push event, and
`/lean-pane/repair/all/continue` resume endpoint, distinguished only by a new
`operation` field (`repair | stub | formalize`). The three properties inherited
for free:

1. **Spend-cap pause/resume.** A cap reached mid-batch pauses on the current
   item (resumable via *Continue remaining* once the limit is raised) rather
   than failing it or overrunning the cap.
2. **Dependency ordering.** Formalizing `B` before the lemma `A` it `uses`
   would deny `B` the recorded context of `A`. `orderTargetsByUses` reuses the
   repair path's `topologicalRepairOrder`, with theorem labels standing in for
   modules and the batch-local `targetUses` graph standing in for the project
   import graph, so dependencies run first.
3. **One run at a time.** Sequential dispatch keeps N concurrent Lea jobs from
   hammering the adapter.

**One dispatcher, two graphs.** `resumeBatch` routes a continued batch to
`runRepairBatch` (walks the project *import* graph, addresses already-recorded
artifacts by label) or the new `runTargetBatch` (dispatches *full target
payloads* through the existing single-item `handleStub` / `handleFormalize`).
Keeping a single continue endpoint meant the client's resume/pause UI didn't
have to learn a second protocol.

**Formalize awaits; stub blocks.** `handleStub` already runs to completion, so
the batch takes its result directly. `handleFormalize` returns as soon as the
run *starts* (the driver finishes in the background), so `awaitJobSettled`
polls the job to its terminal `finishedAt` before advancing to the next
dependency-ordered item — with the job's own timeout as the real settler and a
`+60s` deadline as a pure safety net.

**Failure semantics differ by operation, and the code says why.** A failed
*formalization* carries dependency coupling: items that transitively `use` the
failed one can't formalize against it, so they're skipped and the batch pauses
for the user's continue/stop decision on the independent remainder. A failed
*stub* is a pure per-statement translation with no such coupling, so one
failure never blocks the rest — the loop just continues. This asymmetry is
encoded directly (`if (batch.operation === "formalize")`) and commented at the
branch.

**Eligibility delegates to the per-item predicates.** `stubbableItems` /
`formalizableItems` in `leanPaneView.mjs` filter through the *same*
`canStubPaneItem` / `canFormalizePaneItem` used by the individual buttons, so a
batch offers work on exactly the items whose own buttons would — honoring the
`formalizable` flag and skipping in-progress runs and already-valid work. No
second, drifting definition of "what's eligible."

**One batch surface at a time.** The launchers are hidden whenever a batch
panel is already showing (running, paused, or awaiting dismissal), so a second
batch can't clobber the first's progress state.

## 3. Stopping a batch mid-flight

Batches are now interruptible — applied to *all* operations including the
pre-existing repair batch. While a batch is actively working, the progress panel
shows a **Stop** button (`POST /lean-pane/repair/all/cancel` →
`handleBatchCancel`), which:

1. sets a `cancelRequested` flag both runners poll between items (they `break`
   at the next iteration boundary), and
2. **interrupts the item currently mid-run** via `interruptApiRun`. Every run
   type records `job.apiRunId` at start through the shared `runLeaProofJobForJob`
   driver, so the running entry always carries the id the adapter needs to abort.

The interrupt is **best-effort by design**: if it fails, the batch still stops —
just after the current item finishes on its own — rather than wedging.
Bookkeeping is split by liveness: an actively-looping batch settles itself in
its own `finally`; a paused/idle batch (no live loop to observe the flag) is
settled inline by the handler via `finalizeCanceledBatch`. The snapshot exposes
`canceled` and a transient `stopping` (cancel requested, not yet settled) so the
panel can honestly show "Stopping…" then the final stopped state.

**Already-completed work is never rolled back.** A stub or verified proof
produced before Stop was pressed is kept. Stop halts *further* work; it does not
undo *finished* work — the least-surprising contract, and the one consistent
with "git owns proof content" (finished proofs are already committed).

## 4. One-click install: prebuilt-image distribution

A parallel work-stream (Jul 19–20, on branches `one-click-install`,
`phase-2-companion-in-container`, `sandbox-dist`, `sandbox-ci` — **not** on
`gen_repair`) turned the "clone the repo and bake Mathlib locally" install into
"Docker pulls a finished image." Full plan and grounding in
[`docs/PLAN-one-click-install.md`](PLAN-one-click-install.md).

**Grounding first — this is packaging, not architecture.** The plan opens by
confirming from the code that nothing structural blocks a near-zero-step install:
a self-contained Docker image already existed (frontend + adapter + in-process
prover + Lean + Mathlib) but was *never published*; the macOS launcher already
assumed a prebuilt image that didn't exist; and the companion, though it can't
run on a bare machine, is a perfect fit for that same image because everything it
shells out to (`lake env lean`) and reads (proof files on disk) is already
baked in. So all of the work below is distribution, not redesign.

**Phase 1 — publish the image (branch `one-click-install`).**
- `.github/workflows/publish-image.yml` builds and publishes a multi-arch image
  to GHCR. Key decisions: **native per-arch builds** (amd64 on `ubuntu-latest`,
  arm64 on `ubuntu-24.04-arm`), *no QEMU* — Lean under emulation is unusably
  slow — merged into one manifest; **GHCR over Docker Hub** to dodge rate limits
  and account friction; release tags publish `:<version>` + `:latest` while a
  `workflow_dispatch` from a branch publishes `:<branch>` so the pipeline can be
  tested without touching `:latest`.
- `docker-compose.yml` now *pulls* `ghcr.io/darturi/leaui:latest` (no `build:`);
  a new `docker-compose.dev.yml` overlay keeps the local-source build for
  developers — so the user path and the dev path stop competing for one file.
- The launcher/README/CLAUDE wording that already *claimed* a prebuilt image was
  corrected to match reality.

**Phase 2 — one container, both front ends (branch `phase-2-companion-in-container`).**
The Overleaf companion is bundled into the same image, so `docker compose up`
becomes the entire backend for *both* front ends — pulling the harder-to-install
Overleaf audience onto the easy path. A follow-up fix bundles `extension/` too,
because the companion's `shared/` imports `targetParserCore.mjs`; docs across
README/CLAUDE/Overleaf were corrected to "Docker covers both front ends."

**Sandbox-first distribution (branches `sandbox-dist`, `sandbox-ci`).** The
notable process decision: the pipeline was **dry-run against a sandbox remote
before touching the real `:latest`** — the workflow derives its image name from
the repository owner, so a sandbox fork publishes under its own namespace
automatically. `SANDBOX_INSTALL.md` (a Docker-only, three-step preview guide) and
a self-serve README were added, and CI was wired to publish on push to the
sandbox `main`. This de-risks the first genuinely *outward-facing* step of the
project before it points at the canonical namespace.

**Recorded caveat.** The compose file hardcodes `ghcr.io/darturi/...`; the plan
explicitly flags that this one string (and later the launcher/release URLs) must
move if the VIDA-NYU repo becomes canonical — noted so the coupling stays
deliberate rather than a surprise at cutover.

## 5. Documentation

- `docs/WEEKLY-REPORT-2026-07-07-to-07-13.md` — the prior week's report, added
  to the repo (per the docs-in-repo convention).
- `docs/PLAN-one-click-install.md` — the plan + code-grounding behind the
  prebuilt-image install effort in §4, with a running status log.
- `docs/FEATURE-overleaf-batch-stub-formalize.md` — the design record for the
  batch feature in §2–3.
- `SANDBOX_INSTALL.md` — the Docker-only preview install guide (§4).

---

## Status of the work

The week's work is spread across several branches, none yet merged to `main`:

- **Legacy engine retirement:** committed on `gen_repair`
  (snapshot `21873be`, "WIP snapshot: in-progress gen_repair work").
- **Batch Stub/Formalize + Stop:** implemented and tested in the `gen_repair`
  working tree, staged but not yet committed.
- **One-click install, Phase 1:** on `one-click-install` (`e3795ec`, `d92c1b7`);
  plan status recorded as "sandbox pipeline dry-run green."
- **One-click install, Phase 2:** on `phase-2-companion-in-container`
  (three commits through `6e0baf6`) — companion bundled into the image.
- **Sandbox distribution:** on `sandbox-dist` / `sandbox-ci`, dry-running the
  publish pipeline against a sandbox remote before any canonical release.

The branch name `gen_repair` reflects the unifying idea of that strand:
*generation* (stub / formalize) and *repair* are now the same batch mechanism,
differing only by `operation`. The install strand is independent packaging work
that can merge on its own track.

## Test posture

- `tests/leanPaneBatch.test.mjs` (new) — request-validation guards
  (empty/missing items, missing project id, malformed label, definition
  rejected from Stub), `orderTargetsByUses` dependency ordering, and cancel
  behavior (unknown batch → 404; an idle/paused batch settles to
  `canceled`/`done`; a running batch reports `stopping` and defers settlement to
  its loop).
- `tests/leanPaneView.test.mjs` (extended) — `stubbableItems` /
  `formalizableItems` selection and operation-aware `formatRepairOutcome`.
- The shared batch loop (snapshot, pause, continue) remains covered by
  `tests/leanPaneRepair.test.mjs`; the per-item run pipeline by
  `tests/companion.test.mjs`, which was reworked alongside the legacy-engine
  deletion.

The two changed/added test files (`leanPaneBatch` + `leanPaneView`) pass 55/55.
The reworked `companion.test.mjs` reaches 124+ subtests with zero failures but
then stalls on a pre-existing open-handle hang in that file's harness (present
before this week's work) — the full `node --test tests/*.test.mjs` run should be
confirmed green, and that harness hang chased down, before the batch feature is
committed and merged.

---

## What's next

The remaining hardening phases from the prior report are unchanged and still
pending: **Phase 5** (drift-surface consolidation — model catalog, status
vocabulary, settings), **Phase 6** (structural sweep — `server.mjs` and
`content.js` are still well over target size; this week's legacy deletion made a
meaningful dent in `server.mjs` but the split into unit-testable modules is
still to come), and **Phase 7** (the pairing token, the explicit gate before any
external beta). The immediate next step for *this* branch is committing the
batch feature and merging `gen_repair` to `main`.
