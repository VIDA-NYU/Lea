# Implementation plan for the October 2026 audit fixes

Address all seven findings in [the system audit](AUDIT-2026-10-07.md), together
with the integration-fixture drift and worker teardown warnings that weaken
validation. Deliver six reviewable changes, each with regression coverage, then
run the full validation gate. All six implementation changes are now applied
and validated. Existing-data discrepancies await explicit revision selection
before rollout; deployment and the external npm advisory scan remain separate.

The proof-check race and filesystem-ledger drift share a revision-aware proof
file service. Project deletion needs an exclusive lifecycle operation separate
from the namespace migration guard. The network and redaction fixes are smaller
and can land before that shared proof work.

## Implementation sequence

| Change | Findings | Result | Status |
| --- | --- | --- | --- |
| 1 | Test gaps | Trustworthy integration fixtures and fully drained test workers. | Implemented and tested |
| 2 | F3, F4, F6 | Redirects blocked, companion Host validated, public result details redacted. | Implemented and tested |
| 3 | F7 | Busy projects cannot be deleted; run creation and deletion cannot race. | Implemented and tested |
| 4 | F1 | Check evidence is committed only to the revision actually checked. | Implemented and tested |
| 5 | F2 | Tracked Lean filesystem edits enter the canonical proof ledger. | Implemented and tested |
| 6 | F5 | Only responses belonging to the current UI selection can update it. | Implemented and tested |

Changes 2 and 6 do not depend on the proof service. Change 5 builds on change 4;
change 3 supplies the lifecycle coordination needed by that service. Each change
should include its own targeted tests. Final validation follows all six changes.

Preserve the existing in-process prover boundary and shared adapter on port 8001.
Continue using the current SQL blobs and timeline as the proof-content record,
with disk files as the compiler working copy and Git for project transport/assets.
Session/formalization validity remains derived from evidence. Incorporate the
existing working-tree changes, including `project_coordination.py`, without
discarding or replacing them.

## Change 1 Repair the validation baseline

**Files:** `tests/integration/adapter_entry.py`,
`tests/integration/formalize.e2e.test.mjs`, and the adapter tests that start driver
or dispatcher threads, especially `adapter/tests/test_bridge.py`.

Give the successful fake Finished event an explicit `result_kind="proved"` and
keep the declaration, written bytes, and successful CheckResult consistent.
Include an explicit `needs_review` scenario so the harness verifies the
conservative completion contract rather than weakening it to accept every result.

Replace the retry assertions about legacy retirement/restoration commits with
assertions about current SQL snapshots, retained working files, and source
identity. A failed retry with changed source must leave the earlier proof
available as history while the current target reports stale. Cover an unchanged
source retry separately; do not broadly rewrite all failures as expected output.

Track the worker threads created by each test fixture. Signal and join them, stop
fixture-owned dispatchers, and drain brokers before restoring the database path
or deleting its temporary directory. A worker that cannot terminate within the
bounded teardown must fail the test. Avoid sleeps as synchronization.

**Acceptance:** The checked-in integration suite passes without temporary fixture
copies, and the adapter suite emits no unhandled worker-thread exceptions. Run
the affected teardown tests with `PytestUnhandledThreadExceptionWarning` promoted
to an error.

## Change 2 Close network and result-detail boundaries

### F3 Block HTTP tool redirects

**Files:** `prover/lea/http_tools.py`; add focused standalone module tests under
the prover's existing `tests/` layout and retain adapter custom-tools coverage.

Use an explicit urllib opener with redirect following disabled. A 3xx response
becomes a bounded tool error telling the user to configure the final HTTPS
endpoint. Keep the initial call-time HTTPS/public-address validation and current
timeout behavior. Do not introduce redirect support as part of this fix.

This choice stops both the private-address redirect and cross-origin credential
forwarding without maintaining a second set of redirect/header rules. Tools that
currently rely on a redirect will need their configured URL updated.

**Regression tests:** Use the real redirect machinery with fake transports.
Public-to-localhost, public-to-private, HTTPS-to-HTTP, and cross-origin public
redirects must never invoke a second transport. Assert that the dummy credential
is sent only to the original approved endpoint. Cover an ordinary 200 response
and verify that response errors do not expose credential values.

**Acceptance:** The audit redirect reproduction makes no request to its redirected
destination, and direct public HTTPS tools still work.

### F4 Validate the companion Host before routing

