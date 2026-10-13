# Feature: User-Named Overleaf Projects

## Summary

Let users choose a human project name for an Overleaf-backed Lea project instead
of exposing the URL-derived project slug, e.g. `P6a4584b313b8ddc4ba20e377`.

The name should be visible and editable from both:

- the Overleaf Lean pane, as part of the project header, and
- the Overleaf Lea settings pane, as a project-scoped identity section.

This is not only a cosmetic label. The Overleaf path currently uses the
URL-derived slug to derive the Lean namespace and proof repo path
(`slug -> Lea.<Project> -> proofs/Lea/<Project>`, D22). If the product lets a
user rename the project in a way that changes its Lean namespace, that rename is
a project-wide migration: every recorded proof file, import, namespace block,
manifest entry, and future run context must agree on the new namespace.

The feature should keep the stable Overleaf URL mapping as the internal identity
anchor, while letting the user control the name they see and, when requested,
the Lean namespace generated from that name.

## Goal

Make Overleaf projects recognizable in Lea without compromising the existing
single-backend architecture or Lean namespace safety.

Today a user with several Overleaf projects can see project names such as:

```text
P6a4584b313b8ddc4ba20e377
P31aa0d75e3db43ae9a2c4f88
P9c77f319486047339d87fb12
```

Those names are technically useful because they are stable and collision-safe,
but they are poor navigation labels. The user should instead be able to see and
choose names like:

```text
Fourier Series Notes
Compactness Chapter
Pset 4: Topology
```

The feature should preserve these invariants:

- The Overleaf URL/project id still maps to the same Lea project.
- There cannot be two active projects with the same Lean namespace.
- Future formalization runs use the project's current namespace.
- Existing proof content is migrated before the namespace changes are considered
  complete.
- Display changes and namespace migrations are explicit enough that users do
  not accidentally rewrite a whole project when they only meant to clean up a
  sidebar label.

Architecture stays the existing one:

```text
Overleaf page -> Chrome extension -> companion (:31245) -> FastAPI adapter (:8001)
```

No separate prover service is introduced.

## Current Behavior

The Overleaf extension identifies the current document with an
`overleafProjectId`. The companion turns that id into a slug with
`slugProjectId(overleafProjectId)`, and the adapter creates or resolves the
matching project row.

The standalone project model already has both a user-facing `title` and a
technical `slug`/`namespace` pair:

```ts
type Project = {
  id: string;
  slug: string;
  title: string;
  namespace: string;
  repo_path: string;
};
```

However, for the Overleaf path, the first title is usually the same as the
URL-derived slug. The namespace is then derived from that slug:

```text
P6a4584b313b8ddc4ba20e377 -> Lea.P6a4584b313b8ddc4ba20e377
```

The result is correct Lean, but a poor user experience.

The current D22 invariant says the slug is immutable and determines both the
namespace and repo path. This feature intentionally splits the stable external
binding from the user-controlled project identity:

- `slug` remains the durable Overleaf binding and lookup key.
- `title` becomes the visible project name.
- `namespace` may be initialized from, and later migrated from, the user-visible
  project name.
- `repo_path` follows the namespace when a namespace migration is performed.

For standalone/non-Overleaf projects, D22 can continue to hold unless and until
the same rename flow is deliberately extended there.

## Proposed Behavior

### Project identity

An Overleaf-backed Lea project should have a normalized identity shape that both
the Lean pane and settings pane can consume:

```ts
type OverleafProjectIdentity = {
  projectId: string;              // stable adapter project id
  overleafProjectId: string;      // stable external binding
  slug: string;                   // URL-derived, not shown as the main name

  projectName: string;            // visible human name, adapter Project.title
  namespace: string;              // current Lean namespace, e.g. Lea.FourierSeriesNotes
  namespaceEditable: boolean;

  repoPath: string;
  hasRecordedProofs: boolean;
  renameInProgress?: boolean;
};
```

