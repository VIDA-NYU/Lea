# Plan: Reformalization That Preserves Existing Work

> Status: implemented — 2026-10-15. All four delivery slices are present.
> The delivery record below identifies the tested runtime and verification limits.

Read [FEATURE-shared-file-reformalization.md](./FEATURE-shared-file-reformalization.md)
for the preservation boundary, edit permissions, and acceptance criteria.

## Delivery strategy

Deliver four ordered slices: target/scope correctness; serialized staged execution;
recoverable application and helper publication; client/review integration and release.
Keep the shared-file refusal until the complete path is safe. Removing the refusal
or adjusting prompts alone is not a releasable intermediate result.

The first release uses one foreground retry owner per project. This deliberately
serializes even retries in separate files. Queued retries capture fresh baselines
when they start, so no automatic same-file merge or semantic rebase is required.
Independent projects remain parallel. Managed dependency workers share the project
execution slot and run serially after their waiting parent releases that slot. Reuse run
IDs, checkpoint lineage, content blobs, dependency ownership, and existing review
outcomes instead of introducing another general job or transaction framework.

Isolate explicit reformalization operations, including one-target retries, under
one contract. Do not migrate every chat/first formalization to a new runtime.
Existing canonical writers only need to honor foreground ownership and the short
application reservation. General concurrent retry merging, per-declaration lock
expansion, and broad historical private-helper extraction are deferred.

## Current code anchors and required changes

| Existing anchor | Required treatment |
| --- | --- |
| [companion/server.mjs](../apps/overleaf-extension/companion/server.mjs) | The shared-file refusal precedes retry and resume setup. Replace it only for the negotiated safe operation. Keep actual retirement/deletion guards. |
| [companion/retryContext.mjs](../apps/overleaf-extension/companion/retryContext.mjs) | Reuse same-session history, current source, manual names, and previous diagnostics; expose retained drafts explicitly. |
| [prover/lea/agent.py](../apps/lea-standalone/prover/lea/agent.py) | `ProofVerificationState` and the final gate use whole-file `sorry` output; final classification uses `safeverify.theorem_signature` without an explicit selected result. Both must change. |
| [target_completion.py](../apps/lea-standalone/adapter/app/target_completion.py), [formalizations.py](../apps/lea-standalone/adapter/app/formalizations.py), [formalization_reviews.py](../apps/lea-standalone/adapter/app/formalization_reviews.py) | Align target selection and transitive trust across completion/validity/review. Existing review also has whole-file shortcuts. Draft review is an explicit exception to draft exclusion from canonical projections. |
| [artifacts.py](../apps/lea-standalone/adapter/app/artifacts.py), [project_library.py](../apps/lea-standalone/adapter/app/project_library.py), [LibraryInspect.lean](../apps/lea-standalone/prover/lea/LibraryInspect.lean) | Lexical spans are not safe editing boundaries. Reuse compiler names/types/bodies/axioms; extend command/dependency evidence only where preservation requires it. |
| [project_dependencies.py](../apps/lea-standalone/adapter/app/project_dependencies.py), `publish_owned` | It validates a canonical path today. A staged inline helper needs explicit candidate selection, checked host application, and independent ownership settlement. |
| [bridge.py](../apps/lea-standalone/adapter/app/bridge.py), [proof_evidence.py](../apps/lea-standalone/adapter/app/proof_evidence.py), [artifact_snapshots.py](../apps/lea-standalone/adapter/app/artifact_snapshots.py) | Separate attempt paths/evidence from canonical effects, including child promotion and Lea Status snapshots. Actual canonical checks remain required. |
| [project_coordination.py](../apps/lea-standalone/adapter/app/project_coordination.py), [proof_files.py](../apps/lea-standalone/adapter/app/proof_files.py) | Existing guards are useful, but mutation counters are not exclusive application locks and do not exclude external editors. |
| [prover/lea/tools.py](../apps/lea-standalone/prover/lea/tools.py), [check_evidence.py](../apps/lea-standalone/prover/lea/check_evidence.py), [runctx.py](../apps/lea-standalone/prover/lea/runctx.py) | File-tool confinement and `cwd` do not confine compiler metaprograms/build hooks. Route tool and compiler execution through one filesystem-restricted executor. |
| [gitstore.py](../apps/lea-standalone/adapter/app/gitstore.py) | `commit_all(paths=...)` limits staging but commits unrelated already-staged files too. Add an exact-change-set commit primitive preserving unrelated index entries. |
| [companion/cascadeVerify.mjs](../apps/overleaf-extension/companion/cascadeVerify.mjs) | Reuse checked impact results; avoid duplicate cascades or unrequested paid repairs. |

