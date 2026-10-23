# Stage 0 Database Boundary Inventory

> Status: development inventory, selection fixes, runtime baselines and initial
> limit calibration complete — 2026-10-16. Release matrix qualifications below.
> Governing specification: [FEATURE-database-mediated-lea.md](./FEATURE-database-mediated-lea.md).
> Plan: [PLAN-database-mediated-lea.md](./PLAN-database-mediated-lea.md).
> This stage does not change execution, persistence, queue policy, or protocol ownership.

## Scope and classification

[database-boundary-inventory.json](./database-boundary-inventory.json) is the
reviewed register. Stage 0 classified **147 explicit adapter HTTP/WebSocket routes
and 53 companion dispatch branches**; Stage 1 adds two SQL-only routes, making the
current total **149 / 53**. Each entry selects a profile
recording caller, current data source, mutation/effect, current authority,
destination category, migration stage, and compatibility treatment. A profile
describes migration responsibility; its destination category does not assert
that the current implementation obeys that boundary.

The categories are infrastructure, database query, database-only mutation,
source publication, and execution command. Mixed current operations retain
their named violations. For example, a current POST that compares source is a
query, while a GET that wakes an operation is still a read path requiring repair.
Compound companion branches retain their full method/path predicate, including
the shared GET-report/POST-retry branch. They must split into query and command
behavior during migration.

Run the coverage guard and locate exact handlers without importing the application:

```sh
python3 scripts/audit-ui-boundaries.py
python3 scripts/audit-ui-boundaries.py --list
```

The guard is included in `npm run test:scripts` through
[ui-boundaries.test.mjs](../scripts/ui-boundaries.test.mjs). New explicit routes,
changed route identities, obsolete entries, incomplete categories, and changes
to companion dispatch fail the guard. Update classifications deliberately after
review; the checker never generates an automatic fallback category. Companion
dispatch has a reviewed digest because nested method dispatch and new JavaScript
routing styles cannot be inferred safely from path strings alone.

Framework-generated `/openapi.json`, `/docs`, `/docs/oauth2-redirect`, and `/redoc`,
the frontend catch-all, CORS/Host/Origin checks, OPTIONS handling, and ordinary
static delivery are infrastructure. The explicit catch-all is included in the
register; generated framework routes are listed here. No application-data route
is exempt merely because it is a GET or lives under `/health`.

## Boundary violations and their owners

This table records the Stage 0 baseline. Run and sidebar observation violations
are now closed by [Stage 1](./STAGE1-durable-observation.md). [Stage 2](./STAGE2-database-projections.md)
has begun explicit project file-view enrollment; other listed bypasses remain.