**Files:** `overleaf-extension/companion/server.mjs`; add a small
`companion/netguard.mjs` helper and tests in the companion suite.

Parse Host as an authority, with bracketed IPv6 and optional port support. Accept
the adapter's documented local host identities and explicit `LEA_ALLOWED_HOSTS`
exceptions from the companion's supplied environment. Compare exact normalized
hostnames/authorities rather than suffixes. Reject malformed Host values.

Apply the guard before OPTIONS handling, CORS grants, or route dispatch. Preserve
the companion's existing Overleaf and extension Origin policy; its legitimate
origins differ from the adapter's. Do not trust forwarded-host headers. Keep the
adapter's missing-Host policy for non-browser tooling, since real HTTP/1.1 browser
requests include Host.

**Regression tests:** Originless export/status requests with an attacker hostname
return 403 before any adapter fetch or side effect. Cover localhost, IPv4,
bracketed IPv6, ports, malformed values, and configured exceptions. Also verify
that valid Overleaf/extension origins with a local Host remain accepted and that
a valid Origin does not override an invalid Host.

**Acceptance:** The audit companion reproduction returns 403 and its fake export
transport is never called.

### F6 Sanitize normal completion details as well as exceptions

**Files:** `prover/lea/agent.py`, `adapter/app/bridge.py`; extract the adapter's
existing redaction helper into `adapter/app/public_errors.py` if needed by public
serializers. Extend classifier and bridge tests.

Make classifier provider failures return an actionable message containing the
error type rather than the raw exception text. Keep its conservative
`needs_review` result. The vendored prover must not import the adapter.

Sanitize all terminal result details before storing or emitting them, including
Finished events and synthesized/replayed terminal responses. Use the same
sanitized value for SQLite and SSE. Cover configured credentials and known active
provider environment keys as well as the existing credential-shaped patterns;
preserve bounded, useful non-secret diagnostics. Apply the helper at public read
boundaries for previously stored result details without bulk-rewriting history.

**Regression tests:** Classifier failure and a normal Finished event containing a
dummy credential leave no credential in the persisted detail, live done event,
replayed done event, or public run/session detail. Include a non-pattern-shaped
configured secret, missing config, and an ordinary error message.

**Acceptance:** Both secret flags in the audit redaction reproduction are false;
the result remains `needs_review` and still explains how to retry.

## Change 3 Make project deletion exclusive

### F7 Reject deletion until all project work has finished

**Files:** `adapter/app/project_coordination.py`, `routes/projects.py`,
`routes/runs.py`, `projects.py`, and the bridge/registry admission seam.

Add an exclusive deletion operation to project coordination. While it is held,
new mutations and run creation/resume/continue cannot enter that project. Resolve
and validate the project/session again after entering the guard so a request
that started before deletion cannot use an obsolete identity. Preserve the
existing rename semantics and use a deletion/busy error rather than labeling
deletion as a rename.

Reject deletion with HTTP 409 `project_busy` when there are pending/running runs,
active imports, in-flight file/check operations, or admitted workers still doing
final cleanup. Use a project worker lease from dispatcher admission through
cleanup and registry release; a terminal database status alone is not sufficient
to establish that the worker has stopped. Include child work owned by the parent
worker lifetime. The DELETE endpoint does not implicitly cancel runs.

Perform the busy check and deletion under the exclusive operation. Invalidate
the deleted identity so an already-resolved request cannot recreate its old
working directory. Release the operation reliably on both success and error.
Keep unrelated projects available. As defense in depth, the worker stop callback
should stop when its persisted run/project disappears unexpectedly.

**Regression tests:** Running, pending, importing, checking, and terminal-but-still-
cleaning-up projects reject deletion without losing rows or files. Barrier tests
race deletion against run creation and dispatch in both orders. A successful
delete prevents the losing request from writing or admitting work. Idle deletion
and deletion in an unrelated project still succeed.

**Acceptance:** The audit active-run deletion returns 409, and deterministic race
tests cannot produce an admitted worker for a deleted project.

## Change 4 Bind proof checks to immutable revisions

### F1 Persist only evidence for the checked bytes

**Files:** `adapter/app/routes/sessions.py`, `store.py`; add a small
`adapter/app/proof_files.py` service. Use the existing prover check evidence in
`prover/lea/lean_checks.py` and `CheckResult.execution`.

