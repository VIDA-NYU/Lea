# Proof certification and lifecycle fixes — 2026-10-11

The four defects in [the original audit](AUDIT-2026-10-10.md) are resolved on
`LeaCheckAddition`. Changes remain in the feature branch's working tree. This
work did not merge branches, upgrade advisory dependencies, rewrite user proofs,
or make paid model calls. Live services remained stopped; validation used isolated
processes, databases and containers.

## Resolved findings

| Finding | Implemented behavior | Corrected reproduction |
| --- | --- | --- |
| F1: a check certifies replacement bytes | Writers and checkers capture exact source; cold checks use stdin; serial meaning events precede the next mutation. Immutable evidence attaches only to the matching blob. Agent and adapter completion revalidate current source and environment. | [Agent output](audit-2026-10-11/agent-revision.json): the overwritten proof records `error`; its formalization is `failing`, and its run does not finish `proved`. |
| F2: changed imports retain certification | Lean resolves imports; the manifest traverses local and package dependencies, hashes source and compiled artifacts, and pins compiler/configuration inputs. Lake prepares requested imports, then invalidates the warm daemon when necessary. Every current projection derives freshness from that evidence. | [Dependency output](audit-2026-10-11/dependency-revision.json): the dependent becomes `unchecked` immediately after the upstream edit; `check_current=false`; actual Lean rejects it. |
| F3: negation parses as success | Trimmed, case-normalized exact lookup accepts only `PROVED`, `DISPROVED`, `NEEDS_REVIEW`. Invalid output becomes review without another classifier call. | [Classifier output](audit-2026-10-11/classifier.json): negation and prose become `needs_review`. |
| F4: launcher control wedges admission | All supported launch paths use bound `DrainingServer` through import-safe `run_api.py`. Restart preserves arguments/environment. Readiness separates admission from liveness. Unbound control returns 409 before draining. | [CLI output](audit-2026-10-11/cli-restart.json): control returns `restart_unavailable`; readiness and admission remain open. [Container output](audit-2026-10-11/container-lifecycle.log): restart returns ready with a new runtime identity; shutdown exits 0 and releases data ownership. |

## Evidence and recovery

Migration `0032_proof_check_evidence` adds append-only check records with immutable
blob references and versioned compiler evidence, timeline evidence references,
SafeVerify blob/environment evidence, and project-library audit evidence. Verdicts
and references persist together. Garbage collection retains blobs referenced by
checks or SafeVerify records; project deletion removes their scoped records.

Recorded `check_status` stays historical. `check_current` and
`check_stale_reason` are derived on read, with no stored duplicate certification
status. Missing evidence, source changes, import changes and environment changes
cannot retain a current successful certificate. Fresh dependency-build failures
produce failing status; old compiled imports cannot rescue the result. Unrelated
proof edits preserve existing valid checks.

Invocation journals and checkpoints preserve captured source and structured
evidence. Legacy tool strings cannot establish current success. Cached terminal
classifications require current verification before replay. Existing ambiguous
mutation recovery still pauses instead of replaying completed writes. Reviewed
checkpoint compatibility vectors include the compiler/evidence and LSP modules.

An explicit recheck can restore an eligible old successful certification, while
retaining the existing target-completion rules. Review outcomes cannot be promoted
by a manual check. A different artifact cannot revive an old disproof. Namespace
migrations require fresh evidence at the transformed source and destination.

Environment leases coordinate compiler reads with builds and managed writes.
Manual persistence follows environment → file → database lock order. Waiting
respects cancellation/deadlines. A regression exposed a late-observer recovery
race; observers can no longer count the same failed recovery episode repeatedly.

## Migration and backup

The durable local backup is
`apps/lea-standalone/data/backups/pre-proof-evidence-20261011/`: it contains the
SQLite backup, complete proof working directories and the pre-migration inventory.
It is local, gitignored data.

The migration ran against a disposable copy. Every existing column value remained
identical in 153 sessions, 288 runs, 4,962 timeline entries and 778 proof blobs;
SQLite integrity checking returned `ok`. No check evidence or timeline evidence
references were backfilled. [Migration verification](audit-2026-10-11/migration.json).