| Boundary | Current behavior and evidence | Destination and stage |
| --- | --- | --- |
| Live run observation | [runbroker.py](../apps/lea-standalone/adapter/app/runbroker.py) holds replay in memory; [useProofStream.ts](../apps/lea-standalone/src/app/hooks/useProofStream.ts) applies deltas/status directly. Some domain writes precede events, but the complete visible state is not a durable feed. | Transactional changes and persisted partial messages, Stage 1. |
| Sidebar observation | [sessions.py](../apps/lea-standalone/adapter/app/routes/sessions.py) uses `sessions_change_token()` and periodic SQL digests. | Durable changes with consistent snapshot cursors, Stage 1. |
| Proof and status queries | [read_projections.py](../apps/lea-standalone/adapter/app/read_projections.py), [proof_evidence.py](../apps/lea-standalone/adapter/app/proof_evidence.py), formalization/status views inspect files and environment for current evidence. Stats can call proof-status projections. | Worker observations plus pure database projections, Stage 2; peripheral inventory in Stage 6. |
| Read-triggered execution | `GET /api/reformalizations/{operation_id}` calls `reformalizations.wake()`. Library declaration GET can refresh inventory and validate modules. | Remove wake from operation queries in Stage 5; library refresh becomes explicit worker work in Stage 6. |
| Run admission and controls | [runs.py](../apps/lea-standalone/adapter/app/routes/runs.py) already persists run admission but enqueues in memory; interrupt/approval/child-stop reaches process-local rendezvous. | Typed durable commands and independent controls, Stages 3–4. |
| Manual and project writes | [proof_files.py](../apps/lea-standalone/adapter/app/proof_files.py) and project/session handlers write files, create repositories, edit markdown, retire/restore artifacts, or delete projects. | Worker-owned revision-bound operations, Stage 5. |
| Checks and review application | Session checks/rebuild/SafeVerify invoke execution directly. Review decisions can apply retained candidate work through the application service. | Check/decision commands retaining existing preservation and review contracts, Stage 5. |
| Project file reads and exports | Project tree, file, uploads, instructions, memory, blueprint/graph, and ZIP endpoints use disk/Git. | Persist content, inventory, and bounded export results, Stage 6. |
| Overleaf operational authority | [server.mjs](../apps/overleaf-extension/companion/server.mjs) owns JSON jobs, chat mappings, generated targets, batch loops, prompts, cascades, logs, workspace reads, and project markdown writes. | Import associations/history without creating proof evidence; move orchestration to worker, Stage 6. |
| Reverse source transport | [sourceRefresh.mjs](../apps/overleaf-extension/companion/sourceRefresh.mjs) polls adapter requests and asks a connected tab to respond; mirror handling also materializes files. | Publish exact source snapshots and bind user actions to them, Stage 6. |
| Settings and capabilities | TOML/environment/JSON, packaged catalogs, model catalog service, MCP tests, skill import, and built-in subagent YAML/JSON overrides supply application data or execute work. User-authored skills/tools/roles already have SQL records. | DB views/settings with explicit credential intake; network/refresh/test/import commands, Stage 6. |
| Provider-backed reports and compaction | Batch report handlers use FastAPI background tasks; compaction can call a provider. These effects are outside the main solver queue. | Regulated bounded worker commands and durable results, Stages 4 and 6 as each family moves. |
| Live editor | `lsp-info` reads a live file; WebSocket proxies a Lean process. | Versioned persisted buffer requests/results, Stage 7. |
| Lifecycle ownership | [main.py](../apps/lea-standalone/adapter/app/main.py) starts migrations, reaping, recovery, indexing, catalogs and execution; runtime readiness reads globals. | Supervised bootstrap plus execution worker, DB runtime observations, Stage 8. |

Independent Lea Check start/retry routes already return 410. Their three adapter/
companion entries remain retired; migration must not accidentally recreate them.
Current review and status evidence must not become a plain mutable `valid` field.

## Background activity and browser bridges

These paths supplement the route register. Several timers are retries within one
family rather than independent authorities; their shared ownership is recorded
explicitly below.

