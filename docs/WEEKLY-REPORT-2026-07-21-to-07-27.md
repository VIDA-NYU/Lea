# Weekly Work Report — Jul 21 – Jul 27, 2026

**Authors:** Daniel Arturi (`darturi`) · Shaswat Patel (`shaswatpatel123`)
**Repository:** `lea-ecosystem` monorepo
**Branch:** `issue_address`
**Branches:** `fix/subagent-workflow` and `gen_repair` (merged into the integration
line) · `codex/refresh-lea-sandbox` / `sandbox/main` (distribution follow-up)
**Scope:** 32 commits dated this week now reachable from `issue_address`
(29 direct + 3 merges), plus one Docker fix on the sandbox distribution branch.

Last week closed the Overleaf status-engine migration, built project-level batch
Stub / Formalize with interruption, and prepared a prebuilt Docker distribution.
This week brought that work together and then hardened the resulting system. The
sub-agent experiment became a first-class, streamed, stoppable, parallel workflow;
the Overleaf Lean pane gained the standalone UI's project blueprint; the two front
ends were brought onto one visual language; and a full-repository audit was
followed by a concentrated security, correctness, concurrency, and performance
remediation pass.

The audit is the clearest measure of the week's depth. It found 31 issues that the
then-green 462-test adapter suite did not cover. By week's end the adapter had 602
passing tests: 23 findings were fixed, two received honest partial mitigations,
one incorrect finding was withdrawn after its proposed regression test passed
against the old code, and the remaining low-severity or architectural work stayed
documented rather than being silently declared complete.

---

## 1. Sub-agents became a real execution model

The sub-agent work moved beyond "a parent can call a child" into an observable,
controllable concurrency model spanning the prover, adapter, and React UI. The
week opened with live-testing fixes, then completed the parallel execution and
context-management phases of the v2.3.1 plan.

### Resource and correctness fixes from live use

Testing on a hard Burnside proof surfaced failures that unit-only development had
not:

- A proof-candidate child was being handed the coordinator's absolute canonical
  path, outside the child's scratch sandbox, so it could complete without writing
  a usable candidate. Child writes now redirect an out-of-sandbox target to a
  scratch-local basename, while the main agent retains the strict rejection.
- Spawn cards had accumulated at the end of the chat instead of where they were
  created. The UI now interleaves them by creation order, coalesces adjacent
  spawns, and expands each child in place to its final result.
- Approval cards could render above a later write because they used a lagging
  sequence reference. Their sequence is now stamped against the real maximum
  across messages and code steps.
- The proof-candidate turn cap rose from 24 to 150 so a child can perform a
  realistic write/check/edit proof loop.

The most serious live defect was a Lean worker leak. Every checked scratch file
was opened through the shared LSP daemon but never closed, leaving roughly 118
`lean --worker` processes after one session and enough resident memory to force
the machine into swap. `LeanDaemon.close_documents_under()` now closes every
finished child's documents in a `finally`, skips files still in use, and resolves
paths before comparison so macOS symlinks cannot hide a document. A real-Lean
concurrency test also verified four children checking same-named files without
cross-talk while one completed child's documents were reaped.

### Streamed, stoppable, parallel children

`spawn_subagent` was split into preparation and execution so the system can emit a
`SubagentStarted` event before the child blocks. Child events are wrapped as
`SubagentProgress` and streamed upward, but deliberately never enter the
coordinator model's transcript: the parent still receives only the child's
distilled final result, preserving context isolation.

Parallelism is structured rather than indiscriminate:

1. child spawns from one turn run on threads with copied `ContextVar` state and a
   maximum of five concurrent children;
2. independent read-only tools (`read_file`, `search_mathlib`) may run in
   parallel; and
3. writers and checkers remain serial and ordered, so a `write → lean_check`
   dependency cannot be reordered.

Each child has its own stop token. Stopping one child leaves its siblings and
coordinator alive; stopping the coordinator cascades to its children. Exceptions
are isolated per child, and every raised child still produces a terminal result
instead of leaving a permanent "running" row.

