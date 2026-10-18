# Stage 6 source publication and database UI cutover

Stage 6 moves source authority, companion orchestration records and peripheral
application views into the database. The optional companion is now a stateless
transport. Lea's private business worker owns prompt construction, target
resolution, dependency/batch/repair orchestration and generated markdown; its
workspace effects enter the existing typed command queue.

This record describes the development cutover. Direct editor/LSP interaction is
the registered Stage 7 exception. Separating every execution service into an OS
confined process, a full production-image release and native AMD64 qualification
remain Stage 8/release work.

## Source authority

`POST /api/overleaf/publishers` establishes the document's tab identity and
90-second authority lease. `POST /api/overleaf/publications` publishes immutable
versioned source with generation, monotonically increasing sequence, target,
coverage, bundle hash, proof hash and archive inventory. Repeating an exact
sequence returns its original receipt. Reusing it with different input, claiming
another live tab's authority or submitting an obsolete generation is rejected.
After expiry, a takeover must name the current generation. A reloaded tab learns
that generation from a conflict response; this never overrides a live lease.

The extension serializes capture and publication, renews while connected and
keeps one request identity across a lost acknowledgement. Complete archives verify
the sorted path/content digest, file count and supplied excerpts. Partial coverage
remains explicit. Archive references avoid transmitting and storing the same
source tree for each target. Identical source publications renew the lease without
copying their bytes again.

`POST /api/overleaf/publications/submit` commits publication and solver admission
in one database transaction. Publication requires no workspace reservation, Git,
compiler or browser callback, including while a project is blocked. The accepted
worker later materializes the exact bound archive with retained before-images
and a recoverable workspace journal. External divergence becomes a conflict;
provenance failure preserves SQL content.

New solver, continuation, review and repair operations resolve SQL publications.
Business commands retain their exact source references. An accepted binding may
outlive its browser lease, but a changed head, generation or complete archive
cannot silently rebase it. Missing source asks the user to publish it. The old
prepare alias is a SQL lookup; pending/respond callbacks and browser polling are
retired. Historical accepted runs retain their source history.

## Companion migration and ownership

Revisions 0044–0046 add source publications/heads/archives, migration receipts and
issues, normalized companion jobs/associations, versioned compatibility records,
peripheral observations, SQL download artifacts and persistent namespace
generations. Existing runs, sessions, proof blobs, checks, reviews and
reformalization operations retain their identities and authority.

Stop the legacy companion process when upgrading the stack. Startup imports
legacy jobs, chat sessions, generated targets, batches and
non-secret preferences before serving. It records path, digest, format version,
record count and private archive path. Logs have bounded redacted tails. Log
archives contain those tails; the original full logs remain in the legacy
directory. Legacy project registry markdown is imported separately with its digest. Imports are
restartable; post-freeze changes produce issues instead of overwriting SQL.
Missing/conflicting document, target or domain IDs remain diagnostic history.
Malformed references and non-finite legacy JSON become retained migration issues.
A label such as `formalized` cannot create a proof check, approval or current
verification. Unfinished legacy
batches remain paused for explicit source review.

The importer writes `.lea-db-migration.json` only after processing the ledger.
Original files remain, and raw digest-addressed archives are immutable private
files beside the database. Do not delete the old directory after seeing the new
UI. First inspect `GET /api/companion/imports`, compare record counts and digests,
verify archive readability, resolve association issues and retain a database
backup. These archives contain original data and require the same protection as
credentials and proof history.

The public `companion/server.mjs` imports neither workspace/JSON stores nor the
business engine. It has no job timers or run supervisor. It forwards SQL reads,
source publication, commands, receipt recovery and the database change feed.
Running a second transport does not create another executor. The adapter starts
its private Node worker lazily; a random bridge secret, live worker epoch,
accepted parent command and source bindings fence execution. Child commands keep
the parent project and cannot extend its deadline. Receipt replay remains stable
when the first run resolves its project after initial acceptance. The worker's JSON
interfaces are compatibility codecs backed by SQL, rather than a second ledger.

## Peripheral surfaces and settings

Application GETs use SQL for file trees/content, project documents, attachments,
exports, library/compiler inventories, graphs, catalogs, assignments, model
requirements, runtime observations and settings. Cold views return an explicit
unavailable response; expired observations expose their age. Public handlers do
not repair a cache, run a compiler or contact a provider. Core projections are
automatically enrolled; new, changed and incomplete observations are refreshed
ahead of routine scans, with priority also given to views nearing expiry. Their
current claims remain revision-fenced and expire conservatively. Namespace identity
now uses a SQL generation, including ABA renames and process restarts.

