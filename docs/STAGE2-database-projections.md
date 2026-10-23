# Stage 2 database core projections

Stage 2's development exit gate is complete for explicitly enrolled scopes.
Session and proof reads in that mode use SQL exclusively; worker observations
supply bounded freshness claims. Compatibility readers remain registered for
unenrolled scopes until strict cutover. No existing user project was enrolled
during implementation or qualification.

The [migration plan](./PLAN-database-mediated-lea.md) governs later stages.
[Stage 3](./STAGE3-regulated-commands.md) now supplies regulated synthetic command
admission. Production execution, workspace mutations, companion authority, editor
transport and process separation retain their later-stage ownership.

## Delivered boundary

Revision `0039_workspace_observations` adds immutable file generations and blob
bindings, provenance, expiry, per-project enrollment and backfill checkpoints.
Revision `0040_core_projections` adds scope input revisions, core enrollment,
worker observations and derived values, resumable capture, and a one-time SQL
archive of legacy approvals and detailed usage. Existing SQL timeline, blob, run,
evidence, assessment and review identities remain authoritative.

`app/queries` contains database-only session list/detail, stats, search,
formalization/current/history, target status, review context, approvals,
diagnostics, usage, dependencies, library and Lea Status projections. Shared pure
formatting is also used by legacy workers. These modules do not import store,
prover, configuration, `lea.check_evidence`, `ReadSnapshot`, compiler or workspace
services. An import guard and filesystem/service-denied HTTP tests enforce the
seam. Read transactions are query-only and shared across nested projections.

`core_query_dispatch.database_query` selects SQL before legacy filesystem wrappers.
Enrolled project and loose-session reads never scan, repair, compile, import logs,
start maintenance or fall back during a GET. Global lists/search/stats select SQL
when any core scope is enrolled; unenrolled proof evidence in these lists is
conservatively unavailable. Session list/detail include a committed watermark.
Messages and accounting remain live SQL reads rather than waiting for backfill.

Enrolled file tree/content routes return HTTP 409 `projection_unavailable` while
unavailable, changing or expired. Explicit `observation_id` reads retained bytes
from that project, even after deletion or expiry. Binary and over-limit files
retain metadata and report 415/413. Exact UTF-8 bytes, including CRLF, are retained.
Proof history keeps its original bytes and check verdict; a successful historical
check does not imply current validity.

Target metadata describes worker-observed existence, declaration presence,
sorry/admit and shared ownership. It does not infer workspace existence from
historical SQL bytes. Display content uses an existing SQL blob matching the
observed digest; until indexed it is unavailable. Current proof snapshots still
expose the canonical recorded revision.

Session runtime capability is explicitly unavailable until a worker runtime
observation exists; session loading does not probe a compiler. The separate
legacy runtime endpoint and peripheral/export/editor paths remain inventoried
exceptions. Stage 3 adds two routes, bringing the register to 151 adapter routes
and 53 companion branches.

## Freshness and consistency

`app/core_observer.py` invokes existing proof, formalization, artifact, review and
dependency semantics outside requests. It neither compiles nor calls a provider.
It does not call draft assessment, which can compile and modify a stage. Retained
draft review eligibility instead checks the operation baseline and recorded
execution evidence read-only; decisions still revalidate through the mutation
service.

One bounded `ReadSnapshot` records resolved source, configuration, compiler and
dependency digests/versions, Lake-root topology and artifact families. These inputs
and their environment revision are persisted. A pass revalidates before publishing;
resumed captures restore and check earlier inputs. Changed inputs supersede
partial captures. Conditional checkpoints and a final SQL input revision fence
prevent overlapping callers or domain changes from publishing mixed generations.
Filesystem access occurs outside publication transactions.

Revision triggers cover proof-relevant domain writes, scope moves, evidence and
blob updates/deletion. Loose targets shared across conversations invalidate the
owning scope too. Prose and usage writes do not invalidate proof inputs. Applying,
restoring and conflicted publication journals override otherwise valid observations.

Each read matches the exact blob, evidence identity and content digest to its
worker fact. Missing facts, changed SQL inputs, reconciliation, conflict and expiry
remove current-proof, certification and review eligibility claims. Audit history
remains. Imported usage merges with newer SQL accounting by run identity; SQL
replacements take precedence without hiding older runs.