The adapter materializes each child as a real child session and run at start,
streams its normal event vocabulary through its own `RunBroker`, and fills the
same row at finish. This made a child's session view live without inventing a
second frontend protocol. The React UI added live spawn cards, tool/narration
progress, and a per-child **Stop** action.

### Configurable roles and project navigation

A new **Sub-agents** page exposes each built-in role's model, maximum turns,
maximum cost, system prompt, and read-only tool subset. Overrides are stored in a
local sidecar as differences from the vendored YAML defaults, so untouched
defaults continue to flow and "reset" is an actual empty override. Tool
permissions can only tighten the parent's set. A malformed override degrades to
the role default rather than crashing the spawn.

The same work added a per-child cost ceiling and a per-run summarizing stop at the
cap. Top-level runs keep their existing global spend behavior.

Project navigation was also cleaned up: the sidebar shows the three most recently
updated projects, a **See all** row opens a new card-grid Projects hub, and the
new-project dialog is mounted in both the hub and main shell so it works from
either surface.

### Context compaction and slash commands

Phase G added a three-layer context defense: child isolation, deterministic
pruning, then LLM summarization only when pruning is insufficient.

The new condenser:

- masks superseded `lean_check`, `read_file`, and `search_mathlib` results outside
  a recent window;
- preserves the original goal and recent turns verbatim;
- never splits a tool-call/tool-result pair; and
- triggers at a configurable fraction of the real context limit (75% of 200k
  tokens by default, with `0` disabling compaction).

Compaction applies to coordinators and children and emits a typed `Compacted`
event. The adapter's `POST /api/sessions/{id}/compact` uses the same condenser in
forced mode, persists the compacted transcript, and records a durable timeline
marker through migration `0006_timeline_compaction_kind`.

The frontend work was intentionally a general slash-command framework rather
than a hard-coded `/compact` special case: commands have a parser, metadata
registry, autocomplete menu, and action/prompt handler types. `/compact` is the
first action command and replaces its in-flight card with a durable
"Compacted — freed ~N tokens" result listing the files still represented.

### Sub-agent status

The merged tracker records **17 of 25 items landed**, one dropped after
investigation, three Memory-channel items deliberately skipped for now, and four
queued. Phases E (parallel execution) and G (context management) are complete.
Remaining items are steering/visibility refinements and broader search tooling;
the deferred Memory channel retains the constraints that the coordinator is the
sole writer and that memory records obstacles rather than unverified conclusions.

The full rationale and tracker live in
[`apps/lea-standalone/design/v2.3.1-subagents-architecture.md`](../apps/lea-standalone/design/v2.3.1-subagents-architecture.md)
and
[`apps/lea-standalone/design/v2.3.1-subagents-visibility-parallelism-progress.html`](../apps/lea-standalone/design/v2.3.1-subagents-visibility-parallelism-progress.html).

## 2. The project Blueprint came to Overleaf

The Overleaf Lean pane now has an **Items | Blueprint** switch. Blueprint renders
the same project dependency graph as the standalone UI—status-colored nodes,
definition/theorem shapes, dependency arrows, legend, and a selected-node detail
panel—without forcing the user to leave Overleaf.

The implementation preserves the one-backend architecture:

```
Overleaf content script → companion :31245 → adapter :8001 → graph/blueprint services
```

The adapter added by-slug graph/blueprint routes because the extension knows the
Overleaf-derived project slug, not an internal project UUID. Unknown slugs remain
a benign empty state and never create a project. The companion owns slug
translation and adapter access as it does for export, share, and identity.

The graph layout was extracted into the shared framework-free
`packages/lea-blueprint` module. React and the vanilla-JS extension renderer
therefore use one longest-path layout, status mapping, and sizing model. A mirror
test ensures the extension-bundled copy cannot drift from the canonical package.

