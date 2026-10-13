# Plan — Manual Lean Edits in the Overleaf Lean Pane

Implementation plan for `docs/FEATURE-overleaf-lean-pane-manual-edit.md`: a
direct-edit affordance on an expanded Lean pane item, plus cross-target
downstream-impact detection driven by real `lean_check` re-verification rather
than heuristics.

## Status (updated 2026-06-30)

**Phases 0-4 implemented, with one verification gap.** All companion/extension
work (Phases 0, 2, 3, 4) is covered by tests and the full `overleaf-extension`
suite is green (241 tests, up from a 202 baseline). Phase 1's adapter change
is written and mirrors the existing `lean_check_session` test conventions
exactly, but **could not be executed** in the implementation environment: the
adapter's `.venv` was built on the host machine (macOS/Homebrew paths) and
isn't usable in this sandbox, and rebuilding a Python 3.13 venv here failed
(no network access to fetch a standalone interpreter). The Python changes
passed `py_compile` (syntax-valid) and were written to exactly match
`test_lean_check_backfills_verdict_onto_the_step`'s existing shape, but the
new `test_lean_check_with_author_records_a_new_cascade_step_instead_of_backfilling`
/ `test_lean_check_without_author_still_backfills_as_before` /
`test_author_cascade_round_trips_with_no_check_constraint` tests should be run
for real (`cd apps/lea-standalone/adapter && ./.venv/bin/python -m pytest`)
before this ships.

Deviations from the plan below, discovered during implementation:
- Phase 2's "how does a cascade re-check get its own code_step" question
  (flagged as an open risk) was resolved as option (a): `PathRequest` gained
  optional `author`/`summary` fields; when `author` is set, `lean_check_session`
  inserts a new `code_step` (reusing the existing commit_sha) instead of
  back-filling the latest one. Omitting `author` is byte-identical to the
  original behavior.
- Phase 3's module ended up self-contained (`leanSignatureDiff.mjs`) rather
  than merged into `leanDependencyGraph.mjs` — the header-parsing logic was
  substantial enough to warrant its own file, as the plan allowed.
- The balanced-brace header scanner (Phase 3's "Open risks" item) was
  hand-written in `leanSignatureDiff.mjs` rather than importing
  `targetParserCore.mjs`'s `parseBalancedSuffix` across the extension/companion
  boundary — resolving that open question in favor of porting a small helper,
  as the plan's second option allowed.
- `handleLeanPaneEditSave`'s dependent-session resolution
  (`resolveDependentSession`) tries both `theorem` and `definition` kinds per
  dependent, mirroring the existing `resolveTargetUseStatus` pattern — not
  spelled out explicitly in the plan's Phase 2 sketch but a direct consequence
  of Section 1's grounding (a dependent's kind isn't known from the reverse
  index alone).

## 1. Grounding — what's there today, confirmed by reading the code

This plan leans on five facts established while writing the feature spec.
Re-stating them here because each one removes a design decision this plan
would otherwise have to make from scratch:

1. **The adapter already has a run-less user-edit primitive.** `POST
   /api/sessions/{id}/file` (`apps/lea-standalone/adapter/app/routes/sessions.py:101`)
   writes content to the session's working file, commits `author=user`
   (`gs.commit_write`), and inserts a `code_steps` row via
   `store.add_code_step(session_id, None, request.path, commit_sha=sha,
   author="user")`. `POST /api/sessions/{id}/lean-check`
   (`routes/sessions.py:143`) runs the LSP fast-path check and back-fills the
   verdict onto that file's latest `code_step`. Both are exercised today only
   by the standalone UI. Nothing about either route is standalone-specific —
   they take a `session_id` and operate on that session's working copy in its
   git repo, which is exactly what an Overleaf-target session already is.
