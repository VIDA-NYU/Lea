# Plan — Manual-Edit / needs_review Bugfix Round 2

Fixes for the five bugs found reviewing branch `manual_edit_inline_derivate`
(commits `19a82fe..ae5c668` plus the uncommitted needs_review + verbatim-masking
work). Ordered by suggested implementation sequence: smallest/safest first, and
the two dirty-tree bugs (1, 2) fixed before that work is committed.

All file references are `apps/overleaf-extension/companion/server.mjs` unless
stated otherwise. Line numbers are as of the current dirty tree.

---

## Bug 3 — `formalizedJob` shadows a newer `needsReviewJob` (dirty tree)

**Root cause.** In `getTheoremStatus`, the `formalizedJob` branch (~line 3530)
guards recency against `failedJob` and `disprovedJob` but not the newly added
`needsReviewJob`, while the needs_review branch below it requires being newer
than `formalizedJob`. The guards are asymmetric: an older formalized job always
wins over a newer needs_review re-run, so the chip stays "valid" after a re-run
that the prover itself flagged.

**Fix.** Replace the growing pairwise-guard pattern with one selection:

1. Compute `newestTerminalJob` once, over `{formalizedJob, needsReviewJob,
   disprovedJob, failedJob}`, comparing `finishedAt || startedAt` (string
   compare, same as today). Nulls skipped.
2. Keep the branch bodies, but each fires only when its job *is* the newest
   terminal job. This eliminates the whole class of "new terminal status added,
   N existing guards not updated" — the fourth status is what exposed it.
3. `disprovedJob`'s branch has the same omission today; the unified selection
   fixes it for free.

**Tests** (`tests/companion.test.mjs`): (a) formalized job older + needs_review
job newer → manifest item `needs-review`; (b) needs_review older + formalized
newer → `valid`; (c) needs_review newer than disproved → `needs-review`.

**Risk.** Low. Behavior change only where two terminal jobs coexist; the
unified rule is what the existing comments already claim the code does.

---

## Bug 2 — ungated promotion to `formalized` + double compile (dirty tree)

**Root cause.** `resolveProofOutcome`'s needs_review branch (~line 2349)
promotes to `formalized` whenever `local.status === "formalized"` — a
sorry-regex verdict, not a compile. It runs `runLeanCheck` but never reads the
result (and skips it entirely when `absolutePath` is missing; a unit test
currently locks in promotion with `leanCheck: null`). The comment claims the
bar is "sorry-free + compiles"; only sorry-free is enforced. Separately, the
recovery flow compiles twice: `recoverFormalizedStatusFromTargetPath` runs a
gated `runLeanCheck`, its result is discarded, then this branch spawns another.

**Fix.** Make promotion require a passing compile verdict, exactly once:

1. In `applyProofOutcomeToJob`, when recovery succeeds, carry the already-run
   check along: `effectiveLocalStatus = { ...recovered.status, leanCheck:
   recovered.leanCheck }`.
2. In `resolveProofOutcome`'s needs_review branch:
   - `const leanCheck = local.leanCheck ?? (local.absolutePath ? await
     runLeanCheck(job.leaWorkspacePath, local.absolutePath) : null);`
   - Promote to `formalized` only if `leanCheck?.ok === true`.
   - Otherwise return the `needs_review` outcome, attaching the failing/absent
     `leanCheck` as diagnostic metadata (`resultDetail` can mention it).
3. Regex-only evidence with no way to compile (no `absolutePath`, no attached
   check) now stays `needs_review` — that's the point of the status.

**Tests.** Revise `"resolveProofOutcome promotes a needs_review run..."` to
attach `leanCheck: { ok: true }` to `localStatus` (keeps the real toolchain
spawn out of the unit test, per its own comment). Add: formalized-by-regex +
`leanCheck: { ok: false }` → stays `needs_review`; formalized-by-regex + no
check possible → stays `needs_review`. Assert the recovery flow reuses
`recovered.leanCheck` (spy `runLeanCheck` call count via the fetch/spawn seam,
or assert `outcome.leanCheck` is reference-equal).

**Risk.** Low-medium: strictly *less* promotion than the current dirty tree,
never more. Depends on Bug 1 landing so the recovery path can actually supply
a passing check in production.

---

## Bug 1 — `recoverFormalizedStatusFromTargetPath` checks the wrong file (dirty tree)