The feature also closes the "formalized but never decomposed" gap. On demand,
`blueprint_seed.py` reads the artifact index and appends a node for every
formalized declaration missing from `.lea/blueprint.md`. Generation is additive
and idempotent: existing human/agent-authored nodes are untouched, declarations
are matched by fully-qualified `lean:` name, and an unclosed Markdown code fence
is repaired before appending so new nodes cannot become inert text and be added
again on every request.

Edges are evidence-based rather than invented. The generator scans the
comment-stripped declaration span for other formalized declaration names and
skips ambiguous short names instead of guessing. Follow-up review fixes also:

- backfilled the artifact index before generation for pre-index projects;
- stopped `.tex` keystrokes from pointlessly rebuilding a project-level graph;
- updated node selection in place rather than relaying out the SVG;
- syntax-highlighted resolved Lean declarations and signatures in node details;
  and
- preserved source newlines and indentation in multi-line Lean signatures.

The Overleaf view remains deliberately read-only and snapshot-based. Blueprint
authoring stays in the standalone Markdown view; live streaming, session
deep-links, zoom/pan, and export are explicit non-goals for this first version.
See
[`docs/FEATURE-overleaf-blueprint-view.md`](FEATURE-overleaf-blueprint-view.md)
and
[`docs/PLAN-overleaf-blueprint-view.md`](PLAN-overleaf-blueprint-view.md).

## 3. One visual language across both front ends

The standalone UI and Overleaf extension moved from the warm-paper palette to the
same monochrome product chrome: near-white backgrounds, black primary actions,
neutral borders, and persistent color only where it carries proof meaning
(verified, failed, pending) or Lean syntax.

The extension gained `lea-theme.css`, loaded before both content and options
styles. Its prefixed tokens mirror the standalone's canonical `:root` block, and
`themeParity.test.mjs` enforces the mapping, load order, and absence of the
retired palette. The Overleaf pane, options page, standalone Stats/Settings
surfaces, model controls, origin badges, Blueprint, and syntax highlighting were
all brought into the same system. A follow-up made the settings surface scroll
correctly instead of clipping longer content.

## 4. Full-system audit and remediation

[`docs/AUDIT-2026-07-24.md`](AUDIT-2026-07-24.md) reviewed the adapter, vendored
prover boundary, companion, and both UIs. It deliberately focused on defects that
the existing green suites missed and grouped them as security (`S`), correctness
(`C`), concurrency (`X`), performance (`P`), and maintainability (`M`).

### Security boundary and credential handling

- **S1 — localhost boundary:** CORS had only controlled whether a response was
  readable, not whether a cross-site request executed. New middleware rejects a
  non-loopback `Origin` or `Host` before handlers run, with explicit environment
  extensions for reverse proxies. The LSP WebSocket performs the same check, and
  file-URI rewriting refuses paths that normalize outside the Lake root.
- **S2 — GitHub token exfiltration:** the remote validator accepted any HTTPS
  host while push embedded the GitHub PAT in that URL. The host is now pinned at
  route validation, rechecked at push time for legacy rows, and—most
  importantly—allowlisted where the credential is actually injected.
- **S3 — proof-path traversal:** `/lean-check`, `/rebuild`, and `/verify` now use
  the shared containment guard and inherit the `.git` / `.lake` exclusions.
- **S4 — autonomous prompt injection (partial):** `read_file` is confined to the
  working directory and enclosing Lake root, shell tools receive a
  credential-scrubbed environment, and mirrored Overleaf text is framed as
  untrusted data. Arbitrary shell commands remain possible in autonomous mode;
  fully removing that capability requires the larger container-per-run design.
- **S5/S7 — destructive and memory bounds:** artifact retirement is restricted
  to proof artifacts rather than project infrastructure, and uploads enforce the
  25 MB limit while streaming in 1 MB chunks.
- **S6 — secret-file durability:** settings and sub-agent overrides now use
  atomic same-directory replacement, `0600` permissions before content is
  written, `fsync`, correct TOML control-character escaping, NUL rejection, and
  tolerant readers. The root `.env` writer received the same atomic treatment.