The ugly slug should still be available as secondary/debug metadata, but it
should not be the primary label in normal project navigation.

### First-use naming

When the extension first creates or discovers an Overleaf project without a
user-controlled name, it should propose a name.

Recommended default sources, in order:

1. The Overleaf project/document title, if the extension can read it reliably.
2. A source-derived title such as the root document's `\title{...}`, if already
   available from the project scan.
3. A friendly fallback such as `Overleaf Project 1`.

The proposed name should derive a Lean namespace preview:

```text
Project name:   Fourier Series Notes
Lean namespace: Lea.FourierSeriesNotes
```

If the namespace preview collides with an existing project namespace, the UI
should suggest safe alternatives before creation:

```text
Lea.FourierSeriesNotes2
Lea.FourierSeriesNotesTopology
Lea.FourierSeriesNotes2026
```

The user should be able to accept the suggestion quickly. A formalization should
not be blocked forever if the user dismisses the prompt; the system can create a
friendly fallback name and let the user rename later from either surface.

### Lean pane surface

The Lean pane should show project identity in its header, above the file/item
tree.

Example:

```text
Fourier Series Notes        [edit]
Namespace: Lea.FourierSeriesNotes
```

The header edit affordance should open a compact project identity dialog. The
dialog should include:

- `Project name` text field.
- Read-only or editable `Lean namespace` preview.
- Collision/validity feedback before save.
- A clear indication when saving will perform a namespace migration.

If the user changes only capitalization or punctuation in a way that does not
change the normalized namespace, the save can be a metadata-only rename.

If the normalized namespace changes, the dialog should present the operation as
a migration, not as a cheap label edit:

```text
This will rename the Lean namespace:
Lea.P6a4584b313b8ddc4ba20e377 -> Lea.FourierSeriesNotes

Lea will update recorded proof files and imports before future runs use the new
namespace.
```

The Lean pane should refresh its header after a successful settings-pane rename,
and the settings pane should refresh after a successful Lean-pane rename. The two
surfaces are two entries to the same project identity, not two independent
settings stores.

### Settings pane surface

The Overleaf Lea settings pane should include a project-scoped section when it
is opened from an Overleaf project:

```text
Project
Name: Fourier Series Notes
Lean namespace: Lea.FourierSeriesNotes
Overleaf binding: P6a4584b313b8ddc4ba20e377
```

The name should be editable from this section using the same validation and save
path as the Lean pane. The settings pane is the better place to expose advanced
details:

- the URL-derived slug / Overleaf binding,
- whether this project has recorded proofs,
- the current namespace,
- whether a namespace migration is required,
- and any migration failure/retry state.

The settings pane should not make global model/provider settings look
project-scoped. The project name belongs to the current Overleaf project; model
and provider configuration remain shared settings.

When the settings pane is opened outside a detectable Overleaf project, the
project section should be hidden or disabled with neutral copy. It should not
create a project merely because settings were opened.

## Display Rename vs Namespace Rename

This feature should distinguish two rename effects, even if the UI presents them
through one "Project name" interaction.

### Metadata-only rename

A metadata-only rename updates the visible `projectName`/`title` and leaves
`namespace` and `repoPath` unchanged.

This is safe when:

- the user changes display punctuation, spacing, or capitalization and chooses
  not to change the namespace,
- the generated namespace preview is unchanged, or
- the user explicitly selects a "display name only" option from the advanced
  rename UI.

Metadata-only rename should not rewrite Lean files, change import paths, re-run
checks, or invalidate proof status.

### Namespace migration

A namespace migration updates the Lean namespace and the repo path derived from
it. This is required when the user wants the project name to be reflected in
future Lean names, e.g. moving from:

```lean
namespace Lea.P6a4584b313b8ddc4ba20e377
```

to:

```lean
namespace Lea.FourierSeriesNotes
```

The default first-use naming flow should create the friendly namespace before
proofs exist. Later namespace changes are heavier and must be explicit.

