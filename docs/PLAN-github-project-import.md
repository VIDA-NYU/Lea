# Plan: Populate a Lea Project from GitHub

Implementation plan for
[`FEATURE-github-project-import.md`](./FEATURE-github-project-import.md). Read the
feature specification first for product behavior. This document turns it into
ordered, reviewable implementation slices with concrete contracts, code anchors,
tests, and rollout gates.

> Status: implemented and verified on 2026-08-04.

## Outcome

After this plan lands, a user can paste a GitHub repository URL while viewing a
new, existing, or Overleaf-bound Lea project. Lea previews an additive per-file
plan, copies only non-conflicting Lean files into the target project's current
namespace, commits them to that project's existing Git repository, checks them
locally, and attaches matching declarations to existing/tagged formalizations.
Unmatched modules remain available for imports without becoming formalization
cards or Blueprint nodes.

## Guiding facts verified against the current tree

- Project repositories live at `proofs_root / project.namespace.replace(".", "/")`
  through `projects.project_repo_dir`. A project run uses that directory as its
  working directory, and `projects.compose_context_message` already inventories every
  project `.lean` file. No new agent-context mechanism is needed for unmatched modules.
- Project creation/provisioning is split between `projects.py` (filesystem/repository)
  and `store.py` (SQLite metadata). Import targets an already-provisioned project; it
  must not call `provision_project` or initialize another Git repository.
- `GitStore.commit_all(repo, subject, paths=[...])` stages exact paths and uses the
  existing per-repository lock. Import must always pass the added paths so unrelated
  concurrent project changes cannot enter the import commit.
- The only current project-wide mutation interlock is
  `store.project_has_active_run(project_id)`, used by namespace migration. There is no
  general project write lock. The importer therefore needs stale-plan revalidation,
  exclusive file creation, and an import/migration interlock; it cannot assume the
  target tree stays unchanged after preview.
- GitHub skill import already supplies the security pattern in `ghimport.py`: exact
  GitHub URL normalization, shallow clone, timeout, one-shot token injection, error
  scrubbing, and temporary-directory cleanup. Project import should extract/reuse the
  generic clone primitives, not duplicate or call the skill-specific Markdown locator.
- The project declaration scanner is currently private in `graph.py`
  (`_scan_lean_decls`, `_DECL_RE`, `_NAMESPACE_RE`). `artifacts.py` has overlapping
  single-declaration regexes. Import needs all declarations and declaration spans, so
  these must become one shared public scanner before import indexing is built.
- Formalizations are unique by project + `declaration_name`; their file relationships
  live in `formalization_files`. `formalizations.decorate` derives validity from the
  latest path-matched timeline step. The normal artifact row is therefore created only
  for an imported declaration that is actually attached to a formalization.
- `GET /api/projects/by-slug/{slug}/target-status` matches artifact rows by exact
  declaration name and returns declaration presence, `sorry`, check, and source-hash
  evidence to the Overleaf companion. Import reconciliation should feed this existing
  ledger instead of adding a second status system.
- Overleaf's stable formalization origin key is already
  `chatTargetKey({overleafProjectId, targetKind, targetLabel})`, identical to the job key.
  `server.mjs` currently sends it as `new_formalization.origin_key`. GitHub import target
  synchronization must reuse the same key.
- Adapter startup recovery is wired in `main.startup`; agent runs recover through
  `bridge.recover_runs_at_startup()`. Confirmed GitHub imports need a parallel recovery
  hook, not a FastAPI `BackgroundTasks` callback that disappears on restart.
- The standalone entry point belongs in `FilesystemTab.tsx`, whose toolbar already owns
  Export and GitHub Share. Overleaf's entry point belongs in the project settings/pane
  flow in `content.js`, with companion routing beside `/project/identity`, `/project/graph`,
  and `/share/github`.

## Non-negotiable invariants

Every PR in this plan must preserve these:

1. The target project ID, slug, namespace, `.lea` content, Git history, and push remote
   remain authoritative and unchanged by source metadata.
2. Import writes only destination paths proven absent at the instant of creation.
3. A path/declaration conflict skips that source file; it never aborts independent files.
4. Only `.lean` files are imported. Source Lake files, scripts, `.lea` context, LaTeX,
   submodules, and generated/vendor trees never enter the target.
