# Implementation Plan for Database Mediated Lea Execution

> Status: in progress — 2026-10-17. Stage 0 development gate complete with macOS
> and Linux arm64 baselines and calibrated initial policy. Stage 1 observation,
> session-list cutover and retention qualified. Stage 2 core SQL projections and
> observed-input freshness qualified for enrolled scopes. Stage 3 admission,
> fenced claims and recovery implemented and qualified. Stage 4 local solver and
> control command cutover implemented; final qualification is recorded in its
> delivery record. Stage 5 workspace commands and recoverable edits are implemented
> and qualified for the development cutover; results are recorded in its delivery
> record. Stage 6 source publication, SQL companion ownership and peripheral
> cutover are implemented; qualification is recorded in its delivery record.
> Stage 7 queued editor requests and database results are implemented and
> qualified for the development cutover; unsupported language features and
> platform limits are recorded in its delivery record.
> Stage 8 production images, native AMD64/ARM64 coverage and same-data strict
> binary rollback are qualified; exact pins and limits are recorded in the
> [release qualification](STAGE8-release-qualification.md).
> Governing contract: [FEATURE-database-mediated-lea.md](./FEATURE-database-mediated-lea.md).
> Delivery model: additive schema changes, one executor for each operation, staged
> client cutover, then removal of all application-data bypasses.

## Delivery strategy

Migrate the application in nine ordered stages. Start by stopping unsolicited
navigation and establishing a complete inventory. Persist observable changes
before replacing live transport. Establish database projections before removing
filesystem reads. Introduce durable typed commands before transferring controls
and workspace operations. Finish with an isolated execution worker and strict
boundary enforcement.

Each stage must be independently reviewable and deployable. A stage may contain
several pull requests, but its exit gate must pass before dependent capabilities
are enabled. A visible migration capability describes the current contract;
intermediate compatibility does not imply that the full feature has shipped.

Keep SQLite, the existing content blobs and timeline, run IDs, formalization IDs,
reformalization attempts, checks, reviews, and dependency ownership. Add records
for missing responsibilities. Do not build a second generic job history beside
the existing domain model or copy the companion's status vocabulary into a new
source of proof validity.

### Stage map

| Stage | Deliverable | Dependencies | Acceptance criteria advanced |
| --- | --- | --- | --- |
| 0 | Boundary inventory, client presentation ownership, baseline fixtures. | None. | A06 and test foundation for A01–A18. |
| 1 | Durable changes, committed partial text, replayable observation. | 0. | A04, A05. |
| 2 | Database projections for core UI and proof freshness. | 1. | Core portion of A01, A10. |
| 3 | Regulated command schema, admission, claims, and recovery framework. | 0–2. | A03, A07, A08, A16. |
| 4 | Solver work and control commands on the new queue. | 3. | A03, A04, A07, A14. |
| 5 | Workspace mutations, checks, and publication on worker commands. | 2–4. | A08–A10, A14. |
| 6 | Published Overleaf source, companion retirement, peripheral views and operations. | 1–5. | A11, A12, A17. |
| 7 | Versioned editor requests; removal of the direct LSP path. | 2–5; source bindings from 6 for Overleaf editor contexts. | A13. |
| 8 | Process separation, strict cutover, compatibility removal, release audit. | 0–7. | A01–A18 in the final deployment. |

The critical path is 0 → 1 → 2 → 3 → 4 → 5 → 6 → 8. Editor implementation can
progress alongside Stage 6 once the shared command and projection contracts are
stable. Estimate individual delivery slices after Stage 0 inventory; a calendar
estimate before the LSP, peripheral-file, and recovery scope is measured would
hide substantial uncertainty.

## Repository integration points