**Root cause.** The recovery builds `entry.proofPath = target.relativePath`,
but every production `target` comes from `buildLeaTarget`, where
`relativePath` is the **project markdown** path (`relativeToLeaRepo(...
projectMarkdownPath)`) and `moduleName` doesn't exist. So in production the
function sorry-scans the `.md` file, lean-checks it, and always returns null —
the feature is dead code. Its tests pass because they hand-craft targets with
proof-file `relativePath`s that `buildLeaTarget` never produces.

**Fix.** Don't guess the path from `target` at all — resolve it from the run's
own session, the same way `readLeanPaneArtifactFromSession` (line 3384)
already does. That's the authoritative record of what the agent actually
wrote, and it works even when the agent chose a different file name than the
label:

1. Change the signature to `recoverFormalizedStatusFromTargetPath({ state,
   job, target })`.
2. Fetch session detail via `job.leaSessionId || job.recorderSessionId`
   (`fetchApiSessionDetail`, `state.fetchImpl` seam — makes it unit-testable
   with no filesystem tricks). No session → return null.
3. Pick the latest `.lean` code_step containing
   `job.declarationNameHint || job.declarationName || target.targetLabel`
   (fall back to the sole `.lean` step) — identical filter to
   `readLeanPaneArtifactFromSession`; consider extracting that step-selection
   into a shared helper since this is its third copy (see also
   `loadEditableSessionFile`, Bug 4).
4. Map to a repo path with `proofPathFromProjectStep({ namespace, stepPath })`
   where `namespace = detail.project_namespace || job.projectNamespace ||
   projectNamespaceFromSlug(target.projectSlug)`, and build the entry as
   `{ name, proofPath, moduleName: moduleNameFromProjectStep(...) }`.
5. Keep the existing `getLeaProofStatusFromEntry` + gated `runLeanCheck` tail
   unchanged. Fallback when the session fetch fails: try
   `job.recordedProofPath` if set (a prior run/stub recorded it).

**Tests.** Rewrite the two existing unit tests through the new signature with
a mocked `fetchImpl` serving `code_steps`. Add the missing end-to-end case:
drive `applyProofOutcomeToJob` (via the existing formalize-harness pattern,
e.g. the `"formalize on the /api backend tags the theorem formalized"` test)
with a **real `buildLeaTarget` shape** and a needs_review exit, and assert the
job is promoted and the markdown entry upserted. This is the test that would
have caught the bug.

**Risk.** Medium (touches run finalization), but the failure mode today is
"recovery never fires," so any regression is bounded by that. Land together
with Bug 2 and re-run the full suite before committing the dirty tree.

---

## Bug 4 — edit path still searches by the old label after a rename (committed, `ae5c668`)

**Root cause.** `ae5c668` taught `readLeanPaneArtifactFromSession` to search
by `linkedJob.declarationName`, but `loadEditableSessionFile` (line 824) still
filters code_steps by `targetLabel`. After a rename, the newest steps contain
only the new name, so the next "Edit" serves the last **pre-rename** snapshot
— and saving would write that stale content back over the renamed file. The
same stale name also flows into `parseDeclarationHeader(before.content,
targetLabel)` and `classifyEdit({ expectedName: targetLabel })`, so a *second*
rename (B→C) misclassifies as `signature` instead of `renamed` and skips the
`declarationName` update.

**Fix.** Thread the current declaration name through the whole edit path:

1. In `handleLeanPaneEditStart` and `handleLeanPaneEditSave`, compute
   `const effectiveName = linkedJob?.declarationName || targetLabel;` right
   after `resolveEditSession`.
2. Pass it to `loadEditableSessionFile` (filter by `effectiveName`), to both
   `parseDeclarationHeader` calls, to `classifyEdit`'s `expectedName`, and to
   the cascade summary string (`Re-checked after edit to ${effectiveName}`).
3. The rename branch then correctly fires on any Nth rename and keeps
   `linkedJob.declarationName` current.

**Tests** (`tests/leanPaneEdit.test.mjs`): (a) after a save that renames A→B,
`handleLeanPaneEditStart` returns the post-rename content, not the last step
containing A; (b) a second save renaming B→C classifies as
`{ kind: "renamed", from: "B", to: "C" }` and updates `declarationName` to C;
(c) a plain proof edit after a rename still resolves the right step.

