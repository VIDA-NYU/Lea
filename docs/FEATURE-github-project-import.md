# Feature: Populate a Lea Project from GitHub

> Status: implemented on 2026-08-04. This document defines the shipped behavior
> and acceptance criteria.

## Summary

Let a user populate a Lea project's workspace with Lean files from a GitHub
repository. The destination is either:

- a newly created Lea project, or
- the Lea project already bound to the current Overleaf document, whether empty or
  partially formalized.

Import is **additive and non-destructive**. Lea analyzes the repository, maps compatible
Lean files into the destination project's namespace, and adds only files that do not
conflict with existing project files or declarations. Existing files are never replaced,
merged, or renamed.

After copying the eligible files, Lea scans and checks them locally. If an imported
declaration matches a tagged or otherwise existing formalization in the destination
project, the imported file is attached to that formalization and its status is derived
from the local check result. Declarations that do not match a current formalization do
not create artificial work items: their files remain ordinary project modules, visible
to Lea and importable by future formalizations.

A typical Overleaf flow is:

1. An Overleaf document contains several `lea:`-tagged definitions/theorems. Some have
   no Lean artifact yet; others may already be formalized.
2. The user selects **Add Lean files from GitHub** and pastes a repository link.
3. Lea previews which files will be added, which will be skipped, and which tagged
   formalizations appear to match declarations in the new files.
4. The user confirms. Lea copies only non-conflicting files into the current project's
   workspace, commits them, and checks them locally.
5. Matching tagged items update to their derived status. Unmatched files simply become
   reusable modules in the same project.

## Goals

- Populate a new, empty, or partially populated project from a GitHub repository.
- Never overwrite existing project files or silently replace existing formalizations.
- Preserve the destination project's identity, namespace, project context, Git history,
  Overleaf binding, and configured push remote.
- Rebase compatible imported modules into the destination namespace so project-local
  imports resolve from the canonical Lake path.
- Reconcile imported declarations with current tagged/formalization records.
- Make unmatched imported modules discoverable and importable without turning every
  declaration into a formalization card or Blueprint node.
- Derive status from local file contents and local Lean checks, never from claims or CI
  metadata in the source repository.
- Support public repositories and private repositories accessible through the GitHub
  token already managed in Settings.
- Report every added, already-present, skipped, conflicting, and failing file.

## Core product decisions

| Question | Decision |
|---|---|
| What is the destination? | The project from which the user initiated import. A new-project wizard may create a blank destination first. |
| Can the destination already contain work? | Yes. Empty and partially populated projects are first-class use cases. |
| Does import overwrite existing files? | Never in v1. Different content at an occupied destination path is a conflict and that source file is skipped. |
| Is one conflict fatal to the batch? | No. Conflict handling is per file; every independent eligible file is still imported. |
| Does GitHub replace the project's Git repository? | No. GitHub is a temporary source. Added files are committed into the destination project's existing Git repository. |
| Does the source repository become the push remote? | No. The destination project's current remote remains unchanged. |
| Are source Instructions/Memory/Blueprint imported? | No. v1 imports Lean files only. The destination project's `.lea/` context remains authoritative and untouched. |
| What happens to a matching declaration? | It is attached to the existing/tagged formalization, indexed as its artifact, locally checked, and shown with the derived status. |
| What happens to an unmatched declaration? | Its file is retained as a reusable project module. It does not automatically create a formalization or Blueprint node. |
| Can a later tag adopt an earlier unmatched declaration? | Yes. Declaration metadata is indexed so later formalization creation can reconcile with the already-imported module. |
| Does import prove that a file is correct? | No. Only a successful local `lean_check`, plus the existing no-`sorry` rules, can produce the normal proved/defined state. SafeVerify remains separate. |

## Existing architecture this feature extends

A project already owns:

- a canonical workspace/Git repository at `workspace/proofs/Lea/<Project>/`;
- a `projects` row containing its immutable slug, display identity, namespace,
  repository path, and optional GitHub push remote;
- project-scoped formalizations and their associated files;
- an artifact index used by the Overleaf target-status ledger;
- timeline code steps containing file snapshots and local check verdicts; and
- `.lea/instructions.md`, `.lea/memory.md`, `.lea/blueprint.md`, and `.lea/files/`
  context that is composed into future runs.

The importer must operate through these existing boundaries. It writes into the current
project repository, records the write/check through the normal timeline/index model, and
lets existing status readers derive the result. It must not create a second hidden project
or replace the project's repository with the temporary GitHub clone.