## Shared contracts

The contracts below are implemented by migration `0036_reformalization_operations` and the existing run, review, and artifact APIs.

### Admission, queue, and accounting

Extend existing `POST /api/runs` admission with `artifact_operation: "reformalize"`.
Reuse focus/source/session/purpose/idempotency fields. The adapter derives project,
current target, edit policy, and ownership. Older Overleaf solver requests focused
on existing Lean work must be routed safely or rejected without retirement; omission
of the field is not a preservation bypass.

Persist a lightweight retry operation before dispatch. Its `root_run_id` is empty
until execution starts; queued work must not create a normal `pending` run that
existing active-run queries could mistake for an executing solver. Return the
operation reference, `run_id: null`, and queue activity without a provider call.
When ownership is granted, atomically admit the normal root run and link its
continuation lineage. Older clients that cannot represent queue activity receive
the existing busy/update response rather than a fabricated running job.
At actual start, resolve fresh source
from the normal prepared-source/mirror flow, then capture current bytes. Do not run
a stale click-time statement, silently retarget a changed identity, or count queue
waiting against active solver time/turns/cost. Repeated clicks for the same active
operation attach instead of adding paid work. Resolve timeout/deadline fields at
execution start and carry the existing allowance across continuation segments.

Only one foreground retry owns a project's existing mathematics at a time. Wait
for prior canonical writers to settle before capture; subsequent canonical math
writes queue or return their normal busy result. Source synchronization may continue,
but a changed source binding invalidates application. A durable owner is scheduling
metadata, not a held mutex or SQLite transaction. Keep it during dependency waiting;
workers use their dependency reservations and share a serialized execution slot,
without queuing behind the waiting logical owner.
Release it after a settled pause, review wait, cancellation, or completion. Resume
and reviewed application reacquire it through the queue and validate their baselines.

Advertise `capabilities.reformalization = {version: 1, staging: true,
serialization: "project", automatic_shared_files: true}` in existing `/api/health`
only after the release gate passes on that runtime. Older adapters get an explicit
update requirement, not an unsafe fallback or a file-splitting instruction.

### Minimal operation metadata and draft applicability

Reuse existing run/recovery states for execution, pause, and dependency waiting.
Operation ownership/application states are `queued`, `active`, `retained`,
`paused`, `review`, `applying`, and `settled`. Managed dependency suboperations use
`worker`; they do not become a second foreground owner. A retained operation records why: partial work, compatible
pause, correspondence review, conflict, or failure. Requeue resume/approval against
the same operation lineage. Existing mathematical outcomes remain `proved`,
`defined`, `disproved`, `needs_review`, and the existing stop/failure kinds.

A versioned manifest references existing immutable blobs and records:

- project/formalization/root-run/source identities and operation policy;
- exact current file bytes/existence/modes, including relevant untracked edits;
- file/target associations, namespace, local imports, packages/toolchain;
- editable target/helpers, protected commands/content, and any relocation plan;
- explicit selected result declaration/path/kind and its evidence;
- workspace handle, helper subpublications, dependency revisions, and checkpoints;
- application outcome and any source-required downstream impact.

Git HEAD is provenance, not the baseline. Persist draft writes/checks with an
indexed `artifact_scope = attempt` and the operation ID; existing rows remain
`canonical`. Draft rows stay draft history. Application appends canonical code
steps referencing accepted content and actual canonical evidence.

Current artifact, validity, library, export, and current-canvas readers exclude
drafts. Attempt inspection, Lea Status artifact capture, and correspondence review
can select a draft explicitly by operation ID. A newer failed/reviewable draft does
not replace the prior artifact's validity. Source staleness remains independent.

### Edit policy and validation receipt

Store a host-derived scope, not a client-provided list of declarations it may delete.
Include necessary existing unfinished target-only helpers when ownership is known.
For a shared unfinished helper, reserve its fixed mathematical contract through the
existing dependency service; preserve its identity and check its consumers. Protect
completed shared results and unrelated/uncertain content. Manual target edits are
editable starting work with retained before-images, not immutable proof text.

Record one of two application policies:

| Policy | Required evidence and behavior |
| --- | --- |
| `proof_update` | Preserve the target interface and protected mathematics. Previously passing affected consumers must remain passing. |
| `source_update` | Cite the exact changed source contract and old/new Lean contract. Verify the new result, retain dependent source/history, and record actual broken/stale consumers needing repair. A mere source-hash change or solver-selected representation change does not authorize this policy. |