**Risk.** Low. When no rename has happened, `declarationName === targetLabel`
(set at job creation, line 2128), so behavior is byte-identical.

---

## Bug 5 — cascade can't attribute a renamed dependent (committed)

**Root cause.** `resolveDependentSession` looks up the dependent's job by
composing a job key from the dependent file's **parsed declaration name**, but
jobs are keyed by the **LaTeX label** forever. The two coincide by convention
until a dependent is renamed via this very feature — then `declarationName`
(updated by `ae5c668`) diverges from the key, the lookup misses, and the
dependent is reported `unknown/unattributed`; its chip never receives cascade
verdicts.

**Fix.**

1. Add `findJobEntryByDeclarationName(jobs, projectSlug, declName)`: scan
   `Object.values(jobs)` for `job.jobKey.startsWith(`${projectSlug}:`)` and
   `job.declarationName === declName`, preferring the latest job with a lea
   session (reuse `findLatestJobWithLeaSession`'s recency rule).
2. In `resolveDependentSession`, keep the existing label-key lookup as the
   fast path, then fall back to the declaration-name scan. Return the matched
   job's own `targetKind`/`targetLabel` so `recordEditCheckVerdict` and the
   impact summary attribute against the right item.
3. Note in a comment that `jobKey` deliberately stays pinned to the LaTeX
   label (it's the doc-side anchor); `declarationName` is the Lean-side
   identity — this function is the bridge.

**Tests** (`tests/leanPaneEdit.test.mjs`): dependent whose job has
`declarationName: "new_name"` under a key for `old_name`, dependent file
declares `new_name` → cascade attributes it, lean-checks its session, and
records the verdict on its linked job.

**Risk.** Low. Scan is O(jobs), fine at pane scale; fallback only fires when
the fast path misses, which today returns the buggy `unknown` result anyway.

---

## Follow-up: one identity resolver (recommended, after 4 & 5)

Bugs 4 and 5 (and the `ae5c668` fix itself) are the same bug in three
outfits: "what is this target's current Lean declaration name?" is answered
ad hoc at each call site. Extract a single resolver, e.g.

```
resolveTargetIdentity({ state, overleafProjectId, targetKind, targetLabel })
  → { jobKey, activeJob, linkedJob, declarationName, leaSessionId }
```

and a shared `pickSessionLeanStep(codeSteps, declarationName)` (the filter
currently copy-pasted in `loadEditableSessionFile`,
`readLeanPaneArtifactFromSession`, and Bug 1's new recovery). Manifest
enrichment, edit start/save, and cascade resolution all call these. This
prevents the next "fixed the chip, missed the editor" twin-path drift.

---

## Housekeeping (do alongside)

- **Run the never-executed Python tests on the host** before merging anything:
  `cd apps/lea-standalone/adapter && ./.venv/bin/python -m pytest`, plus the
  prover suites (`tests/tools/test_rebuild_module.py`,
  `tests/lsp/test_lsp_daemon.py`, `tests/lsp/test_cascade_rename_integration.py`,
  `tests/interface/`). Three commits of adapter/prover code are still
  syntax-checked only (per `docs/PLAN-overleaf-lean-pane-manual-edit.md`).
- **.gitignore** the LaTeX build artifacts now untracked under `docs/`
  (`*.aux`, `*.fdb_latexmk`, `*.fls`, `*.log`, `*.out`, `*.synctex.gz`, and
  decide whether the fixture PDFs stay). Commit the two `.tex` sources.
- **Verbatim-mask edge case** (`targetParserCore.mjs`): `findOpaqueSpans`
  runs on raw source, so a commented-out `% \begin{verbatim}` opens a mask
  span and can swallow real markers up to the next `\end{verbatim}`. Cheap
  fix: when scanning for `\begin{<verbatim-like>}`, skip matches preceded on
  their own line by an unescaped `%` (the *interior* of a real verbatim block
  is unaffected — only the `\begin` sightings are filtered). Low priority;
  add the failing-case test first to pin the intended behavior.

## Suggested order

1. Bug 3 (small, self-contained) → 2. Bug 2 → 3. Bug 1 (then run full JS
suite + host pytest, commit the dirty tree) → 4. Bug 4 → 5. Bug 5 → 6.
identity-resolver refactor + housekeeping. Bugs 4/5 can alternatively be
implemented *as* the refactor if you'd rather not touch those call sites
twice.
