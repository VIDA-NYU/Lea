# PLAN — System hardening & responsiveness

> Executes the findings in [SYSTEM-REVIEW-2026-07-10.md](SYSTEM-REVIEW-2026-07-10.md)
> (finding IDs A1–D5 below refer to that document). Written 2026-07-10 against
> main @ `15edcd1`.

## Implementation status (2026-07-10)

| Phase | Status | Notes |
|---|---|---|
| 0.1 mid-run spend cap | ✅ done | `bridge.py` cap check on UsageUpdated/TurnStarted; `result_kind="max_spend"`; companion maps it without re-interrupting. 3 new bridge tests + 1 companion test. |
| 0.2 job retention | ✅ done | `companion/jobStore.mjs` (recency helpers extracted + `pruneJobs`); prune at startup and after every run; 8 new tests. |
| 0.3 start-dev default | ✅ done | Keeps data by default; `--fresh` wipes; `--keep-data` accepted as legacy no-op. Docs updated. |
| 0.4 editor-hook watchdog | ✅ done | `extension/editorHookWatchdog.mjs` + page-bridge `OL_LEAN_EDITOR_HOOKED` signal + dismissible banner; 7 tests. |
| 0.5 READMEs | ✅ done (amended) | Both READMEs were already current — the stale artifact was `CLAUDE.md`'s warning, now replaced; cheat-sheet expanded; report/review corrected. |
| 1 integration harness | ✅ done | `tests/integration/` (stub prover entrypoint + real adapter + real companion); `npm run test:integration`. **Caught a real bug on first run:** session-detail run rows lacked usage columns, so every companion job recorded $0 — fixed in `store.py`. Legacy `"success"` done-status alias removed. |
| 2 run lifecycle | ✅ done | Server-side FIFO queue + event hub with catch-up replay in `bridge.py`; events endpoint is a pure observer (no 409); `queued` frames with position; startup re-enqueues pending runs; approval deadline replaces the lock-steal; companion client compensation deleted; UI shows queue position. |
| 3.1 companion SSE push | ✅ done (2026-07-11) | `companion/eventBus.mjs` + `GET /events` SSE route; the jobs-persistence seam (`persistJobs`) publishes the coarse `jobs-changed` catch-all, chat/repair publish explicitly (incl. throttled live chat nudges). Extension: `extension/eventsClient.mjs` (EventSource + backoff, injectable for tests); polls stretch to 30–60 s reconciliation while the stream is up and fall back to the fast cadences when it drops. 12 new tests incl. an HTTP-level SSE test and an end-to-end push test in the harness. |
| 3.2 tex mirror efficiency | ✅ done | Adapter mirror `mode="upsert"` (never deletes); companion passthrough; content.js two-tier sync (active buffer per edit-pause, zip only on activation/unknown-file/10-min refresh/cache miss). 4 new adapter tests. |
| 4.1 artifact records | ✅ done (2026-07-11) | `artifacts` table (one row per scope+declaration, upsert-in-place); `extract_declaration_name` in `artifacts.py`; bridge finalizer records checked files from the run's own FileChanged set (best-effort, ok-verdicts only); `GET /api/projects/by-slug/{slug}/artifacts`. 9 new adapter tests. |
| 4.2 companion reads the index | ✅ done (2026-07-11) | `identifyArtifactFromAdapter` is the primary identification source (this run's rows, hint/label tie-break, namespace-derived path mapping); registry-markdown diffing survives as the logged fallback; a missing markdown entry is back-filled (guarded — never clobbers agent prose) until 4.3 demotes the registry. Harness now proves 4.1+4.2 over the real wire. |
| 4.5 single workspace writer | ✅ done (2026-07-11, pulled ahead of 4.3/4.4 as the most self-contained) | Adapter `POST .../artifacts/retire` (delete-as-commit) + `.../artifacts/restore` (revert from the retire commit's parent; never overwrites a failed run's partial file). Companion retry cleanup retires through the adapter and restores by commit — no more proof bytes stashed on job records; the legacy unlink+stash survives only as fallback for unmappable paths/older adapters. Harness proves the full retire→fail→restore loop over real git. |
| 4.4 status two-source merge | ✅ done (2026-07-11; default flipped 2026-07-13) | Adapter `GET .../target-status` serves per-declaration ledger evidence (file existence, sorry scan — Python twin of `containsSorryMarker` — and the newest check verdict across ALL the project's sessions). Companion `getTheoremStatus` IS the ledger engine: job overlay decides lifecycle, ledger decides file truth; no markdown parsing, no FS regex probes. The legacy five-source engine is deleted; `LEA_STATUS_ENGINE` is ignored (startup warning if still set). **Settled semantic** (the one recorded divergence): after a failed retry restores the previous verified proof, status reports the restored file's validity (`formalized`), not the failed run — file truth wins once no job is active, so dependents can `uses=` the proof that is really on disk; the failed attempt stays reachable through the job history/session link. Asserted in the harness. |
| 4.3 registry demotion | ✅ done (2026-07-11; readers deleted 2026-07-13) | Adapter backfills the artifacts table from a pre-index project's registry markdown (one-time, guarded by "no rows yet"; backfilled rows carry NULL session/run so they can never masquerade as run-recorded). The registry markdown is now write-only for machines everywhere: the markdown-diff identification fallback (`identifyLeaArtifact`/`selectLeaArtifactCandidate`) is deleted (the adapter index is the only identification source; session recovery is the only fallback), the legacy engine's markdown readers are gone, retry cleanup sources its retire candidates from the adapter target-status ledger, and `uses=` resolution answers from the engine (ledger evidence). The companion still *writes* markdown entries (agent-facing view; the prompt contract is unchanged) and reads its own markers solely to splice sections it maintains. |
| 5–7 | ⏳ not started | Drift consolidation, structural sweep, pairing token. |

## Guiding invariants (must survive every phase)

1. **Two front ends, one backend on :8001.** No new prover HTTP server; the
   adapter keeps driving the prover in-process (`CLAUDE.md` guardrail).
2. **Don't store derived state.** Statuses remain *derived* views of a
   verdict ledger; consolidation means fewer ledgers, not cached statuses.
3. **Git owns proof content; SQLite owns metadata.** Phases move the system
   *toward* this split (today the companion violates it), never away.
4. **Every phase lands green.** `npm test` (extension + frontend), adapter
   pytest, and — from Phase 1 on — the integration harness all pass at each
   phase boundary. Each phase is independently shippable and revertable.
5. **Strangler over big-bang.** Monolith splitting (D1) is not a phase; it's
   a rule: any seam a phase touches gets extracted into its own module as
   part of that phase.

## Sequencing at a glance

| Phase | Theme | Findings | Est. | Depends on |
|---|---|---|---|---|
| 0 | Quick safety wins | B1, B4, D4, B2, D5 | 3–4 d | — |
| 1 | Cross-layer contract harness | B6 | 2–3 d | — |
| 2 | Run lifecycle rework | A4, C3 | 4–6 d | 1 |
| 3 | Push channel + mirror efficiency | C1, C2 | 4–5 d | 2 |
| 4 | Single source of truth | A1, A2, A3, B5 | 8–12 d | 1–3 |
| 5 | Drift-surface consolidation | A5, A6, D2, D3 | 4–5 d | 1 |
| 6 | Structural sweep | D1 residue | 3–4 d | 2–5 |
| 7 | Pairing token (pre-beta gate) | B3 | 2–3 d | — |

Phases 0, 1, 7 are order-independent of everything else. 2→3→4 is the
critical path. 5 can interleave after 1. **Minimal high-value core: Phases
0–3 (~2.5 weeks) deliver the money fix, the latency fix, and the deletion of
the two worst compensation-code sites.**

---

## Phase 0 — Quick safety wins (3–4 days)

Independent, small, immediately valuable. One PR each.

### 0.1 Mid-run spend enforcement (B1)
- **Where:** `adapter/app/bridge.py` runner loop.
- **Design:** On each `UsageUpdated` event, accumulate the run's cost into a
  local counter. Compute `baseline = stats.global.cost_usd` once at run
  start (one DB read); on each turn boundary check
  `baseline + run_cost >= config.max_spend_usd` → set the existing stop flag
  (same mechanism as `request_stop`) with `result_kind="max_spend"` and a
  human `result_detail`. Cooperative stop at turn boundary is acceptable and
  should be documented: a single *turn* may overshoot; a single *run* no
  longer can.
- **Companion:** map the new `max_spend` done-status onto its existing
  `MAX_SPEND_MESSAGE` path (`markJobMaxSpend`), replacing the
  between-runs-only check as *enforcement* (keep it as a pre-flight to fail
  fast before creating a run).
- **Tests:** adapter pytest driving a stubbed event stream whose usage
  crosses the cap mid-run → run ends `max_spend`, file state committed;
  companion test mapping the status.
- **Acceptance:** a run started \$0.01 under the cap stops within one turn
  of crossing it.

### 0.2 Job retention (B4)
- **Where:** `companion/server.mjs` job store (extract to
  `companion/jobStore.mjs` while here — D1 rule).
- **Design:** On startup and after each finalization, prune per `jobKey`:
  keep the newest N=20 jobs *plus* unconditionally the newest job of each
  terminal status (so `findLatestJob(key, status)` semantics are unchanged)
  *plus* any job referenced by `chatSessions.json`. Delete log files whose
  job was pruned; cap `jobs/` at 200 files by age.
- **Tests:** prune preserves newest-of-each-status; status derivation output
  identical before/after prune on a synthetic 500-job store.

### 0.3 `start-dev.sh` safe default (D4)
- Flip the default to keep data; add `--fresh` for the wipe; print what
  would have been wiped. Update `CLAUDE.md`, README snippets.

### 0.4 Overleaf-hook watchdog (B2)
- **Where:** `extension/pageBridge.js`, `extension/content.js`.
- **Design:** pageBridge posts `OL_LEAN_BRIDGE_LOADED` immediately and
  `OL_LEAN_EDITOR_HOOKED` when `UNSTABLE_editor:extensions` fires. content.js
  arms a 20 s timer after injection: if `BRIDGE_LOADED` arrived but
  `EDITOR_HOOKED` never did *and* an editor container exists in the DOM,
  render a dismissible banner: "Lea can't attach to the Overleaf editor —
  Overleaf may have changed; check for an extension update." No banner on
  non-editor pages (project dashboard, history view).
- **Tests:** unit-test the state machine (extract to a small module);
  manual check on a real project page.

### 0.5 Rewrite stale READMEs (D5)
- Root `README.md` and `apps/lea-standalone/README.md` rewritten from
  [SYSTEM-REPORT-overleaf-architecture.md](SYSTEM-REPORT-overleaf-architecture.md);
  delete the ⚠️ stale-docs section from `CLAUDE.md` once true.

---

## Phase 1 — Cross-layer contract harness (B6) (2–3 days)

Built *before* the refactors it de-risks.

- **Layout:** `tests/integration/` at the monorepo root with its own npm
  script (`npm run test:integration`); excluded from the default `npm test`
  (needs Python), run in CI.
- **Design:**
  1. A stub prover package: a minimal `lea/` module placed on `PYTHONPATH`
     ahead of the vendored one (env var `LEA_PROVER_MODULE` or `sys.path`
     injection in a test-only adapter entrypoint), whose `run_events(...)`
     yields a scripted sequence — `TurnStarted`, `FileChanged` (writes a real
     file in a temp workspace), `CheckResult(ok)`, `UsageUpdated`,
     `Finished(proved)` — plus scriptable variants (failure, max_turns,
     mid-stream usage for 0.1's cap test).
  2. Boot the real adapter (uvicorn, ephemeral port, temp SQLite + temp
     workspace) and the real companion (ephemeral port, temp
     `.overleaf-lean-stub`).
  3. Drive the extension's exact HTTP shapes: `POST /formalize` with a
     fixture target → poll `/statuses` to `formalized`; then `/lean-pane/manifest`,
     a chat round-trip, and an edit-save.
  4. **Contract assertions:** SSE event names, `done.status` vocabulary
     (this is where the legacy `"success"` alias gets deleted —
     `leaApiClient.mjs:15`), session-detail field names the companion reads.
- **Acceptance:** one command spins the stack and passes in <60 s; CI runs
  it on every PR touching `adapter/` or `overleaf-extension/`.

---

## Phase 2 — Run lifecycle rework (A4, C3) (4–6 days)

The largest robustness win per line deleted. Adapter first, then clients.

### 2.1 Adapter: server-side queue, explicit start
- **Design:**
  - `POST /api/runs` enqueues; a single worker thread drains FIFO (the
    existing `active_run_lock` becomes an implementation detail of the
    worker, not a contract clients see). Run rows gain `queued_at`; queue
    position is *derived* (count of earlier pending runs — invariant 2) and
    exposed in run/session detail.
  - `GET /api/runs/{id}/events` becomes a **pure observer**, attachable any
    time, any number of times:
    - run pending → emit `queued` frames (with position) until it starts;
    - run running → **catch-up replay** synthesized from persisted state
      (the run's messages + latest code_step + turn counter — all already in
      SQLite), then tail live events via a per-run in-process broadcast;
    - run terminal → replay + a final `done` frame, HTTP 200. **The 409
      disappears entirely** — no more busy-vs-finished disambiguation.
  - `interrupt` on a queued run dequeues it directly (the `runs.py:158`
    special case generalizes and its comment-warned race goes away).
- **Files:** `bridge.py` (worker + broadcast), `routes/runs.py`, `store.py`
  (queued_at), migration in `db.py`.
- **Tests:** pytest — FIFO order under 3 concurrent creates; late attach
  gets replay + live tail; two observers on one run; interrupt-queued;
  Phase-1 harness updated to attach *late* deliberately.

### 2.2 Companion: delete the compensation machinery
- `leaApiClient.mjs`: remove the 409 busy-retry loop, jitter, and
  `MAX_RUN_ROW_MISSES` bookkeeping (~150 lines). Keep exactly two behaviors:
  overall job timeout, and re-attach on transport drop (now trivially safe —
  attach is idempotent). Run-row consultation survives only as the terminal
  fallback when the stream can't be re-established.
- Surface `queued` + position through job status → extension shows "queued
  behind 2 runs" instead of a bare spinner (C3's honesty fix).
- **Tests:** existing leaApiClient tests rewritten to the new contract
  (they shrink); harness covers queue-under-contention.

### 2.3 Standalone UI: same simplification
- `stores/proofSession.ts` reconcile→reattach logic drops its
  409-storm defenses; renders queue position.
- **Rollback:** phase is adapter-behavior-compatible for a same-repo ship
  (both clients update atomically in the monorepo); revert = revert the PR
  stack.

---

## Phase 3 — Push channel + mirror efficiency (C1, C2) (4–5 days)

### 3.1 Companion SSE endpoint (C1)
- **Design:** `GET /events?projectId=<overleafProjectId>` (SSE). An
  in-process emitter; every state mutation site (job transitions, status
  recomputation results, chat message appended, repair batch update, usage
  delta) publishes a typed event; the handler filters by project and
  forwards. Keep-alive comment every 15 s. Event types:
  `status-changed {targetKey, status}`, `job-progress {jobKey, turn}`,
  `chat-updated {targetKey}`, `repair-batch {snapshot}`, `usage-updated`.
  Events carry *keys, not payloads* where the payload is heavy — the
  extension refetches the one thing that changed.
- **Extension:** replace the four fast poll loops with one `EventSource` +
  targeted refetch. Keep a slow reconciliation poll (60 s) and automatic
  fallback to the current cadences if the stream errors 3× — the poll code
  paths stay but stop being the primary.
- **CORS:** `EventSource` sends the page origin; already allowed by the H3
  allowlist. No custom headers needed.
- **Tests:** companion test subscribes and asserts events across a
  formalize lifecycle (harness); extension logic extracted into a testable
  `eventsClient.mjs` (D1 rule).
- **Acceptance:** badge flips within 500 ms of job finalization (vs ≤3 s);
  steady-state idle traffic ≈ 1 request/min (vs ~40).

### 3.2 Tex mirror: stop re-zipping the project (C2)
- **Design:** split sync into two tiers:
  - *Active-buffer sync* (per edit-pause): POST just
    `{path, content}` of the live editor buffer to a new companion
    `/mirror-tex/active`, forwarded to the adapter mirror with a new
    `mode:"upsert"` (the adapter's reconcile mode must NOT delete absent
    files for this call — one flag in `routes/projects.py:485`).
  - *Full sync* (zip download + reconcile): only on project activation,
    on switching to a doc path not in the cached set, on a 10-min timer
    while active, and before formalize (`force`) — preserving today's
    self-heal property.
- **Tests:** companion/adapter upsert-vs-reconcile semantics; extension
  tier-selection logic unit-tested.
- **Acceptance:** zero zip downloads during steady editing of one file.

---

## Phase 4 — Single source of truth for proof state (A1, A2, A3, B5) (8–12 days)

The deepest change; strangler-sequenced so every step ships alone and is
observable before the next.

### 4.1 Structured artifact records in the adapter (A3/B5 foundation)
- **Design:** new `artifacts` table (or extend `adapter/app/artifacts.py`):
  `{project_id, declaration_name, kind, proof_path, module, session_id,
  run_id, created_at}`. The run finalizer writes it from what the adapter
  already knows: the run's `FileChanged` set + a server-side declaration
  parse of the changed files (port `inferLeanDeclarationName`). Exposed via
  `GET /api/projects/by-slug/{slug}/artifacts`.
- **Dual-write only** — nothing reads it yet. Add a divergence log:
  companion (next step) compares its own identification against this and
  warns.

### 4.2 Companion reads artifacts as primary evidence (B5)
- `identifyLeaArtifact`'s before/after-marker FS diff becomes the *fallback*;
  primary is "the artifact row for this run". After a bake period with no
  divergence warnings, delete the diff heuristics and the
  `selectLeaArtifactCandidate` ambiguity path.

### 4.3 Registry markdown demoted to a view (A3)
- Structured truth (name→path→module) now lives in 4.1's table. The
  companion stops *parsing* `projects/<slug>.md` (`parseProjectTheoremEntries`
  and the section-splicing code go away); it *regenerates* the file's
  structured sections from the table (agent-facing prose sections are
  preserved verbatim on regeneration — keyed by the existing markers, which
  become write-only). One-time importer backfills the table from existing
  markdown. The prompt contract to the agent is unchanged (it still reads
  the markdown).

### 4.4 Status becomes a two-source merge (A1)
- **Design:** new adapter endpoint
  `GET /api/projects/by-slug/{slug}/target-status?declaration=…` (or batch)
  answering from its ledger: latest verdict per artifact (code_steps +
  edit-check + rebuild results it already records), including
  `sorry`-detection done adapter-side on the committed content.
  `getTheoremStatus` collapses to: **(1)** companion-local job overlay
  (active/queued job, uses-resolution, retry bookkeeping) over **(2)** the
  adapter's verdict. The five-source merge, `proofFileTouchedAfter`, the
  file-regex re-derivations, and the pane/uses inconsistency
  (`server.mjs:5275`) all go away. The precedence rules that must survive
  (active-job-wins; fresh-compile-wins; `sorry`-reinserted detection) are
  each re-homed: the first stays companion-side, the latter two move into
  the adapter's ledger answer.
- **Migration safety:** ship with a `LEA_STATUS_ENGINE=legacy|ledger` env
  toggle for one release; harness runs both and diffs outputs across the
  scripted scenarios (proved, failed, retry, manual-break, rename, repair).
- **Tests:** the existing status tests are the spec — port them to the new
  engine before deleting the old one.

### 4.5 Single writer for the workspace (A2)
- Replace `cleanupPreviousRunArtifacts`/`restorePreviousRunArtifacts`/
  `removeLeaProofFile` direct-FS code with two adapter endpoints:
  - `POST /api/projects/{id}/artifacts/{declaration}/retire` — git-commits
    the deletion (author=system, message tagged `retire-for-retry`,
    records the pre-delete SHA on the artifact row);
  - `POST .../restore` — git-reverts that commit.
  The companion's stash files and `job.retryCleanup` bookkeeping are
  deleted; retry-restore becomes one API call backed by real history.
- **Acceptance for the phase:** `getTheoremStatus` ≤80 lines with two
  evidence sources; zero direct `fs` writes to `workspace/proofs/**` or
  `workspace/projects/**` outside the adapter; working tree clean (vs HEAD)
  after any retry cycle.

---

## Phase 5 — Drift-surface consolidation (A5, A6, D2, D3) (4–5 days)

### 5.1 One model catalog (A5)
- `adapter/app/models_catalog.py` loads
  `packages/lea-model-catalog/models.json` at import (path resolved from
  repo root; the Docker build copies the JSON in). Delete the duplicated
  Python literals. Canonicalize on provider-prefixed IDs everywhere; the
  adapter migrates stored bare IDs on settings read+write; after one
  release, delete `LEGACY_LEA_MODEL_ALIASES` (`server.mjs:106`).

### 5.2 One status vocabulary per boundary (A6)
- `scripts/gen-status-constants.mjs` reads a single
  `packages/lea-model-catalog`-style source (`packages/lea-contracts/status.json`)
  and emits `status.py` + `status.mjs`, checked in and verified in CI
  (regenerate-and-diff). Adapter done-statuses, companion theorem statuses,
  and pane statuses each get one enum + the two mapping tables, all
  generated from the same file. The Phase-1 harness asserts the wire values.

### 5.3 Settings single-master (A5)
- Adapter toml stays the source of truth for shared scalars + keys (already
  declared); the companion **stops writing them to `.env`** (env becomes
  read-only bootstrap). Delete `buildLegacyProviderEnvPatch` and the legacy
  fallback readers after a deprecation release that logs when a legacy path
  is actually exercised.

### 5.4 Shared helpers without a bundler (D2)
- Move `normalizeTargetText`, `buildLeaSessionUrl`, target-key helpers into
  a web-accessible `extension/uiShared.mjs`, lazily imported by content.js
  exactly like `leanPaneView.mjs` already is; options.js imports it as a
  module. Delete the comment-synced copies.

### 5.5 Companion drops its own Lean toolchain (D3)
- Delete `runLeanCheck` (`server.mjs:4679`) and route its remaining callers
  through the adapter's `lean-check`/`rebuild`. `doctor.mjs` reclassifies
  `lean`/`lake` as prover-setup checks, not companion prerequisites.

---

## Phase 6 — Structural sweep (D1 residue) (3–4 days)

By now Phases 0–5 have extracted: `jobStore.mjs`, `statusEngine` (mostly
deleted), `eventsBus.mjs`, `registryView.mjs`, `eventsClient.mjs`,
`uiShared.mjs`. This phase finishes the job:
- `server.mjs` → `routes.mjs` (dispatch table instead of the if-chain),
  `settingsSync.mjs`, `shareExport.mjs`, `leanPane/{manifest,chat,edit,repair}.mjs`.
  Target: no file >1,500 lines.
- `content.js` → thin bootstrap + feature modules
  (`statusBadges.mjs`, `popovers.mjs`, `settingsUi.mjs`, `chatPanel.mjs`,
  `editPanel.mjs`, `repairUi.mjs`) sharing one explicit `uiState` object
  instead of ~60 module-level `let`s (the stale-copy bug class dies with
  the globals). All new modules load via the existing
  web-accessible-resource `import()` pattern — no bundler introduced.
- Pure-logic modules gain direct unit tests (much of content.js becomes
  testable for the first time).

## Phase 7 — Pairing token (B3) (2–3 days, gate before any beta distribution)

- Companion generates a token on first run (stored in `settings.json`,
  0600). `POST /pair` returns it **only** to `chrome-extension://` origins
  (unforgeable by web pages); the options page stores it in
  `chrome.storage.local`; content/options send `X-Lea-Token` on every call.
  Companion requires the token on mutating + exfiltrating endpoints
  (`/formalize`, `/stub`, `/settings/*`, `/share/*`, `/project-export`,
  `/lean-pane/*` mutations) when a token exists; `GET /health` stays open.
- Companion→adapter: shared bearer token in the adapter's toml, read by the
  companion from the same config channel it already syncs settings through;
  adapter enforces on `PUT /api/settings` and the git/push routes first.
- Ship this in the same release as any `BETA_INSTALL.md` distribution push.

---

## Explicitly deferred (out of scope, recorded so they're deliberate)

- **Parallel prover runs** (multiple Lake workspaces/worktrees): C3 is
  mitigated by honest queueing; true parallelism is a prover-workspace
  design question, not an adapter bug.
- **Self-hosted Overleaf support:** would need optional host permissions +
  configurable match patterns; revisit on demand (B2's watchdog at least
  makes the failure visible).
- **Replacing the prompt contract with structured tool output** (the other
  half of B5): requires prover-side changes governed by the vendored
  package's own DESIGN.md; the artifact table (4.1) captures most of the
  value from the adapter side alone.

## Success metrics (measured before/after)

| Metric | Today | Target |
|---|---|---|
| Badge/pane update latency after a state change | ≤3–4 s (poll) | ≤0.5 s (push) |
| Idle HTTP requests, editor open, run in progress | ~40/min | ≤2/min |
| Zip downloads during steady single-file editing | 1 per edit-pause | 0 |
| `getTheoremStatus` evidence sources / LOC | 5 / ~170 | 2 / ≤80 |
| `runApiProofJob` compensation LOC (busy/queue/miss bookkeeping) | ~200 | ~50 |
| Max spend overshoot bound | one full run | one turn |
| Cross-layer contract tests | 0 | ≥6 scenarios in CI |
| Largest source file | 6,124 lines | ≤1,500 lines |