5. Imported bytes never produce a `proved`/`defined` state without a local check step.
6. Unmatched declarations do not create formalizations, artifacts, or Blueprint nodes.
7. Matching/adoption is exact-name and kind-compatible; no fuzzy match or overwrite.
8. SafeVerify evidence is never synthesized or copied.
9. The GitHub token exists only for the clone invocation and is scrubbed from every
   persisted or returned value.
10. Repeating the same import is idempotent.

## Shared API vocabulary

Implement one vocabulary across Python, TypeScript, and the companion.

### Tagged target descriptor

```json
{
  "origin_key": "overleaf-slug:theorem:latex-label",
  "label": "latex-label",
  "declaration_name": "continuity_of_f",
  "kind": "theorem",
  "display_title": "continuity_of_f",
  "statement": "…",
  "source_hash": "sha256…"
}
```

The extension supplies label/declaration/kind/source information; the companion computes
`origin_key` using `chatTargetKey` before calling the adapter. The adapter never accepts a
browser-invented project slug.

### Per-file disposition

Use exactly the specification's values:

```text
add
already_present
path_conflict
declaration_conflict
unsupported_module_layout
excluded
```

Every preview and final result carries `source_path`, `destination_path` when known,
`disposition`, `reason`, post-rewrite `content_sha256`, discovered declarations, and
matched formalization summaries.

### Import lifecycle

Confirmed imports use:

```text
applying -> checking -> complete | complete_with_issues | failed
```

Preview IDs are ephemeral and are not lifecycle rows. A preview that expires returns
`410 import_preview_expired`.

## Delivery map

The seven PRs below are independently reviewable and land in order. PR1–PR4 produce a
fully testable adapter feature. PR5 exposes it in the standalone UI. PR6 exposes it in
Overleaf. PR7 is the concurrency/recovery/end-to-end gate.

---

## PR1 — Shared Lean declaration scanner and declaration-scoped status

This is a behavior-preserving refactor for existing one-declaration files plus the
multi-declaration support the importer requires.

### 1.1 Add a public scanner

**Files:**

- `apps/lea-standalone/adapter/app/artifacts.py`
- `apps/lea-standalone/adapter/app/graph.py`
- new/extended `apps/lea-standalone/adapter/tests/test_artifacts.py`
- `apps/lea-standalone/adapter/tests/test_graph.py`

Move the shared regex/comment/string handling into `artifacts.py` and expose:

```python
@dataclass(frozen=True)
class LeanDeclaration:
    short_name: str
    full_name: str
    keyword: str
    kind: str
    start_line: int
    end_line: int
    span: str

def scan_lean_declarations(code: str) -> list[LeanDeclaration]: ...
def declaration_present(code: str | None, name: str) -> bool: ...
def declaration_contains_sorry(code: str | None, name: str) -> bool: ...
```

Support the keywords already recognized across `graph.py` and `artifacts.py`:
`theorem`, `lemma`, `def`, `abbrev`, `structure`, `class`, `inductive`,
`coinductive`, `instance`, and `opaque`, including the existing modifier prefixes.
Keep nested namespace accounting deterministic. Fully qualified declaration names remain
fully qualified; otherwise prefix the active namespace stack.

Refactor `graph._scan_lean_decls` to call the public scanner while preserving its current
return behavior for graph callers. Do not make project import depend on private graph
helpers.

### 1.2 Make `sorry` status declaration-scoped

`formalizations._validity` currently calls `contains_sorry_marker` on the whole file.
That would mark every formalization in a multi-declaration imported file as failing when
only one declaration contains `sorry`. Change it to use
`declaration_contains_sorry(blob_content, declaration_name)` when a declaration is known,
with whole-file fallback only for unresolvable legacy rows.

Make the same change in `routes/projects.py` target-status so Overleaf and the standalone
formalization read model agree.

### 1.3 Tests

- Multiple declarations in one namespace, including a definition and theorem.
- Nested namespaces and an explicitly qualified declaration.
- Comments/strings containing declaration keywords or `sorry` do not create false hits.
- One declaration with `sorry` does not taint a sibling declaration.
- Graph FQN-to-file resolution is unchanged for current one-file fixtures.
- Existing artifact classification and `declaration_present` tests remain green.