Refreshes, installations, imports, exports, Git publication, report synthesis and
workspace/configuration effects enter regulated commands. Queued commands retain
existing authorization and human-review requirements. An owned local bare Git
repository is used for publication qualification; tests never push a user's
remote. Downloads read bounded SQL bytes after preparation.

Non-secret TOML policy is imported once with provenance. Credentials and bootstrap
connections remain infrastructure. Credential intake uses private 0600 tickets;
commands contain opaque references and SQL views expose only redacted status.
The worker consumes the ticket without copying secrets into command blobs.
Extension storage contains connection and presentation preferences; model, spend,
source-mirror and source-pause policy is read from SQL.

## Bounds and retention

| Item | Bound/policy |
| --- | --- |
| Publication/companion input | 32 MiB aggregate; 4 MiB per source file; 10,000 source files. |
| Business source bindings | At most 1,000 frozen publications per accepted command. |
| Legacy JSON import | 32 MiB/file, 64 MiB aggregate, 10,000 records/file. |
| Legacy logs and registry | At most 1,000 log tails of 64 KiB; 4 MiB per registry document. |
| SQL compatibility documents | 32 MiB/document, 10,000 records; logs retain 64 KiB tails. |
| Peripheral results/exports | 64 MiB; exports expire after 24 hours; cleanup bounded to 20/pass. |
| Peripheral refresh | At most 3 captures/pass; 2-second runtime and 20-second other refresh targets, 120-second observation lifetime. Scope enumeration is bounded and favors least recently observed scopes. |
| Superseded source | 30 days, retained while referenced by heads, runs, requests or business bindings; bounded cleanup. |
| Publication receipts | Preserve current generation/recent sequence window; bounded pruning of obsolete receipts. |
| Terminal business input | Compact after 30 days when no active/paused job retains it; canonical run/proof history remains. |
| Legacy archives | Retained until verified migration/retention decisions; no automatic deletion of the only historical source. |

These are admission/storage bounds, not a claim that every large project has a
fixed refresh latency. Large inventories can exceed one refresh cycle; uncertainty
is visible. Existing compute/editor/control queue capacity, pacing and producer
policies remain in force; Stage 6 does not increase paid execution concurrency.

## Qualification

Qualification is recorded in [the machine-readable report](database-migration-baselines/stage6-qualification.json)
and [the runtime baseline](database-migration-baselines/stage6-http-macos-arm64.json).
Tests use temporary databases, workspaces, scripted model responses and an owned
local bare Git remote. No paid inference or user-remote push was performed.

| Gate | Result |
| --- | --- |
| Native adapter regression run | 1,448 passed; three legacy reader mocks were updated for SQL interfaces and all three passed on rerun. |
| Final observer/boundary and terminal/solver gates | 49 and 51 passed; dispatch/run/solver regression gate: 119 passed. Counts overlap. |
| Linux ARM64 source/queue/workspace/migration/observer/retention | 159 passed with source mounted read-only. |
| JavaScript scripts, extension and frontend | 933 passed. |
| Browser selection, evidence, settings and approval flows | 9 passed. |
| Full native HTTP workflow suite | 14 passed, including native preservation-checked repair, restart, source updates, tabs, chat, batch, rename and export. |
| Repair → restart → controls → publication reproduction | 5 passed in one retained-state sequence. |
| Linux real stateless HTTP and exact acknowledgement replay | 2 passed. |
| TypeScript, production frontend build and boundary inventory | Passed; 172 adapter routes, 27 transport branches and 53 private worker branches are classified. |

Integration exposed and fixed an early terminal-event race, partial-draft capture
before assessment, and delayed dirty-scope refresh after generated markdown
publication. Historical invalid nonterminal `done` frames remain in audit storage
and cannot hide the canonical result. Registry publication preserves proof history
and existing preservation/review policy.

The baseline publishes 1 MiB of source, reuses its archive, and observes regulated
compatibility commands through SQL. Existing queue caps remain unchanged. See the
baseline README for p50/p95 and reproduction. These development samples do not
establish production throughput or native compiler/model latency.

Native repair was qualified on macOS ARM64. Linux ARM64 uses Node 22.23.3,
Python 3.13.11 and SQLite 3.40.1 in the audit-derived image. It supplies correctness
evidence for SQL and the stateless transport; native AMD64, the full production
image and Linux confined repair remain release qualifications.

## Rollback

Back up the database and retain legacy/import archives before deploying. A rollout
rollback must keep the current database authority and a compatible source/command
translator. Stop the adapter-owned worker before restoring an infrastructure
backup; reconcile accepted commands and workspace journals before restarting.
Never enable a retired JSON writer beside SQL, restore source callbacks as a
silent fallback or reconstruct proof validity from imported job labels. A schema
rollback alone does not safely reverse committed workspace or external effects.
