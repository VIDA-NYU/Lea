# Database migration runtime baselines and calibrated policy

Stage 0 reports were captured 2026-10-16, before Stage 1 changes. Raw JSON includes environment,
sample counts, source hashes, capability probes, and limitations. These are
reproducible synthetic baselines, not production capacity or prover benchmarks.
No user database, project, credential, provider call, or compiler was used.

## Runtime observations

| Fixture | macOS arm64 | Linux arm64 container |
| --- | ---: | ---: |
| Python / SQLite | 3.14.4 / 3.53.1 | 3.13.11 / 3.40.1 |
| API ready, ms | 2,203 | 4,210 |
| Health read p95, ms (24 samples) | 3.1 | 1.8 |
| Parked session detail p95, ms (24 samples) | 8.1 | 8.5 |
| Cancellation accepted p95, ms (8 samples) | 4.9 | 5.0 |
| Session list p95, ms (24 samples) | 5.6 | 8.7 |
| Observer disconnect preserves run / reattach replays first frame | pass / pass | pass / pass |
| Isolated workspace executor | macOS Seatbelt available | unavailable, staged retry admission disabled |

Sources: [macOS HTTP](stage0-http-macos-arm64.json),
[Linux HTTP](stage0-http-linux-arm64.json). Cancellation measures HTTP acceptance,
not process termination. Reconnect uses the scripted prover's parked approval;
real DOM selection/replay regressions are in `tests/browser/selection.spec.mjs`.

The container was the cached arm64 audit image `lea-runtime-audit:proof-evidence-v1`
(local image ID `b5f3e034538a`), with current source mounted read-only. Its first
HTTP run exposed missing Git. The completed run installed Git in the disposable
container, matching the published Dockerfile's dependency. It is not a full
Mathlib production image qualification. Linux staged workspace isolation remains
unsupported by the existing executor; this migration does not grant it support.
Native Linux AMD64 and full production images were deferred at this capture;
the subsequent Stage 8 release record qualifies their binaries and lifecycle
behavior. This earlier host fixture still supplies no native AMD64 timing
evidence. Measure representative workloads there before raising conservative
defaults or declaring multiarch capacity.

## Storage workloads and decisions

History shapes: 16 files × 3 revisions × 2 KiB; 128 × 3 × 8 KiB; 512 × 3 × 8 KiB.
Each file is a synthetic session, with three immutable code revisions. This tests
history storage/listing, not a 512-file compiler build. The largest complete
fixture database was about 20.7 MB before queue/blob sweeps. DELETE-mode large
session detail p95 was 5.0 ms on macOS, 4.8 ms in Linux; session list p95 was
8.5 / 8.6 ms. Timing has no production SLO implication.

| Four writers, 128 total 16 KiB progress writes | macOS p95 / max, ms | Linux p95 / max, ms |
| --- | ---: | ---: |
| DELETE journal | 11.9 / 206.0 | 23.0 / 344.7 |
| WAL trial | 24.2 / 68.3 | 64.8 / 141.4 |

Sources: [macOS storage](stage0-macos-arm64.json),
[Linux storage](stage0-linux-arm64.json). Every quick check returned `ok`.
The WAL trial retained approximately 4.1 MB with a connection pinned; a passive
checkpoint completed. WAL did not improve p95 in this workload. Retain the current
DELETE policy for Stage 1; do not enable WAL on performance assumptions alone.
Re-measure after adding change records. Slow snapshot consumers must not keep a
read transaction open across network streaming.

## Calibrated initial limits

The executable policy is `adapter/app/migration_limits.py`. Stage 1 transport and
Stage 3 synthetic queue admission now enforce their limits. Production command
and blob admissions enforce their respective budgets when those handlers migrate.
This does not silently restrict or expand legacy companion/API contracts.

| Class | Starting policy | Basis / required behavior |
| --- | --- | --- |
| Compute backlog | 128 global, 32 producer, 16 project | 128 maximum envelopes occupy 8 MiB; 32/128/256 backlog sweeps completed on both runtimes. Conservative fairness cap, not measured solver throughput. Batch fanout consumes capacity. |
| Envelope | 64 KiB UTF-8 | Larger content uses scoped blobs; reject before parsing large payloads. |
| Control reserve | 64 global | Independent of compute capacity; one pending cancellation per target, one decision per approval. Scheduler fairness is tested in Stage 3. |
| Editor backlog | 8 per producer, coalesce equivalent pending requests, 5 s deadline | Latency-sensitive bound; expired work cannot publish canonical effects. |
| Partial text | 16 KiB UTF-8 chunks, 200 ms normal batch interval | Four concurrent 16 KiB writers stayed below one batch interval at p95. Boundary/finish flushes can occur sooner. |
| Changes | 200 records default, 1,000 maximum, 1 MiB response budget | Metadata only; no source, secrets, transcript, or checkpoint duplication. Polls release DB transactions before sending bytes. |
| Source / bundle | 4 MiB per file / 32 MiB aggregate | Bound individual buffers and multi-file fanout. |
| Attachment / export | 25 MiB / 64 MiB aggregate | Retain existing attachment scale; 4/25/64 MiB blob sweeps succeeded. Native 64 MiB read p95 was 23.7 ms; inserts up to 326 ms. Stream downloads and avoid loading export bundles twice. |
| Heartbeat / lease | 5 s / 30 s | Recovery heuristic, not proof an old executor stopped. Fencing remains mandatory. |
| Retention | editor results 1 h; exports 24 h; changes 7 d | Pinned/referenced data preserved; compact idempotency receipts outlive payloads. Expired cursors require snapshots. |