### Verify

```bash
cd apps/lea-standalone/adapter
./.venv/bin/python -m pytest tests/test_artifacts.py tests/test_graph.py tests/test_formalizations.py tests/test_routes_projects.py -q
```

---

## PR2 — Migration and store contract for imports

### 2.1 Migration `0009_github_project_imports.py`

**Files:**

- new `apps/lea-standalone/adapter/migrations/versions/0009_github_project_imports.py`
- `apps/lea-standalone/adapter/app/store.py`
- adapter DB/store tests

Set `down_revision = "0008_multi_formalization_sessions"` and add:

```text
github_imports
  id text primary key
  project_id text not null references projects(id)
  session_id text references sessions(id)
  source_url text not null
  source_ref text
  source_commit_sha text not null
  source_namespace text
  destination_namespace text not null
  status text not null
  destination_snapshot text
  commit_sha text
  error_detail text
  created_at text not null
  updated_at text not null

github_import_files
  import_id text not null references github_imports(id)
  source_path text not null
  destination_path text
  disposition text not null
  reason text
  content_sha256 text
  code_step_id integer references timeline(id)
  check_status text
  check_detail text
  created_at text not null
  updated_at text not null
  primary key (import_id, source_path)

github_import_declarations
  id text primary key
  import_id text not null references github_imports(id)
  project_id text not null references projects(id)
  destination_path text not null
  declaration_name text not null
  full_name text not null
  kind text not null
  module_name text not null
  formalization_id text references formalizations(id)
  source_hash_at_match text
  created_at text not null
  updated_at text not null
  unique (project_id, destination_path, declaration_name)
```

Add check constraints for import status, file disposition, and check status. Add indexes
for `(project_id, created_at desc)`, `(project_id, declaration_name)`,
`formalization_id`, and recoverable import statuses.

The declaration uniqueness is project/path/name, not merely project/name: it preserves
the evidence needed to report semantic duplicates while the matcher still requires an
unambiguous exact name before adoption.

### 2.2 Store methods

Add transaction-scoped methods rather than issuing route-level SQL:

- `create_github_import(...)`
- `get_github_import(import_id)` / `list_project_github_imports(project_id)`
- `set_github_import_status(...)`
- `upsert_github_import_file(...)`
- `set_github_import_file_check(...)`
- `upsert_github_import_declaration(...)`
- `bind_github_import_declaration(declaration_id, formalization_id, source_hash)`
- `find_unbound_imported_declarations(project_id, declaration_name)`
- `list_recoverable_github_imports()`
- `project_has_active_import(project_id)` for `applying`/`checking`
- one aggregate read returning counts and failures for the progress API

Add one atomic helper for the apply/index phase so a retry cannot duplicate the
provenance session, code steps, file rows, declaration rows, links, or artifacts.

### 2.3 Explicit project deletion

`store.delete_project_cascade` manually deletes dependent rows because foreign keys are
not enabled. Extend it in dependency order:

1. import declaration rows;
2. import file rows;
3. import rows;
4. then the existing session/formalization/project cascade.

If the provenance session is an ordinary project session, the existing session cascade
owns its timeline rows.

### 2.4 Migration/store tests

- Schema columns, constraints, indexes, and migration head.
- Upserts are idempotent.
- Same declaration/path repeated by a later import updates provenance without duplicating.
- Aggregate counts distinguish every disposition/check outcome.
- `project_has_active_import` covers only applying/checking.
- Project deletion leaves no import rows or import provenance session.

### Verify

```bash
cd apps/lea-standalone/adapter
./.venv/bin/python -m pytest tests/test_db_*.py tests/test_store.py -q
```

---

## PR3 — GitHub source acquisition and pure additive planner

The planner must be independently testable without FastAPI, SQLite writes, or network.

### 3.1 Extract generic GitHub clone primitives

**Files:**

- new `apps/lea-standalone/adapter/app/github_source.py`
- `apps/lea-standalone/adapter/app/ghimport.py`
- `apps/lea-standalone/adapter/tests/test_github_source.py`
- existing `test_ghimport.py`

Move/refactor these generic pieces out of skill-specific `ghimport.py`:

- exact root repository URL parser;
- shallow clone with timeout;
- source HEAD SHA/default ref read;
- one-shot token injection and scrubbed errors; and
- tracked-file listing with Git mode information.