### Correctness and proof preservation

- Global usage and origin totals now aggregate the full database rather than the
  newest 100 sessions, so the displayed spend and enforced spend cap cannot fall
  as old sessions age out.
- Namespace migration checks actual active runs joined through the project,
  instead of a derived session status that was blind once the session had code.
- Collation snapshots the canonical proof before promotion and restores it if
  re-verification fails. The SafeVerify rejection tier is now enforced at the
  only decisive point—the promotion boundary—falling through to the next
  candidate on rejection.
- Finished proof outcomes can no longer be downgraded to `failed` because usage,
  transcript, or artifact-index bookkeeping failed afterward. Each secondary
  write is best-effort and the terminal run row remains authoritative.
- Pending-run cancellation and dispatcher admission now compete through one
  conditional database update, so exactly one wins.
- Clearing a provider key retracts only values the loader itself exported to
  `os.environ`; shell-exported credentials remain owned by the shell.
- Project deletion now removes artifact-index rows for both project and
  session-shaped scopes.
- Model requirements are a pure function of provider identity rather than the
  current process environment. Alternative key names and credential-chain
  providers are represented explicitly.
- A run that crashes before storing a replayable transcript no longer disappears
  silently. The next run receives a structured note describing the missing
  attempts and preserving awareness of files left on disk. This is a partial
  fix: safe recovery still needs the prover to expose the last complete
  tool-call/result boundary, and the UI does not yet show the gap.

The proposed search defect **C4 was withdrawn**. Its regression test passed
against the pre-fix code because SQL applies `WHERE` before `LIMIT`; the limit
already bounded matches, not the portion searched. Keeping that correction in
the audit is important: C1 looked superficially similar but really did truncate
an aggregate by folding a paginated result in Python.

### Concurrency and migrations

The run dispatcher no longer blocks the entire queue behind a run whose own
session is busy. Blocked runs move to an ordered deferred list, retain their
relative FIFO position, and are retried while unrelated sessions use available
capacity.

Shared project repositories now serialize adapter-side commits per resolved repo
path. Callers that know what they changed pass a pathspec to `git add -A -- ...`,
preventing one run from committing another run's half-written proof while still
recording deletions. Whole-tree commits remain only where the tree is truly the
unit, such as provisioning and namespace migration.

Startup migrations are protected by a cross-process `flock` spanning plan,
snapshot decision, and apply. SQLite's write lock had serialized individual
writes but not Alembic's read-plan-write sequence; that stopped being sufficient
when revision `0005` gained two `0006` children merged at `0007`. The external
lock preserves the already-applied revision graph and also ensures multiple
starters create one pre-migration snapshot, not one each.

Finally, `lake serve` sessions are bounded by a semaphore (default four), reject
excess WebSockets with 1013, and tear down in `finally` so pump failures cannot
orphan the process.

### Performance hot paths

- `RunBroker.events_after` is now an indexed slice rather than a full-buffer
  filter on every 80 ms poll. Model deltas are batched before publication to
  roughly one SSE frame per browser poll without mutating already-sequenced
  events.
- Persisted spend is memoized for two seconds on the per-event cap path. The
  moving in-run cost overlay remains exact, so the cache does not weaken the
  cap.
- Timeline queries left-join artifact blobs, reducing a 200-step session from
  roughly 200 extra SQLite connections to one query. Blueprint graph reads skip
  proof-body hydration entirely.
- Idle session feeds use an in-process change token with a 30-second SQL
  backstop, and passive observers back off from 0.3 to 1 to 3 seconds.

These tests assert structure—connections opened, frames emitted, reads
performed—rather than wall-clock time, which makes the performance contract
deterministic in CI.

## 5. A queued run now keeps the model the user selected

Previously a run read the persisted default model only when the dispatcher
admitted it. A user could press Send with one model selected, change Settings
while the run waited, and unknowingly execute the queued run on another model.