Queue saturation returns a typed retryable capacity error with retry guidance;
oversize content returns a typed non-retryable size error. Reserved controls do
not bypass identity/scope/revision checks. Legacy limits remain in force until
their handlers migrate. The backlog sweep measures insert/storage cost only;
paid solver service rates and full-project publication remain later-stage gates.

## Stage 2 core projection canary

[The macOS ARM64 core report](stage2-core-macos-arm64.json) uses an isolated
temporary database, one target, 201 code revisions and synthetic execution
evidence. Eleven batches of 20 entities completed in 1,913 ms of capture work,
excluding maintenance scheduling delays. Thirty samples per query measured p95
12.96 ms for session detail, 8.64 ms for session list and 8.18 ms for the proof
snapshot. This measures development fixture cost, not production solver throughput
or an indexing SLO. Tests separately qualify real native compiler evidence and
bounded restartable deletion batches.

Core maintenance refreshes due scopes after 30 seconds, with five-second scheduling
and one-second partial-capture intervals; observations expire at 120 seconds.
Before broad enrollment, qualify project size and scope count against that bound.
The 10,000-entity, 4 MiB/file and 32 MiB observation/batch limits are finite canary
policy; crossing a cap cannot produce a falsely complete/current projection.
Stage 3 also reserves a finite global editor backlog of 64 synthetic commands;
real editor traffic/coalescing remains Stage 7 qualification. Production-specific
control deduplication remains Stage 4 work.

Reproduce the core report with:

```sh
apps/lea-standalone/adapter/.venv/bin/python scripts/benchmark-core-projections.py
```

## Stage 4 command cutover HTTP baseline

[The macOS ARM64 command report](stage4-http-macos-arm64.json) uses the same isolated
scripted HTTP fixture against the final queue implementation and records source
fingerprints for admission, host, handlers, bridge and storage. It opens no user
database and invokes no paid model or compiler. Disconnecting an observer leaves
the run active, and reconnect replays its original first frame.

Eight cancellation submissions measured 18.9 ms p50 / 45.0 ms p95 for durable
acceptance, distinct from worker-observed termination. Startup was 1.82 seconds.
The fixture leaves core enrollment disabled: legacy parked-session detail measured
330.9 ms p50 / 657.8 ms p95; its session list measured 17.8 / 178.4 ms. Those values
are recorded rather than represented as the pure SQL core measurements above.
They are not a production SLO or a like-for-like attribution of all changes since
Stage 0. Broad rollout must qualify the enrolled query mode and representative
workspace size; these observations do not justify raising queue caps.

Actual claims remain atomic. Idle queue and approval polls use read-only hints,
with authoritative checks repeated inside the claim/decision transaction; worker
heartbeats run independently. This avoids a new write-lock reservation on every
empty poll while preserving the command fence.

## Stage 5 workspace command HTTP baseline

[The final macOS ARM64 workspace report](stage5-http-macos-arm64.json), captured
2026-10-17, records 25 matching source fingerprints for admission, execution,
publication, routes, storage and the benchmark. It uses a temporary database,
scripted providers/checks and the actual HTTP worker host. Startup was 1.90 seconds.

| Measurement | Samples | p50 / p95, ms |
| --- | ---: | ---: |
| Save durable acceptance | 8 | 14.4 / 39.4 |
| Save observed completion | 8 | 588.4 / 618.5 |
| Check durable acceptance | 8 | 12.5 / 43.7 |
| Check observed completion | 8 | 519.5 / 874.9 |
| Cancellation durable acceptance | 8 | 30.1 / 79.2 |
| Health read | 24 | 9.7 / 32.2 |
| Legacy parked-session detail | 24 | 258.4 / 331.3 |
| Session list | 24 | 31.6 / 197.9 |

Observed completion includes worker scheduling and receipt polling. These checks
use a scripted exact-byte checker, not native Lean or a paid provider, and do not
measure production solver throughput or control-consumption latency. Core query
enrollment stays disabled in this fixture; the legacy detail numbers are distinct
from the enrolled SQL core canary above. Small sample sizes are development
evidence, not a production latency guarantee.

All eight saves completed, and the final edit matched the SQL content. One check
deliberately returned a failed Lean verdict while its command succeeded operationally. Observer
disconnect/reconnect checks also passed. The existing queue caps remain unchanged.
Cached read-only idle connections release cursors between polls; authoritative
claims still recheck ownership and scope inside the writer transaction. Linux
ARM64 correctness is recorded in the [Stage 5 delivery record](../STAGE5-workspace-commands.md);
this report supplies no Linux timing or native AMD64/full-image qualification.