## User experience

### Entry points

#### Standalone UI

Add **Import Lean files from GitHub** to the project's Filesystem tab, near Export and
GitHub sharing. It is available for any project, including a newly created blank project.

The **New project** dialog may offer **Create and import from GitHub** as a convenience,
but the semantics are still two conceptual operations:

1. create the empty destination project and its namespace; then
2. import eligible Lean files into that destination.

If import fails or the user cancels, the empty project remains valid and usable.

#### Overleaf extension

Add **Add Lean files from GitHub** to the current document's project settings. The action
is available whether its Lea project is absent, empty, or partially formalized.

- If no Lea project exists yet, the normal `ensure_project`/identity flow creates the
  empty project bound to the current Overleaf slug before analysis.
- If the project already exists, the importer targets it directly.
- The current Overleaf sources and tags are synchronized before preview so matching is
  based on the latest document state.

The extension sends the current tagged target descriptors—origin key/label, declaration
name, kind, display title, and source hash—to the adapter. Confirming import may create
missing formalization rows for those current tags, but it never creates formalizations
for unrelated GitHub declarations.

### Step 1: Analyze

The user pastes an HTTPS GitHub repository URL. v1 supports a repository root and its
default branch. Public repositories need no token; private repositories use the global
GitHub token without exposing it to clients.

The adapter shallow-clones into a private temporary directory and computes an import
plan against the **current destination project snapshot**. Preview does not write project
files or DB artifact/formalization records.

### Step 2: Review

The preview shows:

- source repository, branch, and commit SHA;
- destination project and authoritative namespace;
- total candidate Lean files;
- files to add;
- identical files already present;
- path or declaration conflicts to skip;
- files excluded because their module layout cannot be safely mapped;
- existing/tagged formalizations matched by incoming declarations; and
- incoming declarations with no current formalization, labeled **Reusable module only**.

The file list is inspectable and grouped by disposition. The confirmation button reads,
for example, **Add 12 Lean files** rather than the ambiguous **Import project**.

The preview is informative, not a promise that compilation will succeed. Check results
appear only after confirmation and local verification.

### Step 3: Add and verify

On confirmation Lea revalidates the plan under a project write lock, since files or
formalizations may have changed after preview. Newly conflicting files move to the
skipped list; they are never overwritten.

Eligible files are copied, staged by exact path, and committed together with a subject
such as:

```text
import 12 Lean files from owner/repository@91f8c2a
```

The UI then shows progress while each added or identical-and-reconciled file is checked:

```text
GitHub import: checking 7 of 12 files
6 passed · 1 failed · 3 conflicts skipped
```

The project remains usable while checks run unless the existing run/write coordination
requires the import job to hold the project mutation slot. Starting a new run while
verification is active must not race the importer's DB/file writes.

### Completion summary

The result distinguishes file and formalization outcomes:

```text
12 files added · 2 already present · 3 skipped
2 tagged formalizations proved · 1 failing
9 additional modules are available for future formalizations
```

Conflicts remain visible with source path, intended destination path, and reason. A
failed Lean check does not remove the newly added file; it remains available for review
and repair.

## File mapping

### Destination authority

The destination project's namespace is authoritative. Import never renames the project
or changes its namespace to match GitHub.

For a normal Lea-shared repository, its root corresponds to its source project namespace
directory. Relative file paths are preserved beneath the destination project root:

```text
GitHub: Foo.lean
    -> workspace/proofs/Lea/<Destination>/Foo.lean

GitHub: Analysis/Bar.lean
    -> workspace/proofs/Lea/<Destination>/Analysis/Bar.lean
```

If the files consistently use a different source project namespace such as
`Lea.SourceProject`, the importer rewrites exact project-local namespace/import/reference
occurrences to the destination namespace in the **incoming copies only**. Existing
destination files are never rewritten. Relative paths and declaration short names stay
unchanged.

### Supported source layouts

v1 primarily supports repositories produced by Lea's existing GitHub share flow:

- one coherent project module root;
- Git-tracked `.lean` files;
- one common source project namespace or no project namespace; and
- project-local imports resolvable among the repository's Lean files.

The importer scans only Git-tracked Lean files and excludes `.git`, `.lake`, generated
build output, vendored dependencies, submodules, symlinks, and Git-LFS pointers.

