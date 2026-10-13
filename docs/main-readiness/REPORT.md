# VIDA main readiness — 2026-10-12

VIDA main `8fc9a74` is now integrated locally into `LeaCheckAddition` by merge
commit `043f6d4`. The reviewed merge preserved the exact feature tree, and the
post-integration preview has zero conflicts. Application, compiler, browser,
migration and supervision validation passes. Follow-up commit `3ff5bfe` fixes
test compiler selection, a premature test wait and application CI prerequisites;
application runtime behavior did not change.

The earlier actual production images passed their runtime harness: ARM64
natively and AMD64 under emulation on this ARM64 host. Those image results remain
historical evidence for unchanged runtime code. Native GitHub CI on the exact
candidate, feature/PR publication, review and main enforcement remain pending.

Only the feature branch received the authorized local main integration. The
local `main` branch and other real refs were preserved. No push, publication,
remote CI dispatch, GitHub settings change or live database upgrade was performed. Normal Lea
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

Before integration, raw Git merging reported conflicts because the histories
diverged before the mirror. [The resolution manifest](merge-resolutions.json) pins the reviewed
main SHA, selected feature blob and Git mode for every affected path. The
[preview script](../../scripts/preview-main.py) uses temporary Git objects and an
index; it never merges into the checkout or updates real refs. The reviewed
pre-integration candidate resolved **99 raw conflicts**, left no unresolved
conflicts and equaled the tested feature tree. It also preserved historical feature
documentation that a raw merge would delete. Unknown conflicts, changed selected
bytes, file modes or an advanced main fail the preview. Draft recipes cannot
approve a preview. A guarded apply tool defaults to read-only preflight; its
mutation path was initially tested in disposable repositories before use on this
checkout under the later explicit integration authorization.

The authorized local integration applied those choices, inspected the staged
tree and committed both parents. Main is now an ancestor of the feature, and a
fresh [post-integration preview](evidence/local-integration/validated-preview.json)
has zero conflicts or additional candidate changes. A later advance of main
requires reviewing that new delta and refreshing the recipe before integration.

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
match the production runtime. The application job now prepares the pinned Lean
compiler and SafeVerify before its full suites, as the separate real-Lean job
does. The shared preparation script exports the repository's toolchain through
`GITHUB_ENV` for subsequent standalone checks. Compiler fixture tests verify that
selection even when the parent environment specifies an incorrect toolchain.

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
| Complete adapter suite | [1,021 passed](evidence/local-integration/adapter-tests.log), 10 existing framework deprecation warnings; unhandled worker-thread warnings treated as errors. |
| Root JavaScript | [746 passed](evidence/local-integration/javascript-tests.log): 6 script/asset, 655 companion, 85 frontend; no skips. |
| Cross-layer transport/persistence | [8 passed](evidence/local-integration/integration-tests.log). |
| Built frontend in Chromium | [3 passed](evidence/local-integration/browser-tests.log): stale canvas after external edit/reload, gateway settings and vendored KaTeX trust behavior. |
| Website | [23 passed](evidence/local-integration/site-tests.log); build passed. |
| Standalone prover | [42 unit modules](evidence/local-integration/prover-unit.json) and [3 real Lean modules](evidence/local-integration/prover-lean.json) passed, covering recovery, concurrency and cascade rename. |
| Focused real certification checks | [24 passed](evidence/local-integration/certification-tests.log), including cold/warm source capture, imports, warm reuse, classification-time changes and both P1 reproductions. |
| Real SafeVerify | [3 isolated scenarios](evidence/local-integration/safeverify.log) passed: targeted imports, compiled namespaced sibling and rejection of `sorry`, with evidence. |
| Small container, real production entrypoint | [Passed](evidence/local-integration/container-lifecycle.log): both backends, restart, shutdown, ownership reuse, preparation failure, companion death and interruption during preparation. This uses a fake Lake preparation command, not the full production compiler image. |
| Frontend typecheck and production build | [Typecheck](evidence/local-integration/typecheck.log) and [build](evidence/local-integration/frontend-build.log) passed; native Lean asset serving/copy tests passed. |
| Original four audit reproductions | [Source overwrite](evidence/source-attribution.json), [dependency edit](evidence/dependency-freshness.json), [classifier](evidence/classifier.json) and [unbound restart](evidence/unbound-restart.json) now show the corrected behavior. |
| Migration, live preservation and copy restore | Passed as described above. |
| Workflow/shell/Python syntax and whitespace | Passed. |
| Local integration and CI preparation | [38 passed](evidence/local-integration/local-preparation-tests.log): disposable Git integrations, stale/dirty/unknown inputs, ignored-file protection, bounded cache failure handling and pinned CI toolchain export. |
| Actual production ARM64 image, prior gate | [Build](evidence/production-build-arm64.log) and [all three runtime scenarios](evidence/production-arm64.json) passed natively before this ancestry integration. |
| Actual production AMD64 image, prior gate | [Build](evidence/production-build-amd64.log) and [all three runtime scenarios](evidence/production-amd64.json) passed under emulation before this ancestry integration. Native AMD64 CI remains unexecuted. |

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
[merge preview](evidence/merge-preview-production.json) resolved 99 conflicts
with no candidate changes beyond the tested feature tree before the authorized
ancestry integration described below. Native exact-candidate CI still needs to
run after feature publication is authorized.

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

