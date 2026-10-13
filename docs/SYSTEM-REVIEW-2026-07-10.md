# System review — robustness & responsiveness findings (2026-07-10)

> Scope: the whole monorepo with emphasis on the Overleaf path, reviewed
> against main @ `15edcd1` using
> [SYSTEM-REPORT-overleaf-architecture.md](SYSTEM-REPORT-overleaf-architecture.md)
> as the map. **Analysis only — no changes were made.**
>
> Relationship to the prior audit: `apps/overleaf-extension/AUDIT.md`
> (2026-07-06) already found and fixed ~25 bug-level issues. Spot-checks
> confirm the load-bearing fixes hold (atomic per-path-serialized JSON writes
> with corrupt-file recovery at `server.mjs:6026-6065`; CORS restricted to
> Overleaf + extension origins at `server.mjs:6080-6096`; chat-poll backoff;
> provider-aware boot warning). This review therefore focuses one level up:
> **design-level brittleness** the finding-by-finding audit didn't cover, plus
> a few new items.

## What's genuinely solid (context for the findings)

- The typed-event prover seam (`bridge.py`) and the "derived status, git owns
  content, SQLite owns metadata" split are good architecture, consistently
  enforced on the adapter side.
- The cascade pipeline fails closed, real-builds dependents, and propagates
  breakage to a fixpoint — with tests.
- The parser emits diagnostics instead of silently ignoring malformed input.
- There is a working audit culture: findings tagged in source (`AUDIT H1`
  etc.), regression tests added, remediation table maintained.

The issues below are mostly the *shape* of the system — the places where the
prior audit's bugs came from and where the next ones will.

---

## A. Design-level brittleness

### A1. Theorem status is an inference engine over five evidence sources
**Where:** `companion/server.mjs:5240-5410` (`getTheoremStatus`) and its
helpers (`getCurrentTheoremProofStatuses`, `getLeaProjectTheoremStatus`,
`getLeaDirectProofStatus`, `getLatestMappedJobStatus`,
`proofFileTouchedAfter`, `attachTransitiveStubbedUpstream`).

A target's status is re-derived on every poll by merging: (1) the companion's
job records, (2) the project-markdown registry entry, (3) a regex scan of the
proof file currently on disk, (4) the recorded edit-check verdict, and (5)
adapter session data — with explicit precedence overrides layered on top
(active-job wins over file evidence; fresh compiler verdict wins over stale
regex re-derivation; fresh file evidence wins over a job's cached verdict
because a manually re-inserted `sorry` *compiles*). The comments in this
function are essentially bug archaeology: the `needs_review` shadowing bug,
the `repaired`/rename fallback bug, and the valid-before-valid race each
forced another layer. There is also an admitted inconsistency: the
edit-check override applies to the pane's status source but **not** to
`getCurrentTheoremProofStatus` used by `uses=` resolution
(`server.mjs:5275-5278`) — so formalize-time dependency resolution can trust
a proof the pane already shows as broken.

**Why it's the top finding:** every new state or feature has had to touch
this merge, and each touch has historically shipped a subtle mis-ordering.
The root cause is that status is *recomputed from overlapping caches* rather
than owned by one source. The adapter already derives session status from the
latest `code_step` verdict — the same principle (one verdict ledger,
append-only, everything else a view) is available but unused on the
companion side.

### A2. Two writers, one workspace: the companion mutates files that git-owns
**Where:** `companion/server.mjs:3559` (`cleanupPreviousRunArtifacts`),
`:3636` (`restorePreviousRunArtifacts`), `:3684` (`removeLeaProofFile`),
`:5774` (`upsertProjectTheoremEntry`); adapter `gitstore.py`.

The architecture says "git owns proof content" — but on retry the companion
deletes proof files and markdown entries **directly on disk**, bypassing the
adapter's git layer, then keeps its own stash-and-restore backups
(`job.retryCleanup`, the AUDIT H2 fix) to undo the deletion on failure.
That's a hand-rolled, single-level undo re-implementing what the git history
already provides, and it leaves the project repo's working tree diverged from
its HEAD (uncommitted deletions/restores) until the next adapter commit
happens to sweep them up. Any future adapter feature that trusts `git status`
or diffs HEAD against the working tree will trip over this. The clean shape
is a single writer: workspace mutations go through the adapter (the
`POST /api/sessions/{id}/file` primitive already exists and is used for
manual edits), and retry-restore becomes a git revert instead of a stash.