An arbitrary Lake project with several libraries or a custom source root is not converted
wholesale in v1. Compatibility is still per file where possible: safely mappable files
may be added while incompatible files are skipped with `unsupported_module_layout`.

### File disposition rules

Each candidate receives exactly one preview/final disposition:

| Disposition | Meaning | Write? | Reconcile declarations? |
|---|---|---:|---:|
| `add` | Destination path is absent and no semantic conflict exists. | Yes | Yes, after copy |
| `already_present` | Destination path exists with byte-identical post-rewrite content. | No | Yes; re-index/recheck is allowed |
| `path_conflict` | Destination path exists with different content. | No | No |
| `declaration_conflict` | A new path would introduce a declaration already provided by another existing project file. | No | No |
| `unsupported_module_layout` | The source file cannot be mapped/rebased safely. | No | No |
| `excluded` | Generated/vendor/symlink/LFS/non-tracked source. | No | No |

Conflict isolation is file-level. A conflict in `Foo.lean` does not prevent independent
`Bar.lean` from being added.

### Partially filled formalizations

An imported file can complete a tagged formalization when the tag/formalization exists
but has no conflicting current Lean implementation. Examples:

- a tagged theorem has no file yet and the import provides its declaration;
- a planned formalization row exists without a primary file;
- an identical file already exists on disk but was never indexed against the tag.

If the formalization already has a different file or a `sorry` stub at the intended
destination path, v1 does not overwrite it. The incoming file is reported as a conflict.
Replacing or merging an existing stub/proof is a future explicit conflict-resolution
flow, not a side effect of additive import.

## Declaration and formalization reconciliation

### Declaration scan

After mapping/rewrite, scan every `add` and `already_present` file for all supported
public top-level declarations—not only the first declaration in the file. The shared
scanner should return:

- declaration short and fully-qualified name;
- keyword-derived kind;
- destination repo-relative path;
- destination module name; and
- source span/signature for display and `sorry` detection.

Promote/refactor the current declaration logic in `graph.py`/`artifacts.py` so graph,
Blueprint, import, and target-status code share one interpretation.

### Matching rule

Match an incoming declaration to a destination formalization by exact normalized
declaration name within the project. For an Overleaf target, the companion-provided
origin key and source hash identify the current tag, while the declaration name performs
the code match.

A match is accepted only when:

- it resolves to at most one destination formalization;
- the Lean declaration kind is compatible with the tagged kind; and
- no existing primary artifact/file would be displaced.

Ambiguous names, incompatible kinds, and already-implemented formalizations remain
unbound and generate warnings. No “closest” or fuzzy name matching occurs.

### When a declaration matches

For the matched formalization:

1. Link the destination file as its primary formalization file.
2. Upsert the normal project artifact with the declaration, kind, path, module,
   formalization ID, and the current tagged source hash where one exists.
3. Link a single provenance session for this import to the formalization.
4. Record an environment-authored timeline code step containing the exact destination
   file snapshot and GitHub provenance.
5. Run local `lean_check` once for the file and backfill that code step's verdict.

The existing read model then derives status rather than the importer storing a separate
formalization status:

- check pending/no verdict → `unchecked`;
- Lean error → `failing`/invalid in the corresponding UI;
- declaration contains `sorry`/`admit` → incomplete/stub/failing according to the
  existing surface vocabulary;
- check succeeds, declaration is present, and no `sorry` remains → `proved` for
  theorem/lemma targets or `defined` for definitions; and
- a later change to the tagged Overleaf source hash → `stale` under the existing rules.

An imported check is not SafeVerify. The audit indicator remains pending until a local
SafeVerify event exists for the current snapshot.

### When a declaration does not match

Do **not** create a formalization, session conversation, or Blueprint node merely because
the declaration exists in GitHub.

Instead:

- retain the Lean file in the project repository;
- include it automatically in the project's existing Lean-module inventory composed
  into future agent context;
- index lightweight imported-declaration metadata so it can be found later; and
- record the file-level local check result for import reporting.

This makes helper libraries and unrelated proved lemmas available through ordinary Lean
imports without presenting them as work requested by the current Overleaf document.

If a future tag/formalization is created with the same declaration name, the
formalization creation/target-status path checks the imported-declaration index. If the
file still exists, contains the declaration, and its latest local check is current, Lea
can attach it using the same matching rules and immediately derive the appropriate state.

## Blueprint and project context