Introduce a pinned check reference containing the normalized path, owning
project/session, canonical timeline step ID, blob ID/SHA, captured bytes, and
applicable snapshot/namespace revision. A row ID alone is insufficient:
`upsert_user_code_step` can change a row's blob during auto-save.

The prover already reports `content_sha256` and rejects in-check file drift.
Close the remaining adapter gap between check completion and persistence:

1. Capture the current record and disk content under a short per-file guard.
   Return a revision conflict for unexplained disk/ledger differences; recovery
   of a known failed materialization can use its committed revision. Do not
   overwrite unexplained disk changes or attach evidence to a different file.
2. Run the compiler without holding a global project mutex or SQLite write
   transaction. Its execution hash must match the pinned bytes.
3. Re-enter the file guard, compare current disk and canonical revision, then
   conditionally persist the verdict in a single SQLite write transaction.
   Require the expected blob and canonical head, not only the row ID.
4. If anything changed, return 409 `proof_revision_changed`, with no verdict
   applied to the newer step. Clients refresh before checking again. Do not
   retry indefinitely during continuous auto-save.

For cascade/author-attributed checks that create a new timeline entry, use the
captured checked content rather than rereading the file after compilation. A
failed check also belongs only to its checked revision. Normalize missing or
mismatched check hashes conservatively, and update test stubs to carry realistic
execution evidence. Confirm ordinary, cascade, and any other mutable-step verdict
writers use the same persistence rule.

**Regression tests:** Pause after the compiler has produced its result but before
the adapter persists it. Then exercise a new edit row, a coalesced edit retaining
the same row ID, an edit from another session sharing the path, and a cascade
check. Neither successful nor failing old results may attach to replacement
bytes. A no-change control must still persist its verdict. Cover namespace
changes and materialization failure without global serialization.

**Acceptance:** The audit race cannot mark replacement bytes `ok`; current-revision
checks continue to persist normally. Retain the real Lean daemon recovery tests.

## Change 5 Unify tracked Lean file edits

### F2 Keep filesystem saves and the canonical ledger consistent

**Files:** `adapter/app/proof_files.py`, `routes/projects.py`, `routes/sessions.py`,
`store.py`, `formalizations.py`; adjust `FilesystemTab.tsx` and API error handling
only where the new revision response requires it.

Use the shared service for canvas writes and project filesystem edits of tracked
Lean paths. Normalize paths identically and resolve the canonical record plus
every formalization linked to that project/path. Record one user edit for the
file; existing cross-session path resolution should make it current for all
linked targets, without duplicate independent byte records.

For a filesystem edit, attribute the timeline entry to the canonical record's
project session and add a note identifying the filesystem edit. Resolve ambiguity
from the canonical path record rather than guessing by declaration name.
Untracked Lean source and non-Lean assets retain their existing project-file
behavior; when a source is adopted as a formalization, it must enter the ledger.

Clear previous check/artifact evidence and invalidate verification for every
affected target/session. Preserve checked or verified user steps as history:
coalesce only unchecked, unverified user-edit rows. This prevents an old
verification event from appearing current merely because an edited row kept the
same ID. Do not erase historical verification evidence or set stored derived
session statuses.

A no-op save is valid only when both ledger and disk match the submitted bytes.
If the ledger matches but disk differs or is missing, materialize the requested
revision and record the repair conservatively as unchecked. The compiler verdict
then uses change 4's pinned persistence path.

Commit SQL content as the authoritative revision and materialize the working file
with a staged file and atomic replacement. If materialization or the filesystem
route's Git commit fails, retain the recorded revision as unchecked, return a
structured error with its current revision, and support reconciliation/retry.
Never return success while the two disagree. Do not claim that a filesystem
operation and a SQLite commit form one atomic transaction.

Reject manual tracked-Lean edits during active project runs/imports so another
session cannot bypass the existing editor lock and overwrite agent work. This is
a conservative project-level edit restriction, while model worker concurrency
and unrelated asset edits retain their current behavior. Keep long compiler and
model calls outside the short file/DB critical sections.

**Regression tests:** Filesystem edits of a proved target change the canonical
snapshot and validity; a failing check cannot leave the new bytes proved. Cover
two formalizations sharing a file, an edit after SafeVerify, restoration of a
drifted/missing disk file, auto-save coalescing before and after verification,
write/Git failures, an active run in another project session, and unchanged asset
edits. Exported working bytes must match the successfully saved current snapshot.

**Acceptance:** Both filesystem and canvas editors agree with the current proof
record; the audit filesystem reproduction cannot retain a proved label for the
broken replacement, and saving the original bytes restores disk.

