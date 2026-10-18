# Stage 7 queued editor and database results

The live editor now publishes immutable buffers, submits typed language commands,
and reads bounded SQL results. It never opens an LSP WebSocket or forwards raw
JSON-RPC. Monaco owns cursor, selection, undo and the local draft. The worker owns
its private Lean sessions. Application reads now have no registered LSP exception;
full API/worker process and import separation remains Stage 8.

## Protocol and ownership

Revision `0047_queued_editor` adds editor clients, content-addressed buffer bytes,
buffer heads, request slots and persisted results. This is an additive migration;
existing proof history and verification records are unaffected. Editor content has
its own short-lived store, so normal proof-blob cleanup cannot erase an active
buffer, and editor retention cannot erase canonical proof bytes.

| API | Contract |
| --- | --- |
| `GET /api/sessions/{id}/editor?path=…` | SQL published bytes, saved-file revision, observed environment identity and negotiated editor capabilities. A missing index or environment is explicit. |
| `POST /api/sessions/{id}/editor/clients` | Register a bounded, credential-bound producer for one conversation and its canonical workspace scope. The credential is returned only to that client. |
| `DELETE /api/editor/clients/current` | Retire the producer and fence its pending work through SQL; release active-client capacity when closing or switching files. |
| `POST /api/editor/buffers` | Immutable owner/path/version/content binding, exact saved-file base and environment revision. Equal content shares bytes. Conflicting or obsolete versions are rejected. |
| `POST /api/editor/requests` | Typed kind, buffer ID, position in UTF-16 units, environment revision, idempotency key, monotonic request sequence, supersession key and deadline. Returns a command receipt. |
| `GET /api/editor/requests/{id}` | SQL receipt and bounded result; output is available only for the current buffer, request slot, saved-file base and observed environment. |
| `GET /api/editor/receipts?idempotency_key=…` | Recover a lost acknowledgement without creating another request. |
| Legacy `GET …/lsp-info` | SQL initial-content alias. No workspace lookup or execution. |
| Legacy WebSocket `…/lsp` | Always rejected before accept, including local clients. The old proxy implementation is removed. |

Owner registration and publication use the existing local Host/Origin boundary.
Every buffer, submission and receipt additionally requires the editor credential.
A producer cannot use another owner's buffers or results. The generic command
endpoint rejects editor kinds; only typed intake can establish their buffer and
environment bindings. Moving a conversation
to another project or canonical loose scope invalidates its old producer. Shared
loose conversations resolve the canonical owner's SQL file and worker path;
conversation identity is not mistaken for workspace ownership.

Admission and supersession commit atomically. A newer buffer cancels old queued
or claimed requests for that owner/file. A newer request sequence cancels the
previous equivalent slot. The existing queue releases capacity once and fences
late completion. A SQL drain intent stops new editor admission and fences active
analysis; cancelling the drain permits a fresh request. Expiry, revocation and
worker-epoch loss use the same settlement
rules. Retrying a receipt cannot extend its deadline or execute twice.

Buffer/head/request writes enter the durable change feed. Receipt polling reads
one SQL snapshot, including its watermark. Stored results contain the exact buffer
identity, worker generation and observed imported-environment hash. They do not
write code steps, proof checks, approvals or certification evidence.

## Language feature cutover

The installed `lean4monaco` 1.1.14 implementation was inspected, including its
VSCode client setup and infoview RPC session/keepalive protocol. Replacing only
hover/completion would have left its infoview execution channel intact. The live
editor instead uses Monaco with explicit queue providers and a plain goal panel.

| Previously used protocol | Stage 7 behavior |
| --- | --- |
| Initialize/initialized and document open/change/close | Private worker implementation. Browser publishes immutable complete buffers. |
| `textDocument/publishDiagnostics` and `textDocument/waitForDiagnostics` | Worker waits for exact-version diagnostics and the barrier, then publishes bounded draft diagnostics. |
| `textDocument/hover` | `editor.hover`, rendered as untrusted Markdown. |
| `textDocument/completion` | `editor.complete`; at most 200 items, no executable commands or edits to other files. |
| `$/lean/plainGoal` | `editor.goals`, used by the goal panel and “Ask about goal.” |
| `$/lean/plainTermGoal` | `editor.term_goal`, available through the typed query contract. |
| Infoview `$/lean/rpc/connect`, call, keepalive and release; arbitrary widgets | Explicitly unavailable. No browser RPC session or fallback transport. |
| Definition/reference navigation, code actions, semantic tokens and other unadapted language requests | Unavailable in this cutover; no provider is registered. Capabilities enumerate the limit. |
| Lean progress/widget notifications | Not relayed to the browser. Only the newest diagnostic publications are retained privately. |

The private pool is separate from certifying solver checks. Each process uses the
existing owned-process transport and filesystem executor: no network, writes only
to its owned temporary root, and no permission to save the workspace. The worker
checks imported source, compiled artifacts and configuration under a workspace
lease before and after language execution. A changed environment retires the
private server. Unchanged exact-buffer diagnostics can be reused; a changed buffer
is sent as a new LSP document version. Unsupported executors fail explicitly.

## Editor behavior

Edits clear obsolete markers immediately and debounce analysis by 300 ms. Cursor
queries debounce by 250 ms. Diagnostic and goal responses update markers/text;
they never call `setValue`, navigate the UI or replace the local draft. Explicit
Reload uses a Monaco edit operation with undo boundaries.