| Existing code | Planned treatment |
| --- | --- |
| [adapter/app/routes/runs.py](../apps/lea-standalone/adapter/app/routes/runs.py) | Preserve validation and policy; translate solver/control routes into transactional command admission. Remove direct bridge calls from request handlers. |
| [adapter/app/bridge.py](../apps/lea-standalone/adapter/app/bridge.py) | Extract runtime driver and host persistence; replace API-facing run queue/control rendezvous with worker-owned command consumers. Keep the typed prover library interface. |
| [adapter/app/runbroker.py](../apps/lea-standalone/adapter/app/runbroker.py) | Retire as an application observation source after persisted changes and text cover its events. Any internal wakeup must be disposable. |
| [adapter/app/store.py](../apps/lea-standalone/adapter/app/store.py), [db.py](../apps/lea-standalone/adapter/app/db.py) | Reuse blobs/history and serialized write transactions. Extract query-safe modules and append change records transactionally. |
| [read_projections.py](../apps/lea-standalone/adapter/app/read_projections.py), [proof_evidence.py](../apps/lea-standalone/adapter/app/proof_evidence.py), [artifact_snapshots.py](../apps/lea-standalone/adapter/app/artifact_snapshots.py) | Move filesystem capture/freshness work to execution services. Expose pure database projections with explicit observed input revisions. |
| [routes/sessions.py](../apps/lea-standalone/adapter/app/routes/sessions.py), [routes/projects.py](../apps/lea-standalone/adapter/app/routes/projects.py), [proof_files.py](../apps/lea-standalone/adapter/app/proof_files.py) | Separate query, admission, and worker operations. Saves/checks/imports/export/rename/delete effects become commands. |
| [project_coordination.py](../apps/lea-standalone/adapter/app/project_coordination.py), [runregistry.py](../apps/lea-standalone/adapter/app/runregistry.py) | Keep short worker-local synchronization; move admission and durable ownership facts to database records. Never assume their Python locks protect another process. |
| [reformalizations.py](../apps/lea-standalone/adapter/app/reformalizations.py), [reformalization_runner.py](../apps/lea-standalone/adapter/app/reformalization_runner.py), [reformalization_apply.py](../apps/lea-standalone/adapter/app/reformalization_apply.py) | Preserve staged attempts, reservations, journals, and selected-result checks. Link command attempts to existing operation identities. |
| [main.py](../apps/lea-standalone/adapter/app/main.py), [runtime.py](../apps/lea-standalone/adapter/app/runtime.py), [recovery.py](../apps/lea-standalone/adapter/app/recovery.py) | Split web startup from execution startup, migrations, workspace locking, reaping, reconciliation, and worker drain. |
| [overleaf_source.py](../apps/lea-standalone/adapter/app/overleaf_source.py), [routes/overleaf_source.py](../apps/lea-standalone/adapter/app/routes/overleaf_source.py) | Replace prepare-and-callback semantics with published immutable source snapshots and explicit action binding. |
| [companion/server.mjs](../apps/overleaf-extension/companion/server.mjs), [leaApiClient.mjs](../apps/overleaf-extension/companion/leaApiClient.mjs), retired `sourceRefresh.mjs` | Move orchestration and authoritative state to backend services; retain only validated transport compatibility where needed. |
| [companion/cascadeVerify.mjs](../apps/overleaf-extension/companion/cascadeVerify.mjs), [dependencyCoordinator.mjs](../apps/overleaf-extension/companion/dependencyCoordinator.mjs) | Transfer required orchestration to the worker and reuse authoritative dependency/evidence services. Ensure a change triggers one cascade. |
| [useProofStream.ts](../apps/lea-standalone/src/app/hooks/useProofStream.ts), [proofSession.ts](../apps/lea-standalone/src/app/stores/proofSession.ts), [lib/api.ts](../apps/lea-standalone/src/app/lib/api.ts) | Revision-aware snapshot/feed consumption, stable local selection, durable command receipts, no temporary-text reconciliation by string matching. |
| [extension/content.js](../apps/overleaf-extension/extension/content.js), [eventsClient.mjs](../apps/overleaf-extension/extension/eventsClient.mjs) | Publish input snapshots; query database views; track receipts; consume durable changes without companion job overlays. |
| [LiveEditor.tsx](../apps/lea-standalone/src/app/components/LiveEditor.tsx), [editor_worker.py](../apps/lea-standalone/adapter/app/editor_worker.py) | Introduce versioned persisted editor requests; place LSP ownership exclusively inside execution. |
| [config.py](../apps/lea-standalone/adapter/app/config.py), [settings.py](../apps/lea-standalone/adapter/app/settings.py), catalog and upload services | Separate bootstrap/credentials from application settings; persist catalogs, file content, and peripheral projections. |

Proposed module boundaries are `commands` for validation/admission/claims,
`changes` for durable observation, `queries` for pure projections, and `worker`
for execution and workspace services. Choose package layout while extracting;
avoid a broad unrelated rewrite of the current monoliths.

## Stage 0 Establish ownership and the migration inventory

### Delivery record

The [Stage 0 inventory](./STAGE0-database-boundary-inventory.md) and
[checked route register](./database-boundary-inventory.json) classified 147 adapter
routes and 53 companion dispatch branches, with lifecycle/timer/browser bridges
recorded alongside them. The route guard runs in `npm run test:scripts` and
requires explicit classification of new or changed dispatch paths.

Live steps and background resync/reconnect/completion no longer select the
formalization Lea is editing or move the author's historical step. Current-file
selection survives snapshot reordering; refreshes keep the editor, draft, and
cursor mounted. The Overleaf pane retains source/code text selection and applies
deferred results when the range clears. Hook, content-script, and browser
regressions cover this behavior.

The [runtime baseline and policy record](./database-migration-baselines/README.md)
now records native macOS arm64 and Linux arm64 storage/HTTP fixtures, reconnect,
cancellation acceptance, capabilities, journal comparison, and finite calibrated
limits. Stage 0's development gate is satisfied. Native AMD64 and full production
image qualification were deferred to Stage 8 and are now recorded in its release
guide; synthetic history does not
establish production solver throughput. Stage 1 adds two SQL-only routes to the
register, for 149 adapter routes and 53 companion branches.

### Implementation

1. Inventory every client-facing HTTP route, WebSocket, SSE stream, companion
   route, background timer, startup task, and browser message bridge. Record its
   caller, data source, mutation/effect, authority, target contract, stage, and
   compatibility requirement. Include settings, models, skills, MCP tools,
   subagents, source mirrors, library inventories, graphs, blueprints, stats,
   project lifecycle, recovery, imports, exports, and Git sharing.