That local preparation was followed by the authorized integration below. The
remaining merge gates, each requiring later authorization, are:

1. Push the reviewed feature, open the PR and obtain review. Run the application,
   real Lean and native AMD64/ARM64 GitHub checks on the exact candidate. Confirm
   the emitted aggregate `merge-gate` check context before requiring it.
2. Apply and verify main protection: required PR, at least one approving review,
   dismissal of stale approvals and an up-to-date required aggregate check.

Merging the PR, releasing images/site and live migration/rechecks remain later
decisions. The earlier preparation snapshot binds `fc899f9` and remains
historical evidence. Fresh snapshots are required whenever HEAD or main changes.

## Authorized local integration completed

[Merge inspection](evidence/local-integration/merge-inspection.json) verified the
actual index matched the approved tree and contained no unresolved paths.
[Merge commit evidence](evidence/local-integration/merge-commit.json) records
`043f6d4` with parents `5517d77` and VIDA main `8fc9a74`. Its tree remained
`201a75e4fdee57f121f8fc8a5daaff69eca5f1dc`; main's changes had already been ported
before the ancestry merge. The local main branch and all other refs stayed at
their original values.

Validation initially exposed [test setup problems](evidence/local-integration/initial-validation-findings.json).
Temporary companion projects selected a floating `stable` Lean toolchain, whose
version lookup stalled eight cases. The isolated failing case passed with the
installed pinned toolchain. The fixtures now copy the repository toolchain,
and application CI provisions the compiler prerequisites before its suites.
A subsequent run caught a retry test observing `finishedAt` before asynchronous
finalization assigned its terminal status. It now waits for completion and a
terminal status, retaining the assertions on paused/failed outcomes and partial
proof preservation. These corrections are committed in `3ff5bfe`. Only the two
affected existing fixture selections changed in the reviewed resolution manifest;
all 133 choices and file modes remain reviewed.

[Integrated candidate evidence](evidence/local-integration/validated-candidate.json)
binds that code commit and confirms current VIDA main is an ancestor, no raw
conflicts remain, and read-only preflight preserved refs/index/worktree. The full
adapter, JavaScript, cross-layer, browser and preparation suites pass; all real
Lean modules, SafeVerify scenarios and focused certification checks pass. The
[validation summary](evidence/local-integration/validation.json) records their
scope. No application/prover runtime code or Dockerfile changed in this step.
Both full production images were validated previously; they were not rebuilt
for the ancestry merge and test/CI setup changes. Native exact-candidate CI is
still a required later gate.

[Final preservation](evidence/local-integration/data-preservation-after.json)
again verifies all 645 original files and the original database rows/revision,
with migration and restore exercised only on copies. [Cleanup](evidence/local-integration/cleanup.json)
confirms the proof/project file inventory is unchanged, reserved compiler fixture
paths are removed, no normal listeners or owned workers remain, and the small
test image/containers are gone. All 21 distinct baseline Docker image IDs remain.
Normal services remain stopped, and nothing was pushed, published or changed on
GitHub. This later report commit changes HEAD; regenerate a fresh preview before
any future integration or exact-candidate gate.