The client serializes publication, keeps request identity through acknowledgement
loss, and rejects out-of-order results by local revision, current buffer and latest
request sequence. It polls SQL results and checks SQL environment/base metadata
every two seconds, including the SQL worker epoch/lease. An environment change,
worker restart, expiry or disconnected observation
invalidates prior output and schedules fresh analysis when available. Transient
failures retry after the next metadata poll once in-flight requests settle;
unsupported features and permanent scope/quota errors are not retried continuously.
HTTP attempts have a six-second timeout. Worker,
quota, expiry and supersession states are visible. Recreating an expired producer
preserves the local draft. Switching conversation/file disposes the old observer;
its late responses cannot update the new editor. Retirement uses a small keepalive
HTTP request; if disconnected, the owner lease still expires without a browser
callback.

Saving/checking remains on the existing workspace command path. Draft diagnostics
cannot certify different saved bytes, replace a current proof check, or make a
failed save appear verified.

## Bounds and retention

| Resource | Bound |
| --- | --- |
| Active editor producers | 32 globally, scoped and credential-bound; one-hour inactivity expiry. |
| Outstanding editor commands | 8 per producer, 64 globally across the existing editor lane. |
| Request deadline | At most five seconds, including queue time; UI submits 4.9 seconds. |
| Buffer | 4 MiB UTF-8; safe repository-relative `.lean` path; 8 MiB distinct retained bytes and 256 buffer records per producer. |
| Result | 256 KiB serialized, 8 MiB aggregate per producer; superseded slot payloads are cleared. Completion lists capped at 200 and marked incomplete when truncated. |
| Retained request records | 2,048 per producer, at most one hour. Exhaustion returns an explicit quota response. |
| Private language sessions | At most four, reaped after 60 seconds idle or environment failure. |
| Private diagnostics | At most two publications per active document; progress/widget notifications are discarded. The inherited transport bounds a frame at 64 MiB. |
| Retention maintenance | At most 100 terminal requests and 100 unused buffers per pass, plus 32 expired empty producers. |

Superseded unused buffer bytes can be reclaimed earlier; active requests and the
latest unexpired draft keep their bytes. An expired/reclaimed observation requires
a new buffer/request. Receipt retention is not a substitute for saving a draft.
Compute and paid-execution concurrency are unchanged.

## Qualification

The [qualification report](database-migration-baselines/stage7-qualification.json)
and [native baseline](database-migration-baselines/stage7-editor-macos-arm64.json)
record the development cutover. All 20 measured source fingerprints match the
delivery source. Test counts below overlap between broad and final focused gates.

| Gate | Result |
| --- | --- |
| Selected native adapter, schema, command, core/query and retention regressions | 143 passed; latest editor/queue gate: 31 passed. |
| Native Lean protocol and actual worker restart | Passed diagnostics, goals, term goals, hover, completion, changed draft errors, persisted-result observation while stopped, and fresh requests after restart. Canonical proof bytes/evidence remained unchanged. |
| Linux ARM64 SQL/queue/schema/boundary regressions | 108 passed; final editor gate: 21 passed. Native Lean explicitly skipped on the audit image. |
| JavaScript scripts, companion/extension and frontend | 940 passed (17 + 798 + 125). |
| Browser workflows | 13 passed; final four editor workflows passed again after producer retirement. |
| TypeScript, production frontend build and boundary audit | Passed; 179 adapter routes and 27 public companion transport branches classified. |

Twenty **different** small buffers were published and analyzed through the ASGI
HTTP routes and actual editor consumer, with native confined Lean 4.29.0.
Measurement starts before the SQL metadata lookup/publication and includes receipt
polling; it begins after the UI debounce. p50 was 658.76 ms and p95 was
838.43 ms (maximum 841.24 ms). The initial private server took
1186.02 ms for analysis, with Lake/toolchain caches already
prepared. This meets the small-file warm target; it does not qualify Mathlib
initialization, TCP throughput, large projects or production tail latency. One
loaded preliminary unchanged-buffer run exceeded one second at p95; the final
changed-buffer report supplies the agreed fixture and matching fingerprints.

The browser checks cover rapid changes, local draft/cursor/undo preservation,
late and unavailable results, idle environment changes, worker restart identity,
quota saturation/recovery, hover and completion widgets, scope changes and the
absence of an LSP socket. Unit gates additionally cover credential/scope isolation,
UTF-16 bounds, exact acknowledgement recovery, epoch/deadline fencing, drain,
retention, aggregate result quotas and typed-endpoint enforcement. A legacy
approval browser gate now waits for the SQL approval record before checking its
panel; authorization and review behavior are unchanged.

Reproduce the native fixture from the repository root (requires the supported
filesystem executor and installed Lean 4.29.0):

```sh
LEA_TEST_NATIVE_EDITOR=1 LEA_EDITOR_BASELINE_OUTPUT="$PWD/docs/database-migration-baselines/stage7-editor-macos-arm64.json" LITELLM_LOCAL_MODEL_COST_MAP=True apps/lea-standalone/adapter/.venv/bin/python -m pytest apps/lea-standalone/adapter/tests/test_editor_commands.py -k native_worker -q -s
```

The ordinary unit run skips that native-only case. Tests use temporary databases,
workspaces and scripted models where needed, without paid inference or external
Git publication. Linux runtime support, native AMD64 and full image release remain
explicit qualifications; the missing confined executor never triggers a direct
or unconfined editor fallback.

## Upgrade and rollback

Normal adapter startup migrates to 0047. Deploy the matching UI and adapter;
old browser builds receive rejection on the retired socket. No production user
database was altered during qualification. There are no authoritative editor
files or JSON ledgers to import.

Rollback selects a compliant editor build or disables live language features.
Keep SQL proof history and existing workspace journals. Reopening the WebSocket,
restoring the old proxy or enabling infoview RPC is not a rollback option. Full
process isolation, native AMD64, production-image and unsupported-executor release
qualification remain Stage 8/release work.