2. Classify each operation as static/infrastructure, database query, database-only
   mutation, source/blob publication, or execution command. Explicitly register
   every temporary bypass. New routes must declare a category.
3. Remove automatic formalization/file/history selection from `code_step` and
   equivalent extension events. Keep selection stable on incoming records.
   Implement explicit follow behavior only if it is already required by a
   supported workflow; otherwise defer the opt-in control.
4. Record current behavior and representative data fixtures: legacy companion
   jobs, shared files, manual edits, paused attempts, source snapshots, pending
   approvals, dependency waits, disproofs, and deleted/renamed artifacts.
5. Capture baseline latency, database size/WAL behavior, browser reconnect
   behavior, and representative project sizes. Define supported runtime fixtures
   for macOS and each advertised container configuration.
6. Validate the specification's proposed queue/transport defaults and set finite
   blob, upload, and export byte caps by supported artifact class. Record exact
   limits and error behavior before enabling the corresponding admissions.

### Exit gate and rollback

The route inventory has no uncategorized application path. Browser tests show
that incoming updates do not move selection, including while viewing a historical
revision. No execution or storage contract changes in this stage.

Rollback restores the previous presentation behavior only if a concrete product
regression requires it; fixtures and the inventory remain. This stage needs no
schema rollback.

## Stage 1 Persist every observable update

### Delivery record

See [Stage 1 delivery and qualification](./STAGE1-durable-observation.md).
Revisions 0037–0038 provide transactional invalidations, stable partial messages,
SQL-only run replay and the session-list cursor adapter. Both run clients recover
from expired replay; production publishers retain no memory UI buffer. Bounded
worker maintenance preserves canonical text and audit records before compaction.
Companion JSON job notifications remain a separate Stage 6 exception.

### Schema and persistence

Add a durable changes table with a database-assigned monotonic ID, transaction
batch identity, scope, entity identity/revision, change type, and bounded optional
payload. Add message streaming identity/chunk state using existing timeline
storage where suitable. Preserve run attempt IDs and do not reuse message `seq`
as the change cursor.

Refactor write services to accept an existing transaction or use a shared unit of
work. The domain update and its change entry must commit together. Audit direct
SQL writers and finalizers; patching only `emit()` leaves unannounced mutations.
No compiler/provider call or filesystem operation belongs inside that transaction.

Batch text at the specification's 100–250 ms normal interval with a byte cap.
Make a partial assistant message queryable before any update is delivered. Final
and interrupted states retain the same message identity. Add retention that
compacts text chunks only after their content is preserved and clients can
recover through snapshots.

### API and client migration

Provide snapshot watermarks and `GET /api/changes` with pagination; expose an SSE
tail over those persisted records. Snapshot data and watermark must use one read
transaction. Define a reset response for expired cursors and a stable error
contract. Authorized filtering must also work during replay and scope changes.

Migrate standalone and extension observers separately. A rollout switch selects
one authoritative observation path for a client. The old broker may continue
temporarily for older clients; do not let a migrated client apply both streams.
An event wakeup can trigger a database query, but cannot supply authoritative
uncommitted data. Persisted cancellation/approval/control transport is completed
later; their visible records must already appear in this feed.

### Exit gate and rollback

Demonstrate snapshot/subscription races, repeated/out-of-order delivery, dropped
SSE, expired cursors, slow consumers, partial text, tool errors, approvals,
terminal outcomes, tombstones, and API restart. The new feed reconstructs the
same committed state as a fresh query and shows no uncommitted text.

Rollback clients to the previous observation adapter while continuing durable
writes and changes. Do not delete message identities or stop recording updates.
Remove `RunBroker` as a UI source only after both clients have cut over.

## Stage 2 Make core views read exclusively from the database

### Delivery record

The [Stage 2 delivery record](./STAGE2-database-projections.md) covers revisions
0039–0040: file generations, bounded resumable backfills, SQL-only core queries,
worker proof/review/environment observations and explicit project/loose-session
enrollment. Enrolled reads never inspect the workspace or fall back. Exact proof
history, approvals, usage and review lineage remain in SQL; expiry and changed
inputs disable current claims. Native real Lean, filesystem-denied core HTTP,
Linux ARM64 SQL tests and a measured 201-revision fixture qualify the development
gate. Canary enrollment remains explicit; strict removal is Stage 8.

### Projection model

Add current file bindings and workspace observation/environment records linked to
existing blobs, checks, formalizations, and revisions. Separate immutable evidence
from the derived question of whether that evidence applies to the current
observed inputs. Persist tombstones, applying/reconciling state, dependency
invalidation, and worker observation timestamps.

Create pure database projections for session detail, current/historical proof
content, formalization snapshots, target status, Lea Status, diagnostics,
approvals, dependencies, usage, and core file listings. Lift pure domain logic
out of modules that import filesystem-aware prover services. The query package
must not import `lea.check_evidence` or construct a `ReadSnapshot`.

Move file reads, freshness checks, artifact capture, inventory discovery, and
environment inspection into worker-side maintenance services. At this stage they
may still run in the existing backend process, but never in request execution.
Feed their observations into the same revisions and changes used by queries.

