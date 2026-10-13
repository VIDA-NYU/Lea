# Implementation Plan: Faithful Overleaf Formalization and Dual Lean/Lea Checks

**Planned replacement:** Follow the [Live Lea Status implementation plan](PLAN-overleaf-live-lea-status.md) for new work under the [Live Lea Status feature specification](FEATURE-overleaf-live-lea-status.md). The implementation record below describes the existing evaluator-based flow; its replacement is not yet implemented.

**Status:** V1 pilot core implemented; calibration and release gates pending.

**Date:** 2026-09-12

**Code inspected:** `6b09094`, plus the working-tree feature specification.

**Specification:** [Faithful Overleaf Formalization and Dual Lean/Lea Checks](FEATURE-overleaf-faithful-formalization-and-lea-check.md)

## 0. Implementation record

The pilot core in phases 1–7 is implemented as of 2026-09-12. The implementation
preserves the existing extension → companion → adapter → in-process prover
architecture and adds no service boundary.

### Delivered

- Source proof association and version-2 bundles live in
  `shared/proofSource.mjs`, with a browser-safe extraction core and current-buffer
  acquisition in the extension.
- `runs.purpose`, structured stop/recovery metadata, and the dedicated
  `overleaf_faithful` prompt isolate Overleaf translation from LeaChat.
- `alignment_checks` persistence, snapshot capture, typed report validation,
  automatic scheduling, current/history/retry APIs, restart recovery, and separate
  usage accounting live in the adapter's `alignment_*` modules and migration 0020.
- The companion retains source provenance across formalize, edit, repair, and chat
  paths; reconciles current reports by exact source/artifact/dependency identity;
  and treats capped work as paused/resumable.
- The Lean pane renders both status dimensions and a collapsible report for every
  terminal verdict, including approved caveats and evaluator errors.

### V1 simplifications from the proposed internal design

- Snapshot, logical-check, and attempt data are normalized into one append-only
  `alignment_checks` table per attempt instead of four new tables. Each row retains
  the immutable source bundle and artifact/dependency manifest needed to reproduce
  its judgment; retry rows link through `retry_of` and increment `attempt`.
- The evaluator calls the configured provider directly with an empty tool list
  instead of creating an ordinary solver run. Its model/provider/tokens/cost remain
  separately recorded and are included once in aggregate usage.
- The first pass relies on the faithful solver prompt and the independent evaluator
  to expose silent repairs. A typed `report_formalization_finding` solver event is
  still an explicit follow-up before claiming the strongest form of criterion 7.
- Full report history is exposed through the adapter API. The first card UI focuses
  on the current revision-bound report; a dedicated history/evidence navigator is
  deferred.

### Verification completed

- Adapter: 761 tests passed.
- Overleaf extension/companion: 498 tests passed.
- Cross-app integration: 6 tests passed.
- Standalone frontend: 60 tests passed; TypeScript typecheck passed.
- Prover prompt, agent-loop, gate, event, namespace, and subagent-prompt contract
  suites passed.

### Release work not executed automatically

Phase 8's live-model calibration corpus and the manual browser/Overleaf scenarios
remain pending because they require provider spend, a configured live project, and
mathematician review. Continuous checkpoint evaluation and report-guided solver
re-formalization remain post-pilot features, as specified.

## 1. Delivery approach

Implement the feature as a sequence of independently reviewable changes: source
capture and contracts, durable evaluation records, correct pause handling, faithful
solver behavior, a separate evaluator, lifecycle integration, and the two-check UI.
Keep each new behavior behind adapter capabilities and feature settings until its
dependencies are ready.

The pilot must demonstrate two end-to-end cases:

1. A Lean-accepted proof that materially differs from the supplied LaTeX argument
   displays `Lean Check: checked`, `Lea Check: warning`, and a report citing both.
2. A solver that reaches its turn or cost limit retains its partial artifact,
   displays `Lean Check: paused`, provides an assessment when budget permits, and
   resumes from that artifact in the same solver session.

Compiler validity, attempt lifecycle, semantic assessment, source freshness, and
human approval must remain distinguishable in the underlying data. The two public
status labels are projections of those facts; they must not become mutable session
status columns.

All runtime work stays inside the existing extension → companion `:31245` → adapter
`:8001` flow. The adapter invokes the vendored prover in-process. This plan adds no
external service.

### Milestones and dependencies

| Phase | Deliverable | Depends on | Completion evidence |
|---|---|---|---|
| 0 | Contracts and baseline characterization | None | Agreed fixtures and compatibility rules |
| 1 | Complete source bundles and proof association | 0 | Parser and revision tests |
| 2 | Evaluation persistence and immutable snapshots | 0, source contract from 1 | Migration, snapshot, and idempotency tests |
| 3 | Pause/resume and normalized run purposes | 0, purpose migration from 2 | Cap, resume, and artifact-restoration tests |
| 4 | Faithful solver prompt, stop contract, and findings | 1–3 | Prompt and agent-loop contract tests |
| 5 | Read-only evaluator and report validation | 2–4 | Deterministic evaluator integration tests |
| 6 | Automatic scheduling, freshness, recovery, and API | 1–5 | Lifecycle and concurrency tests |
| 7 | Dual status UI and report experience | 3, 6 | UI behavior and accessibility verification |
| 8 | Calibration, compatibility rollout, and release | 0–7 | Reviewed corpus and end-to-end pilot |

Phases 1 and the storage portion of 2 can proceed independently after phase 0.
UI helpers can be developed against fixed response fixtures before phase 6, but the
visible feature should not ship until persistence and terminal behavior work.
These are development dependencies, not instructions to launch agents or jobs.

## 2. Current implementation and consequences

Symbols below were verified in the current source. New modules and routes mentioned
later are proposed names, not existing APIs.