### A3. A regex-parsed markdown file is load-bearing state
**Where:** `workspace/projects/<slug>.md`; `companion/server.mjs:5821-5944`
(`renderProjectTheoremEntry`, `parseProjectTheoremEntries`,
`parseMarkerAttrs`, HTML-attribute escaping), `:5814`
(`collapseBlankRunsOutsideFences`).

The per-project theorem registry is a markdown document with
`<!-- lea:theorem name="…" proof="…" -->` markers, parsed by regex and
rewritten by string slicing — and the *prover agent itself* also writes prose
into this same file (descriptions, solving process), guided only by prompt
text. It is used as evidence for artifact identification and `uses=`
resolution. The AUDIT M7 fix patched one corruption vector (blank-line
squash inside fences), but the design remains: machine-critical fields
interleaved with free-form agent output in one mutable text file. The
structured fields (name → proof path → module) belong in the adapter's
SQLite or a JSON sidecar; the markdown can stay as the human/agent-facing
view generated *from* it.

### A4. Attach-to-start SSE and the overloaded 409
**Where:** adapter `routes/runs.py:165-186` (attach is what starts a run;
409 = "slot busy" *or* "already completed"), `runs.py:139-162` (the special
case for interrupting a pending run nothing is driving);
`companion/leaApiClient.mjs:520-664` (`runApiProofJob`).

Because a run only starts when its event stream is first attached, and the
single-run slot rejects a second attach with the same 409 it uses for
"already done", the client has grown ~200 lines of compensation: re-attach
loops with jittered delays, run-row consultation to disambiguate 409s,
`MAX_RUN_ROW_MISSES` unreachability bounding, dropped-stream adoption of the
row's terminal outcome. It now works (H4/L8 are fixed and tested), but the
complexity lives on every client — and the queue is *client-side*: each
queued waiter polls for the slot, ordering between waiters is a race, and a
run can starve past its timeout under contention (acknowledged in AUDIT L8).
A server-side queue in the adapter (runs start when created or when the slot
frees, in order; the stream is pure observability; distinct status codes for
busy vs. finished) would delete most of this machinery in one move — for
both the companion *and* the standalone UI, which has its own
reconcile→reattach logic the `runs.py` comments describe fighting request
storms with.

### A5. Settings are multi-master with hand-maintained reconciliation
**Where:** `companion/server.mjs:93-125` (`SHARED_SETTING_ENV_FIELDS`,
`ADAPTER_SHARED_SCALARS`, `LEGACY_LEA_MODEL_ALIASES`), `:2989`
(`syncSharedSettingsFromAdapter`), `:3175` (`buildLegacyProviderEnvPatch`);
root `.env`; adapter `config/lea.local.toml`; extension `chrome.storage`.