Current observations expire after **120 seconds**. Core maintenance checks every
five seconds while enrolled scopes exist, rotates due scopes, refreshes after
30 seconds and drains partial batches at one-second intervals. File maintenance
shares this host; without core enrollment its idle cadence remains one minute.
External changes are detected by scans without requiring watcher delivery.
Freshness means **as of the stated observation**, bounded by expiry. A query cannot
instantaneously detect an external disk write. Stat/manifest validation is not an
atomic filesystem snapshot; publication and actual checks retain their stronger
execution/journal guards. Overload or worker downtime yields unavailability
instead of extending a certificate indefinitely.

## Bounded capture and migration

File capture defaults to 100 entries per pass, maximum 200, with 4 MiB per file,
32 MiB per batch and a 10,000-entry inventory ceiling. Git, Lake build,
node_modules, Python caches and symlinks are excluded. No-follow descriptor reads
reject path redirection. Missing workspaces cannot publish empty replacements.
Deletion has its own persisted phase and bounded tombstone batches. The active
pointer advances only after every batch; current reads synchronize during partial
replacement while explicit old observations remain readable.

Core capture defaults to 20 entities, maximum 200, with a 10,000-entity migration
ceiling and 32 MiB input/value batch budgets. Sessions, code history, runs, targets,
dependencies and library facts use persisted checkpoints. Limits fail
conservatively rather than truncate a generation and label it complete. Historical
proof blobs are reused. Observation history is retained; no new pruning of proof
or observation generations is enabled by this stage.

Backups precede nonempty database upgrades. Unstamped adoption remains supported:
under the migration lock, blob triggers are temporarily removed before older
timeline rebuilds and recreated at 0040. Failed upgrades refuse startup; live
readers never serve an intermediate schema.

## Canary operation and rollback

From the configured adapter environment:

```sh
cd apps/lea-standalone/adapter
.venv/bin/python -m app.workspace_observer --project PROJECT_ID --core
```

This enrolls core and file projections; maintenance publishes first observations.
For a loose conversation, call `app.core_observer.enroll('s:' + SESSION_ID)` from
an operator Python session. Associated loose-target conversations resolve to the
enrolled owning scope.

Use the project command with `--disable --core` to select compatibility readers
while retaining SQL history. For loose scopes, pass `enabled=False` to `enroll`.
This is migration rollback before strict cutover; never restore an older database
over accepted work or drop additive migrations. Health advertises version 1,
explicit enrollment and observation lifetime.

## Qualification on 2026-10-16

Fixtures compare fresh legacy and SQL proof results at the same observed inputs.
They cover source changes/deletion, dependencies/toolchains, configuration and
nearer Lake roots, missed watchers, expiry, SQL changes, shared loose conversations,
mixed-input rejection, review/withdrawal lineage, legacy audit/accounting retention,
blob corruption and query-only transactions. File tests cover exact/history bytes,
symlinks, missing workspaces, binary/size states, scoped history and deletion
checkpoints across restarts.

A native real-Lean fixture compiles a transitive dependency, then forbids compiler
launches during observation and SQL reads. Changing the dependency removes current
proof validity while retaining its historical successful check. The native adapter
suite passed 1,372 tests before final added edge fixtures; focused final runs
qualify those additions and subsequent changes. The exact final counts are in the
[Stage 3 verification record](./STAGE3-regulated-commands.md#verification).

The [201-revision benchmark](./database-migration-baselines/stage2-core-macos-arm64.json)
records capture batches and 30 samples per query. This is an isolated synthetic
canary workload, not compiler throughput or a production indexing SLO. Qualify
enrollment count and sustained scan cadence against expiry before broad rollout.
Native AMD64 and the full published image remain release-matrix work; the cached
Linux ARM64 audit image does not establish workspace isolation support.

The two existing changed-source retry/companion ledger integration failures remain
documented in [Stage 1](./STAGE1-durable-observation.md), including reproduction
against unchanged HEAD. They are not counted as passing freshness evidence.

Reproduction from repository root:

```sh
(cd apps/lea-standalone/adapter && LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest tests/test_core_projections.py tests/test_core_real_evidence.py tests/test_commands.py tests/test_database_files.py tests/test_observation.py tests/test_migrations.py -q)
apps/lea-standalone/adapter/.venv/bin/python scripts/benchmark-core-projections.py
node --test --test-name-pattern='wire contract|actual adapter process restart|expired replay|session-list snapshot|enrolled core HTTP' tests/integration/formalize.e2e.test.mjs
python3 scripts/audit-ui-boundaries.py
git diff --check
```