### Backfill and confidence

Run resumable, bounded worker backfills. Existing SQL proof blobs retain their
IDs; scan supported non-proof files into the blob/current-binding model. Index
exact bytes and provenance. A missing historical file is unavailable, never an
empty proof or fabricated passing check. Previously recorded checks become
current only when exact inputs/environment are established.

Compare database projections to legacy reads in read-only diagnostics. Run
filesystem comparisons outside the query path and bind both samples to the same
observed revision so transient workspace changes do not look like projection
bugs. Classify mismatches before cutover; do not copy a legacy false-positive
validity label merely to achieve equality.

For enrolled projects, select database queries explicitly. Unindexed content
shows a bounded synchronization state or submits a reconciliation command when
that facility arrives. A GET never performs lazy indexing, repairs history, or
wakes a solver. The legacy read route remains a named compatibility path only.

### Exit gate and rollback

Run core views with `Path`/filesystem/compiler/runner access blocked in the query
context. Validate external-write detection, missed watcher events followed by a
scan, dependency and toolchain changes, worker observation expiry, multi-file
revision consistency, and retained proof/review history.

Rollback the read selection to the previous adapter before strict cutover;
continue storing observations and revisions. Backfilled data and new immutable
content remain. Never revert by restoring an older database over new history.

## Stage 3 Build regulated durable command admission

### Delivery record

The [Stage 3 delivery record](./STAGE3-regulated-commands.md) covers revision 0041,
credential-bound typed intake and receipts, atomic scoped idempotency/capacity,
independent scheduling lanes, durable attempts/epochs and fenced recovery.
Deterministic handlers pass concurrent SQLite, saturation/control independence,
revocation, deadline, retry/suspension and real-host HTTP checks. This satisfies
the synthetic framework gate; solver/control/workspace commands keep their
existing owners until Stages 4–5. The boundary register now has 151 adapter
routes and 53 companion branches. No user scope or producer was enabled by tests.

### Schema and validation

Introduce commands and claim-attempt records, approval/question resolution
records as needed, and runtime ownership/heartbeat records. Link solver command
attempts to current `runs`, `run_attempts`, checkpoints, invocations, and
reformalization operations instead of duplicating their semantics.

Implement a registry of versioned command schemas, permitted producers, scope
resolution, expected revisions, effective permission/budget limits, scheduling
lane, handler capability, retention, and recovery policy. Reject unknown fields.
Derive authoritative project/target/actor IDs; validate every referenced snapshot
and blob against that scope. Keep raw keys and execution policy out of payloads.

Make idempotency uniqueness include the authenticated producer. Canonical payload
hashing includes the semantic scope, expected revisions, and user limits. The
server allocates admission order. Maintain finite pending counters/caps in the
same admission transaction and release them on settlement; test concurrent
admissions at the limit rather than using a read-then-insert counter.

### Transaction and claim mechanics

For a submission, one transaction must check idempotency, insert the immutable
payload references, create/link required domain records, reserve accepted queue
capacity, append changes, and persist the receipt. Use existing run-submission
services inside that transaction. If any component fails, no accepted command
or disconnected user message is left behind.

Claim ready commands using a conditional update under `BEGIN IMMEDIATE`, setting
owner, worker epoch, generation, lease, and attempt identity. Commit before
executing. Heartbeats, result updates, and terminal transitions compare the
ownership token. SQL polling is sufficient for correctness; a disposable wakeup
is an optimization. Do not restore an authoritative in-memory FIFO.

Implement a singleton worker host initially in the current process with bounded
compute concurrency, independent control consumption, and editor capacity reserved
for Stage 7. Persist project-level execution/publication reservations needed by
each handler. Use existing short local locks inside the host, with no API thread
depending on them to authorize admission.

Classify recovery per handler: safe idempotent retry, checkpoint continuation,
journal reconciliation, or uncertain-effect suspension. A lease timeout changes
ownership eligibility; it cannot alone authorize a second live filesystem writer.

### Exit gate and rollback

Use deterministic handlers with real SQLite to verify idempotent acceptance,
concurrent claim exclusion, scope rejection, revocation, queue caps, lease
ownership, stale completion rejection, timeout, and unknown-effect recovery.
Include a control command accepted while compute capacity is full.

No paid or mutating production handler moves in this stage. Disable new admission
to roll back, settle synthetic work, and retain additive schema. Establish the
first compatibility release capable of reading the new command records before
Stage 4 accepts real work.

## Stage 4 Move solver runs and controls onto commands

### Delivery record

See [Stage 4 solver/control cutover](./STAGE4-solver-controls.md). Revision 0042
atomically links existing runs/operations and exact tool gates to commands. The
memory FIFO is removed; request handlers persist solver/control intent without
calling runner signals. Both ordinary and staged drivers fence paid invocations
and final acceptance. Legacy pending work is adopted in bounded rotating passes
after reaping and checkpoint/journal recovery. Compatible responses carry version-1
receipts; control acceptance is distinct from worker-observed results. The local
compatibility producer retains the existing local security boundary; external
credentialed command intake cannot forge internal lineage references.

