# Overleaf Extension Audit — 2026-07-06

Scope: everything under `apps/overleaf-extension/` (Chrome extension, companion
server, shared parsers), including the uncommitted changes on
`cleanup_after_self_heal` (the 409 run-queueing in `leaApiClient.mjs` and the
rename-bookkeeping fixes in `server.mjs`). Baseline: all 342 tests in
`npm test -w apps/overleaf-extension` pass.

Each finding lists: what's wrong, how it fails, and a suggested fix.
Checkboxes are for tracking remediation.

## Remediation status (2026-07-06)

**All findings below have been addressed.** After the fixes the suite is green
at **346 tests** (342 original + 4 new regression tests for H4×2, H1, and the
M1 sorry-marker helper; the M8 config test was rewritten to the new
warn-not-throw contract). Fixes are tagged `AUDIT <id>` in the source.

| Finding | Status | Where the fix landed |
|---|---|---|
| H1 | ✅ Fixed | `writeJson` now routes through `atomicWriteJson` with per-path serialization; `readJson` backs up corrupt files and starts fresh. New test in `companion.test.mjs`. |
| H2 | ✅ Fixed | `cleanupPreviousRunArtifacts` stashes proof/markdown backups on `job.retryCleanup`; `restorePreviousRunArtifacts` restores on failed/timed-out/interrupted/max-spend runs, drops them on verified ones. |
| H3 | ✅ Fixed | `setCorsHeaders` reflects only overleaf.com / extension origins; unknown browser origins are 403'd in `routeRequest`. |
| H4 | ✅ Fixed | `runApiProofJob` re-attaches on a mid-stream drop, adopts a terminal run row, and bounds-out with an interrupt when the adapter is unreachable. Two new tests. |
| H5 | ✅ Fixed | `spendLimitReached`/`currentSpendUsd` now read the adapter's `/api/stats` global (TTL-cached), with the local tally only as fallback; popover uses the same source. |
| M1 | ✅ Fixed | Single `containsSorryMarker` helper (correct `\b`, comment-stripping) replaces all three copies. New unit test. |
| M2 | ✅ Fixed | `pollChatSession` retries with backoff up to a cap instead of freezing the panel. |
| M3 | ✅ Fixed | Edit-save records a fail-closed "unconfirmed" verdict and returns 200 when the post-write check can't run. |
| M4 | ✅ Fixed | Scroll handler rAF-throttled; in-progress `/statuses` poll 3s; usage popover 5s. |
| M5 | ✅ Fixed | 50 MB body cap; bad JSON → 400; oversized → 413. |
| M6 | ✅ Fixed | `paneItemToFormalizeTarget` uses the marker `label`, not the declaration name. |
| M7 | ✅ Fixed | `collapseBlankRunsOutsideFences` leaves ```lean fences untouched; markdown writes are atomic. |
| M8 | ✅ Fixed | `config.mjs` warns and falls back to null on a bad env value instead of throwing at startup. |
| M9 | ✅ Fixed | `runLeanCheck` runs in its own process group and escalates SIGTERM→SIGKILL. |
| M10 | ✅ Fixed | uiBridge scoped to `:5173`; `openOverleafDocumentTab` validates the URL is an overleaf.com origin. |
| L1 | ✅ Fixed | Removed `extractTurnProgress`+helpers, `delay`, `findNamedSectionHeadingStart`, and the buggy `isChatRunActive` export. |
| L2 | ✅ Fixed | The `lastRunImpact` clear is persisted before the run starts. |
| L3 | ✅ Fixed | `inductive`/`instance` (and structure/class) added to the declaration keyword lists; `isDefLike` now = "not theorem/lemma". |
| L4 | ✅ Fixed | `buildLeaSessionUrl` guards `new URL` and falls back to the default origin. |
| L5 | ✅ Fixed | `pruneRepairBatches` drops settled batches beyond a recent window. |
| L6/L7 | ✅ Fixed | Shared `jobRecency`/`jobsByRecencyDesc` (finishedAt‖startedAt, plain string compare) used by every "latest job" selection. |
| L8 | ✅ Fixed | Jittered retry delay for queued/dropped re-attaches. |
| L9 | ✅ Fixed | Sync comments on the `o4-mini` placeholder in content.js and options.js. |
| L10 | ✅ Fixed | Boot warning now reflects the configured model's provider via `validateLeaRuntime`. |

---

## High — data loss, security, or wrong results

### H1. `jobs.json` / `settings.json` writes are not atomic, and a corrupt file bricks companion startup
- **Where:** [companion/server.mjs:5517](companion/server.mjs#L5517) (`writeJson`), [companion/server.mjs:5508](companion/server.mjs#L5508) (`readJson`), [companion/server.mjs:136](companion/server.mjs#L136) (`createServer`)
- **What:** All persistence goes through `writeJson`, a plain
  `fs.writeFile` (truncate-then-write). An atomic implementation exists
  (`atomicWriteJson`/`atomicWriteFile`, [server.mjs:3026](companion/server.mjs#L3026)) **but is never
  called** — it's dead code. Worse, several async jobs run concurrently
  (formalize + repair batch + cascade continuations) and each calls
  `writeJson(state.jobsPath, state.jobs)` freely; two overlapping
  `fs.writeFile` calls on the same path can interleave. And on startup,
  `readJson` only tolerates `ENOENT` — any JSON parse error is rethrown, so a
  half-written `jobs.json` makes `createServer()` throw and the companion
  refuses to start until the file is deleted by hand.
- **Failure scenario:** Companion is killed (or crashes) mid-write during a
  run's progress update → truncated `jobs.json` → every subsequent
  `npm run dev:overleaf` crashes at boot with a JSON parse error.
- **Fix:** Route `writeJson` through the existing `atomicWriteFile`
  (temp + rename), serialize writes per path (a simple promise-chain mutex),
  and make `readJson` fall back to the default (with a warning + `.corrupt`
  backup) on parse errors.

### H2. Re-formalize deletes the previous good proof *before* the run, with no restore on failure
- **Where:** [companion/server.mjs:282-306](companion/server.mjs#L282) (`handleFormalize` →
  `cleanupPreviousRunArtifacts`), [companion/server.mjs:3236](companion/server.mjs#L3236)
- **What:** When a target is re-formalized (and there's no reusable stub),
  the previous run's proof file and its project-markdown entry are removed
  up front. The removal record is stored on the job as `job.retryCleanup` —
  but nothing ever reads it back: if the new run fails, times out, or hits
  the spend cap, the old verified proof is simply gone (from disk; it still
  exists in adapter git history, but nothing in this app recovers it).
  Dependents that `import` the deleted module now fail to build too.
- **Failure scenario:** User re-formalizes a proved theorem with a slightly
  edited statement; the run hits `max_turns` → the item drops from
  "formalized" to "unformalized" and every downstream item breaks, even
  though nothing new was produced.
- **Fix:** Defer cleanup until the new run has produced a verified artifact
  (cleanup in `applyProofOutcomeToJob` on success), or actually implement
  restore-from-`retryCleanup` on failure (the adapter's git history makes
  this recoverable via the session file-write API).

### H3. Companion HTTP API: `Access-Control-Allow-Origin: *` with no authentication on endpoints that spend money and write secrets
- **Where:** [companion/server.mjs:5528-5532](companion/server.mjs#L5528) (`setCorsHeaders`), routes at
  [companion/server.mjs:2450](companion/server.mjs#L2450)
- **What:** Every response carries `ACAO: *`, and no endpoint requires any
  token. Any web page running in the user's browser (any `http://localhost`
  page, and — depending on the browser's Private Network Access enforcement —
  public sites too) can: start paid LLM runs (`/formalize`, `/stub`,
  `/lean-pane/chat/message`, `/lean-pane/repair/*`), rewrite provider API
  keys and raise the spend cap (`POST /settings/lea` writes keys into the
  root `.env` and pushes them to the adapter), set the GitHub push token,
  set the git remote and push the project to an arbitrary repo
  (`/share/github/*`), download the full Lean project
  (`GET /project-export`), and read job logs (`GET /jobs/<id>`).
