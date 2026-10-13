# Feature: View the Blueprint from the Overleaf Extension

## Summary

The standalone UI has a **Blueprint** view (`apps/lea-standalone/src/app/components/BlueprintTab.tsx`
+ `BlueprintGraph.tsx`): a project-level dependency graph derived from
`.lea/blueprint.md`, with nodes colored by live-derived Lean status
(`planned`/`stated`/`ready`/`proved`/`failed`, plus a SafeVerify audit tier) and
shaped by kind (definition = box, lemma/theorem = ellipse). The Overleaf
extension has **no equivalent** — a user working in Overleaf can see per-theorem
status in the Lean pane, but cannot see the project's proof decomposition as a
whole.

This feature adds a **read-only blueprint viewer to the Overleaf extension's Lean
pane**: the same derived dependency graph, rendered natively inside Overleaf,
scoped to the Lea project that backs the current Overleaf document.

**Scope is deliberately "view," not "author."** The blueprint is co-authored by
the agent and by the human *in the standalone UI* (its Markdown sub-view + the
agent's file tools). In Overleaf we surface it, we don't edit it. Editing stays
in the standalone; this spec explicitly excludes the Markdown authoring
sub-view and any write path.

## Why this is small

The backend already produces everything the viewer needs. The blueprint is
parsed and the graph derived by two adapter services that are **already
project-agnostic**:

- `adapter/app/blueprint.py` — parse + advisory `warnings`.
- `adapter/app/graph.py` — `build_graph(project, proofs_root)` → nodes enriched
  with derived `status`, `verified`, `sessions`, `file`, plus `uses` edges.

They are exposed today only through **by-id** routes
(`GET /api/projects/{project_id}/graph` and `/blueprint`,
`adapter/app/routes/projects.py:226-245`). The Overleaf companion, however, only
ever identifies a project by the **slug** it derives from the Overleaf document
(`slugProjectId`), and reaches the adapter through the existing family of
`/api/projects/by-slug/{slug}/…` routes (export, share, identity, artifacts,
target-status). So the only backend gap is **two by-slug wrapper routes**; the
derivation logic is untouched.

The client-side gap is that the graph layout + SVG rendering currently live in
React (`BlueprintGraph.tsx`) and must be reproduced in the extension's
vanilla-JS content script.

## Architecture

The request path mirrors every other project-level Overleaf panel (share,
export, identity):

```
content.js  ──▶  companion (:31245)          ──▶  adapter (:8001)            ──▶  graph.py
 (renders SVG)     /project/blueprint?…            /api/projects/by-slug/         build_graph(project,
                   /project/graph?…                  {slug}/graph                   proofs_root)
                   overleafProjectId → slug         (never creates a project;
                                                     unknown slug = 404)
```

The extension never talks to the adapter directly — it calls the companion,
which owns the slug derivation and the adapter base URL (exactly as
`/project-export`, `/share/github`, `/project/identity` already do).

## Changes by layer

### 1. Adapter — two by-slug wrapper routes (`routes/projects.py`)

Add, alongside the existing by-slug block (`routes/projects.py:367+`), using the
existing `_require_project_by_slug` helper:

```python
@router.get("/api/projects/by-slug/{slug}/blueprint")
def get_blueprint_by_slug(slug: str) -> dict:
    project = _require_project_by_slug(slug)
    content = project_service.read_doc(project, _proofs_root(), "blueprint.md")
    return {"content": content, "warnings": blueprint_doc.validate(content)}

@router.get("/api/projects/by-slug/{slug}/graph")
def get_graph_by_slug(slug: str) -> dict:
    return graph_service.build_graph(_require_project_by_slug(slug), _proofs_root())
```

- **No new logic** — these delegate to the same `read_doc` / `validate` /
  `build_graph` calls the by-id routes use. Refactoring the by-id handlers to
  share a private helper is optional (the bodies are two lines each).
- **Semantics match the other by-slug routes:** unknown/malformed slug → 404
  ("No Lea project exists for this document yet."), *never* create a project
  (creation stays the formalize/mirror path's job).
- **Empty state:** a project that exists but has no `.lea/blueprint.md` returns
  `{"nodes": [], "edges": []}` (graph) and the seeded/empty template (blueprint)
  — the existing `read_doc` behavior; the client renders an empty state.

Tests: extend `adapter/tests/test_routes_projects.py` with by-slug cases (404 on
unknown slug; a seeded project returns nodes with derived status), reusing the
fixtures already there for the by-id graph/blueprint tests and
`test_graph.py`/`test_blueprint.py`.

### 2. Companion — client methods + two routes

**`companion/leaApiClient.mjs`** — add two fetchers next to the existing by-slug
clients (`mirrorOverleafTex`, `getProjectArtifacts`, share/identity):

```js
export function getProjectGraphBySlug({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/graph`);
}
export function getProjectBlueprintBySlug({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/blueprint`);
}
```

**`companion/server.mjs`** — add two GET routes in the URL dispatch
(near `/project-export` / `/project/identity`, `server.mjs:3123-3160`):

- `GET /project/graph?overleafProjectId=…`
- `GET /project/blueprint?overleafProjectId=…`

Each derives the slug from `overleafProjectId` (same helper the export/identity
routes use), calls the corresponding `…BySlug` client, and returns the JSON.
A 404 from the adapter (no project yet) is passed through as a benign
"no blueprint yet" signal, not an error — the pane shows the empty state.

> **Scope note (graph only):** `/project/graph` is the one route the viewer
> strictly needs — the derived graph is self-contained. `/project/blueprint`
> (raw markdown + warnings) is optional for v1; include it only if we choose to
> show the warnings banner (see §UI, "Warnings"). Ship `/project/graph` first.

### 3. Extension — a Blueprint view in the Lean pane

**Host surface: a top-level toggle in the Lean pane.** The pane today renders a
document-driven tree of theorem items. Add a small segmented control in the pane
header — **Items | Blueprint** — mirroring the standalone's Graph/Markdown
segment. "Items" is the current pane (default); "Blueprint" swaps the body for
the graph. Rationale: the blueprint is *project*-level, so it belongs at the
pane's top level, not inside the per-file tree. This keeps the existing pane
untouched when the toggle is on "Items."

**Rendering — port the layout + SVG to vanilla JS.** The React component's two
responsibilities move into the content script (or a lazily-imported
`extension/blueprintPaneView.mjs`, following the pattern of `leanPaneView.mjs`):

1. **Layout** — the layered-DAG algorithm `computeLayout` from
   `BlueprintGraph.tsx:57-101` (longest-dependency-path leveling, centered rows,
   cycle guard). This is pure and framework-free.
2. **Draw** — build the SVG (`<path>` edges with the arrowhead marker, `<rect>`
   for definitions, `<ellipse>` for lemmas/theorems, two `<text>` labels per
   node) with `document.createElementNS`, plus the legend and a node-detail
   panel. Port `statusLabel` / `statusClass` verbatim.

**Reuse opportunity (recommended):** extract `computeLayout`,
`statusLabel`/`statusClass`, and the status/kind constants into a shared,
framework-free `shared/blueprintLayout.mjs`. The extension imports it directly;
`BlueprintGraph.tsx` imports the same module (it's already TS-consumes-mjs
elsewhere), so the algorithm has **one** source of truth instead of two copies
that can drift. If we don't extract, the content script gets a faithful port and
a comment pointing back to `BlueprintGraph.tsx`.

**Node interaction — adapt to the Overleaf context.** In the standalone, clicking
a node lists the sessions that built it and deep-links into chat. In Overleaf
there is no session list surface, so the node-detail panel shows: key, kind,
status (with the audit-pending nuance), `lean:` decl, and statement prose. The
"worked on by" session links are **dropped in v1** (they point at a UI the
extension doesn't host). Optional future enhancement: a node whose `lean:` decl
matches a pane item could scroll/highlight that item, or offer "Open in Lea UI"
via the existing `uiBridge.js` tab.

**Styling.** Add a `bp-*` block to `extension/content.css` mirroring the
`bp-*` rules in `src/styles/lea-v2.css:585+` (status colors, shapes, legend,
detail panel), themed to the pane's existing palette. Reuse the same class
names so the visual language matches the standalone.

**Warnings (optional, tied to `/project/blueprint`).** If included, render the
advisory-warnings banner above the graph exactly as `BlueprintTab.tsx:54-65`
does. Since the blueprint is read-only here, warnings are purely informational;
acceptable to defer to v2.

**Data flow & refresh.** On switching to the Blueprint view (and on pane
refresh), `GET {companionBase}/project/graph?overleafProjectId=…`, then
layout + draw. Loading / empty / error states match the standalone
(`BlueprintGraph.tsx:138-150`). Status is a **snapshot** (the adapter reuses
stored verdicts and never recompiles); refresh on view-activation and on the
pane's existing manifest-refresh signal is sufficient — no live streaming in v1.

## States

| State | Trigger | Render |
|-------|---------|--------|
| Loading | fetch in flight | "Loading graph…" |
| Empty (no project) | adapter 404 for the slug | "No Lea project for this document yet — formalize a theorem to start one." |
| Empty (project, no nodes) | `nodes: []` | Standalone's empty hint (add a `## node` in the standalone, or let Lea sketch it). |
| Populated | `nodes.length > 0` | Layered graph + legend + detail panel. |
| Error | non-404 failure | Error message (matches `bp-graph-err`). |

## Explicit non-goals (v1)

- **No editing** of the blueprint from Overleaf (no Markdown sub-view, no write
  route). Authoring stays in the standalone.
- **No live status streaming.** Snapshot-on-refresh only.
- **No session deep-links** from nodes (no host surface in the extension).
- **No zoom/pan/export.** Same limitation as the standalone today; the SVG sits
  in a scrollable canvas. (If desired, this is a shared enhancement for both
  front ends, out of scope here.)

## Alternative considered: deep-link to the standalone

Instead of a native port, the extension could open the standalone's Blueprint
tab for this project via the existing Lea-UI tab (`uiBridge.js`). Cheaper (no
SVG port), but it requires the standalone dev server running and takes the user
out of Overleaf into a different app — which defeats "view it *from* the
extension." **Rejected as the primary approach**, but worth keeping as the
node-level "Open in Lea UI" affordance noted above.

## Implementation order

1. **Adapter** by-slug `/graph` (+ optional `/blueprint`) routes + tests. Ship
   and verify with `curl` against a seeded project's slug.
2. **Companion** client methods + `/project/graph` route (+ optional
   `/project/blueprint`).
3. **Shared** `shared/blueprintLayout.mjs` (extract from `BlueprintGraph.tsx`),
   and point the React component at it (no behavior change — verify the
   standalone graph still renders).
4. **Extension** pane toggle + SVG renderer + CSS + empty/error states.
5. Manual verification in Overleaf against a project that has a blueprint with a
   few nodes across statuses.

## Touch list

- `apps/lea-standalone/adapter/app/routes/projects.py` — 2 routes.
- `apps/lea-standalone/adapter/tests/test_routes_projects.py` — by-slug tests.
- `apps/overleaf-extension/companion/leaApiClient.mjs` — 2 clients.
- `apps/overleaf-extension/companion/server.mjs` — 2 routes.
- `apps/overleaf-extension/shared/blueprintLayout.mjs` — **new** (shared layout).
- `apps/lea-standalone/src/app/components/BlueprintGraph.tsx` — import shared layout.
- `apps/overleaf-extension/extension/blueprintPaneView.mjs` — **new** (renderer) or inline in `content.js`.
- `apps/overleaf-extension/extension/content.js` — pane toggle + wiring.
- `apps/overleaf-extension/extension/content.css` — `bp-*` styles.