## Namespace Rules

Namespaces are globally unique across local Lea projects.

The namespace segment derived from the project name should:

- be a valid Lean module-name segment,
- start with a letter,
- contain only alphanumeric characters after normalization,
- avoid Lean keywords and reserved names,
- use UpperCamelCase by default,
- be reasonably short,
- and live under the `Lea.` root.

Examples:

| Project name | Namespace |
|---|---|
| `Fourier Series Notes` | `Lea.FourierSeriesNotes` |
| `Pset 4: Topology` | `Lea.Pset4Topology` |
| `2026 Compactness` | `Lea.P2026Compactness` |

The product should validate uniqueness on the adapter side. Client-side
validation is useful for immediate feedback, but it is advisory only because
another project can be created or renamed concurrently.

Display names do not need to be globally unique. If two projects have the same
display name, the UI can disambiguate with the Overleaf binding, namespace, or
last-updated time. Namespace collisions, however, must be rejected before any
files are moved or rewritten.

## Namespace Migration Behavior

A namespace migration should be treated as a project-level operation with a
preflight phase and a commit phase.

### Preflight

Before rewriting anything, the adapter/companion should verify:

- the current Overleaf project resolves to exactly one adapter project,
- the requested namespace is syntactically valid,
- no other project already owns the requested namespace,
- no formalization, chat, repair, cascade check, or manual edit save is actively
  running in the project,
- the project repo exists and is clean enough for Lea-managed changes,
- every recorded Lean artifact that will be rewritten is inside the project repo,
- and the migration can determine the old and new module prefixes.

If preflight fails, no files should be changed.

### Rewrite scope

The migration must update every project-owned place where the old namespace is a
semantic Lean reference.

At minimum:

- namespace declarations,
- matching `end` declarations when they name the namespace,
- imports of project-owned modules,
- fully qualified references to project-owned declarations,
- generated Lean artifact paths or module names in pane manifests,
- companion job/session metadata that caches `projectNamespace`,
- adapter project metadata (`title`, `namespace`, `repo_path`),
- future prompt context that tells the prover which namespace to use,
- and any project docs seeded with the old namespace when those docs are
  intended to describe current project identity.

The migration should not rewrite arbitrary natural-language prose unless that
prose is a generated Lea identity block. For example, a theorem statement that
mentions the string `Lea.OldName` as mathematical text should not be changed by a
blind search-and-replace.

### Git and DB effects

Because proof bytes live in git, a successful namespace migration should create
a git commit in the project repo.

Recommended commit subject:

```text
rename project namespace: Lea.OldName -> Lea.NewName
```

The adapter DB should then update the project row:

```ts
{
  title: "Fourier Series Notes",
  namespace: "Lea.FourierSeriesNotes",
  repo_path: "proofs/Lea/FourierSeriesNotes"
}
```

Existing sessions should remain attached to the same `project_id`. Their past
messages and historical code steps do not need to be rewritten as historical
records, but any current-session detail endpoint should report the new project
namespace for future runs.

### Verification

After rewriting, the system should verify the migrated project rather than
assuming text replacement was enough.

Recommended v1 behavior:

- Run the same fast Lean check path used by manual edits on every recorded proof
  file that was changed by the migration.
- Update the latest visible status for affected items from real check results.
- If a check cannot be run immediately, mark affected items as needing recheck
  rather than showing stale success.

If migration verification fails, the user should see:

- the project name that was requested,
- the old and new namespaces,
- which file or declaration failed,
- and whether future runs are still using the old namespace or have switched to
  the new one.

The system should prefer all-or-nothing semantics where practical. If full
rollback is not practical, it should leave a clear migration record and a retry
path rather than partially hiding the failure.

## API Surface

The companion should expose project-identity endpoints to the extension. Exact
names are flexible, but the shape should be project-scoped and shared by both UI
surfaces.