- **Failure scenario:** A malicious page fetches
  `http://127.0.0.1:31245/share/github/remote` with its own repo URL then
  `/share/github/push` — exfiltrating the user's proofs — or simply burns
  the user's OpenAI budget with `/formalize` calls.
- **Fix:** Bind CORS to the two legitimate origins
  (`https://www.overleaf.com` + the configured Lea UI origin) instead of
  `*`, and/or require a shared token (the extension already round-trips
  settings, so distributing a token via `/settings` handshake on first
  pairing is feasible). At minimum, gate the mutating + exfiltrating
  endpoints.

### H4. A dropped SSE stream mid-run fails the job client-side while the adapter run keeps going (and keeps spending)
- **Where:** [companion/leaApiClient.mjs:516-548](companion/leaApiClient.mjs#L516) (`runApiProofJob` loop),
  [companion/leaApiClient.mjs:406-420](companion/leaApiClient.mjs#L406) (`streamApiRun`)
- **What:** The new 409 loop re-attaches only when the attach is *rejected*
  with 409. If an already-attached stream dies mid-run (adapter hiccup,
  transient network error, companion-side fetch abort), `streamApiRun`
  returns `{ ok:false }` with no `httpStatus`, the loop breaks, and the job
  is finalized as failed — but the adapter-side run is neither re-attached
  nor interrupted. The prover keeps running (and billing) with no observer;
  its eventual success is never applied to the job, so the badge reads
  "failed" for a theorem that was actually proved.
- **Failure scenario:** 10-minute proof run; adapter's HTTP connection is
  reset at minute 7 → companion marks the job failed, the run completes at
  minute 9, the money is spent, the status is wrong until a manual
  re-check happens to re-derive it from disk.
- **Fix:** On a mid-stream error (`doneStatus === null`, not aborted),
  consult the run row exactly like the 409 branch does: if
  `pending|running`, wait and re-attach; if terminal, adopt the row's
  outcome; only fail (and `interruptApiRun`) when the row is unreachable
  past the deadline.

### H5. Spend-cap enforcement uses a different ledger than the one displayed — and it resets on every `start-dev.sh`
- **Where:** [companion/server.mjs:4190-4194](companion/server.mjs#L4190) (`spendLimitReached`),
  [companion/server.mjs:2327-2359](companion/server.mjs#L2327) (`handleGetUsage`)
- **What:** The popover shows spend from the adapter's `/api/stats`
  (all-time, includes standalone-UI runs, survives resets). The *enforced*
  check (`spendLimitReached`) sums the companion's own `state.jobs` — which
  `start-dev.sh` wipes by default, and which never includes runs made from
  the standalone UI or chat-mirror runs recorded on sessions but not jobs.
  The two can disagree in both directions: the popover says "limit reached"
  while runs still start, or (post-reset) the cap is effectively lifted.
  Additionally, enforcement only happens **between** runs
  (`recordUsageAndEnforceSpendLimit` is called once, after `runApiProofJob`
  returns) — a single run can overshoot the cap by its entire cost.
- **Fix:** Enforce against the same adapter `/api/stats` number the popover
  displays (fall back to the local tally only when the adapter is
  unreachable), and document that enforcement is per-run-boundary.

---

## Medium — functional bugs and fragile behavior

### M1. `sorry`/`admit` detection regex is wrong in the two most load-bearing places
- **Where:** [companion/server.mjs:5073](companion/server.mjs#L5073) (`getLeaProofStatusFromEntry`),
  [companion/server.mjs:5136](companion/server.mjs#L5136) (`getLeaDirectProofStatus`)
- **What:** Both use `/\bsorry\b|admit\b/` — the `admit` alternative is
  missing its **leading** `\b`, so any word *ending* in "admit"
  (`readmit`, `Nat.readmit`, a comment containing "readmitted") marks a
  fully-proved file as `sorry_stub`. The codebase has three variants of
  this check: the correct `/\b(sorry|admit)\b/` in `validateStubArtifact`
  ([server.mjs:3908](companion/server.mjs#L3908)), the correct
  `/\bsorry\b|\badmit\b/` in [leanDependencyGraph.mjs:207](companion/leanDependencyGraph.mjs#L207), and the broken
  one above. All three also match inside comments and string literals
  (`-- no sorry here` → stub), which matters because this regex *overrides
  a job's recorded "formalized" verdict* (the stub-evidence branch in
  `getTheoremStatus`, [server.mjs:4885-4891](companion/server.mjs#L4885)).
- **Fix:** One shared `containsSorryMarker(content)` helper with
  `/\b(sorry|admit)\b/` used everywhere; strip `--`/`/- -/` comments before
  testing if false positives on comments are worth eliminating.

### M2. A single failed chat poll silently freezes the chat panel forever
- **Where:** [extension/content.js:1697-1729](extension/content.js#L1697) (`startChatPolling` /
  `pollChatSession`)
- **What:** Polling reschedules only from inside a *successful*
  `pollChatSession`. Callers invoke it as
  `pollChatSession().catch(() => {})`, so if the fetch itself throws once
  (companion restarting, transient network error), no next poll is
  scheduled, `leanPaneChatSending` stays `true`, and the panel shows "Lea
  is working…" indefinitely while the composer stays disabled — even after
  the run finishes.
- **Fix:** Wrap the body in try/catch and reschedule (with backoff / a
  retry cap) on failure, or move rescheduling to a `finally`.

### M3. Manual-edit save can write the file and then lose the verdict
- **Where:** [companion/server.mjs:1280-1293](companion/server.mjs#L1280) (`handleLeanPaneEditSave`)
- **What:** The sequence is: write file → run lean-check → record verdict →
  cascade. If the lean-check *request* fails (adapter restart, timeout),
  the handler returns 502 — but the file **was already saved**. No verdict
  is recorded and no cascade runs, so the pane keeps showing the pre-edit
  status ("valid") for content that was never checked, and dependents are
  never re-verified. The user sees "Save failed" and may save again, but
  if they don't, state is silently stale. The same shape exists in
  `runPostRunCascade` when `checkStatus` is absent (deliberate there, but
  the edit path claims failure while having succeeded at the write).
- **Fix:** On check failure after a successful write, record an
  `"unconfirmed"`-style verdict (the cascade already has the fail-closed
  vocabulary for exactly this) instead of returning a plain 502, or
  return 200 with `ownResult.checkStatus: "unknown"` so the client shows
  the truth: saved, not verified.

### M4. Aggressive, unthrottled polling loops
- **Where:**
  - [extension/content.js:2623-2664](extension/content.js#L2623): `/statuses` re-polls every **250 ms**
    for the entire duration of any in-progress run; each call makes the
    companion do per-target filesystem scans and possibly adapter fetches.
  - [extension/pageBridge.js:354-356](extension/pageBridge.js#L354): the full document text +
    re-parse is posted every 1.5 s unconditionally; each message triggers
    `renderStatusBadges` + `scheduleStatusRefresh` in content.js.
  - [extension/content.js:163-166](extension/content.js#L163): a capture-phase `scroll` listener
    calls `requestTargets()` + `renderStatusBadges()` on **every** scroll
    event with no throttling — each `requestTargets` makes pageBridge
    re-parse the whole document.
  - [extension/content.js:3326-3340](extension/content.js#L3326): `/usage` polled every **1 s**
    while the settings popover is open (each hit fans out to the
    adapter's `/api/stats`).
- **Fix:** 2–4 s for the in-progress `/statuses` loop (matching
  `LEAN_PANE_POLL_DELAY_MS`), rAF-throttle the scroll handler, and 5 s for
  the usage popover.

### M5. Companion accepts unbounded request bodies and turns malformed JSON into a 500
- **Where:** [companion/server.mjs:5499-5506](companion/server.mjs#L5499) (`readBodyJson`),
  [companion/server.mjs:143-152](companion/server.mjs#L143)
- **What:** No size cap on the buffered request body (the mirror endpoint
  legitimately receives whole projects, but nothing stops a 2 GB POST from
  OOMing the process), and `JSON.parse` failures propagate to the generic
  `internal_error` 500 instead of a 400.
- **Fix:** Cap at a sane limit (e.g. 50 MB), catch parse errors →
  `400 invalid_json`.

### M6. Pane "Re-formalize" of a renamed item forks the job identity
- **Where:** [extension/leanPaneView.mjs:308-316](extension/leanPaneView.mjs#L308)
  (`paneItemToFormalizeTarget` uses `leanDeclarationName || label`),
  [companion/server.mjs:206](companion/server.mjs#L206) (`handleFormalize` keys the job by that label)
- **What:** The whole rename-bookkeeping design pins job identity to the
  LaTeX marker label and bridges the declaration-name divergence via
  `findLatestJobWithLeaSessionByDeclarationName` — but only in
  `resolveEditSession`/`resolveChatSession`/`resolveDependentSession`.
  `handleFormalize` does no such bridging: the pane sends the *current
  declaration name* as `targetLabel`, so after a manual rename, hitting
  "Re-formalize" in the pane creates a job under a **new** jobKey
  (`slug:theorem:<newName>`), while the in-document badge still posts the
  original label. The two surfaces now track two divergent job histories
  (busy-checks don't compose: one can start while the other is
  in-progress), and the old key's cleanup path won't find the new key's
  artifacts.
- **Fix:** Either send the marker `label` (not `leanDeclarationName`) from
  `paneItemToFormalizeTarget`, or apply the same declaration-name bridge in
  `handleFormalize` before building the target.

### M7. `project.md` rewriting can corrupt recorded Lean signatures
- **Where:** [companion/server.mjs:5319](companion/server.mjs#L5319), [companion/server.mjs:3310](companion/server.mjs#L3310)
  (`upsertProjectTheoremEntry` / `removeProjectTheoremEntries` both end
  with `.replace(/\n{3,}/g, "\n\n")`)
- **What:** The blank-line squash runs over the *whole* markdown, including
  inside the ` ```lean ` signature fences and users'/agents' prose. A
  signature or solving-process section containing two consecutive blank
  lines is silently reflowed on every upsert of *any other* theorem. Both
  writes are also non-atomic (see H1) on the file the artifact-diffing
  (`identifyLeaArtifact`) treats as evidence.
- **Fix:** Apply the squash only to the seam text the code itself
  constructs (or drop it entirely), and write via the atomic helper.

### M8. Bad env values crash the companion at startup
- **Where:** [companion/config.mjs:62-68](companion/config.mjs#L62)
  (`normalizeOptionalNonNegativeNumber` throws), called from
  `applyEnvDefaults` inside `createServer`
- **What:** `LEA_MAX_SPEND_USD=abc` (or negative) in `.env` throws during
  `createServer()`; the process dies with an unhandled error rather than a
  message pointing at the bad variable. (`LEA_MAX_TURNS=abc` degrades
  gracefully to `NaN` → default via `||` chains, an accidental but working
  path.)
- **Fix:** Warn + fall back to `null` for unparseable env values at
  startup; keep the throw for interactive settings validation only.

### M9. `runLeanCheck` never escalates past SIGTERM
- **Where:** [companion/server.mjs:4233-4281](companion/server.mjs#L4233)
- **What:** On the 60 s timeout the child gets `SIGTERM`; `lake env lean`
  (and the elan toolchain underneath) can ignore it while compiling. The
  promise resolves anyway, so the orphaned compiler keeps a core busy, and
  concurrent leaked checks can pile up.
- **Fix:** Follow up with `SIGKILL` after a grace period; also consider
  `detached`+process-group kill so `lake`'s children die too.

### M10. Any `localhost` page can drive the extension's tab opener
- **Where:** [extension/manifest.json:19-22](extension/manifest.json#L19) (uiBridge injected into all
  `http://localhost/*` and `http://127.0.0.1/*` pages),
  [extension/background.js:14-24,51-73](extension/background.js#L14) (`OPEN_OVERLEAF_DOCUMENT` opens an
  arbitrary http(s) URL in a new active tab)
- **What:** The bridge is meant for the Lea UI, but it's injected into
  every local page (dev servers, dashboards, anything on those hosts). Any
  such page can post `OPEN_OVERLEAF_DOCUMENT` with any URL and the
  background worker will focus/open it — a tab-spam/phishing primitive,
  and `tabs.query` results implicitly leak "is this URL open" timing.
- **Fix:** Restrict the uiBridge match pattern to the configured Lea UI
  origin/port (`http://localhost:5173/*` by default), and validate in the
  background worker that the requested URL's host is an Overleaf origin.

---

## Low — hygiene, dead code, small inconsistencies

### L1. Dead code in the companion
- `atomicWriteJson` / `atomicWriteFile` ([server.mjs:3026-3038](companion/server.mjs#L3026)) — never
  called (and their absence is finding H1).
- `extractTurnProgress` + its private helpers `firstPositiveInteger` /
  `turnProgressSources` ([server.mjs:4096-4136](companion/server.mjs#L4096)) — never called.
- `delay` ([server.mjs:4307](companion/server.mjs#L4307)) — never called.
- `findNamedSectionHeadingStart` ([server.mjs:5422](companion/server.mjs#L5422)) — never called.
- `isChatRunActive` ([chatPrompt.mjs:180-185](companion/chatPrompt.mjs#L180)) — exported, never
  imported anywhere; it also contains an internal bug (the
  `status === "running" || ...` check is unreachable because
  `if (response.activeRun) return true` already returned), so if it's ever
  adopted it will misbehave. Delete or fix before reuse.

### L2. `handleChatMessage` clears `lastRunImpact` in memory but doesn't persist the clear on the failure path
- **Where:** [companion/server.mjs:913-915](companion/server.mjs#L913)
- **What:** The previous run's impact is deleted before `startChatRun`; if
  the run fails to start, the handler returns early without
  `persistChatSessions`, so the on-disk record still has the impact while
  memory doesn't — a restart resurrects a notice the user's send was meant
  to supersede. Cosmetic, but it's exactly the stale-offer class the
  self-repair plans keep fixing.
- **Fix:** Persist after the delete (or only delete after a successful
  start, accepting the documented race the comment already discusses).

### L3. `containsDeclaration` / signature parsing don't know all Lean declaration keywords
- **Where:** [companion/server.mjs:5475-5480](companion/server.mjs#L5475) (`theorem|lemma|def|abbrev|structure|class`),
  [companion/leanSignatureDiff.mjs:11](companion/leanSignatureDiff.mjs#L11) (`theorem|lemma|def|abbrev` only)
- **What:** A definition the agent legitimately models as `inductive` or
  `instance` is invisible to `containsDeclaration` — session-step lookup
  then falls back to "sole .lean step" heuristics or misses entirely, and
  `parseDeclarationHeader` returns null (classified as a blanket
  "signature" change — safe, but noisy: every proof-only edit to such a
  file triggers a full cascade).
- **Fix:** Add `inductive|instance|structure|class` to both keyword lists
  (the diff module's header-end scanner already handles `where`).

### L4. `buildLeaSessionUrl` in content.js can throw during render
- **Where:** [extension/content.js:3014-3018](extension/content.js#L3014), reached from
  `getLeaSessionLink` ([content.js:2997](extension/content.js#L2997)) inside popover rendering
- **What:** `new URL(baseUrl)` throws on a malformed stored
  `leaUiBaseUrl` (user typo in options), and the call sites don't guard —
  the popover render dies. The companion normalizes its copy; the
  extension-local fallback path doesn't.
- **Fix:** try/catch with fallback to `DEFAULT_LEA_UI_BASE_URL`.

### L5. `state.repairBatches` grows without bound
- **Where:** [companion/server.mjs:1964-1965](companion/server.mjs#L1964)
- **What:** Every batch is kept in memory forever (including its
  `importsByModule` map). Days-long companion sessions with many batches
  leak steadily. Also `repairBatchCounter` + `Date.now()` ids are fine, but
  nothing ever deletes a finished batch.
- **Fix:** Prune done batches after the status endpoint has served them
  (e.g. keep the last N or expire after an hour).

### L6. `findLatestJob` sorts by `startedAt` while the newest-terminal selection uses `finishedAt || startedAt`
- **Where:** [companion/server.mjs:3102-3106](companion/server.mjs#L3102) vs
  [companion/server.mjs:4862](companion/server.mjs#L4862)
- **What:** Two adjacent recency definitions. A job that started earlier
  but finished later (possible with the new 409 queueing, where a queued
  run can outlive a later-started chat run) is "latest" under one rule and
  not the other. Today's callers mostly tolerate it; it's a trap for the
  next selection bug of the exact class the newest-terminal comment
  describes.
- **Fix:** One shared `jobRecency` helper used by both.

### L7. ISO-string recency comparisons via `localeCompare`
- **Where:** [companion/server.mjs:1177](companion/server.mjs#L1177), [server.mjs:3105](companion/server.mjs#L3105), [server.mjs:5218](companion/server.mjs#L5218), etc.
- **What:** `String(b.finishedAt || b.startedAt).localeCompare(...)` works
  for well-formed ISO strings but is locale-sensitive by contract and
  treats `null`→`"null"` oddly (`"null"` > any ISO year < 3000 in most
  locales — an in-flight job with `finishedAt: null` never reaches these
  filters today, but the coercion is fragile). Plain `<`/`>` string
  comparison is sufficient and deterministic.

### L8. New 409 queueing: no ordering between queued waiters
- **Where:** [companion/leaApiClient.mjs:516-548](companion/leaApiClient.mjs#L516)
- **What:** Multiple queued runs each retry every 3 s; which one attaches
  when the slot frees is a race, so a run can be starved past its
  `timeoutMs` while later arrivals win repeatedly. Bounded (timeout
  interrupts the pending run — tested), but the batch/e2e behavior under
  contention is nondeterministic. Fine for the current single-user tool;
  worth a comment or a jittered/backoff retry so waiters don't
  thundering-herd in lockstep.

### L9. Stale defaults drift between surfaces
- **Where:** [extension/content.js:4](extension/content.js#L4), [extension/options.js:8](extension/options.js#L8)
  (`DEFAULT_LEA_MODEL = "o4-mini"`) vs the catalog default in
  `packages/lea-model-catalog` (companion uses `DEFAULT_LEA_MODEL` from the
  catalog).
- **What:** The extension's hardcoded fallback model is only used before
  the first successful `/settings` fetch, but if the catalog default moves
  again (the legacy-alias map in server.mjs shows it already has, twice),
  the extension will render/save a model the companion may re-map. Import
  isn't possible in the content script, but the constant deserves a
  "keep in sync with lea-model-catalog" comment or removal in favor of
  "unknown until /settings responds".

### L10. Companion startup warning checks the wrong condition
- **Where:** [companion/server.mjs:5550-5552](companion/server.mjs#L5550)
- **What:** The boot log warns only about `OPENAI_API_KEY`, but the
  configured model may be Anthropic/Google (multi-provider support exists
  everywhere else). Cosmetic, but it tells Anthropic-key users "jobs will
  not start" incorrectly, and stays silent for a Google-model user with no
  key at all.
- **Fix:** Use `validateLeaRuntime(state, { requireApiKey: true })` for the
  boot message.

---

## Explicitly checked, no action needed

- **The uncommitted 409 re-attach loop** (`waitBeforeRetry`,
  `fetchApiRunRow`, terminal-row adoption): logic is sound; abort/timeout
  interplay is correct and covered by the four new tests. The remaining gap
  is the *mid-stream* disconnect case — tracked as H4 above.
- **The uncommitted rename bookkeeping** (`recordRenamedDeclaration`,
  `repaired` in the terminal-candidate list): consistent with the
  jobKey-pinned identity model; the batch update over all jobs under one
  key is correct because an item has one working file. Covered by new
  tests.
- **`zipTex.mjs`**: correct for Overleaf's store/deflate zips; no ZIP64,
  which Overleaf doesn't produce at the relevant sizes. Data-descriptor
  entries are handled by reading sizes from the central directory.
- **`targetParserCore.mjs` masking** (`maskOpaqueSpans`): the
  verbatim-interior masking is applied consistently across all scanners,
  including `leanPaneManifest.mjs`'s loose label extraction.
- **Cascade fail-closed behavior** (`cascadeVerify.mjs`): rebuild-first,
  real-`lake build` verdicts, and transitive propagation are all present
  and tested; injection keeps it cycle-free.
- **Secrets handling**: provider keys and the GitHub token are never
  persisted in `chrome.storage` or `settings.json` (`sanitizeSettingsForStorage`
  strips them); responses carry presence booleans only. (The exposure risk
  is the CORS surface, H3 — not storage.)
