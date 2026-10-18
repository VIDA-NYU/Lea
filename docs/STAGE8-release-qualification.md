# Stage 8 production qualification and rollback

The three remaining release gates passed on native Linux AMD64 and ARM64 in
[GitHub Actions run 39081557247](https://github.com/darturi/UnifiedLeaEcosystem/actions/runs/39081557247):
full production image qualification, native architecture coverage, and same-data
rollback to a qualified strict binary bundle. Immutable role IDs, archive
checksums and retained evidence are recorded in
[`stage8-release-qualification.json`](database-migration-baselines/stage8-release-qualification.json).
The runtime contract is [STAGE8-process-boundary.md](STAGE8-process-boundary.md).

The baseline is source `6780f3d4f262a9b4ca7a70dbcabf35dd2818da6b`, retained by
[`codex/stage8-strict-baseline-20261017`](https://github.com/darturi/UnifiedLeaEcosystem/tree/codex/stage8-strict-baseline-20261017).
The successor is source `c8dc8f62efa1ddc6ca5e333e29317ef30ea433af` on the
qualification branch `codex/stage8-release-gates`. The tested application and
release tooling match this checkout; subsequent changes only record documentation
and evidence. No registry image was published and no deployed stack was changed.

| Native runner | Production baseline and successor | Packaged-code regressions | Same-data binary rollback |
| --- | --- | --- | --- |
| Ubuntu 24.04 AMD64 | Passed, all three roles | 52 passed, 1 skipped | Passed |
| Ubuntu 24.04 ARM64 | Passed, all three roles | 52 passed, 1 skipped | Passed |

Both architectures ship Lean `4.29.0`, Python `3.13.11`, SQLite `3.40.1` and Node
`22.23.3`. Each qualifies migrations from both historical revisions, a complete
Mathlib build, real trusted proof compilation, and SafeVerify proof acceptance /
`sorry` rejection. The skipped regression is the opt-in macOS confined-editor
case; unsupported Linux application execution is explicitly tested to fail closed.

## Production ownership

Production API, worker and companion images use the actual multi-stage
Dockerfile. The worker contains the pinned compiler, complete Mathlib and
SafeVerify. API and companion containers use read-only roots and have no project
workspace or provider-configuration mount. The serving probes never substitute
application source from the host. Test helpers enter only the private worker.

Workers and the migration job share the Docker **local volume**
`lea-workspace-ownership` at `/app/prover/workspace/.lea-runtime`. Compose retains
the existing data/config/proof/project/import directories and Mathlib volume.
Keep the new ownership volume too; recreating it changes the workspace identity
and the database will refuse that replacement. `LEA_WORKSPACE_OWNERSHIP_VOLUME`
selects a different name for an independent workspace. Every deployment that
shares the same workspace or database must use the same ownership volume.

This volume is required because the measured Docker Desktop host bind mount
accepted two exclusive `flock` calls, including two independent opens within one
container. The same probe on a Docker local volume blocked the second open both
within and across containers. Startup now tests that the lock is enforced before
writing a binding. The production worker profile also requires an actual shared
ownership mount; an unsupported filesystem fails closed.

The database binding records the resolved root and a private persistent volume
identity. Existing version-1 path bindings acquire the version-2 identity under
the exclusive lock. A different root or ownership identity requires an offline
migration. Both bootstrap and worker take workspace ownership, so another DB
cannot migrate or start beneath a live owner of the same tree. Worker ownership
lasts until process exit; bootstrap releases it after its one-shot migration.

Before the first upgrade, stop every old stack that can access these directories.
The old in-process architecture does not implement the new ownership protocol.
Never use volume recreation, database replacement, or a schema downgrade as an
upgrade or rollback mechanism.

The privileged one-shot migration also prepares permissions for the shared
service UID (UID/GID 0 in these images). The API then runs with all capabilities
dropped and a read-only root. Permission preparation covers only the dedicated
database mount, its configured SQLite database and sidecars, and the private
HTTP/credential intake directories. Directories become `0700` and files `0600`;
intake traversal is bounded and rejects symlinks and unexpected file types before
changing permissions. Proof trees, configuration, exports and backups are not
recursively re-owned. The native qualification starts with legacy runner-owned
storage to exercise this upgrade path.

## Executable release gate

[The release workflow](../.github/workflows/stage8-release-gates.yml) verifies
host, Docker-daemon and image architectures, then runs on native Ubuntu AMD64 and
ARM64 runners. It installs no emulator. The baseline source is pinned in
[stage8-release-baseline.json](../.github/stage8-release-baseline.json). Each
architecture job:

1. Builds all three baseline production targets and qualifies them before that
   baseline is eligible for rollback.
2. Builds all three successor targets with their packaged source identity and
   qualifies that bundle independently.
3. Runs ownership/recovery/process regressions against the packaged successor
   application code. Only tests and their development dependencies are added to
   that disposable regression container.
4. Rehearses binary rollback against the original database and workspace volumes.
5. Saves both actual image bundles, both source archives, role IDs, runtime
   versions, checksums and the qualification/rehearsal reports as CI artifacts.

[stage8_release.py](../tests/integration/stage8_release.py) upgrades disposable
databases from revisions `0019_custom_tools` and `0031_usage_details`. It checks
historical content retention, rejection of legacy current-proof claims, static
UI delivery, the stateless companion, SQL admission/results, receipt reuse, API
restart, worker crash/expiry/restart, duplicate ownership across distinct DBs,
and queued worker drain while API reads remain available.

Trusted packaging probes compile a small Mathlib proof using the shipped Lean
binary and require SafeVerify to accept that proof and reject its `sorry`
counterpart. Node and Python architectures are measured independently of image
labels. These trusted probes do not enable application execution on an unsupported
platform. Linux still advertises its confined executor as unavailable; there is
no unconfined editor or staged-execution fallback.

Reproduce after building each role, supplying the exact source identity used by
the build:

```sh
python3 tests/integration/stage8_release.py qualify --require-native \
  --source SOURCE_COMMIT --api-image API_IMAGE --worker-image WORKER_IMAGE \
  --companion-image COMPANION_IMAGE --output baseline.json
```

## Rollback rehearsal

The first strict baseline is certified here, rather than selecting a historical
in-process binary. The successor adds packaged build identity to SQL health
reporting. The two bundles contain different API/worker binaries and share schema
`0048_process_boundary` and the same journal/command contracts. This certifies
this exact pair, not arbitrary future downgrades.

The rehearsal first runs the baseline, drains it, and starts the successor on the
same data. The successor persists a completed command receipt, a metadata edit
and an uncertain claimed command. After drain, the private fixture executes the
real file-publication path and interrupts it after SQL intent/history commit,
before materialization. It then starts the exact older strict bundle without
running migration, restoring a backup, or recreating a volume.

The older worker must reconcile the prepared publication, retain both original
and successor history, preserve the completed receipt and its idempotency key,
suspend the uncertain claim, and reject completion by the previous claim owner.
The API must serve those retained SQL results. HTTP build identity and immutable
role IDs verify that the older binaries were actually restored.

```sh
python3 tests/integration/stage8_release.py rollback \
  --baseline baseline.json --candidate candidate.json --output rollback.json
```

GitHub evidence artifacts are retained for 30 days; binary/source archives for
14 days. Retain the qualified bundle before its artifact expiry if it is to remain
an operational rollback target. Verify `SHA256SUMS`, load the saved images,
and use the exact recorded role IDs together. Publication or deployment of those
images is a separate operation. This qualification writes no registry tags and
uses no production database, production workspace, or paid model requests.

Retained local copies live under `.release-artifacts/stage8/39081557247/`, which
is ignored by Git and excluded from Docker build contexts. Each architecture's
ZIP contains the actual six images (baseline and successor, three roles each),
both source archives, checksums and the three qualification reports. The release
record pins both the outer ZIP and inner archive digests. Keep that directory
with the deployment's retained artifacts; the checked-in report alone cannot
restore image bytes.

On the matching native Docker host, load a retained bundle without unpacking a
second large copy (`ARCH` is `amd64` or `arm64`):

```sh
unzip -p .release-artifacts/stage8/39081557247/native-ARCH.zip strict-bundles.tar.gz \
  | gzip -dc | docker load
```

The adjacent `baseline-ARCH.compose.json` and `candidate-ARCH.compose.json`
overrides (also checked into each architecture's evidence directory) pin all serving roles and the migration image by their recorded local
image IDs, with pulls disabled. For this qualified schema-compatible rollback,
drain the current worker, inspect/suspend uncertain work, stop all serving roles,
and preserve the database and every workspace/ownership volume. Start the selected
bundle with the same Compose project and base file, using `up -d --no-deps lea
worker companion`; do not rerun migration for this binary-only rollback. Verify
health, retained receipts and publication recovery before resuming admission.

The explicitly dispatched publication workflow consumes the image archives from
its successful qualification jobs. It verifies source, architecture, compiler
and historical-migration results, the exact rollback pair, and the loaded image
IDs before pushing those same bytes. It does not rebuild images after testing.
Provenance regressions reject missing coverage, unsuccessful native evidence,
retagged identical rollback binaries, backup restores and mismatched bundles.
