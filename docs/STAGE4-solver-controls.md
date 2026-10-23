# Stage 4 solver and control command cutover

This records the Stage 4 boundary. The subsequent workspace cutover is documented
in [Stage 5](./STAGE5-workspace-commands.md); the counts below retain their original scope.

Solver scheduling and UI controls now use the durable queue. The local adapter
still hosts the worker; process separation and removal of remaining compatibility
paths belong to Stage 8. Workspace and companion operations retain their explicit
Stage 5–7 scope. This delivery does not automatically enroll project read views.

## Admission and lineage

Revision `0042_solver_commands` links existing runs and reformalization operations
to commands, adds exact tool approval requests, and records a bounded legacy
adoption checkpoint. It does not create a second solver history, billing history
or set of proof identities. `runs`, run attempts/checkpoints/invocations, timeline,
formalizations, dependency attempts and publication journals retain their roles.

`store.create_run_bundle` commits the run, conversation/message, source context,
dependency ownership, staged-operation binding, capacity reservation, command and
changes in one transaction. Capacity failure rolls all of these back. Staged
operation admission also creates its conversation/linkage and command atomically.
Staged binding occurs before a command becomes claimable, so a fast consumer
cannot accidentally run an isolated retry against the canonical workspace.

The registry uses `run.execute` for chat, initial formalization, checkpoint/manual
continuation, source-bound and dependency runs. Existing purpose, model, turn
allowance, approval policy, deadline and continuation hashes remain on their
authoritative SQL records. Companion batch fanout reaches this same admission for
each activation and consumes capacity per item. Companion batch orchestration and
source preparation remain Stage 6 work; no parallel batch/solver ledger is added.

`operation.dispatch` governs deferred reformalization/source preparation and
continuation admission. Its reservation transfers to the resulting run command
inside that run's transaction. The source callback and staged workspace protocols
are preserved. `GET /api/reformalizations/{id}` is passive and no longer wakes work.

The old in-memory run FIFO and dispatcher thread are removed. The compatibility
`bridge.enqueue_run` helper only creates an SQL command and starts a worker for
library/test callers. Production request handlers do neither. Ordinary API startup
starts the host; no browser/event attachment is needed for progress. Task lookup
reads the committed user message directly rather than loading the full UI view.

## Producers and compatible API

The existing local HTTP routes translate into a server-assigned
`compatibility:local` producer behind the existing Host/Origin defenses. This is
the current single-user trust model, not a new multi-tenant authentication system.
The producer's random credential is not exported. A client cannot select its
producer, handler policy, ownership token or internal execution references.

Generic bearer-authenticated `/api/commands` continues to expose only operator
synthetic fixtures. External operator/standalone/Overleaf credentials cannot submit
the internal solver/control kinds. Health advertises `solver_version: 1`,
`solver_admission: local_compatibility_translator`, `synthetic_only: false`, the
internal registry, and a separate `external_kinds` list. A future client-identity
cutover must preserve those distinctions.

Existing run/session/formalization identifiers remain in responses; a version-1
`command` receipt is added. Existing run observation still uses SQL replay and
durable changes. `GET /api/runs/{id}` includes the solver receipt and the latest
20 control receipts. The receipt endpoint for generic authenticated producers
does not expose another producer's commands.

Existing configured-model, source and continuation preflight validation is retained.
Workspace provisioning and imported-declaration adoption move to worker execution.
The complete API is not yet filesystem-free: named legacy preflight, peripheral
workspace/source, settings/catalog and editor paths remain in the boundary register
until their scheduled cutovers. Stage 2's enrolled query guarantees remain intact.

## Controls and exact tool gates

The independent control lane handles `run.stop`, `child.stop`, `run.approve`,
`operation.cancel` and `runtime.control`. Request handlers commit intent and a
receipt without invoking bridge signals or a decision rendezvous. Worker handlers
resolve their own live task handles. AST guards cover the migrated request paths.

Stops return `status: interrupt_requested`, `worker_observed: false` and a command
receipt. Approvals return `status: accepted`, the decision and receipt. HTTP 200 is
retained for these compatible endpoints; acceptance does not mean termination or
tool execution. Current standalone/companion callers already treat success as
acceptance and continue observing persisted state. External clients that depended
on synchronous status strings must use the advertised version and poll results.

