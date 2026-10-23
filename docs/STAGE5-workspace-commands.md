# Stage 5 workspace commands and recoverable edits

The local adapter now admits workspace operations through SQL and executes them
in worker consumers. File saves, Lean checks, rebuilds, SafeVerify, review
publication, dependency actions, namespace changes, artifact retirement/restore,
attachments and project deletion return durable receipts. The original guarded
domain implementations remain private worker callbacks; HTTP callables enter
the database admission wrapper before any workspace/environment guard.

This is the Stage 5 development cutover. Published Overleaf source, import/mirror
transport, peripheral reads and companion cache authority retain their Stage 6
scope. Editor LSP remains Stage 7; separate processes and filesystem confinement
remain Stage 8. Core query enrollment remains opt-in.

## Schema and authority

Revision `0043_workspace_commands` adds:

| Record | Responsibility |
| --- | --- |
| `workspace_requests` | Typed action, existing project/session/formalization scope and durable result reference. |
| `workspace_file_revisions` | Immutable submitted bytes, original base, draft identity and command linkage. |
| `workspace_files` | Current SQL content binding, encoding and materialization/provenance outcome. |
| `workspace_effects` | Publication intent, retained before-images, installation intent and recovery/conflict state. |
| `workspace_blob_refs` | Keep accepted arguments, attachment bytes and referenced result content alive through existing blob collection. |

The existing `artifact_blobs`, timeline, formalization/check/review records,
application journals and command attempts remain authoritative. Proof edits add
run-less user steps and edit notes. They do not introduce another proof history.
Each accepted draft remains immutable even when a newer draft arrives. Large
arguments and results use blobs; receipt code content references existing blobs
rather than duplicating proof strings in the command envelope.

Workspace binding/effect changes increment core input revisions. Enrolled reads
report applying/conflicted state while publication is unresolved; a cached check
cannot remain current across a workspace effect. Proof, attachment and non-proof
content stay available in SQL after Git failure. Binary attachments have an
explicit base64 encoding; text content reads never present them as source text.

## Admission and API contract

The fixed internal `workspace.execute` handler uses the existing local
compatibility producer and compute capacity. Its action registry validates the
same request models as the existing endpoints; callers cannot select an executor,
claim, producer or handler policy. Generic credentialed `/api/commands` still
rejects these internal execution references. This retains the existing local
Host/Origin security boundary, without claiming a new multi-tenant identity model.

`GET /api/health` advertises `workspace_version: 1`, the local typed translator
and mandatory edit bases on the versioned interface.

```text
POST /api/workspace/commands                  -> 202, command + draft identity
GET  /api/workspace/commands/{command_id}     -> SQL receipt + domain result
GET  /api/workspace/receipts?idempotency_key= -> original accepted receipt
GET  /api/workspace/file?session_id=&path=    -> SQL content/base/materialization
```

The file query also accepts `project_id`. It performs no filesystem reads,
compilation or live-worker lookup. Terminal duplicate submissions return the
original domain result, including its original failure status.

Example versioned edit:

```json
{
  "version": 1,
  "action": "session.save",
  "idempotency_key": "original-save-request",
  "draft_id": "local-draft-17",
  "expected_revision": "proof:123:immutable-blob-id",
  "arguments": {
    "session_id": "existing-session",
    "request": {"path": "Target.lean", "content": "theorem target : True := by trivial\n"}
  }
}
```

An edit's explicit expected base is mandatory here. A `missing` base requires an
absent destination; it cannot overwrite an existing unobserved file. Proof bases
include the current timeline/blob identity; other managed files use their SQL
revision or the current observed binding. Checks also capture the SQL file base
at admission and reject a changed base before execution.

Legacy workspace endpoints now return HTTP 202 receipts. When an old editor omits
its base, the translator captures a SQL base. Updated project/document editors
send the base from the view that began their draft. A legacy filesystem view of
an untracked text file supplies a content fingerprint, checked by the worker
before first adoption. This compatibility path does not make that peripheral
GET database-only; its read cutover remains Stage 6. The versioned API's SQL-only
file query reports unobserved content explicitly.

Admission validates known SQL ownership reservations before accepting canonical
edits. It repeats scope/revision/ownership checks at execution/publication.
Reservation failure rolls back the command, blobs and draft together. Previously
accepted idempotency keys remain observable even if the scope subsequently changes.