| Current implementation | Consequence for this feature |
|---|---|
| `extension/targetParserCore.mjs` parses marked statement environments and metadata. | Add proof association to the shared parser instead of extracting proofs with a separate UI regex. |
| `shared/theoremParser.mjs` fingerprints statement, kind, `uses`, and `context` with a version-1 hash. | Introduce a version-2 source contract without pretending old jobs contain proof provenance. |
| `content.js:buildFormalizationSourceContext` supplies a bounded excerpt and mirror information. | The full proof must be a separate input; excerpt truncation cannot silently truncate the proof. |
| Adapter `routes/projects.py:mirror_overleaf_tex` writes the mirror synchronously but schedules its git commit afterward. | A mirror acknowledgement is not an immutable source revision. Snapshot capture must establish one. |
| `routes/runs.py:RunRequest` exposes `autonomous`; `bridge.py:run_lea` maps it to the default prompt. | Add an explicit, persisted purpose independent of tool approval policy. |
| `prover/lea/prompt.py` contains shared hard rules and the default/interactive prompts. | Do not append a faithful-translation instruction to conflicting general proof-search rules. Compose a dedicated policy. |
| `prover/lea/agent.py:run_events` requires a proof or verified final artifact for noninteractive completion. | Both informative solver failure and report-only evaluation require explicit stopping behavior in code. Prompt changes alone will loop. |
| The agent emits `Finished("max_turns", ...)` for both turn and per-run cost exhaustion. | Add structured stop reasons; avoid inferring cost exhaustion from English summaries. |
| `leaApiClient.mjs:runApiProofJob` observes SSE and reconciles dropped streams against the run row. | Extend live and reconstructed terminal payloads together. A disconnected stream is not evidence that a solver stopped. |
| `server.mjs:resolveProofOutcome` handles stubs before generic failures; several cap paths restore artifacts early. | Normalize pauses before stub/failure mapping and route every exit through a finalization hook. |
| `runPostRunCascade`, `finishChatRunCascade`, edit-save, and repair completion already exist. | Add one reusable evaluation notification after those operations settle. Do not duplicate cascades. |
| `jobStore.mjs` selects active jobs by `in_progress` and retains terminal artifacts from an explicit status set. | Add pause bookkeeping deliberately so partial attempts survive pruning and remain resumable. |
| `formalizationApproval.mjs` hashes transitive project-local imports. | Reuse the dependency traversal concept, while resolving bytes from a frozen snapshot. |
| Companion `eventBus.mjs` publishes small invalidations; `eventsClient.mjs` refetches state. | Send check invalidations through the existing channel instead of creating one browser stream per checker. |
| The dispatcher uses per-session admission and can stop a busy session's incumbent run. | Background evaluators must not enter the solver session and accidentally supersede it. |
| Latest-run and transcript queries feed formalization validity, SafeVerify, and chat replay. | Keep evaluator run ownership separate and include its cost without making it the solver's latest conversational turn. |

### Storage discrepancy to resolve explicitly

The feature specification and repository `AGENTS.md` describe git-owned proof bytes
and SQLite metadata. The inspected code has since introduced `timeline` and
`artifact_blobs`: migration `0003_timeline_and_blobs`, the store's code-row helpers,
and `formalizations.py:decorate` read persisted blob content. `db.py:init_db` now
delegates schema changes to Alembic under `adapter/migrations/versions/`.

Implementation must preserve the working storage paths and the instruction against
adding a new proof-content store. Do not resurrect the old `code_steps` schema,
remove blobs, rewrite existing history, or copy entire Lean files into report JSON.
New evaluations store immutable revision references and metadata. Use the existing
artifact read interface and capture a git revision for evaluated bytes where the
specification requires one. Phase 0 documents this discrepancy in a short storage
decision; a broader storage migration is outside this feature.

## 3. Proposed resolutions for ambiguous specification details

These are implementation defaults to make the plan actionable. They are proposals,
not previously approved product decisions. Record them with the contracts in phase
0; any different choice must update its corresponding fixtures before coding.

### 3.1 Public state semantics

- Keep exactly the six Lean and six Lea values specified in the feature document.
- `N/A` means no current assessment exists. Include a machine-readable reason such
  as `no_artifact`, `untouched_stub`, `not_evaluated`, `superseded`, or `disabled`.
  This resolves the specification's use of N/A both for absent artifacts and for
  existing artifacts awaiting their first current assessment.
- Queueing uses public `in-progress` plus `phase: queued`; it does not introduce a
  seventh status. An evaluator in the queue affects only Lea Check.
- `error` is a failed artifact/checking operation, not a promise that recovery is
  impossible. Authentication and compiler errors can be corrected; turn/cost stops
  still have their own `paused` presentation.
- A partial-artifact report may receive `approved`, as the specification permits,
  only with the visible qualifier “approach so far” and a nonempty list of remaining
  obligations. It does not claim the source proof has been fully verified.
- No source proof for a theorem results in a statement assessment plus a warning
  about missing method evidence. The solver may translate the statement and prepare
  a stub, then pause with `source_proof_required`; it must not invent a method.
- A solver that identifies a source gap and stops faithfully uses
  `source_revision_required`, with the issue visible and a conditional resume
  action. Ordinary failure to find a Lean tactic is not proof that the mathematics
  is wrong.
- Display `checked` only from applicable compiler evidence for the selected
  artifact. Preserve the existing conspicuous warning for stubbed dependencies;
  report evaluation cannot grant dependency completeness or human approval.

### 3.2 Spending and stopping

- Automatic evaluation consumes the same account/project spending controls as the
  solver. Exhausting a global cap must never launch an extra paid evaluator.
- If an eligible check cannot start because the cumulative cap is reached, persist
  an evaluation with `paused` and `budget_unavailable`, plus a system-generated
  partial/failure envelope. It has no semantic verdict yet. Offer Retry Lea Check
  after budget is available.
- Use the solver's recorded model/provider for V1; do not silently select a different
  model when settings change while evaluation is queued.
- Proposed pilot evaluator limits: six model turns, a ten-minute execution timeout,
  and a $1 per-attempt budget, also bounded by the remaining cumulative cap. These
  values require calibration in phase 8 and remain adapter configuration, not a new
  UI settings project. Queue wait does not consume execution timeout.
- Allow one schema-correction response inside those limits. Do not reserve an
  unlimited extra response after exhaustion. In-flight provider cost can exceed an
  estimate; account for it honestly rather than promising a precise billing cutoff.