Real-process tests cover execution without attached observers, duplicate decisions,
cancellation, replay and restart. Native tests cover shared-file preservation,
continuation, helpers, journal rollback and lease fencing. Source/model preflight,
companion orchestration, remaining workspace mutations and process separation keep
their explicitly registered later-stage scope.

### Solver execution

Register formalize, chat, continue, re-formalize, batch, and dependency commands.
Reuse current prompt policy and context construction initially; move companion
prompt orchestration in Stage 6. An admitted command owns exactly one logical
operation lineage. Resume/checkpoint attempts do not create duplicate user
messages, formalizations, or billed invocation records.

Translate existing `/api/runs` and continuation routes into the admission service.
Keep compatible response identifiers when possible, and add receipt/capability
fields. If a legacy synchronous contract cannot be preserved honestly, return a
versioned async receipt with an explicit supported-client requirement. The old
in-memory dispatcher must not claim commands owned by the new dispatcher.

### Controls and decisions

Convert stop, child-stop, approval decisions, and author answers into control
commands. API routes must no longer import or call bridge signal/decision methods.
The worker's independent control loop resolves durable intent to its own task
handles. It remains operational while every compute slot waits for approval.

Bind approvals to exact pending approval and invocation identities. Add atomic
decision settlement and durable cancellation precedence. Before releasing a tool,
the gate checks cancellation/revocation in the database. Preserve bounded approval
timeouts and current Overleaf approval policy; the new architecture must not
introduce additional routine approval prompts.

Replace any connection-drop interruption with persisted command deadlines and
explicit cancellation. Expose worker-observed stop separately from cancellation
acceptance. Convert runtime/drain controls that affect execution using the same
control mechanism; OS service restart remains an infrastructure action.

### Exit gate and rollback

Run real API + queue + scripted prover tests with both clients disconnected.
Exercise duplicate submits, claim/cancel races, stop during a tool, stop after
completion, duplicate/conflicting approvals, cancellation preceding approval,
waiting dependency parents, child cancellation, global spend enforcement, API
restart, and worker recovery from checkpoints. No extra paid work is invoked to
test transport robustness.

Rollback stops new admission, drains or explicitly suspends claimed work, and uses
the compatibility release that understands command ownership. Previously accepted
commands remain on the same executor. A route flag cannot hand a running operation
back to the legacy dispatcher. Keep legacy request shapes as translators only.

## Stage 5 Queue workspace mutations and make publication recoverable

### Delivery record

See [Stage 5 workspace commands](./STAGE5-workspace-commands.md). Revision 0043
links typed workspace requests, immutable drafts/current bindings and recoverable
effects to the existing command queue. Legacy workspace routes return SQL receipts;
versioned edits require an explicit base. Worker consumers own saves, checks,
verification, manual edit cascades, metadata/namespace changes, attachments,
artifact operations, reviews, dependencies and reversible project deletion.

Exact-byte checks, create-only installation, displaced before-images and existing
publication journals protect canonical changes. Git failure retains SQL content.
Real SIGKILL tests qualify intent, file installation, namespace, Git and deletion
boundaries. Uncertain multi-file operations retain conflicts instead of being
replayed. Supported clients consume durable results, recover receipt loss by the
original key, and retain newer drafts across older save completion.

Legacy source/import/mirror transport and peripheral reads remain Stage 6;
LSP and process separation retain their Stage 7–8 scope. Stage 8 subsequently
qualifies native AMD64/ARM64 production images. Tombstone purge policy and
representative production limits retain their separate qualification scope.

### Command handlers

Move file saves, saved-file checks, SafeVerify, rebuilds, dependency publication,
namespace changes, project deletion/cleanup, and other project mutations into
worker handlers. HTTP handlers only persist blobs, validate admission, and return
receipts. An edit's expected base revision is mandatory on the new API.

Extract manual-edit/cascade behavior from companion and route handlers as those
operations move. Preserve effect attribution and run-less user history. Checks
bind exact bytes/environment; a failed Lean verdict can be an operationally
successful check command. Helper or target publication must continue through
the existing preservation and dependency services.

Adapt existing application journals for operations lacking recoverable publication.
Persist intent and before-images, mark affected current evidence unavailable,
install and check files, then publish database revisions/outcomes/changes. Record
observed external conflicts before overwriting any managed destination. Do not
wrap compiles or Git operations in a long SQLite transaction.

For recorded proof updates, keep SQL content as authority. For non-proof files,
introduce the same immutable blob/current-binding contract. Git failure after
content persistence is an explicit provenance/materialization outcome, reconciled
without discarding the submitted edit.

### Client behavior

Track each saved draft by local draft ID and command receipt. Leave newer edits
dirty when an older save finishes. Display conflict with current database content;
do not reload over a local draft. Use persisted command results for checks and
verification. Reconcile command receipt loss by its original idempotency key.

### Exit gate and rollback

Kill the worker before intent, after intent, during file installation, after
canonical checking, after DB publication, and during Git recording. Recover the
baseline, exact intended result, or explicit conflict, preserving drafts and
history at every point. Repeat with shared declarations, helper publication,
source-driven contract changes, disproofs, manually renamed targets, stale saves,
and external interference.