- `.lea/instructions.md`, `.lea/memory.md`, `.lea/blueprint.md`, and `.lea/files/` are
  never copied from GitHub by this v1 feature.
- Existing project context is never overwritten or rewritten.
- Matching a current formalization may update the status of an existing Blueprint node
  because graph status is derived from its declaration/file/check evidence.
- An unmatched imported declaration does not automatically add a Blueprint node.
- A user or Lea may later reference an imported module while planning a new node in the
  normal way.

## API design

### By project ID

```http
POST /api/projects/{project_id}/github-imports/preview
Content-Type: application/json

{
  "repository_url": "https://github.com/owner/repository"
}
```

The response contains an opaque preview ID, source commit, destination snapshot token,
per-file plan, and declaration/formalization matches.

```http
POST /api/projects/{project_id}/github-imports
Content-Type: application/json

{
  "preview_id": "8ab4…"
}
```

Confirmation consumes the preview, revalidates conflicts, copies and commits eligible
files, records the import, and starts local checking.

```http
GET /api/projects/{project_id}/github-imports/{import_id}
```

Returns progress and final file/formalization outcomes.

### By Overleaf-bound slug

The companion uses equivalent by-slug adapter routes:

```text
POST /api/projects/by-slug/{slug}/github-imports/preview
POST /api/projects/by-slug/{slug}/github-imports
GET  /api/projects/by-slug/{slug}/github-imports/{import_id}
```

The preview request additionally carries the current tagged target descriptors after the
companion has synchronized the Overleaf document. The route ensures the empty project if
the slug does not exist; it does not reject an existing project.

## Persistence

Add job/provenance metadata without storing duplicate project content:

```text
github_imports(
  id, project_id, source_url, source_ref, source_commit_sha,
  status, destination_snapshot, targets_json, created_at, updated_at, error_detail
)

github_import_files(
  import_id, source_path, destination_path, disposition,
  content_sha256, check_status, check_detail
)

github_import_declarations(
  import_id, project_id, destination_path, declaration_name,
  full_name, kind, module_name, formalization_id, created_at, updated_at
)
```

These tables are rebuildable indexes/provenance. Lean bytes remain in the destination
project Git repository; formalization state remains derived from normal artifacts and
timeline check evidence.

Create one regular project session titled, for example,
`GitHub import — owner/repository @ 91f8c2a` to own imported code-step/check provenance.
It has no fabricated user/assistant conversation. Link it only to formalizations actually
matched by the import.

## Import pipeline

1. **Resolve target.** Load or ensure the destination project and acquire a stable
   project snapshot. Preview may run during reads; confirmation is blocked if an active
   run is mutating the project.
2. **Clone source.** Validate exact GitHub HTTPS host, shallow-clone to a temporary
   directory, and scrub the one-shot token from every error/output.
3. **Inventory.** Use `git ls-files` to collect candidate Lean files and apply security
   and resource guards.
4. **Map/rewrite in staging.** Determine source module root/namespace, calculate each
   destination path, and rewrite exact source-project namespace occurrences in temporary
   copies only.
5. **Analyze target.** Compare content hashes, scan existing and incoming declarations,
   identify current formalization/tag matches, and return the preview plan.
6. **Revalidate.** At confirmation, take the project write lock and recompute conflicts
   against the current tree/formalization snapshot. Downgrade newly conflicting files to
   skipped rather than overwriting them.
7. **Copy and commit.** Write only `add` paths, stage only those exact paths, and create
   one import commit. An import with zero additions may still reconcile/recheck
   `already_present` files but creates no empty commit.
8. **Index.** Insert import file/declaration metadata, the provenance session/code steps,
   and artifact/formalization links for accepted matches.
9. **Check.** Run each added or reconciled file once, preferably in project-local import
   dependency order, and backfill check evidence/progress.
10. **Evaluate matches.** After file checks settle, start one idempotent semantic Lea Check
    for every matched Overleaf formalization whose frozen source bundle accompanied the
    import. Several declarations in one Lean file receive separate Lea Checks.
11. **Refresh clients.** Reload formalizations, target status, Blueprint graph, and file
   inventory. The Overleaf pane merges those ledger facts through its existing status
   engine.

Matched imports remain first-class editable formalizations. Target status exposes the
session that owns the current artifact revision; when no companion solver job exists,
the pane reconstructs and persists the minimal session/formalization association from
that ledger evidence. A pane edit therefore follows the normal path: save a user code
step, run Lean Check, cascade when the declaration interface changed, supersede the old
revision-bound Lea Check, and start a new Lea Check with trigger `manual`.