`POST /api/runs` now accepts and validates an explicit model, stores it on the
run, and the dispatcher pins that value while still reloading other live settings
such as max turns and spend cap. The standalone sends the current picker value
for both new sessions and follow-ups. Older clients and the autonomous Overleaf
path may omit the field and retain the persisted-default behavior.

## 6. Integration, packaging, and documentation

The week's three merges brought the `fix/subagent-workflow` and `gen_repair`
lines together. This integrated the prior week's batch Stub / Formalize / Stop
work—previously described as staged or branch-local—alongside the new sub-agent
and Blueprint features. The result is one shared adapter serving both front ends,
with no new service boundary.

On the sandbox distribution branch, the Docker frontend stage was corrected to
preserve the monorepo-relative layout required by the new
`packages/lea-blueprint` import. The build now runs from
`/web/apps/lea-standalone`, copies the shared package into `/web/packages`, and
copies the resulting nested `dist/` into the runtime image. This keeps the
prebuilt-image distribution path compatible with cross-workspace frontend code.

Documentation added or materially updated this week:

- [`docs/AUDIT-2026-07-24.md`](AUDIT-2026-07-24.md) — full-system findings,
  evidence, remediation status, and remaining risks.
- [`docs/FEATURE-overleaf-blueprint-view.md`](FEATURE-overleaf-blueprint-view.md)
  and
  [`docs/PLAN-overleaf-blueprint-view.md`](PLAN-overleaf-blueprint-view.md) —
  product scope and implementation record for the Overleaf Blueprint.
- [`apps/lea-standalone/docs/MIGRATIONS.md`](../apps/lea-standalone/docs/MIGRATIONS.md)
  — automatic startup migration behavior, pre-migration snapshots, manual
  verification, rollback guidance, and revision history.
- `apps/lea-standalone/README.md` — replaced the now-dangerous statement that
  local database state is disposable with the correct automatic-migration
  guidance. Since SQL owns proof content after v2.3, resetting is no longer a
  harmless migration strategy.
- [`docs/WEEKLY-REPORT-2026-07-14-to-07-20.md`](WEEKLY-REPORT-2026-07-14-to-07-20.md)
  — the preceding week's report, committed as part of the integration.

---

## Status of the work

- **Integrated on `issue_address`:** sub-agent streaming, stopping, parallelism,
  role overrides, Sub-agents page, Projects hub, context compaction, slash
  commands, Overleaf Blueprint, visual-language synchronization, per-run model
  snapshots, and the complete Jul 27 audit-remediation series.
- **Audit:** 23 findings fixed, S4 and C10 partially mitigated, C4 withdrawn.
  Remaining work is mostly low severity plus two architectural boundaries:
  container-per-run isolation for autonomous shell execution and recoverable
  complete-turn transcript checkpoints.
- **Sub-agent tracker:** 17/25 landed, one dropped, three deferred Memory items,
  four queued; parallel execution and context management are complete.
- **Distribution:** the Docker monorepo-layout fix is on
  `codex/refresh-lea-sandbox` / `sandbox/main`, not on `issue_address`.
- **Working tree before this report:** clean. This report is the only new file
  created for the request.

## Test posture

Fresh verification on Jul 27:

- **Adapter:** `602 passed` (8 deprecation warnings) via
  `apps/lea-standalone/adapter/.venv/bin/python -m pytest -q`.
- **Standalone frontend:** `50 passed` via
  `npm run test:frontend -w apps/lea-standalone`.
- **Overleaf companion/extension:** `417 passed` via
  `npm test -w apps/overleaf-extension`.

The audit remediation alone added 159 adapter cases (462 → 602) plus a new
21-check prover sandbox suite. Fixes were validated against the old behavior by
temporarily reverting them and confirming the new regression tests fail; the C4
test's refusal to fail is what correctly withdrew that finding. Sub-agent
coverage additionally exercises real thread overlap, concurrency caps, failure
isolation, child stopping, event streaming, LSP document cleanup, condenser
pair-boundary safety, and durable manual compaction.

---