## Worker families and scheduling

Registered actions cover:

- Session save, combined edit/check/cascade, Lean check, rebuild, verification
  and manual compaction.
- Project creation/update, instructions/memory/blueprint saves and generation,
  general file edits, target synchronization, identity/default name, Git remote
  and push, artifact retirement/restore, upload/delete and project deletion.
- Formalization creation/update and revision-bound review decisions.
- Dependency ensure, retry, worker activation and continuation.

Two workspace consumers operate independently of solver and control consumers.
SQL reservations permit one workspace command per project or loose session.
Pending workspace effects and claimed workspace work defer solver dispatch in
that scope. Unrelated scopes can proceed. An internal dependency activation
retains its existing lineage and becomes executable after its submitting
workspace command settles. Source preparation follows the same scope fence.

The existing 128 global / 32 producer / 16 project compute caps and separate
64-control reserve remain unchanged. Inputs remain bounded: 64 KiB command
envelopes, 4 MiB edit content, 25 MiB attachments, 32 MiB large arguments/cascade
inputs, and 64 MiB / 10,000-file recovery snapshots. These are development
policies, not production throughput claims.

Idle consumers reuse worker-local read-only autocommit connections, close each
cursor and hold no read transaction between polls. Real claims repeat all checks
inside the writer transaction. Workspace polls use 200 ms intervals; heartbeat
and controls remain independent. A drain racing a claim returns unstarted work to
the queue without reserving capacity twice or leaving an ownerless live task.

## Publication, external edits and recovery

A proof save commits its immutable bytes, timeline step, note, current binding,
before-image and intent together. Checks become unavailable before file
installation. Installation retains the displaced destination and uses a
create-only link for the new file. A racing external destination wins; its bytes
and the submitted draft are retained as a conflict. No Git reset is used.

Namespace, generated-title, artifact and attachment mutations retain bounded
before-images in their operation journal. Their managed replacements/removals
check the exact captured destination, retain displaced files, and publish current
bindings only for their recorded intended bytes. Retirement publishes a deleted
binding; restoration publishes the restored binding while preserving history.
Recovery archives are excluded from later namespace rewrites and snapshots.

Git and compilers run outside SQL write transactions. Git failure after content
publication records `provenance_pending` and retains the edit. Default-name
adoption reserves its SQL name first, then updates/commits generated docs outside
the writer. A test enters another write transaction during Git recording to
qualify this boundary.

Project deletion first moves its repository to `.lea-trash/{command_id}`, then
deletes its SQL domain records. Startup restores that directory if SQL deletion
did not commit; a committed deletion retains its recovery copy. Tombstones are
conservatively retained. An automated purge/retention policy and broad disk-size
qualification remain release work; this cutover does not recursively erase the
only recoverable copy inside a request.

The startup data lock and process reaper precede reconciliation. The old host
epoch is invalidated before application recovery. SQL-owned file intent can be
materialized without a compiler/model call. Work interrupted before intent can
be requeued with the same receipt/draft. A completed mechanical save or committed
deletion can be recognized without rerunning its domain operation.

Uncertain multi-file work becomes an explicit conflict with retained before-images.
It is not automatically replayed. Reconcile such a project with a schema-compatible
operator workflow before admitting more writes; automatic resolution is not
advertised. Accepted cancellation and receipt reads remain available. A file
changed by an external editor during recovery is retained instead of overwritten.

Both staged helper publication and reviewed-candidate acceptance retain the
existing preservation services and their command fences. Losing a workspace
claim cannot accept a reviewed publication. Auxiliary compaction records actual
usage even if ownership is lost, while new dispatch and transcript publication
check current ownership. There is no automatic retry of uncertain paid work.

## Manual edits and clients

The combined `session.edit` operation owns save/check/rebuild/transitive cascade
ordering. It records affected dependents in SQL. A failed source build cannot be
overridden by a warm LSP result; unavailable checks get an unverified head.
An affected file without a recorded editing context is reported as `unknown`,
with no check attributed to an unrelated session or companion job.
The companion requests this operation and projects its results. Its legacy
fallback orchestration and cache/status authority remain named Stage 6 work.

A failed Lean verdict can be an operationally successful command:
`command.state = succeeded`, `command.result.status = error`. Verification,
diagnostics and checks retain their existing exact-byte/environment semantics.