Interrupted verification is recoverable: on startup, resume imports in `checking` state
by checking only files whose import code step lacks a verdict. Index writes and matches
must be idempotent.

## Concurrency and failure behavior

- Preview never mutates the target.
- Confirmation refuses to start while a project run or namespace migration is active.
- A destination snapshot token detects changes since preview; confirmation re-plans
  instead of applying stale assumptions.
- Exact-path writes are serialized with other writers to the same project repository.
- Existing files are never deleted during rollback because every write target was absent
  when revalidated.
- If copying fails before commit, remove only files created by this import.
- If Git commit succeeds but DB indexing is interrupted, recovery scans the import commit
  and completes the indexes; it does not rewrite or discard project history.
- Lean check failures make the import `complete_with_issues`, not failed.
- The source clone and preview cache are always deleted after consumption/expiry.

## Security and limits

- Accept only HTTPS GitHub repository URLs on exact allowed hosts.
- Inject the token only into the temporary clone command and scrub it from all output.
  The source clone is never retained as the destination Git repository, so its remote
  configuration cannot leak into the project.
- Disable hooks, do not initialize submodules, and skip Git-LFS smudge.
- Reject symlinks, special files, path escapes, nested repositories, and LFS pointer
  content presented as Lean source.
- Treat repository contents as untrusted data. Import only `.lean` files; never activate
  source-provided project instructions, scripts, Lake configuration, or `.lea` context.
- Apply configurable defaults such as a 60-second clone timeout, 1,000 candidate Lean
  files, 1 MiB per Lean file, and 100 MiB total candidate content.
- Never infer success from GitHub Actions, badges, commit messages, or source metadata.

## Error and outcome behavior

| Condition | Behavior |
|---|---|
| Invalid/non-GitHub URL | Reject preview with `400 invalid_repository_url`. |
| Private/missing repository or bad token | Reject with a scrubbed repository-unavailable error and Settings hint. |
| No tracked Lean files | Preview succeeds with zero additions and a blocking `no_lean_files` message. |
| Some incompatible/conflicting files | Preview and import continue for all independent eligible files. |
| Every candidate conflicts | Import writes nothing; may reconcile byte-identical existing files; returns a complete no-op summary. |
| Active project run/migration | Confirmation returns `409 project_busy`; preview can be retried later. |
| Destination changes after preview | Re-plan under lock and return the updated skipped/conflict outcomes. |
| Imported file fails Lean check | Keep the file, record failure, and mark matching formalizations through normal derived status. |
| Adapter restarts during checking | Resume unchecked import files idempotently. |

## Explicit non-goals for v1

- Overwrite, merge, or replace an existing Lean file, stub, or proof.
- Import into a separate automatically created project when the user initiated the
  action from an existing project.
- Replace the destination project's Git history or GitHub push remote with the source
  repository.
- Import source `.lea` context, LaTeX files, conversations, usage, or SafeVerify records.
- Turn every imported declaration into a formalization or Blueprint node.
- Convert arbitrary multi-library Lake projects or fetch their custom dependencies.
- Pull/synchronize later changes from the same GitHub repository.
- Automatically choose between ambiguous declaration matches.

## Acceptance criteria

1. A user can import into a newly created blank project.
2. A user can import into an existing Overleaf-bound project containing some completed,
   planned, or missing formalizations.
3. Every safely mappable source Lean file whose destination path and declarations do not
   conflict is added under the current project's canonical namespace path.
4. Existing destination files are byte-for-byte unchanged after import.
5. A conflicting file is skipped without preventing independent files from being added.
6. A byte-identical existing file is treated as already present and can be re-indexed
   against a matching tagged formalization without being rewritten.
7. Exact source-project namespace/import occurrences are rebased only in incoming copies;
   existing project files are never rewritten.
8. An imported declaration matching a current tagged/planned formalization is linked to
   that formalization and its status becomes locally derived from declaration presence,
   `sorry` detection, source hash, and `lean_check` evidence.
9. An imported declaration with no current formalization creates no formalization card or
   Blueprint node, but its module appears in future project context and can be imported by
   later proofs.
10. A later formalization/tag with the same exact declaration name can adopt a still-valid
    previously imported unmatched declaration.
11. Files that fail local checks remain in the project and are reported clearly; matching
    formalizations show the existing failing/incomplete state.