## Follow-up: startup with an existing project library

A user-started `start-dev.sh` exposed a slow startup path on October 12. Before
opening its listener, the adapter refreshed all project-library environments,
invoking Lean dependency resolution for every saved module. The original process
was still scanning imports after eight minutes with five projects and 105 indexed
modules. The previous empty-library startup tests did not cover this dataset.

Startup now performs a source inventory without compiler dependency resolution.
Unchanged records retain their historical verdicts and evidence; new or changed
sources remain unchecked. Current certification still uses the stored source and
dependency evidence, so an imported source, compiled artifact, configuration or
file deletion cannot retain a current certificate. Unrelated edits preserve it.
Compiler resolution remains part of normal checks and library validation. Startup
also logs its cleanup, database, recovery and inventory stages.

The four initial regression cases failed before the fix. The affected startup,
real-process lifecycle, project-library and real Lean evidence suites then passed
47 tests; the expanded startup suite passed all eight cases, including dependency
and configuration edits. Worker-thread exceptions were treated as errors. A new
canonical-launcher test inventories 130 modules with tripwire compiler binaries,
asserts readiness without invoking them, and checks clean shutdown.

A disposable copy of the user's actual database and proof directories became
ready in 1.665 seconds with five projects and 105 indexed modules, without invoking
Lean. Recorded proof history remained identical in the copy and live database,
all 641 inventoried live proof files remained identical, and the test process
exited zero and was removed. The copy used the existing `0032` schema observed
after the user's launch; this probe did not migrate or reset live data. These are
focused follow-up results; the earlier complete validation remains historical.
The probe did not restart the original user-started stack. After confirming no
pending or running proof jobs, that stuck launch was stopped. Its startup handler
did not finish after a graceful interruption, so the adapter and its verified
compiler children were terminated; the supervisor cleaned up the frontend and
companion. Recorded proof history remained identical. The normal stack remains
stopped for the user's next launch with the fix.

## Follow-up: Overleaf settings stalled on usage

The next user launch reached readiness, but the Overleaf settings popover retained
placeholders and a pending save. Adapter settings, health and model requirements
returned promptly; companion `/settings` waited on `/api/stats`. A disposable
database-copy traceback identified `formalizations.decorate` calling dependency
reconciliation, which refreshed every project module and traversed Lean imports.

Formalization status reads now project dependency readiness from recorded module
evidence without refreshing or compiling the library. Filesystem freshness still
applies, and explicit dependency coordination keeps its existing validation.
A regression verifies both current and externally changed published modules while
rejecting any refresh/build during formalization and stats reads. It failed before
the fix. A read of the user's copied 153-session database now completes in 1.357
seconds, renders its usual 100-session page, invokes neither refresh nor validation,
and leaves the copied database byte-for-byte unchanged.

Companion metadata requests now have five-second deadlines covering headers and
response bodies. The popover loads saved settings before the optional model
catalog, preserves edits when that catalog arrives, and uses separate usage-error
feedback. Loading or unavailable settings cannot be saved; failures display cached
values, unknown credential indicators and an explicit retry. Saves preserve drafts
on failure and explain uncertainty after a timeout. Clearing a saved spending cap
also takes precedence over an older Chrome cache. No new provider calls or retries
were added.

The three initial popover regressions failed before implementation. Validation
passed 751 root JavaScript tests and 121 affected adapter tests; the final popover
suite passed all 101 cases after the last UI adjustment. Remaining warnings are
framework deprecations and Node's existing VM-loader warning, with no unhandled
worker exceptions. A temporary browser fixture using the real content script
confirmed populated model/turn/key state and a visible, save-disabled retry state
at the existing 360-pixel panel width, without horizontal overflow. Its data were
disposable; the user's open Overleaf tab and pending draft were not changed.

The already-running user stack still needs to load the new code. Preserve any
draft settings, reload the unpacked extension and refresh Overleaf to discard the
old pending save before restarting the stack. Test processes are cleaned up; this
follow-up does not start a replacement development stack or publish changes.

## Follow-up: Lean pane inventory timeouts during Formalize all

