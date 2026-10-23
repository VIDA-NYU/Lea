# Stage 1 durable observation delivery

Stage 1's development gate is complete: run observation and session-list
invalidation use committed SQL records, and bounded retention is qualified against
both run clients. Stage 2 now covers opt-in database core projections; see
[the Stage 2 delivery record](./STAGE2-database-projections.md). The remaining
supported-image release matrix and existing unrelated integration failures remain
explicit below.

## Implemented contract

- `0037_durable_observation` adds an append-only `domain_changes` cursor and
  transaction batch identity. INSERT/UPDATE/DELETE triggers cover every current
  domain table, including direct SQL finalizers, usage, controls, evidence and
  publication records. Changes contain identity/scope metadata, not secret config,
  source text, transcripts or checkpoint bodies. Each change ID is its invalidation
  revision. Composite entity IDs are JSON arrays of primary-key values.
- Domain writes and their changes commit/rollback together. The adapter connection
  factory and Alembic register `lea_change_batch()` with a fresh identity at each
  BEGIN. External SQL writers must register the same hook; unregistered domain
  writes fail instead of silently bypassing observation. Frozen legacy messages/
  code_steps and migration/observation internals are explicitly excluded. Coverage
  tests fail when a new table lacks a reviewed classification.
- Scope moves notify both the new scope and the previous scope (`detach`). Deletes
  retain tombstone identity/scope while parents are still present. Consumers may
  receive several invalidations in a transaction batch and must re-query state.
- `GET /api/changes?after=N` returns bounded metadata pages: default 200, maximum
  1,000 and 1 MiB. Filters intersect project/session/formalization/run scope and
  apply identically on replay. `stream=true` tails the same SQL pages, emits cursor
  heartbeats, and releases read transactions before sending network bytes. A lagging
  observer has no growing server-side event buffer.
- `GET /api/snapshots/sessions/{id}` supplies a versioned SQL-only snapshot of
  session metadata, timeline records/code blob references, selected public run
  fields, and a watermark from one short read transaction. It deliberately omits
  workspace freshness claims and legacy filesystem-aware projections. Stage 2
  adds those database projections before replacing the full legacy session view.
- Expired/future change cursors return HTTP 409 `snapshot_required`, floor and
  watermark; invalid limits/cursors return validation errors. An already attached
  tail emits `resync` and closes if its cursor expires. `compact_changes()` is an
  explicit worker maintenance primitive with a seven-day prefix retention policy;
  no GET prunes data. Canonical proof history is never pruned. The global cursor
  remains monotonic even after every change has been compacted.
- Every production `RunBroker.put()` first stores its normalized event. The
  existing `/api/runs/{id}/events` route now reads SQL by run/attempt/cursor, so a
  dropped broker or API process does not discard committed replay. A bounded page
  is loaded per poll. Large events become a persisted `resync` requiring the
  already committed snapshot, rather than duplicating huge source payloads.
- Standalone manual reconnect retains the last persisted SSE event ID. Duplicate
  partial chunks are ignored; missing chunks reload committed state; partial and
  final messages share one stable ID. A replay cannot reopen a final message.
  The companion retains its compatible run-event vocabulary and receives the
  same persisted events. Observation does not admit/restart/cancel a run.
- Assistant text is committed in UTF-8 chunks of at most 16 KiB, with a 200 ms
  normal batching interval and flushes at tool/turn/finish boundaries. Partial
  timeline rows have attempt ID, sequence, and streaming/final/interrupted state.
  Chunk delivery contains only the chunk and identity metadata. Narration finalizes
  the same row; recovery preserves partial text and marks it interrupted. Child
  narration and parent previews follow the same committed batch, avoiding one SQL
  write per child token. Final transcript import consumes each already recorded
  narration occurrence once. Explicit run deletion also removes its replay
  payloads, recording scoped tombstones while the parent row still exists.

The normal text deadline is checked on incoming tokens; a provider stalled with
an incomplete sub-cap batch flushes on the next token/boundary. This is not a
hard real-time flush guarantee or a provider latency SLO.

## Session-list cutover and retention

`GET /api/sessions` now returns its list and `cursor` from one shared read
transaction. The existing `GET /api/sessions/events` contract tails
`domain_changes`, accepts `?after=` / `Last-Event-ID`, and emits persisted cursor
IDs. It no longer reads the process token or periodically hashes the session list.
Snapshot/subscription races and direct SQL writes from another process cannot fall
between these two authorities. Initial legacy attachments still receive a refresh;
expired/future cursors receive a `sessions_changed` event with `reset: true`.

The standalone snapshots before connecting and before reconnecting, closes its
failed EventSource, coalesces invalidations, backs off between failures, and
ignores callbacks from retired sources. Request ownership prevents delayed list
responses from overwriting a newer response. A restored database may supply a
lower cursor. None of these operations changes selected session, target, file,
historical revision, editor draft, or cursor.

`0038_observation_retention` adds persisted per-run/per-attempt floors and high
watermarks, plus `run_observation_records`. Requests/resolutions retain separate
approval identities; diagnostic identities and the latest transient values remain
queryable in the SQL snapshot after transport expiry. Ordinary messages, code and
diagnostics also keep their existing canonical timeline/blob records.

The maintenance host runs independently of query traffic. Each run-event batch
examines at most 20 candidate attempts and removes at most 200 rows / 1 MiB.
Only terminal runs untouched for seven days qualify. A text chunk is removable
only when its exact message identity, run, attempt and sequence are represented
by a final/interrupted canonical row. Child previews carry the same committed
message metadata and additionally validate child/parent lineage. Legacy text
without sufficient identity is protected, rather than silently discarded; rotating
attempt checkpoints prevent such a run from starving other attempts.