In both modes reject unrelated semantic changes, deleted work, added trust
assumptions, and failures without verified attribution. New target/result modules
must elaborate. A compatibility/legacy consumer may fail only for a recorded
source-update impact or an unchanged pre-existing failure; do not certify it.
If a dependent shares the result module, extract the result safely before accepting
completion. If attribution or safe isolation cannot be established, retain the
candidate and explain the limitation rather than silently weakening a claim.

A receipt references before/after blobs, protected text/relocations, compiler
contract comparisons where needed, actual canonical checks, source bindings,
helper publications, and consumer impact. It can justify carrying A's completion
lineage across B's same-file update when A is preserved and rechecked. It cannot
fabricate human correspondence/audit approval or copy stale SafeVerify evidence.

### Selected results, reviews, and disproofs

Use explicit formalization ID, qualified result declaration, result kind, path,
source revision, and content/environment identities through the prover final gate,
classifier, adapter, review, and SafeVerify. Never infer the result from the first
theorem or last written file. Proof/definition results match the selected target;
a disproof may name a different checked declaration associated with that same target.

A checked draft with `needs_review` is eligible for its own target's correspondence
review. Extend review selection with operation ID and attempt scope, leaving
canonical validity separate. Bind approval to the exact candidate/result kind,
source, blobs, environment, and relevant canonical baseline. Approval queues an
`apply_reviewed_candidate` operation without a new solver/model call. Revalidate,
check actual canonical inputs, and publish approval lineage only if the bindings
remain valid. Changed inputs make the review stale; withdrawal before application
cancels it. Approval never waives preservation/trust checks. Existing post-application
withdrawal policy does not rewrite files or run history.

## PR1 — Explicit edit scope and target-aware completion

**Primary anchors:** `artifacts.py`, `target_completion.py`, `formalizations.py`,
`formalization_reviews.py`, `project_library.py`, `prover/lea/LibraryInspect.lean`,
`prover/lea/agent.py`, `prover/lea/interface.py`, and `prover/lea/safeverify.py`.

- [x] Define the scope/policy/selected-result contracts above and conservative
  discovery of existing target-only helpers. File links/namespaces alone cannot
  establish ownership. Preserve manual target progress/history and protect other
  content, including untagged or unchecked declarations.
- [x] Use compiler command/source ranges or conservative unique edit anchors.
  Treat sections, namespaces, variables, attributes, notation, instances, and
  mutual blocks with their real affected scope; do not edit via next-declaration
  lexical spans. If the baseline has a syntax error inside the target, preserve
  protected text/context and validate the complete result rather than demanding
  an impossible baseline build.
- [x] Extend compiler inspection only for missing dependency/command evidence.
  Compare complete types, definition bodies, visibility, and axiom closure when
  shared context or location changes. Retain private helpers where moving them
  would change identity/visibility. Keep completed shared mathematics protected.
- [x] Change `ProofVerificationState`, final-check enforcement/messages, result
  classification input, and checkpointed verification state to select the actual
  target/result. Whole-file `sorry` output must not force a solver loop for an
  independent completed B, nor should checking helper H certify B.
- [x] Pass the actual selected result signature to `_classify_final_result` and
  selected declaration to SafeVerify. Preserve proof/disproof distinctions and
  unavailable/error/rejected audit outcomes. CLI/unfocused cases retain a
  conservative fallback when no unique result can be established.
- [x] Align adapter completion/validity and review eligibility with selected-result
  transitive trust: independent sibling `sorry` is allowed; target dependence on
  `sorryAx`/unsupported axioms is not. Review must not retain whole-file shortcuts
  that reject an otherwise eligible independent candidate.
- [x] Implement `proof_update` versus evidence-backed `source_update` impact
  validation. Do not require a source-required new target to keep old consumer
  proofs valid; preserve those proofs and report their actual repair needs.
- [x] Define scoped relocation manifests: exact paths, collision checks, FQNs,
  visibility, compatibility imports, and all current artifact/file associations.
  Historical paths stay historical; no helper-only artificial work items.

**Tests:** B beside completed A and unrelated unfinished C; B transitively using C;
wrong-first theorem and last-written helper; target-selected disproof; Unicode and
ambiguous/private names; target-owned unfinished helpers; mutual/context changes;
manual progress; source-required signature/body changes versus unjustified proof
update breakage; safe extraction and import cycles; stale checkpoint verifier state.

**Gate:** a compiling destructive candidate fails preservation, while the actual
independent target can pass the prover gate, adapter, review, and audit selection.
No production admission change yet.

## PR2 — Serialized attempts, durable drafts, and execution protection

**Primary anchors:** `routes/runs.py`, `store.py`, `bridge.py`, `recovery.py`,
`project_coordination.py`, `proof_evidence.py`, `artifact_snapshots.py`,
`read_projections.py`, `prover/lea/tools.py`, `runctx.py`, and `check_evidence.py`.
Use a small orchestration/workspace module as needed; do not require a service or
module per state transition.