Keep skill URL support (`tree`, `blob`, raw, gist) in `ghimport.py`. Project import accepts
only `https://github.com/{owner}/{repo}[.git]` in v1.

Clone with hooks disabled, submodules off, and `GIT_LFS_SKIP_SMUDGE=1`. Never persist the
tokenized URL outside the child process arguments/error scrubber.

### 3.2 New pure planner module

**New file:** `apps/lea-standalone/adapter/app/github_project_import.py`

Separate pure analysis from orchestration:

```python
def inventory_source(clone: Path, limits: ImportLimits) -> SourceInventory: ...
def infer_source_namespace(files: list[SourceLeanFile]) -> NamespaceInference: ...
def map_source_file(file, source_namespace, destination_namespace) -> MappedFile: ...
def plan_import(source, destination_repo, destination_namespace,
                formalizations, tagged_targets) -> ImportPlan: ...
```

Use dataclasses for `SourceLeanFile`, `MappedFile`, `PlannedFile`, `TaggedTarget`,
`DeclarationMatch`, and `ImportPlan`; routes serialize them only through response models.

### 3.3 Inventory and mapping rules

- Start with `git ls-files -s -z -- '*.lean'`; inspect mode `120000` to reject symlinks.
- Reject `.lake`, vendor/dependency/build roots, nested repos, LFS pointer bodies, path
  escapes, per-file/aggregate size excess, and more than the configured file cap.
- Infer at most one common `Lea.<Segment>` source project namespace. No namespace is
  valid; inconsistent/multiple project roots make affected files unsupported.
- Preserve repo-relative paths beneath the target repo.
- Rewrite exact source-project namespace occurrences using the same bounded logic as
  namespace migration, but only in staged incoming text. Promote
  `projects._rewrite_namespace_text` to a public helper rather than copy it.
- Compute SHA-256 after rewrite; this is the hash used for `already_present`.
- Derive destination module name as
  `<destination namespace>.<relative path without .lean, slash -> dot>`.

### 3.4 Conflict algorithm

Scan the current target repository once with the PR1 scanner. For each mapped source file:

1. Existing destination path + identical rewritten bytes → `already_present`.
2. Existing destination path + different bytes → `path_conflict`.
3. Absent path but an incoming FQN/short name would collide with a declaration in a
   different existing file → `declaration_conflict`.
4. Duplicate declarations among incoming files make every member of the ambiguous group
   `declaration_conflict`; never let sort order choose a winner.
5. A formalization already linked to a different primary file makes the source file a
   `declaration_conflict` for that match.
6. Otherwise → `add`.

Planner target matching is side-effect free. It can predict a match to an existing DB
formalization or a supplied tagged target, but rows for missing tagged targets are created
only during confirmation.

Kind compatibility:

- `definition` accepts definition-like Lean keywords.
- `theorem`/`lemma` accept `theorem` or `lemma`.
- `counterexample`/`disproof` may accept theorem/lemma declarations.
- `other` is never auto-matched without an exact compatible stored declaration kind.

### 3.5 Preview registry

Add an in-process, lock-protected preview registry holding:

- opaque UUID;
- target project ID and namespace;
- source clone path and source commit;
- source inventory/plan;
- destination snapshot token;
- expiry (15 minutes); and
- a consumed flag.

The snapshot token hashes project namespace plus the sorted current `.lean` path/content
hashes; `HEAD` alone is insufficient because a tool or user can leave uncommitted content.
Clean expired previews opportunistically on create/get and at startup. A restart invalidates
previews and the client re-analyzes.

### 3.6 Planner tests

Use real local Git repositories as clone fixtures, following `test_ghimport.py`; no network.
Cover:

- public/private helper behavior and token scrub;
- tracked versus ignored/generated/symlink/LFS files;
- namespace inference and incoming-only rewrite;
- nested relative paths/module names;
- all six dispositions in one mixed plan;
- identical file re-index case;
- existing and incoming declaration collisions;
- multi-declaration file with one predicted target match and one unmatched declaration;
- stale snapshot and expired/consumed preview.

### Verify

```bash
cd apps/lea-standalone/adapter
./.venv/bin/python -m pytest tests/test_github_source.py tests/test_ghimport.py tests/test_github_project_import.py -q
```