- User Stop ends the requested operation and prevents launching new automatic work
  from that stop. Show an eligible assessment as paused with an explicit Retry
  action. Cap-driven pauses otherwise trigger an evaluator when budget permits.
  This narrows the automatic-pause rule to respect an explicit stop request.

### 3.3 Source syntax and scope

Proposed explicit association, placed inside a normal proof environment:

```tex
\begin{proof}
% lea: proof-for=main_result
The author's argument goes here.
\end{proof}
```

This uses the Lea marker label, not the LaTeX `\label`. It adds no typeset content
and requires no change to `lea-tags.sty`. Recognize it only as proof-association
metadata; it must not create another formalization target.

Support adjacent `proof` and `proof*` environments when association is unique.
Explicit associations can cross files. Multiple explicit proofs for one target are
ambiguous in V1 and require selecting one, rather than concatenating alternative
arguments. Do not infer an association across an intervening statement environment,
whether or not that environment is Lea-marked. Respect section boundaries too.

Custom proof environments may use the explicit marker when their bounds can be
parsed reliably. Inline `leacode` targets have no natural-language method to compare
unless an explicit source proof is associated; flag this limitation instead of
silently treating Lean code as the paper's prose argument.

### 3.4 UI and rollout defaults

Keep reports collapsed initially, with a prominent warning count and summary.
Provide a history control on every completed report. Retain historical reports until
the existing project/session deletion policy removes their owner; do not add an
independent short retention period in V1.

Use a deployment-level evaluator mode `off | shadow | visible`. Shadow mode is
limited to an explicitly selected development/pilot cohort and still records usage.
Defer per-project disable controls and a separate checker-model picker. Define a
separate faithful-solver flag so prompt rollout and evaluator rollback are independent.

## 4. Contracts and proposed module ownership

Paths in this document are repository-relative unless stated otherwise. New modules
should keep HTTP wiring small and make core behavior testable without a live model.

| Proposed module | Responsibility |
|---|---|
| `apps/overleaf-extension/extension/proofSource.mjs` | Browser-safe proof association and source bundle construction using the existing parser's environment/masking rules |
| `apps/overleaf-extension/shared/checkState.mjs` | Pure projection of compiler/attempt/evaluation facts into public statuses and legacy compatibility values |
| `apps/overleaf-extension/companion/alignmentChecks.mjs` | Adapter client orchestration, settled-target notifications, and manifest enrichment |
| `apps/overleaf-extension/extension/leaCheckView.mjs` | Pure report view models and action eligibility |
| `apps/lea-standalone/adapter/app/alignment_schemas.py` | Validated request, finding, snapshot manifest, and report types |
| `apps/lea-standalone/adapter/app/alignment_store.py` | Transactions, history, idempotency, attempts, and report persistence |
| `apps/lea-standalone/adapter/app/alignment_snapshots.py` | Immutable source/artifact/dependency capture and evidence resolution |
| `apps/lea-standalone/adapter/app/alignment_checks.py` | Eligibility, scheduling, completion, and freshness reconciliation |
| `apps/lea-standalone/adapter/app/alignment_runner.py` | Separate evaluator driver using prover/provider interfaces in-process |
| `apps/lea-standalone/adapter/app/routes/alignment_checks.py` | Read/start/retry/history API |
| `apps/lea-standalone/prover/lea/overleaf_tools.py` | Scoped structured solver findings and report submission; no adapter imports |

### Source bundle

Define versioned types with `targetKey`, formalization identity, full statement,
full associated proof, association method/status, source file/ranges, `uses`,
`context`, relevant referenced source, mirror availability, and truncation flags.

Store separate hashes for input identity and snapshot bytes. V1 should normalize
line endings only for mathematical input; broad whitespace collapse is unsafe for
LaTeX comments, verbatim content, and macro arguments. Retain the existing version-1
hash for legacy freshness compatibility. Verify a new version-2 hash with shared
golden vectors in JavaScript and Python.

The bundle hash covers every source fragment supplied to the evaluator, including
relevant definitions and dependency statements. A proof-body-only LaTeX edit must
invalidate both formalization-source freshness and Lea Check.

### Revision identity and evidence

Use a snapshot ID whose manifest contains:

- source bundle hash, durable source revision, and exact source file hashes;
- target declaration and file path;
- Lean artifact content hash and immutable git revision;
- sorted project-local dependency paths and content hashes;
- recorded Lean toolchain and Mathlib revision;
- solver findings available at the checkpoint;
- extraction version and evaluator prompt/rubric version.

The git revision is the revision from which the exact evaluated file can be read,
not “whatever HEAD is now.” Unrelated commits in a shared project must not make a
current report stale. Compare relevant file content and source hashes, preserving
the evaluated commit as provenance. Changed bytes that later change back do not
silently resurrect a previously superseded approval; track the invalidation event.

A file/range evidence reference belongs to its snapshot. Resolve it there first;
only offer navigation into the current source when the referenced file still
matches. Hashes supplied by a model or browser are claims to verify, not authority.

### Evaluation and attempt records

Use one logical `alignment_checks` record for a snapshot and evaluation configuration,
with append-only `alignment_check_attempts` for execution and retries.

| Record | Main fields and constraints |
|---|---|
| `alignment_snapshots` | ID, target/formalization owner, manifest, source/artifact/dependency hashes, durable revision references, creation time |
| `alignment_checks` | ID, owner, snapshot ID, prompt/rubric/model configuration hash, trigger, source solver run/edit ID, explicit supersession metadata |
| `alignment_check_attempts` | ID, check ID, attempt number, evaluator run ID, queued/running/paused/completed/failed state, reason code, timestamps, report JSON/schema version, previous-attempt ID |
| `formalization_findings` | ID, solver run ID, formalization ID, source revision, event ordinal/tool-call ID, structured issue, action taken, optional code-step reference |
| `alignment_requests` | Durable settled-target notification, expected revision/checkpoint, pending/processed state, created time; unique trigger identity |

Add purpose and structured stop-reason metadata to ordinary `runs`. Evaluation
attempts link to ordinary run usage records; do not count the same tokens once on
the run and again on the check.