Rollback is handler-specific after drain and journal reconciliation. Use a build
that understands current file bindings, command ownership, and journal versions.
Never re-enable direct file-write routes over commands still awaiting publication.

## Stage 6 Complete source publication and remove companion authority

Implemented and qualified for the development cutover. See
[the Stage 6 delivery record](./STAGE6-database-ui-cutover.md) for import checks,
retention, runtime measurements, platform evidence and rollback constraints.

### Source and continuation

Add immutable source publication APIs with document/client identity, authority
generation, client sequence, source and bundle hashes, coverage, and version.
Provide an atomic publish-and-submit operation for explicit user actions.
Database-only publication remains available while a project solver is blocked;
worker materialization respects workspace reservations.

Migrate extension capture before removing prepare callbacks. Replace
`overleaf-source/prepare` consumers with lookup/binding of the already-published
snapshot. Reject stale or ambiguous binding before paid work. Preserve source
identity and source-faithfulness validation at dispatch and application. A newer
snapshot never silently rebases a queued command.

Delete the pending-source polling/callback path only after both the extension and
standalone continuation workflows use published snapshots. A missing connected
tab becomes missing published source with an actionable state, not a reverse
request to operate the browser. Preserve publisher-generation conflict behavior
for multiple tabs and distinguish incomplete source archives.

### Companion data migration

1. Read existing `jobs.json`, `chatSessions.json`, generated-target mappings,
   repair-batch records, and useful logs with a bounded importer. Record source
   file digest, migration version, and provenance so import is restartable.
2. Match existing adapter session/run/formalization IDs first. Reconcile active
   jobs with durable run/checkpoint state; importing a job must never start a run.
3. Import missing operational associations, history, and bounded diagnostics.
   Conflicting associations become explicit migration issues. Successful job
   labels alone cannot create proof checks, human approvals, or current validity.
4. Freeze companion writes at the cutover checkpoint, import final deltas, and
   verify counts/reference integrity. Support rollback to a read-only legacy
   snapshot; do not resume two operational ledgers.
5. Move prompt construction, artifact resolution, dependency coordination,
   cascade/repair orchestration, and markdown materialization to worker services.
   Generated markdown becomes an output of database-owned records.
6. Reduce the companion to an optional compatibility transport. It may validate
   and forward API operations or relay database change feeds. Remove its workspace
   path requirement, authoritative JSON stores, job timers, and run supervision.

Existing log/source files are archived until migration verification and retention
requirements are satisfied. Do not delete the only useful historical record to
make the new companion appear stateless.

### Remaining application surfaces

Finish the Stage 0 inventory. Persist project instructions/memory/blueprint input,
file trees, uploads, library/compiler inventories, graphs, catalogs, model
capabilities, skill/MCP assignments, runtime status, and all required settings
views. External service refreshes become worker commands. API reads do not call
providers or GitHub.

Import non-secret TOML settings once with provenance. Retain bootstrap connection
and credential settings in infrastructure; implement secret references and redacted
database status without logging secret values or storing them in command payloads.
Migrate extension configuration to connection/UI preferences only.

Queue imports, exports, and Git publication using existing user authorization.
Store bounded download artifacts or chunked export bytes in the database. Validate
that API downloads work without filesystem access. Keep file-size limits and
retention explicit. Do not add new paid repair or external-publication behavior
merely because it is now a command.

### Exit gate and rollback

Run the companion without workspace access or writable job storage. Exercise
formalize, chat, retry, edit, repair, batch, source updates, multiple tabs,
standalone continuation, project rename, export, and authorized Git workflows.
All application surfaces except the registered temporary LSP path now satisfy
database-only reads and command-only execution.

Rollback source and companion client capability only with a compatible translator
backed by the same database. Imported history remains. Reopening a retired JSON
writer or reviving a callback source authority requires abandoning strict rollout
and a new migration decision; it is not an automatic fallback.

## Stage 7 Replace direct LSP interaction

Implementation and qualification are recorded in [the Stage 7 delivery record](./STAGE7-queued-editor.md).

### Implementation

Publish immutable editor buffers bound to project/file identity, parent published
revision, buffer version, and owner. Submit typed language requests with position,
buffer ID, environment revision, deadline, and supersession identity. Results
are persisted and enter the change feed. Keep editor output separate from check
and certification evidence.

Implement per-buffer coalescing for pending requests, finite per-producer caps,
short retention, bounded result size, and cancellation of superseded requests.
Unchanged buffers reuse immutable content. The worker hosts the LSP session and
any low-level Lean RPC needed by supported widgets; browser clients never connect
to that session directly.

Prototype against `LiveEditor.tsx` and enumerate the actual `lean4monaco`/infoview
protocol used by the product. Completion/hover alone may not cover goals, widgets,
or server RPC. Adapt each required request into the durable contract or explicitly
disable the unsupported feature in strict mode. Do not ship an advertised editor
that silently opens the old WebSocket.

Preserve editor cursor, local undo, local drafts, and selection across diagnostic
updates. Label worker-unavailable or superseded analysis accurately. A buffer check
can never replace current proof evidence for different saved bytes.

### Exit gate and rollback

