# Weekly Work Report — Jul 7 – Jul 13, 2026

**Author:** Daniel Arturi (`darturi`)
**Repository:** `lea-ecosystem` monorepo
**Scope:** 16 commits (13 direct + 3 merges), ~11,400 insertions / ~1,000 deletions across the Overleaf extension, the Node companion, the FastAPI adapter, and the install/setup scripts.

Where last week was about *building* the Overleaf Lean-pane workflow (navigation, inline tags, manual edits, self-repair, GitHub export), this week was about *hardening* it. A system review on Jul 10 catalogued the architecture's real weaknesses; the bulk of the week executed the resulting plan — a mid-run spend cap, an integration harness, a reworked run lifecycle, a push channel that replaces polling, and the first pass at a single source of truth for proof state. Alongside that structural work sat a set of user-facing fixes: human-readable project names, a resizable pane, proportional per-file progress bars, a code-rendering block, and a one-command installer.

---

## 1. System review and the hardening plan

The week opened with a full architectural review (`docs/SYSTEM-REVIEW-2026-07-10.md`) and a phased execution plan (`docs/PLAN-system-hardening.md`). The review named the system's structural risks — money could overshoot the spend cap mid-run, the UI learned about state changes only by polling every 3–4 seconds, proof status was derived from five overlapping sources of truth, and the companion wrote proof files directly to disk in violation of the "git owns proof content" rule.

The plan turned those findings into seven phases governed by five invariants that had to survive every change: two front ends but one backend on `:8001`; never store derived state; git owns proof content while SQLite owns metadata; every phase lands green on all test suites; and strangler-style refactoring over big-bang rewrites. This framing is the single most important design decision of the week — it made each subsequent change independently shippable and revertable rather than one large risky merge.

## 2. Quick safety wins (Phase 0)

**Mid-run spend enforcement.** Previously the spend cap was only checked *between* runs, so a single run could blow well past the limit. The cap is now checked inside `bridge.py` on each `UsageUpdated`/`TurnStarted` event, ending the run with a `max_spend` result. The design deliberately accepts a cooperative stop at the turn boundary — a single *turn* may overshoot, but a single *run* no longer can — because a hard mid-turn kill would corrupt in-flight file state. The companion maps the new status onto its existing spend-cap message path without re-interrupting.

**Job retention and safe defaults.** The companion's job store was extracted into its own module (`jobStore.mjs`) and now prunes old jobs while unconditionally preserving the newest job of each terminal status, so status-derivation semantics are unchanged. Separately, `start-dev.sh` was flipped to *keep* session data by default, with an explicit `--fresh` flag to wipe — the old default silently destroyed proofs on restart.

**Editor-hook watchdog.** Overleaf can change its editor internals and silently break the extension's attachment. A watchdog now detects when the page bridge loaded but the editor hook never fired, and shows a dismissible banner telling the user the extension may need an update — turning a silent failure into a visible, actionable one.

## 3. Integration harness (Phase 1)

A cross-layer contract harness (`tests/integration/`, `npm run test:integration`) was built *before* the refactors it was meant to de-risk. It boots a stub prover, the real adapter, and the real companion, then drives the extension's exact HTTP shapes end to end. The decision to build it first paid off immediately: on its first run it caught a real bug — companion job runs were recording `$0` of usage because session-detail rows lacked usage columns — which was fixed in `store.py`. It also became the place where the legacy `"success"` done-status alias was finally deleted, since the harness pins the wire vocabulary.

## 4. Run lifecycle rework (Phase 2)

The adapter's run endpoint became a server-side FIFO queue with an in-process event hub, and the events endpoint became a *pure observer*: a client can attach at any time, any number of times, and receive a catch-up replay synthesized from persisted state followed by a live tail. The key win is that the HTTP 409 "busy vs. finished" ambiguity disappears entirely, which let ~150 lines of compensation code (busy-retry loops, jitter, run-row miss bookkeeping) be deleted from the companion client. Queue position is *derived* (a count of earlier pending runs, honoring the no-derived-state invariant) and surfaced to the UI, so a waiting run now honestly shows "queued behind N runs" instead of a bare spinner.

## 5. Push channel and mirror efficiency (Phase 3)

**Sub-second updates via SSE.** This is the headline user-visible change (commit *"badge/pane/chat updates now arrive on push (sub-second) instead of a 3–4 s poll floor"*). The companion gained an event bus and a `GET /events` SSE route; the extension replaced four fast polling loops with a single `EventSource`. Events carry *keys, not payloads* — the extension refetches only the one thing that changed. Crucially, the polling code was not deleted but demoted: it stretches to a 30–60 s reconciliation cadence while the stream is healthy and automatically falls back to the fast cadences if the stream drops. The result: badge latency fell from ≤3–4 s to ≤0.5 s and idle traffic from ~40 requests/min toward ~1–2.

**Stop re-zipping the project.** The tex mirror was split into two tiers: a lightweight active-buffer sync that upserts just the live editor file on each edit-pause (via a new adapter `mode="upsert"` that never deletes absent files), and a full zip-download reconcile that runs only on activation, file switch, a 10-minute timer, or before a formalize. Steady editing of one file now triggers zero zip downloads while preserving the self-heal property.