- [x] Add the next Alembic migration for queue/operation metadata, draft applicability,
  and a journal referencing existing blobs. Preserve old row defaults. Reuse atomic
  admission/idempotency and run-lineage recovery rather than another general scheduler.
- [x] Implement one foreground retry owner per project and enqueue without paid
  dispatch. Refresh prepared source/current bytes when ownership is granted. Include
  absent paths and metadata associations, and verify copied snapshots coherently.
- [x] Make canonical math writers honor the owner and handle existing active writers
  before capture. Use a documented dependency-suboperation exception; test that a
  waiting parent cannot deadlock its own worker. Do not hold a mutex/DB transaction
  for the duration of ownership. Preserve queue order and cancellation on restart.
- [x] Materialize only the required effective Lake source/context closure, with
  identical module names and its own writable build outputs. Reuse immutable or
  copy-on-write dependency material without writable canonical/cache links.
- [x] Establish an actual filesystem-restricted executor on each supported runtime
  before advertising capability. Put compiler/Lake subprocesses, metaprograms,
  module initialization/build hooks, shell/file tools, and children inside that
  boundary. Allow only attempt sources/temp/build outputs to be written; canonical
  source and other project trees are read-only or unavailable for mutation.
- [x] Run final canonical checks through the same restricted execution boundary with
  canonical sources readable but not writable and only required build outputs
  writable. Only the adapter's application routine may change canonical sources.
  Specify a supported-runtime capability matrix and verify OS-enforced policy;
  path filters or shell removal alone do not count as implementation.
- [x] Keep confined shell and existing proof tooling when supported; do not invent
  a replacement tool suite as a prerequisite. Exclude tools that cannot obey the
  execution boundary, except typed host dependency operations. Fail preparation
  before a model call if the selected runtime cannot enforce it.
- [x] Map old canonical paths to attempt paths in active execution without rewriting
  stored transcripts. Never allow path fallback to mutate canonical work. Route
  child promotion, file/check events, and snapshots into attempt-scoped history.
- [x] Keep canonical artifact finalization and current projections separate. Use pure
  draft inspection/checks, not project catalog refresh with a draft repo override
  under the canonical project ID. Expose explicit attempt reads and own-target
  draft review; no broad exclusion that makes review impossible.
- [x] Retain draft blobs independently of temporary directories, plus source/check
  context and usable progress for later retries. Compatible resumes restore the
  same operation; changed baselines retain it as stale/conflicting without automatic
  merging or a new billed run. Paused/review waits release foreground ownership.

**Tests:** two same-file retries execute serially and the second sees the first's
settled result; distinct-project concurrency; dirty/untracked bytes; queue source
refresh, cancellation and accounting; managed-worker bypass; draft/current/read/review
separation; checkpoint restore; symlink/absolute-path escapes; shell parity; a Lean
`#eval`, initialization/build-hook, or child attempt to write a canonical sentinel
is denied in both draft and final-check execution. Keep these fixtures local.

**Gate:** target execution changes only the attempt workspace and retained drafts
remain readable. Only separately accepted helper subpublications can change
canonical work before the target settles. Execution protection is demonstrated;
no solver merge engine or broad ordinary-chat runtime migration is required.

## PR3 — Guarded application, helper subpublications, and recovery

**Primary anchors:** `project_coordination.py`, `proof_files.py`, `proof_evidence.py`,
`gitstore.py`, `store.py`, `project_dependencies.py`, `project_library.py`,
`routes/projects.py`, and `main.py`.

Use a narrowly scoped application journal. Required states are `prepared`,
`applying`, `published`, `restoring`, `conflict`; record per-path progress and the
Git/evidence/mapping identities. Do not add a distributed transaction framework.

- [x] Revalidate exact source/files/existence/dependency/environment/namespace and
  associations before applying. Unexpected divergence retains a conflict rather
  than invoking automatic rebase or another solver. Known helper publications
  update the parent baseline only through their checked receipts.
- [x] Acquire environment coordination before the short project application gate,
  then deterministic file/repository guards and short DB transactions. Include
  cooperating saves/runs/imports/dependencies/migrations/reviews and authoritative
  snapshot/export readers. Hold no DB write transaction during filesystem/compiler
  work. Read-only current views can stay available outside the application interval.
- [x] Rebuild changed imports and actual result modules, then required consumers,
  against fresh outputs. Record ordinary no-regression or source-update impact;
  unavailable required checks retain an unaccepted candidate. Reuse input-identical
  evidence where justified, never relabel draft evidence as canonical.

