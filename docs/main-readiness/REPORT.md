# VIDA main readiness — 2026-10-12

Implementation and local validation remain on `LeaCheckAddition`. The application,
proof-certification, migration, dependency and supervision checks listed below
pass. **Both actual production images now build and pass the runtime harness:**
ARM64 ran natively and AMD64 ran under emulation on this ARM64 host. The local
production-image gate is complete. Local integration guards, manual publication
controls and the proposed main enforcement procedure are now prepared and
validated. Native GitHub CI jobs have not run. Actual integration, publication of
the feature/PR and GitHub enforcement still require later authorization.

No merge or rebase of this checkout, push, publication, remote CI dispatch,
GitHub settings change or live database upgrade was performed. Normal Lea
listeners on 8001, 5173 and 31245 remain stopped. Test
servers used temporary data and ports; owned test containers were removed. Other
projects' Docker images, containers and volumes were left intact.

## Reconciliation with VIDA main

The reviewed VIDA main is `8fc9a74bb7de07b655105e46fe661eb3473482b2`. The feature
contains an earlier mirrored main snapshot at `91cc152`; its real Git merge base
with VIDA main is `15edcd1c58668847ec820d128150eba735d28c8b`. Main's changes after
the mirror were ported into the feature without changing its ancestry. The
[49-path review inventory](evidence/main-delta-review.json) records the original
port method, not a claim that subsequently edited files remain byte-identical.

The combined implementation preserves:

- Main's Portkey settings, model catalog, native Anthropic gateway routing,
  cache breakpoints, Node HTTP handling, website assets and installation flow.
- Main's container serving the adapter and Overleaf companion together, with
  complete Mathlib preparation before either backend starts.
- The feature's exact-source and dependency evidence, immutable history,
  current-status projections, recovery safeguards, usage accounting and
  Overleaf target coordination.

Provider routing falls back from native Anthropic gateway requests only on
404/405/501 before emitting events. The current token and price accounting is
preserved. Recovery compatibility fingerprints cover 17 relevant modules,
including import parsing. Reviewed older checkpoints can migrate only at a
settled turn boundary; ambiguous completed mutations still pause.

Real SafeVerify validation found another defect: Lean 4.29's `import all M`
syntax was interpreted as an import of a module named `all`. The import utility
now resolves `M`, with a regression and real kernel checks for targeted Mathlib
imports and a compiled namespaced sibling.

Raw Git merging still reports conflicts because the histories diverged before
the mirror. [The resolution manifest](merge-resolutions.json) pins the reviewed
main SHA, selected feature blob and Git mode for every affected path. The
[preview script](../../scripts/preview-main.py) uses temporary Git objects and an
index; it never merges into the checkout or updates real refs. The reviewed
candidate resolves **99 raw conflicts**, leaves no unresolved conflicts and
equals the tested feature tree. It also explicitly preserves historical feature
documentation that a raw merge would delete. Unknown conflicts, changed selected
bytes, file modes or an advanced main fail the preview. Draft recipes cannot
approve a preview. A guarded apply tool defaults to read-only preflight; its
mutation path was tested only in disposable repositories.

This prepares a reviewed resolution recipe. A future authorized merge must use
those choices and rerun the gates; it is not an automatically clean merge.

## Dependency disposition

The unused React Router dependency was removed. KaTeX was updated to 0.18.2 in
both workspaces and its extension assets were synchronized. Compatible affected
transitives were refreshed, including tar, brace-expansion, qs and pbkdf2. Vite
is 6.4.4. The static-copy plugin and its vulnerable glob/watch dependency chain
were removed; a small native plugin now serves and copies only enumerated Lean
editor assets. Tests exercise both the production URLs and actual Vite HTTP
requests for editor JavaScript and fonts.