The user reported two inventory-refresh timeouts that replaced the entire pane
during a running Formalize all batch. The manifest requested the adapter's
dependency inventory, whose GET routes still called full library reconciliation
and could resolve/build Lean modules or wait for the active compiler environment.
The extension also cleared its existing tree on refresh errors and stopped its
in-progress poll until another interaction triggered a refresh.

Dependency GETs now project readiness from immutable recorded evidence and
filesystem freshness, without refresh/build work or environment leases. Explicit
dependency operations retain validation. Their companion reads have bounded
five-second deadlines for headers and response bodies; proof operations retain
their own deadlines. Edited/deleted sources still lose current readiness, and
historical dependency outcomes remain stored unchanged.

Inventory failures retain the loaded DOM, expanded rows, code, edit drafts and
scroll position. An accessible warning identifies the last loaded inventory and
potentially outdated statuses. Read retries back off from five to thirty seconds,
pause while hidden, recover on visibility and stop when the pane closes. Personal
approval is disabled until fresh inventory arrives. Superseded failures, project
navigation and view switches cannot overwrite a newer display. Import-preview
callers still receive failures rather than treating cached inventory as a
successful refresh. These retries do not start proof/model jobs.

The initial two dependency-read and four pane-failure regressions failed before
implementation. Final validation passed **764 root JavaScript tests** (6 script,
673 Overleaf and 85 frontend), **51 affected adapter tests**, including real Lean
evidence cases, and **8 cross-layer integration tests**. The pane suite includes
timeouts, HTTP/network failures, draft/DOM preservation, retry backoff, hidden and
closed panes, approval suspension, late failures and Blueprint view switches.
Dependency-read regressions hold an exclusive environment lease while reading,
reject compiler calls, and exercise changed/deleted sources. No unhandled worker
exceptions appeared. The root suite required permission for isolated localhost
test listeners; normal service ports and the user's stack were left untouched.

A disposable copy of the user's current database returned the project's one
dependency in **1.318 seconds**, without compiler work or changes to the copied
database. A temporary browser fixture using the actual content script confirmed
the real ten-second timeout preserves expanded content, shows the warning,
recovers successfully, and fits both 520- and 360-pixel pane widths without
horizontal overflow. Its warning used the existing theme tokens and legible
foreground/background colors. The fixture contained only example data.

All isolated processes/tabs were cleaned up. The user-started adapter/companion
were not stopped or restarted, and live proof/history/settings were not modified.
The active batch can finish before activation: reload the unpacked extension,
refresh Overleaf, and restart the development stack without `--fresh`. Changes
remain local on `LeaCheckAddition`; nothing was merged, pushed or published.

## Follow-up: remove repeated proof work from Overleaf reads (2026-10-12)

The loading slowdown came from repeated current-evidence projections on metadata
paths. The saved database contains 158 sessions, 109 library modules, and seven
proof-check records with manifests containing 5,940–12,961 dependency entries.
Profiling the accounting endpoint found 87,315 hash lookups and roughly 175,000
filesystem stats per response, including duplicate projections of the same proof.
Its warm execution took 3.626 seconds under the profiler; the companion settings
response hit the five-second usage deadline. Project identity also projected full
session validity just to answer whether recorded proof history existed.

The fix separates historical existence and accounting from certification:

- Project identity uses a project-scoped SQL existence query, including all
  recorded nonscratch proof steps. It does not project proof validity.
- `/api/stats?include_proof_status=false` returns complete accounting rows without
  proof blobs or certification status. The companion uses this projection. The
  normal Stats response retains its current certification fields. Cost totals,
  pending/unconfirmed usage, provider accounting details and batch-report charges
  remain authoritative; project usage no longer inherits the 100-session limit.
- Current-status projections share decoded evidence, environment validation and
  file observations within one synchronous response. Every recorded dependency
  hash and identity is still checked. A final identity scan includes source files,
  missing configuration candidates and nearer Lake-root candidates. Changes cause
  one fresh projection; continuous changes return a retryable 503. Positive
  certification decisions are never retained for a later response. Compiler
  checking, final completion and evidence persistence use independent validation.
- Hashes and file identities come from the same observation. A file changed
  during an uncached hash read cannot populate the reusable content-hash cache.
- Library freshness is checked once per module, rather than once per declaration.
  Target-status and nested formalization/dependency reads share the response's
  observation set.