Use foreign keys, constrained enums, stable ordering, and uniqueness for
`(check_id, attempt_number)` and repeated trigger/tool-call IDs. Enforce one active
attempt per logical check transactionally. Index current/history lookup by owner,
snapshot, and creation order. Resolve completed automatic duplicates to the existing
report. An explicit Retry creates a new attempt; ordinary refresh never does.

Completed attempt reports are immutable. Supersession is separate metadata; a
historical report can retain its original verdict while no longer being current.
Use system-generated error reports when no valid model report exists. A paused
attempt keeps a partial report envelope with no final verdict.

### APIs and events

Proposed adapter routes:

| Operation | Route | Contract |
|---|---|---|
| Publish current source | `PUT /api/formalizations/{id}/alignment-source` | Validate source bundle, resolve owner, return authoritative source revision |
| Enqueue/check current snapshot | `POST /api/formalizations/{id}/alignment-checks` | Expected revision + trigger/idempotency token; return existing check or accepted queued check |
| Read current assessment | `GET /api/formalizations/{id}/alignment-checks/current` | Always distinguish no current assessment from a failed lookup |
| Paginated history | `GET /api/formalizations/{id}/alignment-checks` | Current and superseded checks with summary data only |
| Read an attempt report | `GET /api/alignment-checks/{id}/attempts/{attemptId}` | Immutable report and snapshot metadata |
| Read bounded evidence | `GET /api/alignment-checks/{id}/evidence` | Snapshot-bound file/range; never arbitrary filesystem access |
| Retry/resume evaluator | `POST /api/alignment-checks/{id}/attempts` | Budget/source checks and new attempt, optionally continuing retained evaluator context |
| Pause evaluator | Existing run interrupt endpoint | Stop only the selected evaluator run |

Use existing companion POST conventions for matching `/lean-pane/lea-check/*`
wrappers. The browser does not need adapter credentials. Return 409 for a source or
artifact mismatch and refetch the current target; do not reassign an old report.
Budget refusal records paused evaluation state. Provider/configuration failures
record an error when a check was actually requested. An adapter transport failure
leaves cached evidence labeled as last known and does not fabricate an assessment.

Persist before emitting the specification's `alignment_check_*` and finding events.
Bridge detailed evaluator events through existing run brokers. Companion publishes
one `alignment-check-updated` invalidation type with project/target/check identifiers.
Reconnect performs a current-state read; duplicate or missed events cannot create a
new paid check. Manifests contain summaries, while report bodies load on expansion.

## 5. Phase-by-phase work breakdown

### Phase 0 — Characterize behavior and freeze contracts

**Changes**

- [ ] Add small fixture sets for current job outcomes, current manifest mappings,
  run purposes, and report states. Capture existing interactive/default prompt
  assembly outputs under controlled configuration.
- [ ] Audit call sites that use `status`, `inProgress`, `resultKind`, latest run,
  latest transcript, artifact retirement/restoration, and active-session queries.
- [ ] Record the storage discrepancy from section 2 and the source/report contract
  from section 4. New schema changes belong in Alembic, not `init_db` SQL.
- [ ] Finalize the defaults in section 3 and translate the conceptual report JSON
  into real typed enums; pipe-separated sample strings are not executable schemas.
- [ ] Specify status projection precedence for active, paused, stubbed, checked,
  invalid, restored, and not-yet-checked artifacts.

**Exit gate:** No implementation relies on parsing terminal prose, a mutable HEAD,
or one undifferentiated “latest run.” All public states have fixture examples.

### Phase 1 — Capture and retain the author's proof

**Files:** parser core, new `proofSource.mjs`, `shared/theoremParser.mjs`,
`shared/leanPaneManifest.mjs`, `content.js`, companion source normalization/sync.

- [ ] Reuse environment scanning, opaque-span masking, and balanced parsing.
  Recognize `% lea: proof-for=...` without treating it as a target activation.
- [ ] Implement explicit-first association over the project inventory, followed by
  conservative adjacency within one file. Report duplicate target labels,
  unresolved references, competing proofs, and unbalanced proof environments.
- [ ] Pass the same complete bundle through formalize, stub, batch formalize,
  re-formalize, chat mutation, and repair paths. Preserve complete proof text even
  when the contextual excerpt reaches its existing limit.
- [ ] Refresh the active editor buffer before extraction and validate the mirror
  revision. When mirroring is disabled, freeze supplied source bytes explicitly and
  do not use cached mirror text. Never use PDF layout to guess source boundaries.
- [ ] Add `proofAssociation`, `sourceProofStatus`, location ranges, versioned hashes,
  and explicit unavailable/truncated flags to source synchronization.
- [ ] Keep an independent statement-only legacy hash. A legacy job without proof
  provenance has unknown alignment freshness, not an inferred current assessment.
- [ ] Include relevant referenced definitions and LaTeX dependency statements in
  freshness computation; unrelated project files need not invalidate all reports.
- [ ] Validate explicit target `context` as translation guidance, while treating
  arbitrary text in the paper as data. Guidance that requests changing the author's
  method must surface a conflict rather than quietly override fidelity policy.

**Verification:** Shared browser/Node parser fixtures for adjacency, cross-file
association, comments/verbatim, standalone tags, leacode, definitions, ambiguous
proofs, and proof-only edits. Hash golden vectors include comments and line endings.

**Exit gate:** Solver and evaluator can retrieve the exact same source bundle;
neither depends on a truncated excerpt to reconstruct a long proof.

### Phase 2 — Add durable records and freeze snapshots

**Files:** next Alembic revision under `adapter/migrations/versions/`, new alignment
schemas/store/snapshots, existing GitStore/store read interfaces, backup/deletion
integration.

- [ ] Add the records and indexes in section 4 and `runs.purpose`/stop metadata.
  Resolve the current Alembic head at implementation time; do not assume a numeric
  filename is still available.
- [ ] Migrate existing runs to an explicit legacy purpose. No historical run or
  artifact receives a synthetic semantic approval.
- [ ] Implement validated create/get/history/claim/settle operations. Use atomic
  insert-or-select for deduplication and compare-and-set on attempt settlement.