Apply each target or independently publishable helper operation in this order:

1. Persist before/after blobs, exact path/mapping operations, selected result,
   policy/impact, and journal identity before changing canonical source.
2. With the application reservation, revalidate and archive each existing destination
   by same-filesystem atomic rename to an operation-owned displaced path, rather
   than truncating it after a hash check. Inspect the displaced version against
   expected input and retain its content/provenance. Install via a no-replace
   operation for the now-vacant destination; if an external writer occupies it,
   retain that file and enter conflict. Adds are create-if-absent; deletions retain
   their displaced version. Validate the concrete platform primitive in tests.
3. Run actual canonical result/consumer checks in the restricted executor, compare
   protected scope and final input identities, and hold the real evidence pending
   acceptance. An affected consumer allowed to fail under `source_update` remains
   explicitly failing/stale; it is not part of a fabricated successful check set.
4. Commit only the exact operation paths using an index-safe Git primitive. Preserve
   unrelated staged/unstaged work. Include the journal ID in the commit so recovery
   can locate it after a crash before its journal update. No-content changes need
   fresh assessment/evidence but no redundant content commit.
5. In one short DB transaction append canonical history/check links, update current
   mappings and completion or reviewed outcome, persist preservation/impact receipts,
   invalidate affected stale evidence, and mark the journal `published`. Then release
   the reservation and emit existing change notifications.

**External-editor boundary:** the adapter gate excludes participating writers, not
arbitrary processes. Archiving and no-replace installation close the simple
check-then-overwrite gap and retain observed displaced content, but cannot capture
every intermediate write or prevent writes through previously opened descriptors.
Keep displaced versions until durably retained; do not describe this as universal
filesystem compare-and-swap. If a known external edit makes validation/restoration
uncertain, keep all observed versions and withhold current acceptance for affected
paths. No routine user approval or file organization step is added.

- [x] Add a staged-candidate input to helper publication instead of calling
  `publish_owned` against a not-yet-written canonical path. Validate the exact
  reserved declaration/type/body/axiom policy in the attempt, apply through this
  host routine, then mark the existing helper reservation globally ready from
  actual canonical evidence. Never mark it ready from a stage check alone.
- [x] For inline helpers, publish only the helper closure. A same-file helper-only
  candidate starts from canonical bytes with the target still unchanged. If it
  cannot build independently, retain owner-local checked progress and finish via
  safe extraction or a joint checked target/helper application; do not wait on the
  same run to produce a globally ready copy of its own unpublished helper. Other
  consumers cannot reuse that owner-local draft. Record each helper's own identity
  when the joint operation settles.
- [x] Adopt ownership of an existing unfinished shared helper only with its pinned
  contract and existing path/identity resolved. Do not create a duplicate declaration
  or another helper registry. Ordinary target-only glue needs no new formalization.
- [x] After independent helper publication, refresh the parent's relevant inputs,
  recheck affected draft work, and retain its target edits. This is controlled input
  refresh, not merging competing retry results. Helpers stay published if the parent
  later fails; cancellation must preserve workers needed by other consumers.
- [x] On failure restore only operation-owned, nondivergent paths using guarded
  no-replace/archival operations and retained before-images. Never reset a branch or
  overwrite a known later version. A Git-committed/DB-unsettled operation finishes
  idempotently or creates an exact-path compensating commit, retaining all versions.
- [x] Recover unsettled journals before affected current projections/new mutations.
  Recovery dispatches no model. Invalidate/rebuild derived caches; authoritative
  mapping failure cannot be treated as best-effort successful completion. Do not
  clean up a referenced draft, displaced version, or checkpoint prematurely.

**Tests:** fresh-input rejection, exact-path Git/index preservation, extraction,
source-update dependent impact, inline helper publication from a stage, helper-only
same-file publication without leaking B, owner-local/joint settlement, parent failure
after helper success; failure injection at each numbered step; external atomic save
before displacement, occupied path after displacement, divergent restoration and
open-descriptor boundary; Git commit before journal update; repeated restart and
lock-order/dependency-wait deadlock tests. Do not label these external tests as proof
of universal exclusion.

**Gate:** supported adapter operations cannot lose protected work or accept a
half-applied move. Captured baseline/candidate/displaced versions remain recoverable,
source-required repair needs are explicit, and recovery is idempotent/model-free.

## PR4 — Reviews, clients, acceptance, and release

**Primary anchors:** `routes/runs.py`, `formalization_reviews.py`, `recovery.py`,
`companion/server.mjs`, `retryContext.mjs`, `formalizationApproval.mjs`,
`cascadeVerify.mjs`, `extension/content.js`, `leanPaneView.mjs`, and the existing
standalone canvas/formalization views.