- Each inventory or badge response fetches target evidence in one batch, including
  linked formalization IDs and alternate names. Namespace revision guards remain
  in place. Action paths continue to fetch their own evidence. Metadata deadlines
  cover target-status and Lean-runtime reads as well as usage/settings.
- Checkpoint compatibility pins the reviewed checker source hash and permits the
  preceding runtime only at the existing settled boundary. Evidence schemas and
  checkpoint payloads have not changed; legacy evidence still requires rechecking
  and ambiguous mutations cannot be replayed automatically.

Initial isolated validation used the current project's saved Overleaf `main.tex`, a
SQLite backup, copied companion state, and temporary adapter/companion ports.
Live proof/config/database writes, external requests and compiler/model work
were blocked during metadata requests. Five inventory responses and four rounds
of overlapping inventory, badge, settings and usage requests retained all five
items with their existing defined, paused, proved, disproved and missing outcomes.
The final loaded run measured warm inventory responses at **1.06–1.68 seconds**,
badges at **0.83–1.51 seconds**, settings at **22–200 milliseconds**, and usage at
**100–190 milliseconds**. The first inventory took **2.93 seconds** while separate
compiler and regression suites were running. All requests succeeded; there were
exactly nine target-status requests for the five inventories and four badge
responses, with no per-item duplicate requests or unexpected adapter errors.
The later investigation below found that copied jobs retained their live adapter
URLs: the probe's isolation guard rejected their Lea Status/alignment requests.
These initial timings therefore cover an incomplete inventory path and must not
be used as end-to-end performance evidence. The corrected probe rewrites copied
job URLs and requires every per-item assessment request to complete.

Four currently checked project files, including the shared dependency, were also
checked from an APFS-cloned disposable copy of the entire Lean workspace. All
eight cold/warm checks succeeded, retained their exact source bytes and produced
fresh source/dependency evidence. Cold checks took 29–52 seconds and warm checks
6–12 seconds on this Mathlib-sized project under concurrent test load. Those are
compiler operations; metadata reads do not invoke them. The disposable workspace
and its Lean workers were removed after validation.

The final metadata probe verified byte-for-byte preservation of the live SQLite
database, proof files, configuration and root `.env`, including 4,996 timeline
rows, 785 immutable blobs, seven check-evidence records and 293 recorded runs.
The existing companion continued its normal `jobs.json` bookkeeping. An existing
integration-harness gap initially wrote seven new temporary test logs to the
default companion log directory. Only those seven logs were removed. Job logs now
follow the state's configured jobs directory, and the integration harness uses
its own environment file and asserts that logs remain inside its disposable home.
The user-started adapter and companion were not stopped or restarted. No model
calls, proof rewrites, migrations, merges, pushes or publication were performed.

Regression coverage includes repeated Mathlib-sized evidence projections, changes
during a read, source/import deletion and replacement, configuration and Lake-root
changes, continuous-change rejection, fresh observations on subsequent polls,
large-history accounting, batched inventories and namespace guards. Final
validation passed **1,045 adapter tests**, **765 root JavaScript tests** (6 script,
674 Overleaf and 85 frontend), **8 cross-layer integration tests**, frontend
typecheck and the production frontend build. Standalone interface check/rebuild,
rebuild-tool, 31 recovery unit tests and five disposable real-Lean recovery,
concurrency and warm-import regressions passed. The real-Lean recovery suite
reported a warm median of 359 ms and p95 of 492 ms on its small fixtures, with zero
healthy fallback checks. No unhandled worker-thread exceptions appeared. The
existing frontend bundle-size and Python deprecation warnings remain.

These initial changes were subsequently activated locally with data-preserving
restarts. The additional inventory regression fixes and complete-path validation
are recorded below. No new production container image was created or published.

## Persistent inventory refresh regression follow-up

The user's persistent “Refresh delayed” warning exposed three remaining causes:

- Formalization inventory synchronization was classified as a source/environment
  mutation. It waited for the exclusive compiler lease, although it only updates
  database target/source-binding/import metadata. A timed-out HTTP client could
  leave that worker waiting behind ongoing Lean checks or dependency builds.
  This route now retains its project mutation reservation, including namespace
  migration exclusion, while explicitly opting out of the compiler environment
  lease. Actual proof saves, imports, renames and compiler mutations retain their
  existing environment coordination.