Browser tests cover rapid edits, out-of-order completion, goal/hover results,
buffer changes during requests, environment changes, reconnect, worker restart,
scope changes, quota saturation, and ignored stale diagnostics. Meet the warm
diagnostic target or document which language features remain unavailable.

Remove or reject the direct LSP WebSocket for migrated clients. Before strict
cutover, a recorded compatibility mode may retain the old editor for unmigrated
clients. After strict cutover, rollback selects a previous compliant editor build
or disables the feature; it cannot restore a direct execution channel.

## Stage 8 Separate processes and enforce the final boundary

Implementation: [Stage 8 delivery and qualification](STAGE8-process-boundary.md).
API/worker isolation and local/container lifecycle checks are implemented.
[Production release qualification](STAGE8-release-qualification.md) completes
full Dockerfile builds for all three roles on native AMD64/ARM64 and rehearses
rollback from a distinct successor to the retained strict baseline on the same
database/workspace. The exact source and binary archives are pinned; historical
in-process binaries remain forbidden automatic rollback targets. Linux confined
execution remains unavailable and fails closed.

### Process and import separation

Create a worker entrypoint that owns workspace locks, process reaping, command
consumers, LSP/compiler processes, publication reconciliation, provider execution,
dependency services, catalogs, and runtime heartbeat/capability publication.
The API entrypoint imports only query/admission/infrastructure code. Trace imports
to remove indirect prover imports from store/projection/settings modules.

Move existing startup and recovery behavior out of web `main.py`. Run schema
migration once through a supervised bootstrap/migration owner before either
process serves work. Split the existing data-directory lock into explicit
migration ownership and singleton workspace-worker ownership; both processes
must be able to access SQLite concurrently without bypassing that singleton.

Keep all workspace mutation inside one worker process initially. Its internal
thread locks remain effective there. Admission and status in the API use only
database records. Persist worker epoch, claim generation, and resource reservations;
before restart/takeover, establish that prior owned processes are stopped or fenced
and reconcile publication journals. SQLite fencing does not fence a live old
compiler's filesystem writes by itself.

Run the API with no workspace mount/access, no provider execution credentials,
and no execution tool registration where the platform supports those controls.
SQLite needs normal access to its own database/WAL files; static assets and the
credential intake boundary retain their explicit infrastructure access. Scope
the worker's filesystem and external capabilities using the existing confined
executor and supported-runtime capability checks.

Update local launchers, [scripts/supervisor.mjs](../scripts/supervisor.mjs),
development scripts, container entrypoints, Compose definitions, health checks,
and shutdown handling. API restart leaves worker tasks running. Worker restart
leaves application queries available and publishes stale/recovering state.
Deleting or recreating persistent volumes is never an upgrade mechanism.

### Strict cutover

Expose negotiated contract versions for database queries, changes, commands,
source snapshots, and editor capabilities. Enable strict mode only after all
required paths and enrolled projects pass their gates. Legacy endpoints are
command/query translators or reject incompatible clients before side effects.
Unsupported command kinds and unindexed content fail explicitly.

Remove `RunBroker` as an observation authority, the legacy run dispatcher,
API-to-bridge signals, companion job writers, source callbacks, direct LSP,
filesystem-backed GETs, request-triggered scans/recovery, and obsolete feature
flags. Retain no automatic fallback that weakens the boundary.

Update READMEs, the system report, runtime diagrams, and developer guidance to
state the final contract. Mark superseded historical choices precisely:
in-process web execution, live filesystem-backed UI views, and Git-only content
claims no longer describe the destination architecture. Keep historical design
documents as historical records with a link to the new governing specification.

### Exit gate and rollback

Run every acceptance criterion against the final two-process deployment, including
an API process denied project filesystem/prover access. Run supported container
and macOS lifecycle suites. A worker crash cannot take down read access. An API
restart cannot duplicate work. No registered migration bypass remains.

Rollback uses a previous compliant API/worker/client combination that understands
the live schema and journal formats. Drain/suspend claims, reconcile effects,
then roll back binaries. After strict cutover, a known-noncompliant architecture
is not an automatic rollback target. Prefer forward repair or disable the affected
feature while retaining database readability and pending commands.

## Migration mechanics and compatibility policy

### Schema sequencing

Create additive Alembic migrations from the actual head at implementation time;
the current head is `0036_reformalization_operations`. Do not reserve migration
numbers in advance. Add nullable fields/tables first, backfill in bounded resumable
worker batches, verify references, then tighten constraints in a later release.
Keep migration transactions short and take the existing pre-upgrade backup.

Record backfill checkpoints and per-project enrollment versions. Backfill reads
may observe changing work; capture a revision watermark and replay subsequent
changes or take a brief publication reservation for the final synchronization.
Block strict enrollment of a project with unresolved destructive conflicts or
missing required content. Other projects can continue migrating.

New tables do not require rewriting historical IDs. Maintain a migration map for
legacy companion IDs and external origin keys. Preserve missing-content markers
and historical uncertainty. An importer must not convert a job string such as
`formalized` into a newly verified theorem.

### Switching authority

Use explicit server-controlled migration modes: legacy compatibility, comparison,
database reads, queued execution by operation family, and strict. These are
proposed implementation concepts, not existing flags. Persist the selected owner
for accepted work so a configuration change cannot redirect it mid-operation.