Shared scalars (model, max turns, spend cap, keys) have one declared source
of truth (the adapter's toml) — good — but the companion still mirrors them
to `.env`, keeps legacy env-patch builders, and maintains an alias map
because the adapter stores bare Anthropic model IDs while the catalog uses
provider-prefixed ones (`server.mjs:106-114` — the map shows the IDs have
already diverged twice). Each new provider or ID format grows the alias map
and the sync code. Related: the model catalog itself exists twice —
`packages/lea-model-catalog/models.json` (JS) and the adapter's
`models_catalog.py` (Python) — kept consistent by convention only
(`CLAUDE.md` says "keep them consistent"). One generated artifact (Python
reading the same `models.json`, and one canonical ID format) would retire
both drift surfaces.

### A6. Three status vocabularies with pairwise mappers
**Where:** adapter done-statuses
(`proved|disproved|needs_review|answered|max_turns|cancelled|failed`, plus
the legacy `"success"` alias kept for old test doubles —
`leaApiClient.mjs:10-15`); companion theorem statuses (`formalized`,
`defined`, `disproved`, `sorry_stub`, `in_progress`, `edit_broken`, …); pane
statuses (`missing-stub`, `stub-generated`, `valid`, `stale`, `mixed`, … —
`leanPaneView.mjs:9-22`); mapped by `mapLeanPaneStatus`
(`server.mjs:5152`), `getEquivalentTheoremStatus` (`server.mjs:5505`), and
`SUCCESS_DONE_STATUS`. Every new outcome (e.g. `needs_review`,
counterexamples) must be threaded through three vocabularies and their
mappers; the `"success"` alias is fossil evidence that the layers have
already drifted once.

---

## B. Robustness gaps (new findings)

### B1. Spend cap is enforced only between runs, at both layers
**Where:** adapter `routes/runs.py:64` (checked at `POST /api/runs` only —
`bridge.py` never consults the cap mid-run despite receiving
`UsageUpdated` events); companion `server.mjs:4639`
(`recordUsageAndEnforceSpendLimit`, called after a run returns).

A single run can overshoot the cap by its entire cost — with
`LEA_JOB_TIMEOUT_SECONDS=900` and an expensive model, that is a materially
unbounded overrun, and a repair *batch* checks between items only. The
bridge already sees per-turn usage events; a mid-run check there (cap
reached → cooperative interrupt, same mechanism as `request_stop`) would
make the cap mean what users think it means.

### B2. The whole Overleaf integration hangs on one unstable hook, and fails silent
**Where:** `extension/pageBridge.js:8`
(`window.addEventListener("UNSTABLE_editor:extensions", …)`);
`extension/content.js:3989` (`injectPageBridge` — fire and forget).

If Overleaf renames/removes the `UNSTABLE_`-prefixed CodeMirror hook (the
name is an explicit instability warning), the extension degrades to
*nothing*: no badges, no targets, no error — the user just sees an extension
that stopped working. There is no detection ("bridge injected but no
editor-ready event within N seconds → surface a banner"), no version canary,
and only `www.overleaf.com` is matched (self-hosted Overleaf CE/Pro is
silently unsupported). A watchdog + user-visible degradation notice is cheap
insurance for the day Overleaf ships a breaking change.

### B3. Localhost services trust every local process
**Where:** companion `:31245`, adapter `:8001`.

The H3 fix correctly gates *browser* origins, and requests without an
`Origin` header (curl, native processes) were deliberately left open —
reasonable for a dev tool, but worth stating as an accepted risk: any
process on the machine can start paid runs, read/write provider keys via the
adapter's `PUT /api/settings`, set the GitHub push remote, and export
projects. If this ever ships beyond personal dev machines (the beta-install
docs suggest it will), a pairing token between extension ↔ companion ↔
adapter is the next step.

### B4. Unbounded local state growth
**Where:** `state.jobs` / `jobs.json` — nothing prunes jobs (only repair
batches are pruned, `server.mjs:2136`); `jobs/*.log` accumulate. Every
`/statuses` call filters/sorts the whole job map per target
(`jobsByRecencyDesc`). Fine for weeks, quadratic-ish shading over months
without a `reset:local`. A retention window (keep last N per jobKey) closes
it.

### B5. The prompt is the contract, with post-hoc validation as the only net
**Where:** `server.mjs:4369-4507` (`buildLeaPrompt` and variants encode
paths, namespaces, declaration names, and import expectations as prose);
`:4328` (`validateStubArtifact`), `:5717` (`identifyLeaArtifact` — infers
the produced artifact by diffing before/after markers, with an explicit
ambiguity path in `selectLeaArtifactCandidate`). Some of this is inherent
to driving an agent, but the artifact-identification heuristics exist only
because the run's *output location* is requested in prose instead of
reported structurally. The adapter already records `code_steps` per run —
having the finalizer trust "the run's own code steps for this session"
first, and the FS diff only as fallback, would shrink the heuristic surface.
(Half of this exists: `readLeanPaneArtifactFromSession` prefers session
steps for display; the job finalizer still diffs.)

### B6. No cross-layer integration test
Companion tests fake the adapter (`fetchImpl` doubles), adapter tests fake
the prover, frontend tests are unit-level. Nothing exercises
extension-shape → companion → real adapter → real (or stub) prover. The
`"success"` legacy alias (`leaApiClient.mjs:14`) exists precisely because
test doubles and the real adapter drifted. One smoke test that boots the
adapter with a stubbed `run_events` and drives `/formalize` end-to-end would
catch contract drift where it actually bites.

---

## C. Responsiveness

### C1. Everything is polled; nothing is pushed to the extension
**Where:** `content.js:11-25` — statuses every 3 s during a run
(`STATUS_REFRESH_IN_PROGRESS_MS`), Lean pane every 4 s
(`LEAN_PANE_POLL_DELAY_MS`), chat every 4 s, usage every 5 s, repair-batch
status on its own timer; each `/statuses` hit re-runs the A1 evidence merge
(per-target FS reads + possible adapter fetches).

The adapter already streams (SSE at `/api/runs/{id}/events` and
`/api/sessions/events`), and the companion consumes those streams — then
flattens them into files/memory that the extension polls back out. Latency
floor for any badge/pane/chat update is the poll cadence; robustness tax is
the poll-failure edge-class (AUDIT M2 was exactly this). One SSE endpoint on
the companion (job/status/chat deltas, forwarding what it already receives)
would cut update latency from seconds to instant *and* delete the retry/
backoff bookkeeping in `content.js`.

### C2. The tex mirror re-downloads the whole project zip per edit-pause
**Where:** `content.js:3083-3164` (`syncTexMirrorNow`,
`collectProjectTexFiles`). The dirty-flag gating is good (no downloads when
idle, none before first formalize), but once a project is activated, *any*
edit → 1.5 s debounce → full `GET /project/<id>/download/zip` + unzip + POST
of every `.tex` file to the companion. On a large project under active
editing that's a zip download roughly every keystroke-pause, against
Overleaf's servers (rate-limit exposure) and across the wire to the adapter.
The active-file overlay (`latestActiveTexPath` buffer) already exists —
syncing *just the active buffer* on edit and the full zip only on
activation/structure change would cut ~all of the recurring cost.

### C3. Global run serialization with a blind queue
One prover run at a time, globally (`bridge.py:62` `active_run_lock`) — a
long standalone-UI run blocks every Overleaf formalization, which queues
client-side (A4) with no user-visible position/ETA; the extension just shows
"in progress" while jittered re-attach loops spin. Serialization itself is a
defensible resource decision (one Lake workspace); the brittleness is that
the *queue is invisible and unordered*. A tiny adapter-side queue with a
queryable position would make waiting states honest in both UIs.

---

## D. Hygiene / structural drag

- **D1. Two monoliths generate the bug classes.** `server.mjs` (6,124 lines)
  and `content.js` (4,013 lines, classic IIFE, ~60 module-level mutable
  globals). The audit's stale-copy findings and the
  PLAN-self-repair-stale-offers fixes are the signature failure mode of
  render-from-globals; the companion's divergent-recency/duplicate-regex
  findings are the signature failure mode of a 6 kloc file. The seams are
  already visible (status derivation, job store, prompts, project registry,
  share/export, lean-pane handlers) — extraction is mechanical.
- **D2. Duplicated helpers with comment-enforced sync.** The content script
  can't `import`, so `normalizeTargetText` (`content.js:3979` vs
  `shared/theoremParser.mjs:10`) and `buildLeaSessionUrl` (`content.js:3414`
  vs `server.mjs:4769`) exist twice, kept aligned by comments (the
  acknowledged AUDIT L9 pattern). A trivial build step (or moving these into
  the lazily-imported `.mjs` modules, which content.js already does for
  three other files) removes the class.
- **D3. The companion still shells out to `lake` itself** (`runLeanCheck`,
  `server.mjs:4679`) even though the adapter exposes lean-check/rebuild —
  so the companion needs `lean`/`lake` on PATH (doctor checks for them) and
  there are three check paths with different semantics (companion-spawned
  lake, adapter LSP check, adapter rebuild). Consolidating on the adapter's
  two would drop a toolchain dependency and a timeout/kill code path.
- **D4. `start-dev.sh` destroys session data by default** (`--keep-data` to
  opt out). A destructive default in the most-typed command is a footgun;
  the safe direction is opt-in wiping (`--fresh`).
- **D5. Stale root READMEs** — *withdrawn on verification*: both READMEs were
  already rewritten for the in-process architecture (the `easy_install`
  branch). The stale artifact was `CLAUDE.md`'s "stale docs" warning itself,
  which repeated a pre-rewrite state; it has been replaced with doc pointers.

---

## Priority shortlist (if/when acting on this)

1. **C1 — companion → extension push channel (SSE).** Best
   responsiveness-per-effort in the system; also deletes poll-failure code.
2. **A4/C3 — adapter-side run queue; decouple run start from stream
   attach.** Removes the largest compensation machinery in two clients.
3. **B1 — mid-run spend enforcement in `bridge.py`.** Small, closes a real
   money hole.
4. **A1 (+A3) — single status authority.** Make the adapter's verdict ledger
   (code_steps + edit checks) the one source; render companion/pane
   statuses as views. Biggest long-term de-brittling.
5. **A2 — single writer for the workspace** (all mutations via adapter; git
   revert instead of stash/restore).
6. **B2 — Overleaf-hook watchdog + degradation banner.** Cheap insurance
   against the highest-impact external break.
7. **A5/A6/D2 — drift-surface consolidation** (one model catalog, one status
   enum per boundary, shared helpers via build step).