- Lea Status and alignment artifact snapshots independently projected the same
  imported artifacts. The corrected complete inventory probe reproduced
  **11–13 second** responses, beyond the extension's ten-second deadline. The
  outer artifact snapshot now owns one read-projection scope, so nested source
  and dependency checks reuse file observations within that snapshot. Every new
  snapshot revalidates independently, and the final change scan remains in place.
  Profiling one target reduced a warm snapshot from 2.324 to 0.829 seconds;
  before the fix its nested scopes made 43,956 file observations. Synchronization,
  Lea Status and alignment metadata clients now also have bounded five-second
  deadlines covering headers and response bodies. Proof-action deadlines remain
  unchanged.
- Ninety-one historical usage-pending jobs and two blocked dependency workflows
  from other projects repeatedly broadcast global inventory refreshes. Nine
  refresh events were observed in fifteen seconds. Usage backoff/counters remain
  durable, but unchanged accounting retries no longer send refresh events. Actual
  accounting/outcome changes notify the affected project. Dependency polls still
  request fresh readiness on every attempt; unchanged progress is quiet, while
  changed progress and continuation notify the affected project. Legacy changes
  lacking a project identity retain a global notification fallback.

Regression tests failed before these fixes and now cover shared/exclusive
compiler leases, retained proof-save locking and namespace reservations, shared
artifact observations with fresh source/import checks on later reads, metadata
header/body deadlines, durable silent accounting backoff, project-scoped changed
accounting, and blocked/unavailable dependency polls that remain fresh without
repeated refresh events. Existing continuation/cancellation tests also pass.

The final corrected isolated probe used a SQLite backup, copied companion state
and the current saved `main.tex` from Overleaf project
`6acd52f285e713c960581c6d` (FeatureAgTest4). It blocked writes to live data and
proof/config files, compiler/model calls and external network requests. Copied
job URLs pointed exclusively to the isolated adapter. Under each of a held
shared check lease and a held exclusive build lease, five inventories and four
rounds of overlapping badge/settings/usage requests succeeded. Across both
leases, all **ten inventories** included all five items and completed in
**3.93–5.05 seconds**, with **50 Lea Status comparisons**, **50 alignment reads**,
18 batched target-status reads, no assessment failures and no unexpected
requests. Forced historical usage retries produced only the SSE `hello` event
for the current project. Both isolated process groups exited and copied
databases/configuration/companion state were removed. Live proof history,
database bytes and root environment were unchanged by the final probe; the
normal companion continued its own `jobs.json` bookkeeping.

After activation, **five consecutive live inventory requests** completed in
**3.86–4.04 seconds**, with all five items and no diagnostics. A separate
twenty-second current-project event observation contained only `hello`. The
existing Overleaf tab showed no delayed-refresh warning and retained **four
verified items and one verified disproof**, with all five Lean checks current.
No document edits, proof rewrites or model calls were made during this follow-up.

Final validation passed **1,051 adapter tests**, **772 root JavaScript tests**
(6 script, 681 Overleaf and 85 frontend), and **8 cross-layer integration tests**.
There were no unhandled worker-thread exceptions. The adapter suite retains ten
existing Python deprecation warnings. `git diff --check` passed. This follow-up
does not replace the real-Lean and frontend build evidence recorded above.

The adapter and companion were restarted only at an idle boundary, under the
user's existing request to restore local Overleaf testing, without `--fresh`.
Backups were taken before each restart; the final backup is
`apps/lea-standalone/data/backups/inventory-lock-restart-20261013T005638Z`.
Post-restart comparisons preserved **159 sessions, 5,007 timeline rows, 788
immutable blobs, 10 proof-check evidence records, 295 runs, 690 proof working
files and all existing job IDs**. The adapter is ready, the companion is running
and Vite was not restarted. Work remains on **LeaCheckAddition**; nothing was
merged, pushed or published, and GitHub settings were not changed.

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
adds the local preparation above, followed by evidence/report commit `5517d77`.
`043f6d4` integrates VIDA main locally; `3ff5bfe` contains the tested validation
setup fixes. No normal services were started as part of delivery.