| Path or activity | Caller and source | Current effect and authority | Target category and stage |
| --- | --- | --- | --- |
| API startup/shutdown hooks | Web lifecycle in `main.py`; DB, workspace, owned-process records. | Acquires data lock; reaps processes; migrates; recovers runs, applications, dependencies, batches and imports; refreshes library; starts catalog; closes Lean/MCP on shutdown. | Infrastructure migration owner and worker lifecycle, Stage 8. |
| Run dispatcher, run threads and approval waits | `bridge.py`, `runregistry.py`; SQL rows plus Python queues/maps/events. | Starts solver work, owns slots, handles stop/gates and emits broker events. | Execution commands and independent controls, Stages 3–4. |
| Reformalization worker threads and dependency handoff | `reformalizations.py`, runner/apply and dependency services; operation journals/DB plus workspace. | Prepare/continue/apply staged work; retain drafts; claim shared helpers. A query can currently wake this path. | Execution commands with preserved journal/ownership, Stages 4–5. |
| Runtime drain/restart thread | `runtime.py`; runtime flags and live handles. | Stops admission and signals tasks, coordinates supervisor. | Durable execution controls plus infrastructure supervision, Stages 4 and 8. |
| Catalog service thread | `catalog_service.py`; provider catalogs, config and cache. | Fetches/rebuilds model state outside durable queue. | Refresh commands and persisted views, Stage 6. |
| FastAPI report/mirror background tasks | `routes/formalize_batch_reports.py`, `routes/projects.py`; report rows, providers and mirrored source files. | Generates reports or commits source mirror after the response. | Worker commands with durable receipts, Stage 6. |
| Companion startup and 5-second reconciliation | `createCompanionServer`; JSON state, adapter records, source-client map. | Recovers jobs/batches, prunes JSON, refreshes source requests/usage/dependency waits/batches. | Worker recovery plus DB-only companion, Stage 6. |
| Companion batch/repair/job supervision and timeouts | Companion batch loops, API client and job finalizers; jobs/logs plus adapter streams. | Starts/finalizes work, cascades checks, requests interruption and persists overlays. | One worker orchestrator and durable queue, Stages 4–6. |
| Companion SSE keepalive/event bus | `eventBus.mjs`, `GET /events`. | In-memory invalidations and connected source-client tracking. | Durable database change transport, Stages 1 and 6. |
| Standalone run reconnection and health polling | `useProofStream.ts`; event stream, `/api/health`, session-detail responses. | Rehydrates execution data; previously reset selection/history. | Selection fixed here; durable changes Stage 1, DB health Stage 8. |
| Standalone sidebar, stats and project refresh | `App.tsx`, sessions/projects stores and `StatsPage.tsx`; session SSE and API queries. | Refreshes lists/counters/project documents. Sidebar does not select a session from an incoming update. | Database queries and durable changes, Stages 1–2 and 6. |
| Canvas snapshot/highlighter and live-editor autosave | `Canvas.tsx`, `LiveEditor.tsx`; snapshot API, static highlighting assets, local Monaco buffer and LSP. | Refreshes snapshots; autosaves user edits after debounce; editor transport remains direct LSP. | Stable file/editor ownership fixed here; saves Stage 5 and editor requests Stage 7. |
| Extension pane/status/chat/batch/import/usage polls | `content.js`, `eventsClient.mjs`; companion events and HTTP. | Reconciles badges/pane/chat/batches/import progress; rename retries and visibility changes trigger fresh requests. | Database queries/changes and worker commands, Stages 1 and 6. |
| Extension source debounce/archive refresh | `content.js`, source client, `zipTex.mjs`; active buffer and Overleaf project archive. | Parses source and sends mirrors; may answer reverse source request. | Allowed browser source capture plus immutable publication, Stage 6. |
| Source and target editor bridge | `pageBridge.js` ↔ `content.js` using `OL_LEAN_TARGETS_VISIBLE`, target/diagnostic clicks, `OL_LEAN_REQUEST_TARGETS`, `OL_LEAN_EDITOR_HOOKED`. | CodeMirror observation/decoration, document polling, watchdog retries; browser source is authoritative input. | Browser-local capture and display remain allowed; snapshot admission Stage 6. |
| Source navigation bridge | `OL_LEAN_NAVIGATE` / `OL_LEAN_NAVIGATE_RESULT`, active-document retry timer. | User-triggered source navigation/highlight. | UI-owned navigation; never expose this to worker/model, Stage 0 contract. |
| Open/focus browser tabs | `OPEN_LEA_SESSION` through `background.js`; `OPEN_OVERLEAF_DOCUMENT` through `uiBridge.js`. | User-triggered tab open/focus and source links, guarded by extension sender/origin checks. | UI-owned explicit navigation; never a worker capability, Stage 0 contract. |
| Presentation-only timers | Tooltip/highlight removal, object URL revocation, saved/error indicators, dialog focus, composer sizing, chat bottom-follow. | Browser-local cleanup/layout; chat already stops bottom-follow when the user scrolls away. | UI/infrastructure; retain existing user-controlled behavior. |

Provider-key validation timers and HTTP abort timers bound current external calls;
they are part of the registered external/settings family. They do not create an
exception to the future command boundary. Transport keepalives and browser
watchdogs similarly do not authorize work.