12. Imported passing files do not show the SafeVerify audit mark without a local audit of
    the current snapshot.
13. The destination project's Instructions, Memory, Blueprint text, Overleaf binding,
    Git history, and configured push remote are preserved.
14. Import from the Overleaf extension ensures a missing project but targets an existing
    project when present; it never rejects a project merely for being partially populated.
15. Private-repository import never persists or returns the GitHub token.
16. Repeating the same import is idempotent: unchanged files become already-present,
    existing links are reused, and no duplicate sessions, artifacts, or declaration rows
    are created.
17. Restart recovery completes pending checks/indexing without duplicating writes.

## Test plan

### Adapter

- Public/private clone, URL host validation, token scrubbing, timeout, and temp cleanup.
- Destination namespace mapping and incoming-only namespace rewrite.
- Empty target, partially populated target, and target with an active run.
- Mixed batch: added, already-present, path-conflict, declaration-conflict, unsupported,
  excluded, passing, failing, and `sorry` files.
- Revalidation when a destination file appears after preview.
- Multi-file and multi-declaration scan with matches to zero, one, and several existing
  formalizations.
- Matching-kind guards and ambiguous/duplicate declaration handling.
- File/code-step/check provenance, artifact linking, source-hash adoption, and derived
  status behavior.
- Unmatched declaration indexing and later formalization adoption.
- Exact-path Git commit, zero-write import, interrupted-index recovery, and repeated-import
  idempotency.

### Standalone UI

- Import entry point from an empty and populated project.
- Preview grouping/counts and per-file conflict details.
- Confirmation count reflects only `add` files.
- Progress and completion summary distinguish formalization matches from reusable-only
  modules.
- Filesystem/Formalizations/Blueprint refresh after import.

### Overleaf companion and extension

- Ensure missing project, reuse existing project, and synchronize tagged target descriptors
  before preview.
- Partially formalized document with one matched missing target, one existing conflict, and
  one unmatched helper module.
- Target-status refresh transitions matching pane items correctly while leaving unrelated
  tags unchanged.
- No imported declaration is invented as a new Overleaf item.

### End-to-end scenario

1. Create an Overleaf project with three tagged targets: one already proved, one planned,
   and one missing.
2. Import a repository containing a conflicting version of the proved target, a valid file
   for the planned target, and two unrelated helper modules.
3. Confirm the existing proof is unchanged and its incoming conflict is skipped.
4. Confirm the planned target is linked and shows the locally derived passing status.
5. Confirm both helper modules are present and importable but do not appear as new
   formalizations or Blueprint nodes.
6. Start a future formalization and successfully import one of those helper modules.

## Implementation touch list

- `apps/lea-standalone/adapter/app/github_project_import.py` — preview, mapping,
  conflict planning, copying, reconciliation, and recovery.
- `apps/lea-standalone/adapter/app/routes/projects.py` — by-ID and by-slug import routes.
- `apps/lea-standalone/adapter/app/graph.py` / `artifacts.py` — shared public declaration
  scanner.
- `apps/lea-standalone/adapter/app/store.py` + migration — import/file/declaration indexes,
  idempotent match/link operations, and progress reads.
- `apps/lea-standalone/adapter/app/projects.py` / `gitstore.py` — project mutation lock and
  exact-path import commit helper.
- `apps/lea-standalone/adapter/app/formalizations.py` and creation paths — later adoption of
  unmatched imported declarations.
- `apps/lea-standalone/src/app/components/FilesystemTab.tsx` plus API/types — standalone
  preview/progress/results.
- `apps/overleaf-extension/companion/leaApiClient.mjs` / `server.mjs` — target-aware by-slug
  proxy and tagged-target synchronization.
- `apps/overleaf-extension/extension/content.js` / `content.css` — Overleaf entry point and
  import dialog.

## Recommended delivery slices

1. **Additive file import:** target-aware preview, namespace mapping, file conflict rules,
   exact-path copy/commit, and summary.
2. **Formalization reconciliation:** shared declaration scan, tagged-target matching,
   artifact/file links, local check evidence, and status refresh.
3. **Reusable unmatched modules:** lightweight declaration index, project-context inventory,
   and later adoption hook.
4. **Overleaf UX:** ensure/reuse project, target synchronization, preview, progress, and pane
   refresh.
5. **Hardening:** concurrency, recovery, security/resource guards, idempotency, and the mixed
   partial-project end-to-end test.