- [ ] Freeze source bundles in the existing git-backed project infrastructure,
  using scoped paths under `.lea/alignment/` for source/manifest data if needed.
  The evaluator itself never writes these files; snapshot creation is adapter work.
  Lean bytes are referenced through existing artifact revisions, not copied into
  new report fields.
- [ ] Resolve pending mirror commits before issuing a source revision. Commit only
  named snapshot/artifact paths; do not sweep unrelated project edits into HEAD.
- [ ] Capture artifact and project-local imports consistently. Under the existing
  repository lock, resolve committed paths and hashes, or compare before/after
  hashes and retry a bounded number of times if active writers changed them.
  Session admission alone does not serialize two sessions sharing a project.
- [ ] If SQL holds the latest artifact before git has the required revision, use a
  focused existing write/commit seam to establish matching provenance. Never use a
  stale git version merely to fill the commit field.
- [ ] Pin revision references for retained reports so normal housekeeping cannot
  prune their evidence. If evidence is already missing, retain the report with an
  explicit unavailable-evidence error; never substitute current source.
- [ ] Integrate foreign-key deletion order and backup/restore. Preserve history
  across companion restart and avoid changing the user's existing artifact storage.

**Verification:** Fresh/upgrade/repeated-start migrations; duplicate concurrent
requests; unrelated HEAD movement; changed dependency; mirror acknowledgement before
commit; shared-project write race; missing historical evidence; rollback on failure.

**Exit gate:** A report's exact inputs can be reread after a restart even after the
working source and Lean files change.

### Phase 3 — Normalize run purposes, pause reasons, and resume

**Files:** adapter run route, bridge, store, registry; prover config/events/agent;
companion API client, outcome/finalization helpers, job store, batch runners.

- [ ] Add server-owned purposes: legacy, chat, Overleaf formalization, Overleaf
  repair, Overleaf chat, and alignment evaluation. Keep omitted-purpose behavior
  compatible with current callers. Select prompt/tool policy per run; never infer
  it only from a session's historical `origin: overleaf`.
- [ ] Carry purpose through queued dispatch, restart recovery, model snapshots, and
  run-detail responses. Keep `autonomous` independent of purpose.
- [ ] Add structured stop codes: `max_turns`, `max_cost`, `max_spend`,
  `user_interrupt`, `timeout`, `source_proof_required`, `source_revision_required`,
  `compiler_error`, `provider_error`, and `process_error`, with stage/recoverability.
  Distinguish per-run cost from turns at the prover branch that currently conflates
  them. Preserve old terminal status projections for existing LeaChat consumers.
- [ ] Update `_done_payload`, bridge live completion, terminal broker reconstruction,
  API client stream parsing, and run-row fallback to expose identical stop data.
- [ ] For the new Overleaf policies, suppress the existing paid summarization call
  after a cost cap unless budget explicitly remains. Use persisted findings and a
  deterministic pause message instead. Include final result classification and
  schema repair in cost accounting.
- [ ] Classify cap stops before generic failed/stub outcome branches. Keep the last
  compiler diagnostic as evidence while the primary state is paused.
- [ ] Reconcile an interrupt or timeout with the adapter's settled run before
  offering Resume or evaluating. While stop acknowledgement is pending, show
  `in-progress` with stopping detail; never resume alongside a still-billing run.
- [ ] Add `paused` to artifact/job retention where applicable and preserve the
  partial artifact's source provenance. Audit `findLatestArtifactJob`, pruning,
  session resolution, and all status-specific selectors.
- [ ] Add Resume formalization to the companion: confirm current source revision,
  preflight available budget, atomically prevent duplicate dispatch, then start a
  new run in the same session with the latest saved artifact and transcript.
- [ ] Resume must bypass fresh re-formalization's retire/cleanup behavior. When the
  source changed, show the new source context and require the source-aware
  re-formalization path rather than replaying an obsolete mathematical contract.
- [ ] Update stub/formalize/repair batch runners to distinguish paused item from
  failed item. Pause a global-cap queue; do not create failures for untouched
  pending items or continue dispatching against the same exhausted limit.

**Artifact restoration rule:** A paused partial attempt must remain accessible for
resume. If the published artifact is restored to keep dependents building, preserve
the attempt checkpoint separately in existing version history, expose both
references, and label its report “paused attempt.” The card's current published
artifact keeps its actual compiler result. Do not attach the partial report to the
restored file or erase the attempt. Test this exception to simple status precedence
explicitly; an older verified artifact cannot be mistaken for newly completed work.

**Verification:** Turn/per-run/global cost pauses; pre-start budget rejection; final
check and cap in the same turn; timeout acknowledgement; stream reconnect; stub plus
cap; restored prior proof plus partial attempt; batch pause/resume; double Resume.

**Exit gate:** A recoverable stop survives refresh and resumes saved work without
starting two writers or showing a generic compiler error.

### Phase 4 — Enforce faithful solver behavior and record findings

**Files:** prover prompt/config/agent/events/interface/registry, new Overleaf tools;
adapter bridge and finding store; companion `buildLeaPrompt` and chat/repair builders.

- [ ] Compose `overleaf_formalization` from applicable workspace/import/tool/soundness
  rules. Do not inherit instructions to change strategy after repeated failures,
  try unrelated tactics first, or stop solely because compilation succeeded.
- [ ] Leave `BASE_PROMPT`, `INTERACTIVE_PROMPT`, and legacy prompt assembly behavior
  unchanged. Check implicit `lea.md`, configured skills, role heads, and injected
  tool hints so they cannot reintroduce contradictory Overleaf strategy guidance.
- [ ] Ask the solver to identify source steps and build a corresponding Lean
  skeleton, with concise comments or named intermediate claims useful as evidence.
  Routine tactic choices remain flexible within the same mathematical argument.
- [ ] Add scoped `report_formalization_finding` with validated category, severity,
  explanation, source evidence, action taken, and optional Lean reference.
  Persist on typed events as they occur, with tool-call deduplication. Do not wait
  for final prose to expose a source issue.
- [ ] Add an explicit structured stop/submission path for “cannot continue faithfully,”
  carrying remaining obligations and source findings. Teach the agent loop to accept
  it without the existing “produce a verified artifact” nudge.