## Selection behavior delivered

Incoming `code_step` records no longer change formalization scope, current versus
historical mode, selected file, or selected history step. Replay/backfill preserves
the selected immutable step by ID when numeric ordering changes. Data and current
artifact refresh signals still update normally.

Explicit session loads may establish an initial view. Background reconciliation,
stream resync, reconnect, and run completion preserve the author's presentation
and composer context while hydrating authoritative data. Current snapshots refresh
without clearing the mounted editor. Current-file choice follows its path across
snapshot reordering, rather than resetting to the first file on every revision.
If that selected file disappears, the canvas names it as unavailable instead of
silently selecting another file.

The Overleaf pane defers destructive re-rendering while the author selects text
anywhere in its body and applies the newest pending update when the range clears.
Previously this protected only live-status text. Existing
item expansion, batch-row focus restoration, and scroll retention remain. No new
automatic follow mode is introduced.

## Representative regression fixtures

| Case | Existing or added executable evidence |
| --- | --- |
| Delayed events, replay/backfill, reconnect/resync, done, scope changes, navigation away/back | [proofStream.test.mjs](../apps/lea-standalone/src/app/proofStream.test.mjs), executing the actual bundled hook. |
| Historical selection, current-file reordering, local draft and cursor, explicit navigation | [selection.spec.mjs](../tests/browser/selection.spec.mjs), actual React hook/store/Canvas with deterministic API and editor-transport substitutions. |
| Overleaf source/code text selection and pane focus/navigation | [contentActions.test.mjs](../apps/overleaf-extension/tests/contentActions.test.mjs), actual content script with fake browser DOM/companion. |
| Pending approvals, usage, ordinary proof writes, failures | [adapter_entry.py](../tests/integration/adapter_entry.py) and [formalize.e2e.test.mjs](../tests/integration/formalize.e2e.test.mjs); scripted prover with real adapter persistence. |
| Shared files, manual edits, paused drafts, publication/recovery | Adapter `test_reformalization_operations.py`, `test_reformalization_scope.py`, `test_routes_sessions.py`, and existing reformalization guide fixtures. |
| Source snapshots and document ambiguity | Adapter `test_overleaf_handoff.py`, `test_overleaf_project_names.py`, and extension source-refresh/content-action tests. |
| Dependency waits, source-faithfulness, selected disproof and review | Adapter dependency, target-completion, Lea Status, formalization-review and certification regression suites. Preserve these during later command conversion. |

These fixtures establish repeatable shapes without copying private production
projects, credentials, live job logs, or database contents into the repository.
They do not establish production capacity or timing claims.

## Stage status and remaining gates

- [x] Classify every explicit API route and companion dispatch branch with a
  checked register; record framework, lifecycle, timer, and browser bridge families.
- [x] Remove runtime-driven target/file/history selection and background refresh
  resets; retain explicit navigation and current data refresh.
- [x] Add regression fixtures for delayed/replayed/completed updates, stable file
  identity, editor draft/cursor retention, and Overleaf text selection.
- [x] Measure synthetic history/storage/HTTP latency, DB size/WAL growth and
  reconnect behavior on available native macOS and Linux arm64 fixtures.
- [x] Calibrate queue/transport defaults and finite upload/export/blob policy before
  corresponding new admissions ship. See the executable `migration_limits.py`.
- [x] Record capability probes, reproducible commands, limitations, and release
  matrix follow-ups in the [baseline record](./database-migration-baselines/README.md).

The Stage 0 development gate is complete. Native Linux amd64 and the full
published Mathlib image remain release qualifications, not measured capacity
claims. Linux's existing isolated-executor capability is unavailable and its
staged retry admission remains disabled. Stage 1 adds two database-only routes to
the reviewed register (149 adapter routes / 53 companion dispatch branches).

Rollback of this slice restores the previous frontend behavior; no schema or
execution changes require reversal. Keep the inventory and fixtures. Later
stages must preserve these selection regressions when replacing transport.