Malformed legacy records and oversized rows remain protected. Invalidations are
compacted in contiguous batches of at most 1,000 rows after seven days. Floors
and transport deletion commit together. Compaction releases the write transaction
between batches, drains backlogs at one-second intervals, and checks idle storage
once per minute. Pruning transport rows does not create one replacement tombstone
per removed event. Explicit domain deletion still emits parent/audit tombstones.
Canonical proof history, stable message identities and audit records are never
pruned by this maintenance.

Expired/future run cursors emit `resync` with a replacement cursor; terminal runs
then settle from their persisted outcome. The standalone reloads committed detail
and accepts the authoritative reset even if it moves backwards. The companion
handles the compatible resync/terminal sequence and reads final usage/artifacts
through its existing queries. Neither observation nor retention admits work.

## Rollout and compatibility

Production run publishers retain no memory replay buffer. Both run clients use
the SQL event route; `LEA_DURABLE_OBSERVATION=false` no longer selects memory.
Direct `RunBroker` construction remains an isolated test sink, not a UI source.
Rollback must use a schema-compatible observation adapter while continuing
persistence; do not remove stable IDs, restore memory authority, or downgrade the
schema. Existing Host/Origin defenses still apply; this local single-user adapter
has no new tenant authentication model.

All migrations still back up a nonempty database before upgrade. Revisions 0036
and the additive observation migrations tolerate existing unstamped-schema
adoption. Companion `jobs-changed` remains a registered Stage 6 exception alongside
its JSON job authority; it is separate from the migrated adapter run observer.
Stage 2 replaces filesystem-aware session/proof queries for enrolled scopes;
unenrolled compatibility readers remain until strict cutover. Its delivery record
and Stage 3 contain the subsequent validation and route-count updates.

The new regression suite uses real SQLite to check rollback/batches, schema
coverage, scoped tombstones and moves, snapshot/watermark races, pagination,
expired cursors, UTF-8 chunk limits, persistence before delivery, stable final
identity, interrupted recovery, broker retirement, and API filtering. Frontend
tests cover duplicates, partial hydration and gap recovery without selection
changes; cross-layer tests verify stable message IDs and completed-run replay.

## Validation on 2026-10-16

| Check | Result |
| --- | --- |
| Broad native adapter suite, including real Lean and filesystem confinement | 1,345 passed. Four process probes were invoked from the wrong cwd; one shutdown fixture expected the old empty SSE payload. Corrected rerun recorded below. |
| Final SQL observation, retention, process-kill and enrolled file-projection tests | 37 passed on native macOS; 37 passed in Linux arm64 (Python 3.13 / SQLite 3.40). |
| Migration/schema adoption regression tests | 34 passed including observation coverage; final seam tests cover the subsequent bounded-scan implementation. |
| Corrected migration/startup/recovery/shutdown group | 42 passed from the adapter directory; all five broad-run failures resolved. |
| Full JavaScript suite | 17 script, 798 extension, 113 frontend tests passed. |
| Final hook tests and TypeScript | 30 passed; typecheck passed. |
| Browser selection/draft/cursor retention | 2 passed. |
| Real HTTP stable messages, adapter restart, expired replay with actual companion, and session-list subscription race | 4 passed. |
| Boundary inventory and whitespace checks | 149 adapter routes / 53 companion branches classified; clean diff check. |

The previously run complete cross-layer integration harness reported two existing failures:
`retry lifecycle: a failed changed-source attempt retains the previous proof as
stale history` times out, and `the ledger engine keeps a changed-source failed
retry stale` reports `formalized` instead of `stale`. Both reproduce against an
unchanged `HEAD` archive in a disposable checkout (6 passed / 2 failed there,
and the migration run reproduced the same failures). They are not counted as passes or hidden
by disabling tests. The four final observation HTTP tests pass separately. These existing companion
retry/staleness failures remain outside the Stage 1 observation changes.

Reproduction commands from the repository root:

```sh
(cd apps/lea-standalone/adapter && .venv/bin/python -m pytest -q)
npm test
npm run typecheck -w apps/lea-standalone
npx playwright test -c tests/browser/playwright.config.mjs tests/browser/selection.spec.mjs --output /tmp/lea-selection-results
node --test --test-name-pattern='wire contract|actual adapter process restart|expired replay|session-list snapshot' tests/integration/formalize.e2e.test.mjs
npm run test:integration
python3 scripts/audit-ui-boundaries.py
git diff --check
```

The broad native run above was launched from the repository root: four child-process
fixtures therefore could not import `app`. Rerunning their complete group from the
adapter directory passed. The fifth failure was a shutdown fixture that asserted
the former `data: {}` body; it now verifies the persisted cursor/reset payload
and still enforces timely stream closure. The corrected group passed all 42 tests.
Two tests added after the broad run collected are covered by the final 37-test
native/Linux seam suites. This is combined coverage, not a claimed single clean
full-suite invocation.

Native amd64 and the full published image remain release qualifications; current
Linux evidence covers the cached arm64 runtime with current sources, not compiler
isolation or the complete deployment matrix.

The native adapter run needs normal socket/DNS access and the macOS executor;
a nested sandbox skips executor cases and prevents socket fixtures. All model
outputs in these integration checks are scripted; no paid provider calls are made.
The Linux check used the same cached audit runtime as Stage 0, current sources
mounted read-only, and pytest installed only in the disposable container. It
validates the new schema/observation code, not Linux workspace isolation support.