2. **`code_steps.author` is a free-text column, not a SQL `CHECK` enum.**
   `db.py`'s `create table code_steps` declares `author text not null default
   'agent'` with no constraint, and `store.add_code_step`'s Python signature
   is `author: str = "agent"` — also unconstrained. Recording cascade
   re-checks as `author = "cascade"` needs **no migration**, just a third
   documented convention value next to `"agent"`/`"user"`.
3. **Every target in one Overleaf project shares one git repo.**
   `projects.resolve_git(session_id, ...)` returns `(gs, repo_key)`; a
   project session resolves to the *shared* repo keyed by the project, not a
   private one (`routes/sessions.py:114`, D24). Recorded proofs live under
   `workspace/proofs/Lea.<ProjectSlug>/...` inside that one repo
   (`buildLeaProjectMarkdownPath`/`proofPathFromProjectStep` in
   `companion/server.mjs`). A project-wide reverse-import scan is therefore a
   walk over one repo's files on disk, not a cross-repo or cross-session
   query.
4. **The companion already reads that repo directly off disk.** `leaRepoPath`
   (`state.settings.leaRepoPath`) is a local filesystem path the companion
   process can `fs.readFile`/`fs.readdir` today —
   `findImportedStubbedTheoremUses` and `findImportedCurrentlyStubbedTheoremUses`
   already do exactly this (`companion/server.mjs:2671`, `:2697`), including
   the `parseLeanImports(content)` helper (`:2754`) this plan reuses verbatim
   for the reverse direction. No new filesystem access pattern is needed —
   only a new traversal (project-wide instead of single-file) and a new
   direction (who imports *this* module, not what does *this* file import).
5. **The chat-mirror request/response/session-resolution shape is the
   template to copy, not chat-specific.** `resolveChatSession`
   (`companion/server.mjs:530`) resolves a target to an existing
   `leaSessionId` via the job store (`findActiveJob`/`findLatestJobWithLeaSession`),
   and the route block at `:1100-1123` shows the `/lean-pane/chat/*` URL
   convention this plan's `/lean-pane/edit/*` routes will match. One
   difference matters: chat's `handleChatMessage` implicitly *creates* a
   session on first send (no `leaSessionId` yet is fine — `startChatRun`
   starts a fresh adapter session). **Edit cannot do this** — editing is only
   offered for an item that already has a recorded artifact
   (`item.leanArtifactContent` truthy, per `content.js:543`), and a recorded
   artifact implies a session already exists. So edit-session resolution must
   *fail loudly* (`no_session` error) rather than fall back to "create one,"
   since silently creating an empty session for an edit would have no prior
   commit history to edit against.

One thing the feature spec didn't fully pin down and this plan resolves
concretely: **how is an edited declaration's "module name" known**, since the
reverse-dependency scan keys off `import <ModuleName>` lines, not labels?
`moduleNameFromProjectStep` (`companion/server.mjs:1600`) already derives this
from `{ namespace, stepPath }` for any recorded target — the same function the
forward (`uses=`) resolution path already calls. This plan's reverse index
reuses it unchanged; no new module-naming logic is needed.

---

## 2. Work breakdown

### Phase 0 — Companion: reverse-dependency index (prerequisite, backend-only)

No UI in this phase. Build and unit-test the dependency graph in isolation
before anything calls it.

**New module: `apps/overleaf-extension/companion/leanDependencyGraph.mjs`**
(kept separate from `server.mjs` the way `leanApiClient.mjs` already is, so it
can be imported by tests without booting the HTTP server)

- `async function listProjectProofFiles({ leaRepoPath, overleafProjectId })`
  — resolve the project's namespace via the existing
  `projectNamespaceFromSlug(slugProjectId(overleafProjectId))`
  (`server.mjs:1567,1585`), then recursively list every `.lean` file under
  `workspace/proofs/<Namespace>/` in `leaRepoPath`. Returns `{ path,
  moduleName, content }[]` (moduleName derived the same way
  `moduleNameFromProjectStep` does, from the path relative to the namespace
  root).
- `function buildReverseImportIndex(files)` — for each file, call the
  existing `parseLeanImports(content)` (move it from `server.mjs` into this
  new module and re-export it from `server.mjs` for the existing call sites,
  so there's one copy) and add an edge `importedModuleName -> file` for each
  import found. Returns `Map<moduleName, FileRef[]>`.
- `function transitiveDependents(moduleName, reverseIndex)` — BFS/DFS over the
  reverse index starting at `moduleName`, returning every reachable file
  (the diamond/transitive case from the feature spec's edge cases — `C`
  importing `B` importing `A` must surface `C` for an edit to `A`). Lean's
  import graph is acyclic by construction (a cycle would mean Lean itself
  fails to build the project), so this needs no cycle guard, but keep a
  visited-set anyway as a defensive bound, not a correctness requirement.
- `async function dependentsOf({ leaRepoPath, overleafProjectId, moduleName })`
  — composes the three functions above into the one entry point the edit
  handlers call. O(project file count) per call; v1 does not cache this
  (feature spec's explicit decision — project sizes are pane-scale).

**Tests: `apps/overleaf-extension/tests/leanDependencyGraph.test.mjs`**
- Fixture: three in-memory `{ path, content }` proof files where `C` imports
  `B`, `B` imports `A`, plus an unrelated `D` that imports nothing relevant —
  assert `dependentsOf(A)` returns `{B, C}` and not `D`.
- A file with no imports at all → empty dependents.
- A module name that doesn't exist in the project (typo, or a freshly-edited
  declaration that was just renamed) → empty dependents, not an error.
- Multiple distinct dependents of the same module at the same level (the
  spec's worked example: `compactness_criterion` has two direct dependents)
  → both returned, no de-dup bug from BFS re-visiting.

This phase has a clean, independently testable success condition and zero
product surface — land it and get `leanDependencyGraph.test.mjs` green before
Phase 1 touches the edit flow itself.

### Phase 1 — Adapter: the one additive change

`apps/lea-standalone/adapter/app/routes/sessions.py` needs **no route
change** (Section 1, point 1). The only adapter-side work:

- Update the `author` doc-comment in `db.py`'s `code_steps` table and in
  `store.add_code_step`'s docstring to read `'agent' | 'user' | 'cascade'`,
  so the convention is discoverable from the schema file itself the way
  `'agent' | 'user'` already is. This is documentation, not code — confirm
  with a quick grep that nothing downstream (UI status derivation, stats
  queries) special-cases the `author` value in a way that would mis-handle an
  unrecognized third value before assuming this is purely additive.
- `apps/lea-standalone/adapter/tests/test_db_code_steps.py`: add a case
  inserting a `code_step` with `author="cascade"` and reading it back, mostly
  as a guard against a future accidental `CHECK` constraint being added
  without updating this convention.

No FastAPI route, no Pydantic model change — `FileWriteRequest`/`PathRequest`
already accept exactly what this feature needs (`routes/sessions.py:30-38`).

### Phase 2 — Companion: edit-save endpoint and cascade verification

**`companion/server.mjs` additions**

- `validateEditPayload(payload)` — mirrors `validateTargetPayload`
  (`:1500`): require `overleafProjectId`, valid `targetKind`, a valid Lean
  identifier `targetLabel`, non-empty `content`. Reuses
  `isValidLeanIdentifier`/`normalizeTargetKind`, already defined.
- `resolveEditSession({ state, target })` — same shape as
  `resolveChatSession` (`:530`) but **does not** treat a missing session as a
  valid "no-session" state; returns `{ leaSessionId: null }` only as an error
  case the handler turns into `errorResponse(404, "no_session", ...)`.
- `async function handleLeanPaneEditStart(payload, state)` → routed as `POST
  /lean-pane/edit/start`. Resolves the session, fetches its current working
  file via `fetchApiSessionDetail` (existing helper, already used by
  `handleChatSession`) to get the current content + path, and calls the new
  `dependentsOf` from Phase 0 using the *current* (pre-edit) module name, so
  the pane can show "editing this may affect N items" before the user has
  typed anything.
- `async function handleLeanPaneEditSave(payload, state)` → routed as `POST
  /lean-pane/edit/save`. Sequence:
  1. Resolve session (404 `no_session` if absent — see Section 1, point 5).
  2. Snapshot the **pre-edit** content + parse its declaration header (Phase
     3) before writing anything, so the before/after comparison has both
     sides.
  3. `POST {adapter}/api/sessions/{id}/file` with the new content (proxy via
     a new `writeApiSessionFile({ fetchImpl, baseUrl, apiKey, sessionId,
     path, content, note })` in `leaApiClient.mjs`, parallel to the existing
     `fetchApiSessionDetail`).
  4. `POST {adapter}/api/sessions/{id}/lean-check` (new
     `runApiSessionLeanCheck({ ... })` in `leaApiClient.mjs`, same pattern).
  5. Classify the edit (Phase 3) using the before/after header snapshot plus
     whether step 4's own check passed.
  6. If classification says "cascade required," call `dependentsOf` (Phase
     0) on the edited module, then for each dependent session, call the
     adapter's `lean-check` endpoint (the dependent's *own* unedited file —
     v1 does not touch dependent files) and `add_code_step`-equivalent via
     that same adapter route, which already back-fills the verdict onto the
     dependent's latest code_step. The `author="cascade"` value needs a thin
     adapter-side acceptance — see below.
  7. Return `{ ownResult, dependentsImpact[] }` per the feature spec's
     `LeanPaneEditSaveRequest`/response shape.

**One real gap to resolve in this phase, not assumed away:** the existing
`POST /api/sessions/{id}/lean-check` route back-fills the verdict onto the
session's *own* latest code_step using whatever `author` that step already
has (`routes/sessions.py:143` area — confirm by reading the full handler
body, not just the part already excerpted in Section 1) — it does not take an
`author` parameter to stamp a *new* step as `"cascade"`. Two options, decide
during implementation rather than guessing here:
  - (a) Extend `PathRequest`/the `lean-check` route with an optional `author`
    field that, when the check changes the verdict, inserts a *new*
    code_step (not just back-filling the latest one) attributed to that
    author — closest to "cascade re-check is its own timeline entry."
  - (b) Have the companion call a different, new small adapter endpoint
    specifically for an out-of-band re-check that doesn't belong to the
    session's own edit history, e.g. `POST
    /api/sessions/{id}/lean-check?recordAs=cascade`.
  Recommendation: (a), since it's a smaller adapter change and keeps "every
  check produces a code_step" as the one model, rather than introducing a
  side-channel check that bypasses the timeline. Flag this as the first thing
  to confirm with a quick read of the full `lean_check_session` handler
  before writing the cascade loop.

**Route registration** (`server.mjs`'s request-dispatch block, alongside
`:1094-1123`):

```text
POST /lean-pane/edit/start
POST /lean-pane/edit/save
```

**Tests: `apps/overleaf-extension/tests/leanPaneEdit.test.mjs`**
- `validateEditPayload`: missing/invalid fields rejected with the right error
  codes, mirroring `companion.test.mjs`'s existing coverage of
  `validateTargetPayload`.
- `resolveEditSession`: no session → `no_session` error, not a created
  session (the one deliberate divergence from chat).
- `handleLeanPaneEditSave` against a fake `fetchImpl` (the existing test
  pattern in `companion.test.mjs`/`leaApiClient.test.mjs` stubs `fetchImpl`
  rather than hitting a real adapter): proof-only edit to a theorem → no
  cascade calls made; signature edit → cascade calls made for each fixture
  dependent; a `def` body edit with unchanged signature → cascade calls still
  made (the def-specific risk row).
- No-op save (identical content) → adapter's existing `{unchanged: true}`
  short-circuit is passed through, no cascade run.

### Phase 3 — Signature-change classification

**New module: `apps/overleaf-extension/companion/leanSignatureDiff.mjs`**
(or co-locate in `leanDependencyGraph.mjs` if small enough by the time it's
written — decide once the header-parsing regex is in hand, not up front)

- `function parseDeclarationHeader(content, declarationName)` — locate the
  `theorem|lemma|def|abbrev <declarationName>` token (the recorded proof
  files are single-declaration-per-file per Section 1.3, so this is "find the
  first top-level declaration," not a multi-declaration search — confirm
  this single-declaration assumption holds for every recorded artifact shape
  by sampling a real `workspace/proofs/Lea.*/**/*.lean` file before relying
  on it) and return `{ keyword: "theorem"|"lemma"|"def"|"abbrev", name, header:
  string }` where `header` is everything from the keyword through the `:=`
  that opens the proof (balanced-paren/brace aware, since binder types can
  contain `:=` themselves in default-value syntax — reuse a balanced-scan
  helper rather than a naive `indexOf(":=")`).
- `function classifyEdit({ before, after })` →
  ```ts
  type EditClassification =
    | { kind: "proof-only" }       // header unchanged, same keyword/name/type
    | { kind: "signature" }        // header changed (incl. binder/type edits)
    | { kind: "renamed", from: string, to: string }  // name changed
    | { kind: "definition-body" }  // keyword is def/abbrev, anything changed
    | { kind: "own-check-failed" };  // own lean_check after the edit failed
  ```
  Whitespace-normalize both headers the same way `normalizeTargetText`
  already normalizes statement text (`theoremParser.mjs`) before comparing,
  so reformatting alone never registers as a signature change.
- `function cascadeRequired(classification)` → `true` for everything except
  `proof-only`. This is the single predicate `handleLeanPaneEditSave` calls.

**Tests: `apps/overleaf-extension/tests/leanSignatureDiff.test.mjs`**
- Proof-body-only edit (tactic block changes, same `theorem foo (n : ℕ) : ...`
  header) → `proof-only`.
- Hypothesis added/removed, binder type changed, conclusion changed →
  `signature`.
- Declaration renamed (`theorem foo` → `theorem foo'`) with everything else
  identical → `renamed`.
- `def`/`abbrev` body changed, header identical → `definition-body`.
- Whitespace-only reformatting of the header (line wrap, extra spaces) →
  `proof-only`, not a false `signature` positive — this is the regression
  test for the normalization step.
- A header that fails to parse at all (malformed Lean, unexpected shape) →
  classification falls back to `signature` (fail toward over-cascading, never
  under-cascading — a missed real signature change is worse than one
  unnecessary cascade check).

### Phase 4 — Extension UI: the edit surface

**`extension/leanPaneView.mjs` additions** (pure, testable, same pattern as
existing exports):

- `canEditPaneItem(item)` — `Boolean(item.leanArtifactContent)`, mirroring
  the existing `canChatPaneItem`/`canFormalizePaneItem` predicates content.js
  already calls.
- `paneItemToEditTarget(item, overleafProjectId)` — same shape as
  `paneItemToChatTarget` (`:315`), reused for both `/lean-pane/edit/start` and
  `/lean-pane/edit/save` request bodies.
- `formatDependentsImpact(dependents)` — turn a
  `dependentsImpact[]`/`affectsDependents[]` array into the short summary
  string the feature spec shows (`"Editing X may affect 2 downstream
  item(s): ..."`), so `content.js` doesn't build display strings inline (the
  existing module already centralizes this kind of formatting, e.g.
  `formatPaneStatus`).

**`extension/content.js` additions**, inside `renderLeanPaneItemDetail`
(`:519`):

- `renderEditButton(item)` — same construction pattern as
  `renderChatButton`/`renderFormalizeButton`, gated by
  `leanPaneView.canEditPaneItem(item)`, inserted into the existing `actions`
  div alongside the current four actions.
- Clicking it calls `POST /lean-pane/edit/start`, then swaps the `<pre
  class="ol-lean-project-artifact">` (currently rendered read-only via
  `renderLeanPaneCode`, `:548-552`) for a `<textarea
  class="ol-lean-project-edit-textarea">` pre-filled with the fetched
  content, plus a small dependents-impact note (from
  `formatDependentsImpact`) shown above it when the pre-save dependents list
  is non-empty, plus "Save" / "Cancel" buttons.
- "Save" posts `POST /lean-pane/edit/save`, disables the textarea while in
  flight (same `aria-busy`/disabled pattern `renderFormalizeButton`'s click
  handler already uses for its button during the request), then on response:
  - re-renders the item's own status/artifact from `ownResult` (reuse the
    existing manifest-item update path the pane already has for
    formalize-from-pane's live polling, item 4/12 in
    `PLAN-overleaf-lean-pane-improvements.md` — do not build a second
    "update one item in place" code path).
  - if `dependentsImpact` is non-empty, renders a small inline list under the
    edited item: each affected item's label, its new status, and (if
    changed) a "broken by this edit" marker — this is the
    `brokenByUpstream` metadata from the feature spec surfacing in the UI.
    These are *other* pane items; the simplest correct v1 rendering is to
    trigger the same manifest-refresh the pane already does after a
    formalize action (`scheduleLeanPaneRefresh`/`refreshLeanPaneNow`,
    already used elsewhere in `content.js`), so affected items update via
    the normal manifest path rather than a bespoke partial-update path for
    cross-item changes.
- "Cancel" discards the textarea and re-renders the read-only artifact view
  (no network call).

**`extension/content.css` additions**: `.ol-lean-project-edit-textarea` (
monospace, sized like the existing `.ol-lean-project-artifact` block) and a
small `.ol-lean-project-impact-note` style for the dependents list, following
the existing `ol-lean-project-*` naming convention throughout this file.

**Tests**
- `leanPaneView.test.mjs`: `canEditPaneItem`, `paneItemToEditTarget`,
  `formatDependentsImpact` — pure-function coverage, same style as the
  existing tests in this file for the chat/formalize helpers.
- `contentActions.test.mjs`: edit button renders only when
  `canEditPaneItem` is true; save posts the expected payload shape; cancel
  makes no network call. Follows the existing pattern of testing `content.js`
  action wiring against a stubbed `fetch`.

### Phase 5 — Docs

- `FEATURE-overleaf-lean-pane.md`: add a one-line cross-reference to this
  feature under "Version 2: Formalization Actions" or a new "Related" note,
  the way `FEATURE-overleaf-lean-pane-file-organization.md` is referenced
  from the improvements plan.
- This plan file: add a Status section once work starts, tracking phases the
  way `PLAN-overleaf-inline-lea-tags.md` does.
- `README.md`: no change expected (manual editing is a pane-internal
  interaction, not a setup-affecting feature) — confirm during Phase 4
  rather than assuming.

---

## 3. Suggested sequencing

1. **Phase 0 (reverse-dependency index).** Pure backend logic, fully unit
   testable with in-memory fixtures, zero adapter or UI dependency. Land and
   green first — every later phase calls into this.
2. **Phase 3 (signature classification)** can be built in parallel with
   Phase 0 — it's independent, pure-function logic operating on Lean source
   text, no filesystem access.
3. **Phase 1 (adapter doc/test touch-up)**, resolved alongside the "extend
   `lean-check`'s recording" decision flagged in Phase 2 — small, but Phase 2
   is blocked on deciding option (a) vs (b) there, so resolve that decision
   early rather than discovering it mid-Phase-2.
4. **Phase 2 (companion edit-save + cascade)**, now that Phases 0, 1, and 3
   all exist to compose.
5. **Phase 4 (extension UI)** last — it's the thinnest phase precisely
   because Phases 0-3 did the real work, matching the pattern already seen in
   `PLAN-overleaf-inline-lea-tags.md` Phase 5 ("expected to need close to
   nothing").
6. **Phase 5 (docs)** once endpoint names and the `author="cascade"`
   convention are final.

## 4. Edge cases to handle

(Carried forward from the feature spec's own edge-case section, with the
concrete handling location named for each.)

- **Cascade racing a live run on a dependent** → Phase 2's cascade loop must
  check `findActiveJob`/the dependent's session for an in-progress run
  (reuse the existing helper) before calling `lean-check` on it, and skip
  with a `busy` marker in `dependentsImpact` rather than racing it.
- **Renamed declaration** → Phase 3's `renamed` classification is reported
  distinctly in `dependentsImpact` (`brokenByUpstream` with a `renamed: true`
  flag), and Phase 4's UI copy for this case should say "declaration
  renamed" rather than the generic "broken by an edit" text, per feature spec
  acceptance criterion 8.
- **Dependent has no recorded session at all** (declared `uses=` but never
  formalized, so it's not in `dependentsOf`'s result because there's no file
  to import anything yet) → not reachable as a "dependent" by construction
  (Phase 0's index only contains files that exist on disk), so no special
  handling needed — confirmed by Phase 0's test coverage already excluding
  this case.
- **Diamond dependents re-checked twice in quick succession** (two edits in a
  short window) → accepted as-is per the feature spec; no de-bounce planned
  for v1.
- **Concurrent edits to the same item from two tabs** → unchanged from the
  standalone canvas's existing last-write-wins behavior (`commit_write`); no
  new conflict handling in this feature.

## 5. Testing & verification

- Unit suites per phase, listed inline above
  (`leanDependencyGraph.test.mjs`, `leanSignatureDiff.test.mjs`,
  `leanPaneEdit.test.mjs`, `leanPaneView.test.mjs` additions,
  `contentActions.test.mjs` additions, `test_db_code_steps.py` addition).
- **Integration smoke test** (manual, before calling this done): a real
  two-target Overleaf project where `B` declares `uses=A`. Edit `A`'s proof
  body only → confirm `B` is never re-checked (no spurious cascade). Edit
  `A`'s signature → confirm `B` is re-checked and, if now broken, shows
  `brokenByUpstream` pointing at `A`. Rename `A`'s declaration → confirm `B`
  shows the `renamed` variant of the message. Edit a `def` `A` used
  computationally by `B` with only its body changed → confirm `B` is still
  re-checked (the def-specific risk row), even though a theorem in the same
  position would not have triggered it — this is the one scenario that most
  directly tests this feature's central claim (proof irrelevance vs.
  definitional unfolding) and is worth doing by hand against the real prover,
  not just asserting it in a unit test with a stubbed `lean_check`.
- Regression: full existing `overleaf-extension` test suite stays green
  throughout — this feature only adds new call sites/options, it does not
  change `validateTargetPayload`, `buildLeaPrompt`, or any existing
  formalize/chat code path.

## 6. Open risks

- **(Medium) The Phase 2 "how does a cascade re-check get its own
  `code_steps` row" decision.** Flagged explicitly in Phase 2 rather than
  assumed — read the full `lean_check_session` adapter handler before writing
  the companion's cascade loop, since the answer changes whether Phase 1's
  adapter touch is doc-only or needs an actual route parameter.
- **(Medium) Single-declaration-per-file assumption in Phase 3.** The header
  parser assumes one declaration per recorded proof file, based on the
  `workspace/proofs/<namespace>/<label>.lean` naming pattern seen in
  `proofPathFromProjectStep`. Confirm against a handful of real recorded
  files before relying on it — if a file ever carries auxiliary `have`/local
  lemmas above the main declaration, "first declaration found" needs to
  specifically mean "the one whose name matches `targetLabel`," not just the
  first one in the file (the plan above already specifies matching by
  `declarationName`, which avoids this trap as long as the search isn't
  simplified to "first declaration" during implementation).
- **(Low) Balanced-brace header scanning for binders with default values.**
  Lean binder syntax can nest `:=` inside a binder's default value
  (`(n : ℕ := 0)`), which would break a naive "first `:=` ends the header"
  scan. Reuse the project's existing balanced-suffix scanning utilities
  (`targetParserCore.mjs`'s `parseBalancedSuffix` family) rather than writing
  a new one, even though this module lives in `companion/`, not
  `extension/` — confirm during implementation whether to import across that
  boundary or port the small helper, given the two currently don't share
  code.
- **(Low) Cascade check cost on a large project.** v1's "re-check every
  transitive dependent on every risky edit, every time" is fine at
  pane-scale (dozens of targets) but would not scale to hundreds — out of
  scope to optimize now (the feature spec already accepts this), just
  recorded so a future "this got slow" report has a known cause and a known
  next step (cache the reverse index, debounce repeated edits to the same
  declaration).