## Change 6 Scope asynchronous UI work to the current selection

### F5 Ignore responses from an obsolete session or formalization context

**Files:** `src/app/hooks/useProofStream.ts`, `App.tsx`, `lib/api.ts`; add a small
selection-generation helper if needed by the Node frontend tests.

Create one selection generation that advances on intentional navigation, new
session creation, and component teardown. Capture it before asynchronous work.
Require the same generation and intended session/formalization context before
applying any detail, stream event, result, local-storage update, or retry.

Separate deliberate session loading from reconciliation of the already-selected
session. A requested load may establish selection; a background reconciliation
may only refresh its current context. Avoid using `closeStream` alone to advance
selection generation, because it is also called during legitimate reattachment.

Abort obsolete fetches where supported, but retain generation checks since an
abort cannot retract a response already resolved. Use a separate stream identity
for reconnect/resync callbacks so events from a replaced stream are ignored even
when the session ID is unchanged.

Apply these guards to `loadSession`, reconcile, reconnect, resync, save-and-check,
SafeVerify, and their error/finally branches. A backend save/check for a previous
session can finish; its UI updates must not replace the newly selected context.
Only preserve completed run status when it belongs to the same session/run, and
never schedule reconnect from an obsolete rejection.

**Regression tests:** Deferred promises for A then B, A then B then A, reconnect
success/failure after navigation, resync from an old stream, and old check/verify
results must not change current selection, content, diagnostics, busy flags, or
local storage. Include a control where the response belongs to the current
generation and must apply. Exercise the actual hook, not only the helper.

**Acceptance:** The audit UI reproduction stays on B after A resolves, and current
session streams still reconnect and reconcile normally.

## Final validation and delivery

Convert the saved audit harness cases into tests that assert the corrected
behavior. Confirm each regression fails against its pre-fix implementation, then
passes with the fix. Use barriers and deferred promises rather than timing sleeps.

Run the adapter pytest suite, root `npm test`, UI typecheck/build, site tests, the
checked-in end-to-end integration suite, affected standalone prover modules, and
the real Lean recovery tests in a disposable project. Run the thread-leak tests
with warnings treated as failures. Socket/process/DNS restrictions must be
distinguished from implementation failures, as in the audit.

Perform a local two-editor smoke test: edit through Filesystem, inspect the proof
canvas, check it, switch conversations during a slow check, reload, and export.
Exercise Overleaf save/check and normal companion requests with permitted Host
and Origin values. Use synthetic declarations and dummy credentials; these
acceptance tests do not need a paid model run or a GitHub write.

Before rollout against existing data, report current tracked-file disk/ledger
mismatches without modifying either copy. Resolve them through an explicit save
of the chosen revision, then recheck current snapshots previously labeled proved.
Fixing future attribution does not validate evidence recorded before the fix.
Preserve history and theorem statements when a recheck fails.

Update the audit report with each finding's fix and regression evidence. Keep
responses and SSE result vocabulary compatible except for the explicit revision
conflicts and busy-project rejection above. No schema migration is expected for
this design; existing blob identities, revision tokens, and verification
references provide the required evidence.

The npm dependency advisory scan remains a separate pending audit item. Its
previous automatic approval rejection concerned sending dependency metadata to
an external service; this plan does not authorize that transmission or depend on
it for any of the seven fixes.

## Execution record

Completed all seven fixes, repaired stale integration expectations, and drained
background workers before test fixtures are restored. Added revision guards to
manual SafeVerify as well as Lean checks, because both could certify a coalesced
replacement. No schema migration or theorem-statement change was needed.

Validation and per-finding coverage are recorded in
[the updated audit](AUDIT-2026-10-07.md#implementation-status): 943 backend tests,
630 companion tests, 82 frontend tests, 8 cross-layer integration tests, 23 site
tests, 16 affected prover modules, 5 real Lean recovery tests, typecheck/build,
and the isolated two-editor browser/API smoke all passed.

Read-only rollout review found 8 disk/history discrepancies among 103 current
paths. Both formalizations currently labeled proved passed recompilation across
all 8 linked snapshots. The remaining discrepancies and revision-selection
steps are in [the existing-data review](audit-2026-10-07/existing-data-review.md).
Historical files were not bulk-restored or erased, and new checks were not
written into the existing database. The external dependency advisory scan
remains pending its separate metadata-transmission authorization.