Cancellation intent and control capacity commit together. The first active-run
stop reason wins, repeated stops reuse one command, and terminal runs remain
terminal. A pending driver cannot claim a run with accepted cancellation, even
before the control consumer executes. The worker settles queued cancellation with
confirmed zero usage; an active tool can finish its in-flight effect/accounting,
but the next invocation is fenced. Child cancellation records the exact child run
and does not cancel its coordinator.

Approval identity includes run attempt and the exact future invocation ID, tool
name and arguments. Acceptance is atomic and idempotent; conflicting decisions
return 409. The worker delivers the accepted decision through SQL. The gate checks
current ownership, producer enablement, attempt and cancellation before consuming
it. Immediately before each provider/tool invocation, the existing durable
invocation recorder checks command ownership and cancellation in its transaction;
gated tools must match the consumed approval. Cancellation accepted first cannot
release the tool. `always_session` grants are durable SQL records. The existing
900-second approval timeout and Overleaf no-gate policy are unchanged.

There is no separate author-question rendezvous in the current prover. Ordinary
author replies remain committed chat activations; source/best-effort answers use
the existing source-bound continuation contract and checkpoint identity. This
cutover does not invent new questions or introduce additional approval prompts.

Runtime drain/restart/cancel intents also use the control lane. A pending drain
already blocks new compute admission before the worker observes it. Runtime
responses distinguish `control_requested` from the observed lifecycle state. OS
signals and supervisor restart remain infrastructure. Controls continue to be
admitted when new compute admission is disabled.

## Scheduling, ownership and recovery

Pending/claimed caps remain 128 global, 32 per local producer and 16 per project,
with an independent 64-control reserve. Existing configured run concurrency
(`LEA_MAX_CONCURRENT_RUNS`, default four) governs real drivers. Two separate
synthetic consumers retain their fixture capacity. Per-project compiler/publication
reservations remain with existing operation/registry rules; a dependency-waiting
parent releases its execution slot so helpers can run in the same project.

The SQL scheduler preserves admission order among eligible runs. Session/project
blockers allow unrelated work to proceed. Global capacity exhaustion stops a pass
before a later run can steal a newly freed slot. Supersede notification caching is
disposable; SQL owns the waiting set and attempt identities. An idle host checks
for its first producer every 200 ms and schedules SQL work at 100 ms intervals.
These intervals do not promise a startup or provider-response SLO.

Empty queue polls and unchanged approval waits use read-only readiness checks.
Actual claims and decision consumption repeat their checks inside the write
transaction. Worker leases renew independently every five seconds, so observing
idle work does not reserve SQLite's writer on each poll.

Five-second heartbeats and 30-second leases fence owner, epoch, generation and
attempt. Real solver/operation work suspends on live lease expiry; it is never
blindly retried. New paid invocations and late success claims are rejected after
ownership loss. Actual completed usage/history remain retained and recovery is
marked for review. Exact run/command linkage prevents a token for one run from
authorizing another run.

Source preparation and continuation threads carry their operation claim across
the asynchronous source callback. They recheck it in the run-admission transaction;
a late callback after lease loss cannot mint a fresh solver command. Operation
dispatch also selects its own bound operation rather than borrowing another
queued operation in the same project.

Both ordinary and staged runners enforce the fence. Staged publication journals
retain the claim identity and recheck it, along with cancellation, in their final
acceptance transaction. Losing authority after file installation restores the
canonical before-images and retains the candidate. Recovery without a live claim
can conservatively retain/restore an unaccepted staged application; it cannot
manufacture a successful publication. Human-reviewed publication retains its
existing independent authorization and moves fully onto commands in Stage 5.

On actual restart, the existing data lock, owned-process reaper, application
journal reconciliation and checkpoint recovery run before command reconciliation.
Only a new/unstarted or explicitly checkpoint-safe pending run is made claimable
again. Uncertain model/tool effects remain in recovery review. The same run and
message identities survive; no duplicate billed invocation is created. The
previous runtime is an explicit predecessor in `checkpoint_compatibility.json`;
only the existing fully settled boundary checks permit migration.