---

## PR4 — Apply, reconcile, check worker, recovery, and adapter routes

This PR makes the backend feature complete before either UI calls it.

### 4.1 Durable confirmation staging

When a preview is consumed, move its clone atomically from the ephemeral temp directory
to a deterministic ignored staging path:

```text
apps/lea-standalone/data/github-imports/<import-id>/source/
```

Create the `github_imports` + planned file rows before target writes. The import ID makes
the staging path derivable after restart, so no arbitrary filesystem path is stored in
SQLite. Update reset-local-state handling only if current `data/` cleanup does not already
cover this directory.

### 4.2 Revalidation and exclusive writes

Before applying:

1. Require the project still exists and its namespace is unchanged.
2. Return `409 project_busy` if `store.project_has_active_run(project_id)` or another
   active import exists.
3. Re-run the planner against the live target and record the final dispositions.
4. For each final `add`, create parents and open the destination with exclusive-create
   semantics (`O_CREAT | O_EXCL` / mode `"x"`). If another writer wins after re-plan,
   downgrade that file to `already_present` or `path_conflict` based on its current hash.
5. Commit only successfully created paths using `GitStore.commit_all(..., paths=added)`.
   Zero additions create no commit.

Add `store.project_has_active_import` to namespace-migration's busy check so a repository
cannot move during apply/check. Checks read paths in the repo and must finish before rename.

### 4.3 Tagged target upsert

During confirmation, resolve each supplied tagged descriptor in this order:

1. existing `origin="overleaf"` + exact `origin_key`;
2. existing exact project declaration name;
3. otherwise create a formalization from the descriptor.

If origin key and declaration name resolve to different rows, record a target conflict and
bind neither. Update the current `source_hash` for a resolved current tag using existing
formalization update rules.

Standalone imports supply no new target descriptors; they match only formalizations that
already exist in the target project.

### 4.4 Import provenance and declaration indexing

Create or reuse one project session:

```text
GitHub import — owner/repository @ <short-sha>
```

For every final `add` or `already_present` file:

- create one environment-authored code step with exact current bytes, `run_id=NULL`,
  summary/provenance naming the import ID/source SHA, and no verdict yet;
- store the code-step ID on `github_import_files`;
- upsert every declaration into `github_import_declarations`;
- for each accepted target match, link the session and file to the formalization and
  upsert the normal artifact with its formalization ID and current target source hash;
- do not create a normal artifact or formalization for unmatched declarations.

One file-level code step is sufficient for several matched declarations because the
formalization read joins through `formalization_files.path`. Link the provenance session
to every matched formalization, not to unmatched declarations.

### 4.5 Recoverable check worker

Add a small single-worker dispatcher in `github_project_import.py` (or a focused
`github_import_worker.py`) modeled on the persistence/dedup ideas in `bridge`, but separate
from model-run admission:

- `enqueue_import(import_id)` is idempotent;
- worker loads current DB/file state, never trusts in-memory preview objects;
- check each distinct final destination path once, in local import dependency order where
  available;
- call `interface_check` and `store.set_code_step_check`;
- update `github_import_files` check fields after each path;
- finish `complete` only when every checked file passed and matched declarations contain
  no `sorry`; otherwise `complete_with_issues`;
- clean durable staging after apply no longer needs it.

Wire `recover_github_imports_at_startup()` in `main.startup` after DB initialization.

Recovery rules:

- `applying` + staging exists: re-run exclusive application/indexing idempotently.
- `applying` + staging missing: verify target hashes for recorded additions, finish what
  can be indexed, and mark absent planned files as interrupted issues.
- `checking`: enqueue only files whose code step has no verdict.
- terminal rows are never re-enqueued.

### 4.6 Later adoption hook

Add a service method:

```python
try_adopt_imported_declaration(project, formalization) -> AdoptionResult
```

It succeeds only for one unambiguous unbound exact-name candidate whose file still exists,
still contains the declaration, has no conflicting formalization primary file, and has a
current check step. It then links the file/session, creates the normal artifact, binds the
import declaration, and applies the formalization's current source hash.

Call it from:

- project formalization creation in `routes/formalizations.py`;
- the run-start path after a new project formalization is resolved in `routes/runs.py`;
  and