- [ ] A partial checkpoint may retain existing sketch placeholders, but cannot
  become a successful final proof. A new completed proof still passes the normal
  compiler/final-artifact guards. Do not weaken guards globally to make the new
  stopping path work.
- [ ] Attach findings to the source/run/revision where observed. If Lean code later
  changes, do not move a previously recorded finding to the newest lines.
- [ ] Apply the faithful policy to Overleaf formalize/re-formalize/repair actions.
  Keep free-form Overleaf chat as conversational interaction; its mutations still
  invalidate and trigger checks. Opening the same session in LeaChat uses LeaChat's
  normal prompt because purpose is assigned per request.
- [ ] Include a required final finding-disposition summary even when there were no
  material issues. The evaluator compares the source independently; a solver's
  empty findings list is not evidence that no silent repair occurred.

**Verification:** Fake model requests unrelated strategy; source gap causes a clean
pause with finding; no artifact required to stop informatively; successful Lean
artifact still checked; finding persists across crash; default/interactive prompt
and final-gate regression tests.

**Exit gate:** The solver can terminate faithfully with actionable evidence instead
of being forced by the loop to keep searching for an unrelated proof.

### Phase 5 — Implement the independent evaluator

**Files:** new adapter runner and schemas; dedicated prover evaluation prompt,
report submission tool, report-only completion mode; admission and usage seams.

- [ ] Use a separate evaluator execution session linked to the originating solver
  session/formalization. Mark it as an internal evaluation session so it does not
  appear as a duplicate mathematical task. Its ordinary run participates in global
  capacity, usage, and cancellation controls.
- [ ] Dispatch to `alignment_runner` before solver-specific bridge setup. Never run
  artifact promotion, retirement, collation, registration, or solver transcript
  replay for an evaluator. Do not compose mutable project memory or `lea.md` into
  the assessment as controlling instructions.
- [ ] Give the evaluator a fresh message history containing only the frozen bundle,
  selected evidence, and solver findings. Retain its own transcript for checker
  retry; solver continuation must still select the solver transcript.
- [ ] Assemble a fixed allowlist: read snapshot evidence, record partial finding,
  submit report. Exclude shell, write/edit, custom HTTP/MCP tools, project updates,
  and spawning tools. Snapshot lookup validates paths and never follows references
  to the mutable workspace. If a future checker needs compilation, add an isolated
  snapshot check explicitly; V1 consumes existing compiler evidence.
- [ ] Use the in-process provider/tool machinery with an explicit report-only
  completion mode. A schema-valid report submission ends evaluation; do not invoke
  the prover's artifact-required gate or proof/disproof result classifier.
- [ ] Validate all rubric dimensions, scope, severity, evidence bounds, categories,
  and verdict consistency. The adapter fills revision/model/time fields from the
  execution context; the evaluator does not get to choose them.
- [ ] Enforce at most one schema repair within budget. If correction fails, persist
  an error envelope with partial findings and a concrete retry recommendation.
- [ ] Persist the report and settled attempt atomically, then emit completion.
  Snapshot mismatch can make the result historical but cannot relabel its inputs.
- [ ] At failure or cancellation, persist observed token/cost usage and partial
  findings. Never emit `proved` or overwrite the solver's compiler result.
- [ ] Audit session listing, latest run, SafeVerify, usage summaries, deletion, and
  export filters for internal evaluator sessions. Account for evaluation once in
  project/Overleaf/global totals while exposing a separate phase breakdown.

**Verification:** No-write capability tests; forged tool calls; source instruction
injection; report-only completion; invalid verdict/category/range; one repair;
same-model snapshot; provider failure; usage retained; solver transcript and
SafeVerify state unchanged by a completed evaluator.

**Exit gate:** A deterministic fake-provider run produces a durable report without
any change to proof files or solver history.

### Phase 6 — Wire triggers, freshness, restart recovery, and API

**Files:** alignment service/routes, dispatcher and startup wiring, companion
alignment module/API client/event bus, formalization manifest enrichment.

- [ ] Add a reusable `notifyTargetSettled` adapter request and companion wrapper.
  Call it after final artifact selection, required verification, restoration, and
  applicable cascade settle. Use an operation/checkpoint ID for idempotency.
- [ ] Wire formalize, re-formalize, recoverable pause, failed attempts with an
  evaluable artifact, manual edit, chat mutation, and repair completion. Explicit
  stub creation and unchanged conversational answers do not trigger paid checks.
- [ ] Do not put the chat hook only in `finishChatRunCascade`: that function can
  return early without a pre-run snapshot. A first chat-created artifact still
  needs assessment. Compare settled artifact evidence independently.
- [ ] Move cap/error early returns through a common finalization path so notification
  is not skipped. Failure to request evaluation must not rewrite a successful
  compiler result into a failed solver run.
- [ ] Persist evaluation requests durably. Adapter recovery reconciles eligible
  terminal Overleaf runs; companion recovery reconciles pending edit/cascade
  notifications. Both converge on the same idempotency key after a crash.
- [ ] Reconcile source updates and project-local dependency changes even when no
  compiler cascade was required. Proof-only edits can change method fidelity while
  leaving Lean dependents valid. Mark affected reports superseded immediately;
  defer automatic reassessment until an eligible settled trigger.
- [ ] Before starting a queued check, verify that its snapshot is still relevant.
  Supersede obsolete queued work without model spend. If an active old check
  finishes, retain its historical report without making it current.
- [ ] Use the shared registry with a checker concurrency limit of one in the pilot.
  Admit background evaluation only when no runnable solver is waiting. Do not
  supersede a solver to free its slot; queued state remains visible if capacity is
  occupied. Avoid making the batch wait synchronously for each evaluator report.
- [ ] Honor cost admission rules: an eligible but budget-blocked request becomes
  paused. Repeated manifest reads do not retry it. User interruption suppresses
  automatic follow-on calls for that operation.
- [ ] On adapter restart, requeue never-started checks; mark interrupted active
  attempts paused if a usable snapshot exists, otherwise error. Never label an
  orphaned active check approved or repeatedly restart paid work without a limit.