Compare reads without effects. Never shadow a solver, write, publication, or paid
provider call. During a family cutover, stop admission briefly, settle or retain
in-flight ownership, switch the sole admission/execution owner, then resume.
Keep receipt lookup available throughout. If a handler is unavailable, leave its
accepted command queued/suspended with an explicit reason; do not send it through
another path under a fresh identity.

### Rollback discipline

Every stage records a known-good rollback binary, compatible schema/command/journal
versions, active-operation treatment, and verification command. Additive migration
downgrades are not a normal rollback. A database restore is disaster recovery and
must account for work accepted since the backup; never use it as a routine
feature-flag reversal.

Hold immutable before-images, imported legacy records, and command receipts until
the new contract's retention requirements and migration audit are satisfied.
Strict mode never silently falls back because a projection is missing or a
worker is unavailable. The UI presents unavailable/recovering/conflicted state.

## Validation strategy

Use the existing adapter, frontend, extension, integration, and browser harnesses.
Add tests for boundaries and user-visible failure cases; avoid tests that merely
repeat the schema implementation. Keep scripted prover tests deterministic and
run paid/networked tests only when explicitly configured.

| Test layer | Required cases |
| --- | --- |
| Database and admission | Transaction rollback, producer/key hashing, concurrent caps/claims, ownership fencing, revision constraints, change atomicity, snapshot watermarks, retention resets. |
| Query boundary | Project filesystem/compiler/provider/bridge access raises immediately; startup/query imports do not load the prover; uncached/unindexed content never triggers a hidden scan. |
| Worker and controls | Compute saturation, independent controls, waiting approvals/dependencies, cancellation precedence, invocation uncertainty, drain and recovery. |
| Publication failures | Process kills at every journal boundary, stale writers, before-images, external interference, canonical recheck failure, draft retention, selected-result and human-review lineage. |
| Client state | Replayed/late records, snapshot versus feed races, stable selection/history/cursor, partial narration, dirty draft preservation, expired cursors, worker offline state. |
| Overleaf migration | Legacy JSON variants, orphaned/conflicting associations, multiple publishers, missing/partial source archives, no callback, stateless companion, retry/batch/cascade equivalence. |
| Editor | Exact buffer/environment results, superseded requests, caps, latency, goals/widgets coverage, no direct socket, no draft certification. |
| Deployment | API and worker restart independently, DB outage/disk full, one active workspace owner, container volume upgrades, stale owned-process cleanup, redacted secrets. |

At each stage, run targeted tests for the touched seam, then the relevant existing
regressions. At stage exit and release, run the broader required suites:

```sh
npm test
npm run test:integration
npm run test:browser
npm run typecheck -w apps/lea-standalone
cd apps/lea-standalone/adapter
./.venv/bin/python -m pytest
```

Use the supported local runtime and CI/container variants for tests requiring
real Lean or the confined executor. Record environment and capability outcomes;
do not claim a filesystem isolation guarantee on an untested/unsupported host.
Failure injection must include both complete process death and recoverable
exceptions; thread-level exceptions alone do not prove crash durability.

## Operational rollout and release gates

Publish queue depth/age by lane/project, acceptance latency, control-consumption
latency, claim/lease recovery, worker heartbeat age, reconciliation lag, query
latency, change replay lag, payload/chunk volume, WAL growth, and database lock
wait. Logs correlate command, run, attempt, project, and worker epoch while
redacting credentials and sensitive source content.

Canary in this order: fixture projects; internal projects with both UI surfaces;
restart and offline scenarios; representative large/shared projects; supported
container deployment; strict default. Read equivalence alone is insufficient:
prove mutation, control, preservation, recovery, and client selection behavior.

Release requires zero unexplained projection mismatches for enrolled workflows,
all A01–A18 checks passing, no inventoried bypass, measured performance against
the specified targets, tested compatibility rejection, and a rehearsed rollback.
Record migration counts, orphan/conflict disposition, retained legacy archives,
latency measurements, supported versions, and remaining disabled language features.

## Implementation checklist

- [x] Stage 0 inventory, stable presentation ownership, runtime baselines and initial limits.
- [x] Stage 1 transactional changes, durable partial messages, session-list cutover and retention.
- [x] Stage 2 database core projections and observed-input freshness for enrolled scopes.
- [x] Stage 3 synthetic regulated admission, durable claims, and recovery policies.
- [x] Stage 4 local solver/control commands with no API-to-runner signals.
- [x] Stage 5 workspace operations and recoverable publication (development cutover).
- [x] Stage 6 published source, migrated companion state, and peripheral coverage (development cutover).
- [x] Stage 7 persisted editor requests and no direct LSP (development cutover).
- [x] Stage 8 isolated worker and strict boundary enforcement (development cutover; release gates recorded separately).
- [x] Stage 8 full production role images and native AMD64/ARM64 qualification.
- [x] Stage 8 retained strict baseline and actual same-data binary rollback rehearsal.
- [ ] Consolidated A01–A18 production rollout sign-off, including representative load and latency evidence.
- [x] Current architecture documentation and launch/deployment instructions updated.