- a new target-sync endpoint used by the Overleaf companion before manifest status reads.

The target-sync endpoint accepts current tagged descriptors, upserts those formalizations,
tries adoption, and returns results. Keep `GET target-status` read-only.

### 4.7 Routes and Pydantic models

**File:** `routes/projects.py`

By ID:

```text
POST /api/projects/{project_id}/github-imports/preview
POST /api/projects/{project_id}/github-imports
GET  /api/projects/{project_id}/github-imports/{import_id}
```

By slug:

```text
POST /api/projects/by-slug/{slug}/github-imports/preview
POST /api/projects/by-slug/{slug}/github-imports
GET  /api/projects/by-slug/{slug}/github-imports/{import_id}
POST /api/projects/by-slug/{slug}/formalizations/sync
```

The by-slug preview accepts project identity hints and calls `ensure_project` if missing;
an existing project is used unchanged. The companion supplies tagged targets. By-ID routes
require an existing project and may optionally accept targets only for test/internal parity;
the standalone UI sends none.

Return structured errors/codes from the specification. Never expose clone stderr directly
without the existing scrubber.

### 4.8 Backend integration tests

Add focused test files rather than expanding `test_routes_projects.py` indefinitely:

- `tests/test_github_import_routes.py`
- `tests/test_github_import_apply.py`
- `tests/test_github_import_recovery.py`

Cover:

- blank and partially populated targets;
- by-slug ensure and existing-project reuse;
- stale preview re-plan;
- active run/import/migration interlocks;
- exclusive-create race simulation;
- exact-path commit and unchanged pre-existing file hashes;
- target row upsert by origin key/declaration;
- matched artifact/file/session/code-step creation;
- unmatched declaration creates no formalization/artifact/Blueprint node;
- pass/error/`sorry` status derivation;
- later target sync adopts an earlier unmatched declaration;
- restart in applying/checking and repeated confirmation/import idempotency;
- token absence from response, DB, Git config, and stored diagnostics.

### Verify

```bash
cd apps/lea-standalone/adapter
./.venv/bin/python -m pytest tests/test_github_import_*.py tests/test_routes_projects.py tests/test_formalizations.py -q
./.venv/bin/python -m pytest -q
```

Manual API smoke test with a local/private fixture can follow the same request order as
the UI: preview → inspect dispositions → confirm → poll until terminal → query target-status.

---

## PR5 — Standalone Filesystem import UI

### 5.1 API/types

**Files:**

- `apps/lea-standalone/src/app/lib/types.ts`
- `apps/lea-standalone/src/app/lib/api.ts`

Add typed contracts for source, target descriptor, declaration summary, planned file,
preview, import result/progress, and error detail. Add:

```ts
previewProjectGithubImport(projectId, repositoryUrl)
confirmProjectGithubImport(projectId, previewId)
getProjectGithubImport(projectId, importId)
```

Use the existing `detailMessage` helper but preserve structured error codes when the UI
needs a special state (`project_busy`, preview expiry, repository limit).

### 5.2 Component

**Files:**

- `FilesystemTab.tsx`
- new `components/GithubImportDialog.tsx` (recommended)
- `lea-v2.css`

Add an **Import** button with a download/GitHub icon beside Share/Export. Keep import state
outside the file viewer so selecting/editing a file does not reset the dialog.

Dialog phases:

1. URL entry.
2. Analyzing.
3. Review grouped by disposition with counts and expandable conflict reasons.
4. Confirming/applying.
5. Checking/progress.
6. Completion summary.

Show matches in a separate section:

- `Will populate formalization`: declaration + current formalization status.
- `Reusable module only`: module/declarations, explicitly saying no formalization will be
  created.

On terminal completion:

- refresh the filesystem tree;
- bump the parent project refresh signal so Formalizations and Blueprint reload;
- keep the summary open until dismissed; and
- offer direct selection of a failing/conflicting path when it exists in the project.

Poll the progress endpoint while `applying`/`checking`, with cleanup on unmount/project
change. A `410` before confirmation returns to URL entry; `409 project_busy` retains the
review and offers Retry.

### 5.3 Optional new-project convenience

Do not block the core UI on a combined wizard. After the project-level import works, add
an optional **Create and import** path to `NewProjectDialog.tsx` that:

1. creates/opens the blank project using the existing store;
2. opens `GithubImportDialog` preselected in the Filesystem tab.

Cancellation leaves the blank project, matching the specification.

### 5.4 Frontend tests

Keep state/format logic in a framework-free `.mjs` helper if needed so the current
Node test runner can cover:

- grouping/counting all dispositions;
- confirmation label counts only `add` files;
- matched versus reusable-only summaries;
- polling terminal/error transitions; and
- refresh effects after completion.

### Verify

```bash
npm run typecheck -w apps/lea-standalone
npm run test:frontend -w apps/lea-standalone
npm run build -w apps/lea-standalone
```

Manual: import a mixed local/GitHub fixture into a blank project and a project with one
existing conflict; inspect the commit/tree/formalization list/Blueprint.

---

## PR6 — Overleaf companion and extension workflow

### 6.1 Extension target shaping

**File:** `apps/overleaf-extension/extension/leanPaneView.mjs`

Add a pure helper that shapes each manifest item into import target metadata:

```js
paneItemToGithubImportTarget(item) => {
  targetKind,
  targetLabel,              // stable LaTeX/job anchor
  declarationName,          // leanDeclarationName || label
  displayTitle,
  statement,
  sourceHash
}
```

Do not generate `originKey` in the extension. The companion adds it with
`chatTargetKey`, ensuring identity stays identical to formalize/chat jobs.

### 6.2 Companion adapter clients

**File:** `apps/overleaf-extension/companion/leaApiClient.mjs`

Add by-slug clients that return the existing `{ok,status,body,error}` result shape:

- `previewGithubImportBySlug`
- `confirmGithubImportBySlug`
- `getGithubImportBySlug`
- `syncProjectFormalizationTargetsBySlug`

### 6.3 Companion handlers/routes

**File:** `apps/overleaf-extension/companion/server.mjs`

Add companion routes:

```text
POST /project/github-import/preview
POST /project/github-import/confirm
GET  /project/github-import/status?overleafProjectId=...&importId=...
```

Handlers:

1. validate/resolve `overleafProjectId`;
2. resolve current project identity using the existing ensure/identity path;
3. normalize manifest targets and compute each `originKey` with `chatTargetKey`;
4. forward to the adapter's by-slug endpoint; and
5. preserve structured per-file results/errors.

Before the Lean-pane manifest merges target-status, call the new target-sync client with
current descriptors so a tag added after an earlier import can adopt the stored module.
This call is additive/idempotent; failure must not make the entire pane disappear—surface
a project-level warning and continue with existing status evidence.

### 6.4 Extension UI

**Files:**

- `apps/overleaf-extension/extension/content.js`
- `content.css`

Add **Add Lean files from GitHub** to the project settings surface. Reuse the standalone
phase language and disposition vocabulary, adapted to vanilla DOM:

- analyze current repo link;
- show files added/already/conflicting/unsupported;
- show which current tagged items will be populated;
- show reusable-only modules separately;
- confirm and poll; and
- refresh identity, manifest, artifacts/target status, Blueprint, and usage-independent
  project UI at completion.

The action remains visible for an existing partially populated project. Do not gate it on
`identity.exists === false`; the only disabled state is a live project mutation/import.

### 6.5 Companion/extension tests

- Client URL/body encoding and result pass-through.
- Companion computes the same origin key as formalize/chat.
- Missing project is ensured; existing project reused.
- Mixed preview results preserved.
- Target sync/adoption before status load.
- `paneItemToGithubImportTarget` uses stable label and current declaration name correctly.
- Completion refresh updates matched items without inventing unmatched items.
- Existing settings/share/formalize flows remain unchanged.

### Verify

```bash
npm test -w apps/overleaf-extension
```

Manual in Overleaf: one already proved target, one missing target present in GitHub, one
source file conflict, and one unmatched helper module. Confirm only the missing target
changes status and the helper remains available through imports.

---

## PR7 — Hardening and end-to-end release gate

### 7.1 Concurrency matrix

Add integration tests for:

- two imports confirmed concurrently against overlapping paths—one exclusive-create
  winner, one deterministic skip;
- import versus Filesystem new-file creation;
- import versus active agent run;
- import versus namespace migration;
- project deletion while preview exists and while a confirmed import is active; and
- adapter restart during apply and during check.