- [ ] Add read/start/history/retry/evidence routes and ownership checks. Resume a
  checker through a new attempt preserving its snapshot; if source changed, offer
  a new current check instead of promoting a stale retry.
- [ ] Add manifest `leanCheck`/`leaCheck` blocks and report summaries. Preserve
  legacy `status`, `resultKind`, `sourceFreshness`, stub warnings, and breakage
  attribution while clients migrate. Read paths remain side-effect free.
- [ ] Turn `needs_review` into a displayed solver/repair finding while the independent
  Lea Check runs. Do not fabricate a completed evaluator warning from legacy prose.
  Preserve the original terminal result in history.

**Verification:** Every trigger; no-artifact/untouched-stub exclusions; busy target;
two tabs; duplicate notifications; source/dependency races; restart at each boundary;
budget admission; obsolete queue; explicit retry; first chat artifact; failed retry
restoration; no duplicate provider dispatch from polls or SSE reconnect.

**Exit gate:** Automatic assessment is durable and exactly attributed across normal
completion, pauses, concurrent changes, and service restarts.

### Phase 7 — Render dual checks, reports, and actions

**Files:** `content.js`, `leanPaneView.mjs`, new report view helpers, CSS,
`eventsClient.mjs`, extension manifest, blueprint and progress helpers as needed.

- [ ] Render both labeled chips on every card and in the in-document popover.
  Use shared projection helpers so cards, overlays, actions, and file summaries
  agree. Preserve a separate result-kind label for definitions/counterexamples.
- [ ] Add the collapsible report with assessment scope/confidence, matches,
  warnings, solver findings/actions, caveats, evidence, recommendation, and technical
  details. Show reports for approved/error/paused outcomes as well as warnings.
- [ ] Show recorded solver findings immediately even before the final evaluator
  report exists. Every issue entry identifies whether it came from the solver,
  evaluator, or system. “Repaired a source gap” must be visible in plain language.
- [ ] Render provisional partial findings as partial. Display “approved — approach
  so far” for partial-artifact approval and keep missing obligations prominent.
- [ ] Load report details lazily, keyed by evaluation and attempt ID. Preserve
  expansion/focus across refresh; discard late fetch results for a different target.
- [ ] Distinguish current published artifact reports from paused-attempt reports and
  superseded historical reports. Evidence navigation opens the evaluated snapshot
  when current source lines have moved.
- [ ] Implement Resume formalization and Retry/Resume Lea Check independently, with
  budget-aware disabled states and a settings link where a cap blocks progress.
  Suppress double clicks while the request is outstanding.
- [ ] Extend companion invalidation events and the browser allowlist. Refresh while
  either solver or checker is active; no forced whole-project report download and
  no extra EventSource per item.
- [ ] Update action gates, batch labels, file progress, relationships, and blueprint
  status consumers. Compiler progress remains compiler progress; a semantic warning
  must not make a checked dependency appear unavailable for import. Add separate
  semantic counts where summarized, without advertising full approval from a single
  success count.
- [ ] Keep human approval browser-local and separately labeled. Lea approval never
  toggles that checkmark; changing proof-source identity must participate in its
  existing revision invalidation mechanism.
- [ ] Add accessible toggle state, keyboard navigation, descriptive evidence links,
  text labels, and polite nonrepeating status announcements. Render reports via
  safe text/Markdown helpers without raw HTML from model output.
- [ ] Register new browser modules in `manifest.json` where required by the current
  lazy import pattern; verify the packaged extension loads them after reload.

**Verification:** Fixtures for all twelve status values and meaningful pairs;
approved-with-caveats; warning; failure; pause; old report after new edits; both resume
actions; multi-tab refresh; budget settings; keyboard and screen-reader semantics;
code excerpts containing HTML/script-like text.

**Exit gate:** A user can inspect both judgments, understand any reported repair,
and resume the correct operation without opening raw traces.

### Phase 8 — Calibrate and release

- [ ] Add a small reviewed evaluation corpus and a runner that records expected
  findings, observed verdict, evidence quality, latency, tokens, cost, prompt
  version, and model. Keep live-model calibration out of deterministic CI.
- [ ] Cover every corpus category in the feature spec, including complete faithful
  proofs expressed with different tactics, unrelated valid methods, source gaps,
  quantifier changes, incomplete artifacts, missing proof evidence, definitions,
  counterexamples, and changed local dependencies.
- [ ] Review with a mathematician before visible pilot release. Proposed gate:
  zero false approvals on seeded material errors in the reviewed pilot corpus;
  every material finding points to real snapshot evidence; no checker infrastructure
  failure is presented as a mathematical mismatch. Record sample size and observed
  false-warning rate rather than claiming a universal accuracy percentage.
- [ ] Run the manual scenarios below in an isolated test data directory. Record
  actual results and limitations; no tests against user session history.
- [ ] Roll out adapter capability support first, then companion, then extension.
  New extension + old adapter renders Lea `N/A` with “check unavailable”; old
  extension + new adapter receives legacy status projections. Never infer approval
  from unsupported fields.
- [ ] Enable shadow evaluation for the selected cohort, then visible pilot, then
  default-on after review. Measure checker failures/cost and adjust proposed budgets.
- [ ] Keep schema/report history during rollback. Disable new dispatch and new
  solver policy independently; settle active runs through ordinary cancellation.
  Do not downgrade or delete report tables as an emergency rollback action.
- [ ] Update feature/plan status with actual implementation evidence and unresolved
  limitations. Retire legacy semantic UI chips only after all consumers migrate.

**Exit gate:** All deterministic tests pass, the reviewed corpus meets the declared
pilot gate, and the feature-spec acceptance checklist has evidence for every item.

## 6. Verification commands and manual scenarios

These are commands for implementation verification; they were not run merely to
write this plan. Use focused suites during each phase and the broader set once at
integration. The root test command does not include adapter Python tests.

From the repository root:

```sh
npm test -w apps/overleaf-extension
npm run test:frontend -w apps/lea-standalone
npm run typecheck -w apps/lea-standalone
npm run test:integration
```

From `apps/lea-standalone/adapter`:

```sh
./.venv/bin/python -m pytest
```

From `apps/lea-standalone/prover`, using its provisioned environment:

```sh
uv run python -m tests.agent.test_run_events
uv run python -m tests.agent.test_gate
uv run python -m tests.events.test_events
uv run python -m tests.prompt.test_import_policy
uv run python -m tests.prompt.test_namespace
uv run python -m tests.subagents.test_subagent_prompt_composition
```

Add new report/Overleaf policy test modules in the same test styles and include their
commands in the implementation's validation record. Read the vendored package's
current guardrails before changing the runtime.

### Required manual pilot scenarios

1. **Faithful success:** A source proof and corresponding complete Lean proof yield
   checked/approved, with evidence and an inspectable caveat section.
2. **Unrelated success:** A deliberately different valid method yields
   checked/warning; inspection shows exactly what changed.
3. **Source gap:** A false intermediate step causes a visible solver finding and
   faithful pause. A historical artifact that repaired it receives a warning.
4. **Turn cap:** Pause mid-proof, refresh, inspect the partial report, then resume
   from the same checkpoint without losing edits or resetting the source contract.
5. **Global cost cap:** Both solver and pending evaluator respect the cap. Raise the
   cap explicitly, retry the checker, and resume the solver as separate actions.
6. **Edit during checking:** Change the LaTeX proof while assessment runs; the old
   report appears only in history. Repeat with a Lean edit and a dependency change.
7. **Restore and resume:** A failed/capped retry coexists with a restored published
   artifact; the UI identifies which artifact each report assessed.
8. **Mutation entry points:** Repeat on manual edit, first chat-generated artifact,
   re-formalize, repair, and batch formalization.
9. **Recovery:** Restart companion and adapter at queued, running, and
   report-persisted-before-event boundaries; no duplicate paid check appears.
10. **LeaChat regression:** Run a normal interactive task, including tool approval
    and continuation, and confirm its prompt, transcript, and status behavior match
    the baseline. Evaluation usage remains visible in project totals.

## 7. Suggested review/commit sequence

| Change | Review focus |
|---|---|
| 1. Contract fixtures and source proof association | Correct source capture and compatibility hashes |
| 2. Alembic records, source snapshots, evidence reads | Provenance, concurrency, migration safety |
| 3. Run purposes and structured stop reasons | Same-session behavior and legacy defaults |
| 4. Pause/resume across companion and batches | Preserved partial work and correct cap presentation |
| 5. Faithful solver prompt, findings, and stopping path | No loop-level pressure toward unrelated success |
| 6. Evaluator runner and report validator | Read-only execution, valid report termination, usage isolation |
| 7. Trigger/reconciliation API and event wiring | Idempotency, freshness, restart behavior |
| 8. Dual status cards, reports, and all status consumers | User understanding, evidence navigation, accessible controls |
| 9. Calibration fixtures and rollout documentation | Measured pilot quality and compatibility |

Do not enable the faithful solver before its structured stop path exists, or the
evaluator before it has its own report completion mode. Those are functional
dependencies, not optional refactors.

## 8. Acceptance-criteria traceability

Numbers refer to the feature specification's numbered acceptance criteria.

| Spec criteria | Implementation phase | Required evidence |
|---|---|---|
| 1–4: dual labels and independent states | 0, 6, 7 | Shared contract fixtures and checked/warning UI scenario |
| 5: caps pause and resume | 3, 7 | Turn/global/per-run cap and resume tests |
| 6: dedicated prompt, unchanged LeaChat | 3–5, 8 | Prompt baselines and interactive regression scenario |
| 7: issue/repair disclosure | 4, 5, 7 | Persisted solver finding plus independent silent-repair corpus case |
| 8: automatic idempotent checks | 6 | Every settled trigger, duplicate request, and restart test |
| 9: read-only evaluator | 5 | Tool allowlist, immutable input, and no-workspace-mutation tests |
| 10–12: reports, evidence, caveats | 5, 7 | Schema contracts and all terminal report fixtures |
| 13: absent/ambiguous proof evidence | 1, 4, 5 | Association diagnostics and no-unqualified-approval test |
| 14–15: exact revisions and supersession | 1, 2, 6 | Source/Lean/dependency changes and late completion tests |
| 16: storage and derived status constraints | 0, 2 | Additive migration and documented existing-storage reconciliation |
| 17: definition/disproof result kinds | 0, 5, 7 | Distinct result-kind tests and evaluator corpus cases |
| 18: existing Overleaf features | 3, 6–8 | Batch, human-approval, cascade, repair, and source-freshness regression |
| 19: evaluator failure preserves Lean result | 5–7 | Provider/schema/cap failures on a checked artifact |
| 20: reload persistence | 2, 6, 7 | Restart and multi-tab reconciliation scenarios |

The “no silent repair” requirement is a product obligation, not something a prompt
or tool can guarantee for every model response. Implementation evidence must include
both structured disclosures and independent evaluator tests against known
undisclosed repairs. Do not equate “the tool exists” with satisfying that criterion.

## 9. Deferred work and extension points

After the pilot, report-guided re-formalization can reuse check/attempt IDs, snapshot
evidence, and finding categories. It should create a new solver run with an explicit
selected report, preserve previous outputs, and receive a new independent assessment.
V1 must not start that loop automatically.

Continuous checking should reuse snapshot/check deduplication and the existing queue,
adding a checkpoint trigger with debounce and a cost policy. It must avoid redundant
assessment of every write and keep terminal assessments distinguishable from interim
ones. Interval controls, multi-model adjudication, and project-wide semantic reports
remain outside this implementation sequence.

## 10. Planning completion checklist

- [x] Read the feature specification and relevant current implementation.
- [x] Identify prompt-loop, pause, source-capture, persistence, and scheduler gaps.
- [x] Define proposed contracts, phase dependencies, and reviewable work units.
- [x] Map implementation work to every feature acceptance criterion.
- [x] Implement the V1 pilot core from phases 0–7, with the simplifications recorded above.
- [x] Run deterministic implementation and cross-app integration tests.
- [ ] Add the typed solver-finding channel and dedicated report-history/evidence UI.
- [ ] Run the reviewed live-model calibration corpus and manual Overleaf scenarios.
- [ ] Complete the pilot and record release evidence.