The live database remains at `0031_usage_details`; starting the updated adapter
will apply the additive migration. Existing evidence-less current certifications
become unchecked until explicitly rechecked. Migration does not infer fingerprints
from today's files or start a bulk revalidation/model job. Recorded history and
run outcomes remain available.

Final [data-preservation checks](audit-2026-10-11/local-data-preservation.json)
confirmed that all 641 backed-up proof-directory files and all rows in the four
checked live tables remained unchanged. Final [cleanup checks](audit-2026-10-11/cleanup.json)
found no test containers, Lean workers or Lea listeners on ports 8001, 5173 and 31245.

## Validation

The initial regression run reproduced six failures across eleven source,
dependency and classifier cases. The unbound lifecycle regression also failed
before the launcher fix. Tests now cover real cold/warm source attribution,
transitive imports, previously unregistered/Unicode modules, implicit imports,
unchanged warm environments, source deletion, mismatched paths/hashes, delayed
and cross-session writes, classification-time edits, legacy data, checkpoint
replay, target completion, child promotion, current projections and lifecycle
control.

| Check | Result |
| --- | --- |
| Complete adapter pytest suite | [1,008 passed](audit-2026-10-11/adapter-tests.log); worker-thread exceptions treated as errors. |
| Root JavaScript suites | 741 passed: 4 launcher tests, 652 companion tests, 85 frontend tests. |
| Cross-layer adapter/companion integration | [8 passed](audit-2026-10-11/integration-tests.log). |
| Standalone prover modules | [42 modules passed](audit-2026-10-11/prover-modules.json); [full log](audit-2026-10-11/prover-tests.log) has no unhandled worker-thread exceptions. Optional SafeVerify grader integrations remain opt-in. |
| Real Lean recovery | [5 passed](audit-2026-10-11/real-lean-recovery.log); no healthy-check fallbacks. |
| Real Lean shared-server concurrency | [Passed](audit-2026-10-11/real-lean-concurrency.log): both two-thread and four-child cases, correct file attribution, sibling-safe document cleanup. Cold Mathlib loading is explicit setup; concurrent checks retain their deadlines. |
| Real Lean cascade rename | [Passed](audit-2026-10-11/real-lean-cascade.log). |
| Real-process launcher/readiness tests | [8 passed](audit-2026-10-11/process-lifecycle.log), including 503 readiness with `Retry-After` during draining and preserved liveness. |
| Production container entrypoint | [Passed](audit-2026-10-11/container-lifecycle.log) using the reproducible [small test-image harness](../tests/integration/container_lifecycle.py) and temporary data/config volumes. Test containers removed. |
| Frontend typecheck and production build | [Typecheck](audit-2026-10-11/frontend-typecheck.log) and [build](audit-2026-10-11/frontend-build.log) passed. Existing bundle-size warning remains outside this change. |
| Diff whitespace validation | Passed. |

The adapter suite retains existing framework deprecation warnings. Dependency
advisories, merge-conflict resolution, bundle size and historical proof repair
remain outside this implementation.

## Main-readiness follow-up — 2026-10-12

The original fixes above were committed in `aa87313`. The subsequent authorized
main-readiness work reconciles VIDA main's gateway and two-backend container
changes, patches the scoped JavaScript advisories, adds application publication
gates and validates upgrade/restore behavior. It also fixes dependency discovery
for Lean's `import all` syntax, found by real SafeVerify checks. The four original
reproductions still demonstrate corrected behavior after reconciliation.

See the [main-readiness report](main-readiness/REPORT.md) for final test evidence,
the reviewed conflict-resolution recipe, dependency disposition and migration
instructions. After the user freed Docker space, both actual production images
built and passed all runtime scenarios: ARM64 natively and AMD64 under emulation.
The checks cover legacy-schema history, real Lean/SafeVerify, stale source and
imports, restart/shutdown and incomplete-cache repair. Native remote CI has not
run. The work remains on `LeaCheckAddition`, services remain stopped, and nothing
was merged, pushed or published. This addendum preserves the historical results
above; the main-readiness report records the completed production gate, fixture
corrections, cross-platform packaging fix and targeted Docker cleanup.