Previously accepted legacy runs and queued operations are adopted in bounded
20-record passes with a persisted rotating checkpoint. Adoption failure rolls
back that reservation and leaves the original pending records. A backlog above
the new caps cannot prevent startup or starve later project scopes. Sub-agent
records remain under their coordinator rather than acquiring a second executor.

## Operation, rollback and verification

There is no switch back to the memory dispatcher. Set
`LEA_COMMAND_ADMISSION_ENABLED=0` to stop new compute admission while continuing
controls, receipts and already accepted work. Drain or explicitly suspend owned
work before changing releases. Keep migrations and a schema-compatible release;
never restore an older database over accepted commands or hand one accepted run
to two executors. These tests register no user credentials, invoke no paid models
and make no changes to user projects.

Qualification covers concurrent admission rollback/idempotency, controls with
every compute slot waiting, cancellation before claim and during a tool, exact
and conflicting approvals, revocation/lease fencing, late-result rejection,
child cancellation, drain intent, legacy adoption and checkpoint recovery.
Native staged tests cover shared-file preservation, dependency helpers, retained
continuations, and cancellation between file installation and SQL acceptance.
Real HTTP tests run without either client attached and exercise durable decisions,
stops, replay and restart. Qualification on macOS ARM64 and Linux ARM64 used
temporary databases, scripted providers and isolated workspaces:

| Check | Result |
| --- | --- |
| Final native migration gate: commands, controls, routes, concurrency, approvals, migrations, observation, bridge, actual restart, source handoff, and two real staged publication/claim cases. | 220 passed. |
| Native shared-file, dependency, retained-continuation, checkpoint and real compiler-evidence preservation suite. | 106 passed; the two additional final staged fencing cases are included in the 220 above. |
| Linux ARM64 queue, solver, route, concurrency, approval and migration gate, using current source. | 119 passed. |
| Focused real HTTP contract, process restart, replay, session-list race, enrolled projections and disconnected solver/control tests. | 6 passed. |
| Complete cross-layer adapter/companion integration harness. | 11 passed, 2 known changed-source retry/ledger failures. |
| JavaScript suites. | 928 passed: 17 script, 798 extension and 113 frontend tests. |
| Frontend type checking and browser selection tests. | Type check passed; 2 browser tests passed. |
| Prover approval, event loop, event shape, child start, parallelism and streaming entry points. | 6 modules passed, 164 checks; approval and dispatch share the invocation ID, and denial records no tool dispatch. |
| Boundary register and patch hygiene. | 151 adapter routes and 53 companion branches covered; `git diff --check` passed. |

An earlier broad native adapter run had 1,389 passes and seven failures. They
exposed startup/FIFO scheduling, obsolete direct-dispatch mock assertions, and
concurrency fixtures missing host startup. The final 220-test gate covers those
corrected paths. The entire broad native suite was not rerun after these fixes;
its result is not represented as a clean full-suite pass.

The complete integration harness still reproduces the two companion changed-source
retry/ledger failures previously reproduced on unchanged HEAD and documented in
Stage 1. They remain unresolved. Native AMD64, the full published image and strict
process separation remain release/later-stage qualification; Linux ARM64 queue
tests do not establish workspace confinement.

The [Stage 4 HTTP baseline](./database-migration-baselines/stage4-http-macos-arm64.json)
records the final implementation's source fingerprints. Eight durable cancellation
submissions measured 18.9 ms p50 / 45.0 ms p95; startup measured 1.82 seconds.
The observer-disconnect and replay checks passed. This fixture leaves core query
enrollment disabled: legacy parked-session detail measured 330.9 / 657.8 ms and
session list 17.8 / 178.4 ms (p50 / p95). These are development observations,
not production SLOs, and do not justify raising the queue limits. See the
[baseline notes](./database-migration-baselines/README.md) for scope and comparison
limits.

Reproduce from the repository root:

```sh
(cd apps/lea-standalone/adapter && LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest -q)
node --test --test-name-pattern='wire contract|actual adapter process restart|expired replay|session-list snapshot|enrolled core HTTP|durable solver commands' tests/integration/formalize.e2e.test.mjs
npm test
npm run typecheck -w apps/lea-standalone
python3 scripts/audit-ui-boundaries.py
git diff --check
```