## 6. Single source of truth for proof state (Phase 4 — "phase four pass one complete")

This was the deepest change, sequenced so each step ships and is observable alone:

- **4.1 — Structured artifact records.** A new `artifacts` table records one row per scope + declaration, written by the run finalizer from the run's own `FileChanged` set. Dual-write only at first: nothing read it until the divergence log confirmed it agreed with the old heuristic.
- **4.2 — Companion reads the index.** Adapter artifact rows became the *primary* identification source; the old before/after file-diff heuristic survives only as a logged fallback.
- **4.5 — Single workspace writer** (pulled ahead as the most self-contained). Retire/restore of proof files now happens through adapter endpoints backed by real git commits, not by the companion stashing proof bytes on job records — restoring the "git owns proof content" invariant that the companion had been violating.
- **4.4 — Two-source status merge.** A `target-status` endpoint serves per-declaration ledger evidence (file existence, a Python twin of the sorry-scanner, newest verdict across all sessions). Shipped behind `LEA_STATUS_ENGINE=ledger` with legacy still the default; the harness runs both engines on one live stack and asserts they agree.
- **4.3 — Registry demotion (to the safe boundary).** The adapter backfills the artifacts table from pre-index projects' registry markdown, with backfilled rows carrying NULL session/run so they can never masquerade as run-recorded truth.

The consistent design theme: dual-write and diff before cutting over, keep a labelled fallback, and gate the final deletion of legacy code behind a bake period.

## 7. User-facing features and fixes

**Human-readable project names.** Overleaf projects were shown by their URL-derived slug (`P6a4584b313b8ddc4ba20e377`). Users can now set a display name from the pane and settings. The important design call: the URL/project-id mapping stays the internal identity anchor, so renaming a display label is cheap and safe, while an actual Lean-*namespace* change is treated as an explicit project-wide migration (every proof file, import, and manifest entry must agree) rather than a side effect of editing a sidebar label.

**Per-file progress bars.** The single aggregate status chip per `.tex` file was replaced with a proportional bar (grey unformalized, red failed, green valid, yellow sorry-stubbed), so a mostly-done file with one problem is visually distinct from a barely-started one — the old precedence-based chip hid that distribution.

**Resizable Lean pane**, a **code-rendering block** (a Jaume request), and several correctness fixes: proofs no longer report "valid" before Lea has actually stopped thinking; and the UI and Overleaf no longer disagree about a proof's kind (proof / disproof / definition) after Lea failed to identify it.

**One-command install.** An `install.sh` / easy-install path was added and then streamlined, lowering the setup barrier ahead of wider distribution.

---

## Remaining tasks and why

The hardening plan is roughly two-thirds executed. What remains, and the reason each is still on the list:

**~~Flip the status engine default to `ledger` (finishes 4.3/4.4).~~ Done 2026-07-13.** The open semantic was settled in favor of file truth: after a failed retry restores a previous proof, status reports the restored file's validity (`formalized`) — the failed attempt stays reachable through the job history and session link. The legacy five-source engine, its markdown readers, and the markdown-diff identification fallback are deleted; `LEA_STATUS_ENGINE` is ignored (with a startup warning); retry cleanup and `uses=` resolution now answer from the adapter's ledger/index. The registry markdown survives as a write-only, agent-facing view.

**Phase 5 — Drift-surface consolidation (~4–5 days).** Several facts still live in two places and can drift: the model catalog (duplicated Python literals vs. the shared `models.json`), the status vocabulary (three hand-maintained enums), settings (companion still writes shared values to `.env`), a few copy-pasted UI helpers, and the companion's own redundant Lean toolchain. The plan collapses each to a single generated or shared source. It is scheduled after the harness (Phase 1) because that harness pins the wire values these consolidations must not change.

**Phase 6 — Structural sweep (~3–4 days).** `server.mjs` and `content.js` are still very large (the largest source file is ~6,100 lines against a target of ≤1,500). Because earlier phases already extracted their seams into modules, this phase can finish splitting them and give the now-pure logic modules direct unit tests — much of `content.js` becomes testable for the first time, and the class of stale-global bugs dies with the module-level `let`s.

**Phase 7 — Pairing token (gate before any beta).** There is currently no authentication between the extension, the companion, and the adapter. Before the extension is distributed to anyone outside the developer's own machine, a pairing token must guard the mutating and data-exfiltrating endpoints, because a local companion server otherwise trusts any page that can reach `localhost`. This is explicitly a pre-beta release gate.

**Explicitly deferred (recorded so they stay deliberate):** true parallel prover runs (mitigated for now by honest queueing), self-hosted Overleaf support, and replacing the agent's markdown prompt contract with structured tool output — the last requires changes inside the vendored prover, governed by that package's own design guardrails.

---

## Test posture

Every phase landed green by design — adapter pytest, the Node frontend and Overleaf suites, and (from Phase 1 onward) the integration harness all pass at each phase boundary. New test coverage tracked the work: spend-cap and artifact-table tests in the adapter, job-retention and SSE tests in the companion, an HTTP-level SSE test and an end-to-end push test in the harness, and both status engines diffed against each other on scripted scenarios.