- [x] Wire the explicit retry operation and capability into individual/batch entry
  points, current source preparation, existing focus/session identity, and retained
  draft access. Show queued activity and reattach using durable backend IDs.
- [x] Remove shared-file/multiple-path/stub retry refusals only on the capable path.
  Preserve real destructive retirement guards, old-adapter update errors, and
  conservative legacy checkpoint compatibility. Do not silently reinterpret
  canonical checkpoints or auto-merge changed paused work.
- [x] Implement own-target attempt review selection and bindings. Approval queues
  exact-candidate application with no provider call, reacquires the project slot,
  rechecks bindings and actual canonical inputs, and publishes reviewed lineage.
  Changed baseline/source/dependencies or withdrawn approval block application
  without losing the reviewed draft. Do not transfer an approval to a new candidate.
- [x] Support `disproved` with an explicit actual result declaration distinct from
  the requested target name. Carry it through prover classification, candidate
  association, canonical mapping, review, history, and UI; no false `proved` label.
- [x] Show draft versus published work separately, with useful partial progress and
  current-source staleness. Surface source-update consumer repair needs separately
  from unexpected retry failure. Reuse application receipts in cascade reporting
  and do not start unrequested paid repairs of unrelated targets.
- [x] Preserve existing mathematical review/audit/pause policies. Exact-input human
  approvals may become stale when their inputs change; explain that state accurately
  rather than fabricating approval from a preservation receipt.
- [x] Cover all acceptance cases with deterministic scripted solver events and real
  local Lean fixtures. Include H/A/B/C in one module, a consumer module, untagged
  definitions/comments, imported/manual targets, helper obligations, reviewed drafts,
  differently named disproofs, and source-driven versus ordinary updates.
- [x] Run relevant existing suites for single-target/no-change retry, source policy,
  dependency coordination, review/audit, imports, namespace migration, and recovery.
  Measure stage preparation/check/storage overhead without extra evaluator calls.
- [x] Enable capability only on tested supported runtimes after all criteria pass.
  Rollback disables new admissions, retains drafts/reviews, and finishes or restores
  journals; schema rollback must not discard retained work.

**Gate:** users can retry shared-file work without reorganizing it, lose no captured
work, complete eligible helpers, review checked candidates, receive honest disproof/
repair outcomes, and wait through overlapping retries without automatic solver merges.

## Verification and acceptance coverage

The commands below describe the verification entry points. Run real execution-boundary
checks outside a nested sandbox on a supported host; those tests explicitly skip
when the host cannot enforce the policy. The recorded local runs used real Lean and
Seatbelt, without provider calls. See the delivery record for actual results.

From `apps/lea-standalone/adapter`:

```sh
./.venv/bin/python -m pytest tests/test_reformalization_*.py -q
./.venv/bin/python -m pytest tests/test_target_completion.py tests/test_formalizations.py tests/test_formalization_reviews.py tests/test_bridge.py tests/test_check_evidence.py -q
./.venv/bin/python -m pytest tests/test_project_coordination.py tests/test_project_library.py tests/test_recovery.py tests/test_gitstore.py -q
```

Run prover verification-state/final-gate/checkpoint/tool-executor/SafeVerify suites
as those paths change; adapter-only testing cannot validate shared-file completion.
Use the configured local Lean toolchain/packages, no provider or network dependency.
From the repository root, run the relevant existing client suites:

```sh
npm test -w apps/overleaf-extension
npm run test:frontend -w apps/lea-standalone
npm run typecheck -w apps/lea-standalone
```

| Feature criteria | Delivery | Required evidence |
| --- | --- | --- |
| AC1, AC2, AC3 | PR1–PR4 | Shared theorem/definition/multi-file retries; protected neighbors; exact selection. |
| AC4 | PR1–PR3 | Imported/manual/dirty sources and editable target before-images retained. |
| AC5 | PR1, PR3–PR4 | Checked extraction, compatibility modules, unique identities and consistent mappings. |
| AC6, AC11 | PR1–PR4 | Prover final gate/classifier, adapter, review, canonical checks and target-selected trust. |
| AC7 | PR1, PR3 | Protected edits and ordinary-update regressions rejected. |
| AC8 | PR2–PR4 | Partial/cancel/pause/time/spend retention; successful helper survives parent failure. |
| AC9 | PR2, PR4 | FIFO same-file retries, second baseline after first settles, no solver merge. |
| AC10 | PR2–PR4 | Stale source/environment/association inputs retained without automatic rebase. |
| AC12 | PR3–PR4 | Per-step crash injection, coherent recovery, exact history, no repository reset. |
| AC13, AC14 | PR2–PR4 | Individual/batch/resume, worker bypass/accounting, existing policies and no-change paths. |
| AC15 | PR1–PR4 | Existing helper scope, independent inline and same-file/joint helper publication. |
| AC16 | PR2–PR4 | Explicit draft review; approve/withdraw/stale bindings; queued application without solver. |
| AC17 | PR1, PR3–PR4 | Explicit differently named disproof, canonical association and correct UI outcome. |
| AC18 | PR1, PR3–PR4 | Evidence-backed source update with retained dependent source and truthful repair impact. |
| AC19 | PR2–PR3 | Cooperating writer serialization, displaced versions, no-replace install, explicit external boundary. |
| AC20 | PR2–PR4 | Enforced file/shell/compiler/build/child restrictions and proof-workflow parity on supported runtimes. |