The shared client observer reads durable receipts and never cancels work when
observation is lost. A lost submission response is reconciled using the original
idempotency key and exact original request. HTTP rejections are not blindly
retried. Temporary receipt-read failures can be retried across API restarts.

Canvas/live editor, project file and Markdown editors retain newer local edits
when an older save completes. Their draft base stays pinned across background
refresh. A conflict keeps the draft open and exposes the current revision; it
does not reload over the draft or change the user's selection.
Initial chat restoration also gates submission: a message typed during a linked
chat's load cannot become an unrelated new chat with another approval policy.

## Qualification and rollback

Development qualification completed on 2026-10-17.

Tests use temporary databases/projects, scripted providers and owned disposable
subprocesses. No paid model or user project is exercised. These gates overlap;
their case counts are not an aggregate coverage total.

| Gate | Result |
| --- | --- |
| macOS ARM64 broad migration, domain, recovery and native publication gate | 401 passed |
| macOS ARM64 queue, solver, migration, observation and default-name gate | 108 passed |
| Final macOS workspace and read-performance gate | 56 passed |
| Linux ARM64 domain/projection/queue gate, read-only current-source mount | 216 passed |
| Linux ARM64 queue, solver, migration, observation and default-name gate | 108 passed |
| Final Linux ARM64 workspace gate, including unowned dependent reporting | 38 passed |
| JavaScript scripts, companion/extension and frontend suites | 933 passed (17 / 798 / 118) |
| Isolated actual HTTP wire, edit, restart/replay, projection and solver cases | 7 passed |
| Chromium smoke, model, selection and approval-policy regressions | 9 passed |

TypeScript checking, the production frontend build, and the boundary register
audit also pass. The register covers 156 adapter routes and 53 companion dispatch
branches; passing it means every boundary has a classification, including the
remaining exceptions. It does not certify database-only access for Stage 6–8.
The build retains its existing bundle-size advisory. The native gates use Python
3.14; the disposable Linux ARM64 image uses Python 3.13 and installs Git/test
dependencies before testing. It is not the full published production image.

Real SIGKILL cases cover before intent, after intent, after displacement, after
installation, after publication, during Git recording, during namespace rewrite,
and on both sides of SQL deletion. Other tests cover stale/duplicate submissions,
capacity rollback, external edits, receipt loss/replay, exact check outcomes,
default-name transaction boundaries, and retired/restored current bindings.

The [final HTTP baseline](./database-migration-baselines/stage5-http-macos-arm64.json)
records matching fingerprints for 25 source files. Eight saves measured 39.4 ms
p95 durable acceptance / 618.5 ms observed completion; eight scripted checks
measured 43.7 / 874.9 ms. Cancellation acceptance measured 79.2 ms p95. Final SQL
content and observer disconnect/reconnect assertions passed. These small scripted
samples do not measure paid/native compiler service or control-consumption time;
the queue limits stay unchanged. See the [baseline methodology and remaining
runtime limits](./database-migration-baselines/README.md#stage-5-workspace-command-http-baseline).

The complete adapter/companion harness reports 11 passed / 2 failed. Its two
changed-source retry/ledger failures were previously reproduced on unchanged HEAD
and are documented in [Stage 1](./STAGE1-durable-observation.md).
Native AMD64/full-image release qualification and final process confinement remain
open; Linux ARM64 tests do not establish those guarantees.

Reproduce the principal Stage 5 checks from the repository root:

```sh
(cd apps/lea-standalone/adapter && LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest tests/test_workspace_commands.py tests/test_commands.py tests/test_solver_commands.py tests/test_migrations.py tests/test_observation.py tests/test_overleaf_project_names.py -q)
npm test
npm run typecheck -w apps/lea-standalone
npm run build -w apps/lea-standalone
npm run test:browser
node --test --test-name-pattern='adapter wire contract|Overleaf manual edits|completed run replay|expired replay|session-list snapshot|enrolled core HTTP|durable solver commands' tests/integration/formalize.e2e.test.mjs
python3 scripts/audit-ui-boundaries.py
git diff --check
```

Rollback disables new compute admission with `LEA_COMMAND_ADMISSION_ENABLED=0`,
drains or explicitly suspends owned work, then reconciles journals with a compatible
release. Keep revision 0043 and accepted receipts/drafts. Never restore an older
database or re-enable direct file-write handlers over accepted commands.