Reproduce this report with:

```sh
LITELLM_LOCAL_MODEL_COST_MAP=True apps/lea-standalone/adapter/.venv/bin/python scripts/benchmark-db-boundary.py --http-only --output docs/database-migration-baselines/stage5-http-macos-arm64.json
```

## Stage 6 source publication and stateless companion baseline

[The macOS ARM64 report](stage6-http-macos-arm64.json) uses the actual HTTP worker
host with a temporary database and scripted prover/checker. All recorded source
fingerprints (51 files) match the delivery source. Captured on 2026-10-17,
startup took 2.45 seconds. This fixture automatically enrolls core
SQL projections; it does not invoke a paid provider or native compiler.

| Measurement | Samples | p50 / p95, ms |
| --- | ---: | ---: |
| 1 MiB source publication (archive reused) | 8 | 95.4 / 216.9 |
| Compatibility query durable acceptance | 8 | 102.5 / 156.3 |
| Compatibility query observed completion | 8 | 709.1 / 1074.0 |
| Cancellation acceptance | 8 | 71.9 / 160.4 |
| Health SQL read | 24 | 31.2 / 64.3 |
| Parked session SQL read | 24 | 121.6 / 333.7 |
| Session list SQL read | 24 | 60.7 / 159.6 |

The worker is adapter-owned Node SQLite; the public companion has no job storage
or workspace requirement. Observed completion includes scheduling and receipt
polling. Archive reuse, disconnected-observer preservation and exact replay
checks passed. Existing queue caps and execution concurrency remain unchanged.
These small synthetic samples do not measure provider, native Lean, full-image or
production project throughput. Linux ARM64 correctness is recorded separately in
[the Stage 6 delivery record](../STAGE6-database-ui-cutover.md).

Reproduce with:

```sh
LITELLM_LOCAL_MODEL_COST_MAP=True apps/lea-standalone/adapter/.venv/bin/python scripts/benchmark-db-boundary.py --http-only --output docs/database-migration-baselines/stage6-http-macos-arm64.json
```

## Stage 7 queued editor baseline

[The macOS ARM64 native report](stage7-editor-macos-arm64.json) records 20 distinct
small-file edits through SQL metadata lookup, immutable buffer publication, typed
ASGI HTTP admission, the actual editor queue, native confined Lean and SQL receipt
polling. All 20 source fingerprints match the delivery source.

| Measurement | Samples | p50 / p95, ms |
| --- | ---: | ---: |
| Changed-buffer diagnostics after debounce | 20 | 658.76 / 838.43 |

Maximum was 841.24 ms. Initial private-server analysis was
1186.02 ms with Lake/toolchain caches already prepared.
The fixture meets the warm small-file target; it excludes the 300 ms browser
debounce, TCP overhead, Mathlib initialization and paid inference. Cold or large
projects can exceed the five-second command deadline and expose unavailability.
Earlier loaded unchanged-buffer samples crossed one second at p95, so these
numbers should not be treated as production tail-latency guarantees.

The [Stage 7 delivery record](../STAGE7-queued-editor.md) provides reproduction,
feature limits and rollback; [qualification JSON](stage7-qualification.json)
records native/Linux/browser evidence. Editor requests/results cannot create
canonical proof checks. The direct LSP transport is rejected for every client.

## Reproduction

Stage 8 supersedes the earlier in-process launch descriptions. Its
[qualification record](stage8-qualification.json) covers the separate API and
worker, database-only route/import checks, lifecycle fencing, browser and
cross-layer regressions. [The container probe](stage8-container.json) runs a
read-only API with no workspace mount and independently restarts both roles.
[Native editor results](stage8-editor-macos-arm64.json) use real HTTP, the
canonical separate processes and confined Lean 4.29.0. These five requests are
correctness evidence, not a latency distribution. The subsequent
[production release qualification](stage8-release-qualification.json) completes
actual Dockerfile builds of API, worker and companion on native AMD64 and ARM64,
including shipped Lean/Mathlib/SafeVerify probes and rollback between distinct
strict binaries on the same database and workspace. Its source/image/archive pins
and retained reports supersede the earlier development release-gate status.
See the [release guide](../STAGE8-release-qualification.md) and
[Stage 8 delivery record](../STAGE8-process-boundary.md) for retention, supported
execution limits and deployment instructions; API startup no longer runs migrations.

From the repository root, using the adapter venv:

```sh
apps/lea-standalone/adapter/.venv/bin/python scripts/benchmark-db-boundary.py --output /tmp/storage.json
apps/lea-standalone/adapter/.venv/bin/python scripts/benchmark-db-boundary.py --http-only --output /tmp/http.json
```

Both use temporary directories, and remove the child API after completion. The
HTTP fixture needs Git, loopback sockets, and the actual adapter dependencies.
For a container, mount the repository read-only at `/repo`, run its adapter Python
with working directory `/repo`, and mount only a report directory writable.
Storage trials may run with `--network none`; dependency installation must finish
before measuring HTTP. No running application needs stopping or reconfiguration.