Update destructive/migration routes to consult active imports where required. Project
delete may either reject an active confirmed import with `409` or cancel/settle it before
the existing cascade; choose reject for v1 because it is easier to reason about and
non-destructive.

### 7.2 Security/resource tests

- Host lookalikes and cleartext URLs rejected.
- Token scrubbed from subprocess errors, DB detail, API, logs captured in tests, and
  destination `.git/config`.
- Symlink/special/nested/LFS/submodule inputs excluded.
- File-count, per-file, total-size, and timeout limits return specified errors.
- Source `.lea`, `lakefile*`, scripts, and `.tex` never copy even when tracked.
- Path normalization handles Unicode, case-sensitive collisions, and `..` safely.

On case-insensitive filesystems, build a case-folded destination collision map during
planning so `Foo.lean` and `foo.lean` cannot race into an ambiguous module set.

### 7.3 End-to-end round trip

Automate the specification's mixed scenario with a local Git remote:

1. Target Overleaf project has one proved, one planned, and one missing tagged target.
2. Source repo contains a conflicting proved-target file, a valid planned-target file,
   and two helper modules (one imported by the matching proof).
3. Preview reports the exact dispositions/match/reusable counts.
4. Confirm preserves every existing target byte, adds the three eligible files, and
   commits only those paths.
5. Local checks populate the planned target; the conflict leaves the proved target
   unchanged.
6. Helpers exist in the filesystem/context but not Formalizations/Blueprint.
7. A later target sync adopts one helper declaration.
8. Repeating the import produces no new file writes, duplicate rows, or empty commit.

### 7.4 Full verification

```bash
cd apps/lea-standalone/adapter && ./.venv/bin/python -m pytest -q
npm run typecheck -w apps/lea-standalone
npm run test:frontend -w apps/lea-standalone
npm run build -w apps/lea-standalone
npm test -w apps/overleaf-extension
npm test
```

Run `npm run doctor` with the stack active, then execute the manual Overleaf scenario on
the unpacked extension.

---

## Acceptance-criteria traceability

| Feature acceptance area | Implemented in | Primary proof |
|---|---|---|
| Blank/existing/Overleaf target | PR4, PR5, PR6 | route + companion integration tests |
| Add only non-conflicting files | PR3, PR4 | mixed planner/apply tests |
| Existing bytes unchanged | PR4, PR7 | before/after content hashes |
| Conflict isolation | PR3, PR7 | mixed batch and concurrent apply |
| Identical-file reconciliation | PR3, PR4 | no-write + artifact-link test |
| Incoming-only namespace rewrite | PR3 | source/destination fixture comparison |
| Tagged formalization status | PR1, PR4, PR6 | target-status + pane reconciliation tests |
| Unmatched reusable modules only | PR2, PR4 | no formalization/artifact/Blueprint assertions |
| Later adoption | PR4, PR6 | target-sync adoption test |
| Failed checks retained/reported | PR4 | filesystem + terminal summary assertions |
| No imported SafeVerify | PR4 | decorated formalization audit assertion |
| Context/binding/history/remote preserved | PR4, PR7 | before/after metadata/Git tests |
| Token safety and limits | PR3, PR7 | scrub/guard tests |
| Idempotency/recovery | PR2, PR4, PR7 | repeated import + restart tests |

## Definition of done

- All seven PR slices and their focused tests are green.
- The full adapter, standalone, and Overleaf suites pass.
- The end-to-end mixed partial-project scenario passes twice against the same source.
- No existing file, project metadata, `.lea` context, remote, or unrelated status changes.
- A matching tag receives local derived evidence; an unmatched module is importable but
  absent from Formalizations and Blueprint.
- A restart during checking resumes without duplicate writes/rows.
- The raw GitHub token is absent from every persistent and user-visible surface.
- The feature specification and this plan are updated if implementation discovers a
  contract change; behavior must not drift silently.

## Out of scope for this plan

Overwrite/merge conflict resolution · updating an already imported file from a later
source revision · full arbitrary Lake-project conversion · importing source `.lea`
context or non-Lean files · replacing the target remote/history · automatic periodic
GitHub sync · imported SafeVerify evidence · fuzzy declaration matching.