## Delivery record — 2026-10-15

| Slice | Delivered behavior |
| --- | --- |
| 1 | Compiler-selected results and transitive axiom checks; exact protected command/context validation; pinned theorem/definition contracts; checked relocation and source-update impact. The selected audit replays the actual compiled module, including private identities. |
| 2 | Durable project FIFO, global capacity reservation before run admission, isolated Lake workspace, scoped draft history, checkpoint continuation, source refresh at dispatch, and OS-enforced subprocess confinement. Earlier retained attempts are available as advisory files without replacing the current baseline. |
| 3 | Before/after/index blobs, displaced-file archive, no-replace installation, read-only canonical checks, exact-path Git commits, atomic database acceptance, restart recovery, helper-only publication, and checked mapping/completion-lineage updates. |
| 4 | Negotiated companion/standalone retries, durable reattachment and queued cancellation, separate draft inspection/comparison, exact-draft review application without a new provider run, explicit disproofs, and retained consumer repair needs. |

### Runtime and operation

| Runtime | Automatic shared-file admission |
| --- | --- |
| macOS, successful `sandbox-exec` probe | Supported and enabled by default. |
| macOS inside a sandbox that rejects the Seatbelt probe | Disabled; existing work and inspection remain available. |
| Linux or Windows | Disabled until an equivalent enforced executor is implemented and tested. |

The command policy covers shell, Lean, Lake, metaprograms, and descendant processes.
It denies command network access, including local service write proxies. Provider
requests and typed dependency actions run through the adapter. Editor source refresh
runs after durable admission in a worker, so waiting for the editor does not block
the admission response or start an execution clock. Cancellation during refresh
releases the capacity reservation without creating a solver run. Attempt sources and
outputs are writable; shared packages and canonical sources are read-only. Canonical
checks can write required build/scratch outputs, never canonical Lean source. Required
packages/toolchains must already be installed; a proof command cannot fetch them.

Build the updated optional SafeVerify executable with `lake build safe_verify` in
`apps/lea-standalone/prover/third_party/SafeVerify`. Its new `--declaration` mode
selects the transitive trust closure while replaying actual compiled bytes. Missing
optional audit support remains unavailable, never a successful audit; helper global
readiness still requires a successful audit.

`LEA_REFORMALIZATION_ENABLED=0` disables new operation admission. Keep migration
0036, blobs, stages, journals, and review events during rollback. Existing interrupted
journals are reconciled before current projections/new writes; recovery does not call
a model. Do not downgrade the schema as a rollback procedure because that would
remove the retained work ledger.

Preservation covers the captured working tree and index versions, protected source,
immutable drafts, and observed displaced/conflicting versions. Manual target progress
is editable starting work with a retained before-image. A failed or reviewable retry
does not replace the canonical proof. A source-required interface change retains
dependent proof text and records repair needs; it cannot promise those proofs still
compile. Compiler ambiguity, unsafe private relocation, or unexplained regressions
retain a candidate for inspection instead of weakening the preservation checks.

The filesystem protocol serializes cooperating adapter writers and protects occupied
paths. It also detects an observed write through an old editor descriptor before
acceptance and retains/restores that version. It does not universally exclude arbitrary
external editors writing at every possible instant. The tests cover named interleavings,
not universal external-editor exclusion.

### Initial delivery verification record

- Combined adapter regression run: **408 passed**, including the real operation
  suite plus bridge/subagents, target evidence, reviews, coordination, library,
  recovery, Git, registry, continuation, Overleaf policy, startup, status, and imports.
- Final real operation/scope run: **32 passed**, plus the later source-refresh
  admission/cancellation case in the 6-case follow-up.