```text
GET /project/identity?overleafProjectId=...
  -> resolve the Overleaf project and return OverleafProjectIdentity

PUT /project/identity
  -> update projectName and, if requested, migrate namespace
```

Useful request shape:

```ts
type ProjectIdentityUpdate = {
  overleafProjectId: string;
  projectName: string;
  namespace?: string;             // optional explicit override
  mode: "display-only" | "rename-namespace";
};
```

Useful response shape:

```ts
type ProjectIdentityUpdateResult = {
  identity: OverleafProjectIdentity;
  migration?: {
    oldNamespace: string;
    newNamespace: string;
    commitSha?: string;
    checkedFiles: number;
    failedFiles: Array<{
      path: string;
      message: string;
    }>;
  };
};
```

The adapter should own authoritative namespace validation and the project row
update. The companion can orchestrate Overleaf-specific lookup and extension
convenience, but it should not maintain a second source of truth for names.

## Edge Cases

- **A user renames before any proofs exist.** This should be cheap: update the
  project title/namespace/repo path, with little or no Lean content to rewrite.
- **A user renames after many proofs exist.** This is a migration. It should be
  blocked while project runs are active and verified afterward.
- **Two browser tabs rename the same project.** The second save should re-fetch
  identity and fail with a clear conflict if the expected old namespace no
  longer matches.
- **The Overleaf document title changes.** Do not automatically rename a Lea
  project after the user has chosen a name. At most, offer the new Overleaf title
  as a suggestion.
- **The project name normalizes to an empty namespace segment.** Reject it or
  suggest a fallback such as `Lea.OverleafProject`.
- **The normalized namespace collides.** Reject before migration and suggest
  alternatives.
- **The user wants duplicate display names.** Allow them when namespaces differ;
  disambiguate in secondary metadata.
- **Historical runs mention the old namespace.** Keep historical messages as
  history. Future runs and current project views should use the new namespace.
- **Manual edits exist in the project.** Treat them like any other proof bytes:
  rewrite and verify the current file content, preserving git history.

## Non-Goals

Version 1 should not add:

- automatic renaming whenever the Overleaf title changes,
- cross-device/cloud synchronization of preferred names beyond the local Lea
  adapter database,
- arbitrary namespace roots outside `Lea.`,
- bulk rename for many projects at once,
- a standalone-project rename UX unless deliberately pulled into this feature,
- semantic Lean refactoring beyond the project's namespace/module prefix,
- or a full AST-based Lean code transformation engine.

The migration should be careful, but it is scoped to the namespace/module prefix
Lea itself generated for the project.

## Acceptance Criteria

1. A new Overleaf-backed project can be given a friendly project name instead of
   showing the URL-derived slug as the primary name.
2. The project name is visible in the Overleaf Lean pane header.
3. The project name is editable from the Overleaf Lean pane.
4. The project name is visible in the Overleaf Lea settings pane when an
   Overleaf project is detectable.
5. The project name is editable from the settings pane through the same
   validation/save path used by the Lean pane.
6. Both surfaces show the current Lean namespace, at least as secondary
   metadata.
7. The URL-derived slug remains the stable Overleaf binding and is not used as
   the primary user-facing name.
8. Namespace collisions are rejected before any migration rewrites files.
9. A metadata-only display rename does not rewrite Lean files or change proof
   status.
10. A namespace-changing rename rewrites project-owned namespace declarations,
    imports, fully qualified project references, manifests, cached namespace
    metadata, and future prompt context.
11. A successful namespace migration creates a git commit in the project repo.
12. A successful namespace migration updates the adapter project row so future
    runs use the new namespace.
13. Existing sessions remain attached to the same project after rename.
14. A namespace migration is blocked or clearly deferred while project-affecting
    runs/edits/checks are active.
15. A namespace migration verifies changed Lean files and surfaces failures
    without pretending the old success status still applies.
16. Existing formalization, Lean pane, chat mirror, manual edit, self-repair,
    sharing, and settings behavior remain intact for projects that are never
    renamed.