[Production npm audit](evidence/npm-audit-production.json) reports zero findings.
[The complete audit](evidence/npm-audit-full.json) has zero critical, high or
moderate findings and six low dependency entries for one underlying advisory:
[elliptic GHSA-848j-6mx2-7j84](https://github.com/advisories/GHSA-848j-6mx2-7j84).
The chain is `vite-plugin-node-polyfills` → `node-stdlib-browser` →
`crypto-browserify` → `browserify-sign` / `create-ecdh` → `elliptic`.

At validation, the latest available elliptic release was 6.6.1, which remains
affected. npm's suggested automatic fix downgrades the polyfill plugin to 0.2.0;
that is not a compatible patch. The chain is retained to avoid an unreviewed
editor compatibility change. The application does not directly invoke its
elliptic signing or key-agreement APIs. These dependencies are marked development
dependencies but can be bundled into browser assets, so the production-only audit
does not establish that every shipped browser transitive is advisory-free.
Removing unused polyfills needs a separate compatibility review when a patch or
replacement is available.

`lean4monaco` 1.1.14 advertises Node 25 while the established runtime uses Node 22.
The local typecheck, production build, asset HTTP tests and browser tests pass on
Node 22.22.3. Both production images also pass with Node 22.23.3, including the
target-architecture companion binary. The existing approximately 8.3 MB frontend
chunk warning remains; bundle reduction is outside this plan.

## Packaging, readiness and CI

The actual Dockerfile uses the monorepo root, frozen Python dependencies, the
patched frontend assets, one shared Mathlib installation and SafeVerify. It now
copies the shared `leanCheckRuntime.mjs` helper needed by the companion. The
small image regression exposed that missing runtime import before it was fixed.
The frontend stage uses `BUILDPLATFORM` for architecture-independent browser
assets; the companion takes its Node binary from a separate target-platform
stage. This fixed a Go/esbuild crash while building the frontend under AMD64
emulation. This follows Docker's [native build-stage pattern](https://docs.docker.com/build/building/multi-platform/).
Mathlib cache fetching retries once after transient download failure, then lets
Lake compile any missing targets. A complete Mathlib build, the no-build
completeness check and the SafeVerify executable build remain mandatory. The
AMD64 retry recovered eight SSL-failed downloads and all required builds passed.
Docker build context exclusions cover private environment files, local settings,
databases, proof working directories and host dependency/build directories.

The production entrypoint supervises both backends and starts the adapter through
the bound canonical launcher. Adapter restart retains the PID and companion;
adapter shutdown or companion exit stops the whole container. Preparation failure
prevents admission. Signals during preparation stop the container cleanly. Health
requires both adapter readiness and companion health. The compose configuration
preserves database, configuration, proof, project, companion and Mathlib caches.

[Application CI](../../.github/workflows/application-ci.yml) defines stable
`application`, `real-lean`, `container-amd64`, `container-arm64` and `merge-gate`
checks. Real Lean tests reject disabled/missing compiler prerequisites. Native
AMD64 and ARM64 jobs build and run the actual production image without publishing.
The aggregate gate fails unless every job succeeds. CI installs Python 3.13 to
match the production runtime.

Both website and container publication depend on the reusable application
validation. Their local workflow definitions now accept only manual dispatch;
merging or tagging cannot publish once this version reaches main. Registry write
permission remains restricted to image publication jobs. Website build uses
read permission for Pages metadata; only deployment receives Pages and identity
write permissions. The validation jobs are read-only. All three workflow files
pass actionlint 1.7.12.
The official binary's release checksum was verified before local use. These
workflow definitions have not run on GitHub. Repository branch protection was
not changed; after authorized publication, VIDA should require `merge-gate`.

## Database preservation and rollout

The local gitignored backup is
`apps/lea-standalone/data/backups/pre-main-readiness-20261012/`. It contains a
SQLite backup, complete proof and project directories, local TOML configuration
(mode 0600) and an inventory. The live database remains at `0031_usage_details`.

[Preservation and restore verification](evidence/data-preservation.json) confirms
153 sessions, 288 runs, 4,962 timeline rows and 778 artifact blobs are unchanged.
All 645 inventoried proof/project/config files match their original hashes, both
in the live directories and the backup. A disposable database upgraded to
`0032_proof_check_evidence` retained every old column value and passed SQLite
integrity checking. It received no manufactured evidence or timeline evidence
references. Restoring the disposable database returned it to the original
revision and rows; restoring all 645 files into a disposable directory reproduced
their hashes. The live database and user proofs were not modified.
[Verification after the production-image tests](evidence/data-preservation-production.json)
again confirms the same rows, revision and all 645 file hashes.

Tests additionally cover upgrades from main's `0019_custom_tools`, the feature's
`0031_usage_details`, an empty database and an already-current database, plus
concurrent startup and failed migration rollback. Old recorded verdicts and run
outcomes remain history. Current certificates without supported evidence become
unchecked and require an explicit recheck. Startup launches no bulk revalidation
or model calls, and a manual check cannot promote an ineligible review outcome.

For a future authorized rollout:

1. Stop the adapter, companion, UI and all proof workers. Make a fresh SQLite
   backup and copy proofs, projects and local configuration; keep the inventory.
2. Run `scripts/verify-local-backup.py` on the existing backup while services are
   stopped, then repeat migration and integrity checks on a copy of the fresh
   deployment database. Never use live data as a migration fixture.
3. Start the supported launcher/image only after merge and rollout approval.
   Verify readiness, preserved timeline/history and an explicit real compiler
   recheck of an eligible target. Expect old current badges to require rechecking.
4. If rollback is needed, stop every process first. Restore the database and
   corresponding proof/project/config snapshot together using the prior code.
   Remove obsolete SQLite `-wal`, `-shm` and `-journal` sidecars only after every
   connection is closed and the backup is verified. Recheck integrity and inventory
   hashes before starting the prior version. Restore was exercised on a disposable
   copy; no live restore was performed.

## Validation evidence

Local compiler: Lean 4.29.0 on ARM64 macOS; Python 3.14.4; uv 0.11.14;
Node 22.22.3. Both Linux production images use Python 3.13.11, Node 22.23.3 and
Lean 4.29.0; the harness checks the actual Node and Lean binary architectures.

| Gate | Final result |
| --- | --- |
| Complete adapter suite | [1,021 passed](evidence/adapter-tests.log), 10 existing framework deprecation warnings; unhandled worker-thread warnings treated as errors. |
| Root JavaScript | [746 passed](evidence/javascript-summary.log): 6 script/asset, 655 companion, 85 frontend; no skips. |
| Cross-layer transport/persistence | [8 passed](evidence/integration-tests.log). |
| Built frontend in Chromium | [3 passed](evidence/browser-tests.log): stale canvas after external edit/reload, gateway settings and vendored KaTeX trust behavior. |
| Website | [23 passed](evidence/site-tests.log); build passed. |
| Standalone prover | [42 unit modules](evidence/prover-unit.json) and [3 real Lean modules](evidence/prover-lean.json) passed, covering recovery, concurrency and cascade rename. |
| Real SafeVerify | [3 isolated scenarios](evidence/safeverify.log) passed: targeted imports, compiled namespaced sibling and rejection of `sorry`, with evidence. |
| Small container, real production entrypoint | [Passed](evidence/container-supervision.log): both backends, restart, shutdown, ownership reuse, preparation failure, companion death and interruption during preparation. This uses a fake Lake preparation command, not the full production compiler image. |
| Frontend typecheck and production build | [Typecheck](evidence/typecheck.log) and [build](evidence/frontend-build.log) passed; native Lean asset serving/copy tests passed. |
| Original four audit reproductions | [Source overwrite](evidence/source-attribution.json), [dependency edit](evidence/dependency-freshness.json), [classifier](evidence/classifier.json) and [unbound restart](evidence/unbound-restart.json) now show the corrected behavior. |
| Migration, live preservation and copy restore | Passed as described above. |
| Workflow/shell/Python syntax and whitespace | Passed. |
| Local integration and CI preparation | [37 passed](evidence/local-preparation-tests.log): disposable Git integrations, default read-only preflight, stale/dirty/unknown inputs, ignored-file protection and bounded cache failure handling. |
| Actual production ARM64 image | [Build](evidence/production-build-arm64.log) and [all three runtime scenarios](evidence/production-arm64.json) passed natively. |
| Actual production AMD64 image | [Build](evidence/production-build-amd64.log) and [all three runtime scenarios](evidence/production-amd64.json) passed under emulation on ARM64. Native AMD64 CI remains unexecuted. |

The browser fixture uses the real built React application and an isolated adapter
with a stub prover. It does not establish real Overleaf-account/Chrome-extension
integration or live Lean editor behavior. The real compiler, recovery and kernel
checks run separately. The optional older SafeVerify fixture skipped a preexisting
user sibling without an olean; the new isolated fixture builds its own sibling
and supplies that coverage without changing the user's files.

Final successful runs followed fixes to the gateway/recovery integration, import
parsing, companion image layout and test fixture timing. A companion success test
uses a 10-second filesystem completion deadline rather than 3 seconds under
concurrent compiler/container load; its behavior assertions are unchanged.

## Production-image gate completed

The shared Docker VM has a 59 GB filesystem. The earlier ARM64 build exhausted
it, as recorded in the [original blocker log](evidence/production-build-blocker.log).
The user freed space; the next check showed about 33 GB available, enough to finish
both full images. Only this task's superseded ARM64 image and exact unshared cache
descendants were removed during the builds. No other project's Docker data was
selected for cleanup.

Both final images were built from the actual Dockerfile. The evidence records
their image IDs, Dockerfile digest, binary versions, build results and runtime
assertions. To reproduce from the monorepo root:

```bash
docker build --platform linux/arm64 -t lea-readiness:arm64 -f apps/lea-standalone/Dockerfile .
python3 tests/integration/production_container.py --image lea-readiness:arm64 --output /tmp/lea-production-arm64.json
docker build --platform linux/amd64 -t lea-readiness:amd64 -f apps/lea-standalone/Dockerfile .
python3 tests/integration/production_container.py --image lea-readiness:amd64 --output /tmp/lea-production-amd64.json
```

Each image passed upgrades from `0019_custom_tools` and `0031_usage_details`,
preserving old successful history while leaving evidence-less certification
unchecked. Both backends and the SPA worked. Real Lean and SafeVerify passed;
editing an imported helper invalidated certification and its failed rebuild
could not fall back to the old olean. An external target overwrite invalidated
freshness and returned HTTP 409 `proof_revision_changed`, retaining the immutable
checked source. Saving the replacement created a new revision whose invalid
proof failed Lean. Restoring and rechecking recovered freshness. Restart changed
runtime identity and resumed readiness; shutdown exited with code 0. A third
scenario removed `Mathlib.olean` in a disposable layer and required cache repair
before admission.

The production harness needed three fixture corrections: the mounted seed script
explicitly imports the adapter; historical timestamps sort before current edits;
and host edits are confirmed visible through Docker Desktop's mount before the
single freshness assertion. The external-edit case now expects the existing
revision-conflict guard and uses an explicit save to check replacement bytes.
These changes did not alter application or prover behavior. The affected gates
were rerun against both final images. The earlier complete application suites
remain the validation for unchanged application code.

[Final cleanup](evidence/production-cleanup.json) removed both final test images,
all owned test containers and the 34 new unshared cache entries descending from
their Mathlib layers. All 22 baseline image inventory records and 216 baseline
cache IDs remain present; other projects' volumes were not touched. Docker has
about 25 GiB free. All 62 remaining Docker containers are stopped, the normal Lea
ports have no listeners, and no owned test or compiler workers remain.

Remote CI was not dispatched and nothing was published. CI defines native AMD64
and ARM64 runners; local emulation does not establish their results. The reviewed
[merge preview](evidence/merge-preview-production.json) still resolves 99 conflicts
with no candidate changes beyond the tested feature tree. A future authorized
merge must use that recipe, rerun the required CI gates and confirm main has not
advanced.

## Local preparation completed

Commit `fc899f9` contains the guarded integration tools, version 2 resolution
manifest, manual publication controls, scoped permissions and bounded CI cache
retry. [The preparation runbook](LOCAL-PREPARATION.md) documents the exact
preflight commands and proposed main protection settings. It contains no remote
settings mutation. Main protection and effective rules were absent at the last
read-only planning check; enforcement is still pending.

[Recipe review](evidence/local-preparation-recipe-review.json) confirms all 133
path choices remain, now with 131 regular and two executable file modes. Only the
two publication workflow blob selections changed. [The committed-code preview](evidence/merge-preview-local-preparation.json)
resolves all 99 raw conflicts and equals that feature tree.
[Read-only preflight evidence](evidence/local-preparation-readonly.json) confirms
the live remote main matched the pinned SHA and the real refs, index and worktree
were unchanged; no merge was started. The apply path rejects ignored local path
collisions explicitly before calling Git, rather than relying solely on Git's
overwrite flag. Its mutation tests used only disposable repositories.

[Validation details](evidence/local-preparation.json) record 37 passing tests,
workflow lint and syntax checks. Runtime application code, the Dockerfile and
proof behavior did not change during this preparation. The earlier full suites
and production runtime results remain evidence for that behavior; they were not
rerun for these preparation-only changes. Native CI must rebuild and validate
the exact eventual candidate, including the new scripts.

[Data verification](evidence/data-preservation-local-preparation.json) again
confirms the unchanged live revision, old rows and all 645 inventoried files,
with migration and restore exercised only on copies. [Cleanup](evidence/local-preparation-cleanup.json)
confirms no normal Lea listeners, running Docker containers or owned test/compiler
workers. This step did not create containers or remove Docker data.

Remaining merge gates, each requiring later authorization:

1. Refresh main and regenerate the preview from the exact clean feature HEAD;
   review any newly advanced main changes. Integrate the approved main locally
   and inspect/commit its pending merge. The real checkout still has 99 raw
   conflicts because ancestry has not been integrated.
2. Push the reviewed feature, open the PR and obtain review. Run the application,
   real Lean and native AMD64/ARM64 GitHub checks on the exact candidate. Confirm
   the emitted aggregate `merge-gate` check context before requiring it.
3. Apply and verify main protection: required PR, at least one approving review,
   dismissal of stale approvals and an up-to-date required aggregate check.

Merging the PR, releasing images/site and live migration/rechecks remain later
decisions. Committed preview evidence binds `fc899f9`; the later documentation
commit changes HEAD, so generate a fresh preview before any future apply.

## Local delivery

- `aa87313`: previous proof-evidence and lifecycle fixes already committed at the
  start of this implementation.
- `e148001`: reconciled gateway support, recovery fingerprints and import parsing.
- `91c4cad`: patched web dependencies and enumerated editor asset plugin.
- `f7993d6`: production packaging, supervisor and owned container/kernel harnesses.
- `8f07778`: reconciled main's website and installation assets.
- `814d79b`: application/compiler/container publication gates, migration and
  browser checks, backup verification and the pinned merge-resolution recipe.

`b01afb7` recorded the initial readiness report and storage blocker. `8e818d7`
contains the tested cross-platform packaging and production-harness fixes. A
further local commit, `4c6d195`, records both completed image checks. `fc899f9`
adds the local preparation above, followed by its evidence/report commit. No
normal services were started as part of delivery.