- Complete extension/companion suite: **788 passed**.
- Prover agent/tool/subagent suites: **140 passed**, including corrected test isolation for the subprocess-concurrency fixture.
- Standalone frontend suite: **86 passed**. TypeScript checking and production
  build passed; the build retains its existing large-chunk advisory.
- SafeVerify executable built successfully. Its fixture script reported **5 passed,
  0 failed, 4 skipped** because the skipped fixture/setup prerequisites are absent.
- Actual draft component rendered and inspected at desktop and mobile widths;
  neither preview produced horizontal page overflow. No provider was used.
- Follow-up dependency/bridge/checkpoint regressions: **142 passed**. Final
  continuation/capability checks: **54 passed**; final queue/source-admission checks:
  **6 passed**. These runs overlap the combined suite; their counts are not additive.
- Additional final boundary tests cover immutable approval rejection, approval
  withdrawal without classifier-history edits, Unicode/CRLF definitions, preserved
  declaration relocation, retained-draft reuse, transitive `sorry` rejection,
  observed old-descriptor edits, and serialized continuation idempotency.

The deterministic three-declaration fixture measured **1.839 s** for capture/stage
preparation and **7.236 s** for assessment plus canonical checks/audit/application.
Its canonical source was **161 bytes**, distinct retained before/after blobs totaled
**320 bytes**, and the staged workspace totaled **12,259 bytes**. These are one small
local fixture's observations, not large-project latency/storage guarantees. Real
mathlib workloads retain their dependency/build/check costs; the implementation
reuses installed package material without granting write access to it.


### Review fixes — 2026-10-15

- Cancellation is serialized with acceptance. Settled receipts remain immutable;
  a durable Stop flag blocks late automatic dependency continuation. New manual
  continuation can explicitly resume retained work. Continuation request identity
  uses the same digest at the HTTP boundary and durable run admission. Admission
  records one pending successor atomically rather than replacing overlapping requests.
- Acknowledged crash recovery retains the same stage and safe conversation, starts
  a fresh turn with the remaining original allowance/deadline, and never resumes
  an uncertain action checkpoint. Normal clean continuation still uses its pinned
  checkpoint and the existing continuation policy.
- Publication acquires the existing gate before preparing a journal. Recovery can
  reconcile legacy competing journals serially. Restore recognizes an already
  reinstalled before-image and journals rollback archive intent before rename.
- The existing run registry enforces one project execution slot for staged parents
  and helpers. Each sibling snapshots after its predecessor settles; accepted helper
  receipts refresh ancestors and newly imported source inputs under the publication gate. Receipt IDs make that
  refresh idempotent, including a crash after acceptance but before refresh.
- Canonical preservation receipts identify unaffected protected declarations whose
  contracts remain stable. Their successful completion lineage survives a source
  update to another target after actual rechecking. Affected consumers remain repair
  work, and no human approval is synthesized.
- Approval resets FIFO admission time. Namespace migration and deletion honor the
  durable logical owner even between execution segments.
- Standalone retries reuse the selected target conversation (or its latest linked
  conversation), retain chat history, and reattach after reload during source
  preparation or continuation capacity waits. Late responses respect navigation.
- Snapshot capture uses existing confined dependency preparation and compiler input
  resolution, including a cold build cache. It copies only project sources, actual
  imported source closure, pinned config, and supported context files. Recovery
  archives are never copied into later stages; prior drafts use bounded blob-based
  advisory materialization.

### Review-fix verification

- Combined adapter suite: **505 passed**, covering real confined Lean operations,
  scope, admission, registry, sessions, recovery, reviews, preservation evidence,
  dependency/library behavior, Git, bridge/subagents, startup, and Overleaf handoff.
- Final continuation admission checks: **7 passed**, including simultaneous matching
  and conflicting requests. These include two additional overlap cases added after
  the combined suite started; counts overlap the combined run.
- Final helper/import/recovery checks: **5 passed**, including newly imported
  foreign source inputs and interruption after helper acceptance; counts overlap
  the combined run.
- Extension/companion suite: **788 passed**. Prover agent/tool/subagent suites:
  **140 passed**. Standalone frontend: **91 passed**.
- Final TypeScript checking, production build, Python compilation, and whitespace
  checks passed. The existing production chunk-size advisory and dependency
  deprecation warnings remain. No provider calls were made.

Operation/scope coverage now contains **54 cases** across the combined and final
focused runs. The new cases exercise settled cancellation, late automatic wakeups,
public continuation replay, acknowledged uncertain recovery, competing journals,
repeated restoration, rollback rename interruption with an external edit, stable
neighbor completion through a source change, approval FIFO, durable migration
exclusion, standalone reload/continuity, bounded snapshots with cold transitive
imports, serialized siblings, and a crash after helper acceptance.
