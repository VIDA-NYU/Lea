# Stage 3 regulated durable commands

This record describes the Stage 3 framework qualification. The subsequent
[Stage 4 cutover](./STAGE4-solver-controls.md) adds internal solver/control handlers,
keeps external synthetic admission distinct, and updates health capabilities.

Stage 3's synthetic admission and ownership framework is implemented. The
registry accepts deterministic operator fixtures only. Production solver,
approval, cancellation, workspace and editor operations retain their existing
owners until Stage 4–7 cutovers. No existing user producer was registered during
implementation.

## Contract and storage

Revision `0041_regulated_commands` adds authenticated producers, ordered commands,
claim attempts, worker epochs/heartbeats, atomic capacity counters and project
reservations. Domain changes are transactional. Commands reference existing
project/session/formalization/blob identities. Production handlers must link
existing run/attempt/operation lineages rather than introduce parallel histories.

`POST /api/commands` authenticates a bearer credential and returns HTTP 202 with
the persisted receipt. Request streaming is bounded at 64 KiB before JSON parsing.
Unknown kinds, versions or fields are rejected. The strict version-1 synthetic
payload accepts text and an optional scoped blob ID; credentials, execution policy
and arbitrary paths are not payload fields.

`GET /api/commands/{id}` reads the submitting producer's SQL receipt. The API never
calls a handler. Accepted work and observation survive browser disconnection.
Results flow through SQL records and durable changes, with no worker callback to
the browser.

The registry contains `synthetic.echo` (compute), `synthetic.control` (control),
`synthetic.editor` (editor) and `synthetic.uncertain` (recovery fixture). Only the
authenticated `operator` producer kind may submit them. Standalone and Overleaf
identities have no enabled production command kinds yet. Health advertises the
version, enabled kinds, permitted producer kind and `synthetic_only: true`.

Admission uses one `BEGIN IMMEDIATE` transaction to resolve scope, check producer
restrictions, validate references and expected revision, reserve capacity, assign
admission order, insert the command and emit changes. Failures roll back all
counters and command writes. Foreign projects, unassociated targets and foreign
blobs are rejected.

Idempotency is unique per authenticated producer and key. A canonical semantic
hash includes resolved scope, expected revision, deadline and payload. Replays
return the original receipt after later domain changes; changed semantics under
the same key conflict. Claiming rechecks expected revision before execution.

## Capacity, claims and recovery

| Lane | Pending and claimed capacity | Execution policy |
| --- | --- | --- |
| Compute | 128 global, 32 per producer, 16 per project | Two synthetic consumers; one active compute reservation per project. |
| Control | 64 global | Independent consumer, accepted and processed while compute is full or blocked. |
| Editor | 64 global, 8 per producer | Independent synthetic consumer; at most five-second deadline. |

Capacity includes queued and claimed receipts until settlement. The editor global
cap is a finite synthetic policy; actual editor coalescing and traffic qualification
belong to Stage 7. Saturation returns 429 `queue_full` with retry guidance, disabled
admission returns 503, and oversized envelopes return 413. Deadlines are UTC and
bounded to 24 hours; editor deadlines are shortened to the five-second policy.

`command_host.py` starts a singleton SQL-fenced synthetic owner, initially inside
the adapter. It waits until a producer exists, then polls committed records.
Conditional claims record owner, epoch, generation, attempt and lease before
execution. Compute/control/editor consumers are independent. No authoritative
in-memory FIFO is introduced.

Heartbeats run at five-second intervals with 30-second leases. Heartbeat and
completion compare the complete ownership token and require live worker and
command leases, an unexpired deadline and an enabled producer. Revocation suspends
active receipts and fences completion. Old attempts cannot overwrite new results.
Results are bounded to 1 MiB.

Recovery examines at most 200 receipts per transaction. Safe synthetic work can
retry; expired work settles; uncertain effects suspend for review. Lease expiry
does not authorize a second live filesystem writer. A real handler must supply
checkpoint/journal/reconciliation policy and domain linkage before admission.
Capacity and reservations are released atomically at settlement. Attempts retain
their outcome lineage.

## Operation and rollback

In an isolated configured adapter environment, register an operator fixture,
optionally scoped to one project:

```sh
cd apps/lea-standalone/adapter
.venv/bin/python -m app.commands --producer canary --project PROJECT_ID
```

The credential is printed once; only its hash is stored. Supply it as a bearer
credential. Registration does not submit work. Omit `--project` only for an
intentionally unbound operator fixture. Revoke using:

```sh
.venv/bin/python -m app.commands --producer canary --revoke
```

Set `LEA_COMMAND_ADMISSION_ENABLED=0` to stop new admission. Existing receipts
remain readable, and accepted synthetic work retains its owner until settlement
or recovery. Keep the additive schema and a compatible release. Do not hand
accepted commands to a legacy dispatcher via a read-selection toggle. No automatic
command retention/pruning is enabled in this foundation.

## Verification

Real SQLite tests cover concurrent idempotency and claims, caps and rollback,
reserved controls under saturation, scope/blob/revision rejection, producer
separation/revocation, lease expiry, stale completion, deadlines, retry and
uncertain-effect suspension. A host test blocks both compute consumers and verifies
independent control settlement. HTTP fixtures cover authentication, body caps,
receipt replay and actual host settlement without creating a solver run.

Validation on 2026-10-16:

| Check | Result |
| --- | --- |
| Full native adapter suite, from adapter cwd, including native executor cases | 1,372 passed before the final additional edge fixtures and small fixes. |
| Final native core/file projections, commands, observation and migrations, including real Lean | 69 passed. Final project-list seam smoke check also passed after its addition. |
| Same final SQL tests in cached Linux ARM64 runtime, excluding native Lean fixture | 68 passed; Python 3.13 / SQLite 3.40, current source mounted read-only. |
| Real temporary HTTP adapter: wire contract, actual restart, expired replay through companion, session-list race, enrolled core and synthetic commands | 5 passed. |
| Full JavaScript suite | 17 script + 798 extension + 113 frontend = 928 passed. |
| TypeScript | Passed. |
| Browser selection, editor draft and cursor retention | 2 passed. |
| Boundary register and whitespace | 151 adapter routes / 53 companion branches; clean diff check. |

The broad suite and final focused runs are separate evidence; these counts are
not combined into a claim of a new complete full-suite run. Model behavior is
scripted, with no paid provider work. Native Lean qualification uses macOS
confinement. Linux SQL qualification does not establish compiler isolation support.
Native AMD64/full published image checks and sustained production indexing remain
release qualification. The two existing changed-source retry/ledger integration
failures reproduced on unchanged HEAD remain recorded in the Stage 1 report;
the five migration-specific HTTP checks above pass separately.

Stage 4 may now register solver/control handlers, preserve policy and lineage,
and translate legacy routes into admission. It must qualify cancellation/approval
races, checkpoints, restarts and spend accounting before real work switches owners.
Stages 5–8 retain workspace effects, companion retirement, editor requests,
process isolation and strict compatibility removal.
