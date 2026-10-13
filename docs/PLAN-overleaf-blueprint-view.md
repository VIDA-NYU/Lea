# Plan: View the Blueprint from the Overleaf Extension

Implementation plan for `docs/FEATURE-overleaf-blueprint-view.md`. Read that spec
first for the *why* and the scope boundaries (read-only; view, not author). This
doc is the *how*: ordered, PR-sized steps with exact anchors, code, and
verification.

## Guiding facts (verified against the tree)

- The graph derivation is already project-agnostic:
  `graph.build_graph(project, proofs_root)` and
  `blueprint.validate(content)` (`adapter/app/graph.py`, `adapter/app/blueprint.py`).
  It is exposed **by-id only** at `routes/projects.py:226` (`/blueprint`) and
  `:240` (`/graph`).
- The Overleaf companion knows a project only by **slug**
  (`slugProjectId(overleafProjectId)`), and reaches the adapter through the
  `/api/projects/by-slug/{slug}/…` family. `_require_project_by_slug`
  (`routes/projects.py:374`) is the 404-on-unknown-slug helper; it never creates
  a project.
- Companion request path for a project-level GET panel (the template to copy):
  route dispatch `routes/projects.py`-style block at `server.mjs:3160`
  (`/share/github`) → `handleShareStatus` (`server.mjs:694`) → `resolveShareTarget`
  (`server.mjs:513`) → a by-slug client in `leaApiClient.mjs`.
- `leaApiClient.fetchJson` (`leaApiClient.mjs:76`) **never throws** — it returns
  `{ ok, status, body, error }`. Handlers branch on `result.ok` / `result.status`
  (see `handleShareStatus`, which treats a 404 as a benign "no project yet").
- The client React renderer is `BlueprintGraph.tsx`: `computeLayout` (`:57-101`),
  `statusLabel`/`statusClass` (`:30-40`), constants (`:13-25`), SVG draw
  (`:152-232`). All framework-agnostic except the `useState`/`useEffect`/JSX shell.
- Extension → companion calls pass `overleafProjectId` as a query/body param
  (`content.js`, e.g. `/share/github?overleafProjectId=…` at `content.js:615`).

## Steps

The four PRs are independently landable in order. PR1 is verifiable with `curl`
alone; PR2 with `curl` against the companion; PR3 is a no-behavior-change
refactor gated by the standalone still rendering; PR4 is the user-visible UI.

---

### PR1 — Adapter: by-slug `/graph` and `/blueprint` routes

**File:** `apps/lea-standalone/adapter/app/routes/projects.py`

Add to the by-slug block (after `get_share_status_by_slug`, ~`:451`). Refactor
the two by-id handlers to share a helper so there is one implementation:

```python
# ── Blueprint / graph by slug: the Overleaf companion's read-only view (F-blueprint)
# Same derivation as the by-id routes; slug-resolved for the companion. Never
# creates a project — unknown slug is a 404, like every other by-slug route.

def _blueprint_payload(project: dict) -> dict:
    content = project_service.read_doc(project, _proofs_root(), "blueprint.md")
    return {"content": content, "warnings": blueprint_doc.validate(content)}


@router.get("/api/projects/by-slug/{slug}/blueprint")
def get_blueprint_by_slug(slug: str) -> dict:
    return _blueprint_payload(_require_project_by_slug(slug))


@router.get("/api/projects/by-slug/{slug}/graph")
def get_graph_by_slug(slug: str) -> dict:
    return graph_service.build_graph(_require_project_by_slug(slug), _proofs_root())
```

Then rewrite the by-id `get_blueprint` (`:226`) body to `return _blueprint_payload(_require_project(project_id))`
(note: it currently calls the bare `store.get_project`-less `_require_project`; keep that helper).

**Tests:** `apps/lea-standalone/adapter/tests/test_routes_projects.py`

- `test_graph_by_slug_unknown_slug_404` — GET a made-up slug → 404, detail
  "No Lea project exists for this document yet."
- `test_graph_by_slug_returns_derived_nodes` — seed a project (reuse the fixture
  the by-id graph test uses), write a `.lea/blueprint.md` with 2 nodes + an edge,
  assert `nodes`/`edges` shape and derived `status` matches the by-id route for
  the same project (call both, assert equal — guards the shared helper).
- `test_blueprint_by_slug_returns_warnings` — a blueprint with a dangling `uses`
  → `warnings` non-empty; unknown slug → 404.

**Verify:**
```bash
cd apps/lea-standalone/adapter && ./.venv/bin/python -m pytest tests/test_routes_projects.py -q
# then against a running adapter with a real seeded project slug:
curl -s localhost:8001/api/projects/by-slug/<slug>/graph | jq '.nodes | length'
curl -s -o /dev/null -w '%{http_code}\n' localhost:8001/api/projects/by-slug/nonexistent/graph   # → 404
```

---

### PR2 — Companion: client methods + `/project/graph` route

**File:** `apps/overleaf-extension/companion/leaApiClient.mjs`

Add next to `fetchProjectShareStatus` (`:198`). These return the `fetchJson`
result object (`{ok, status, body}`) — do **not** unwrap:

```js
export function getProjectGraphBySlug({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/graph`, {
    method: "GET",
    headers: buildHeaders(null),
  });
}

export function getProjectBlueprintBySlug({ fetchImpl, baseUrl, slug }) {
  return fetchJson(fetchImpl, `${baseUrl}/api/projects/by-slug/${encodeURIComponent(slug)}/blueprint`, {
    method: "GET",
    headers: buildHeaders(null),
  });
}
```

**File:** `apps/overleaf-extension/companion/server.mjs`

Handler (place near `handleShareStatus`, ~`:694`), reusing `resolveShareTarget`:

```js
export async function handleProjectGraph(payload, state) {
  const target = resolveShareTarget(payload, state);
  if (target.error) return target.error;
  const result = await getProjectGraphBySlug(target);
  if (!result.ok) {
    if (result.status === 404) {
      // No Lea project for this document yet — an empty graph, not an error.
      return { statusCode: 200, body: { ok: true, exists: false, nodes: [], edges: [] } };
    }
    return errorResponse(result.status || 502, "graph_fetch_failed", adapterDetail(result, "Could not reach the Lea adapter."));
  }
  return { statusCode: 200, body: { ok: true, exists: true, nodes: result.body?.nodes || [], edges: result.body?.edges || [] } };
}
```

Import `getProjectGraphBySlug` at the top with the other `leaApiClient` imports.
Route dispatch (mirror `/share/github` at `:3160`):

```js
if (request.method === "GET" && url.pathname === "/project/graph") {
  const result = await handleProjectGraph({
    overleafProjectId: url.searchParams.get("overleafProjectId") || ""
  }, state);
  sendJson(response, result.statusCode, result.body);
  return;
}
```

> **v1 ships graph-only.** Add `handleProjectBlueprint` + `/project/blueprint`
> only if the warnings banner is in scope (see spec §UI). The graph route is the
> one the viewer needs.

**Tests:** `apps/overleaf-extension/companion/` has a Node `--test` suite. Add a
handler test with a stub `fetchImpl` (the `state.fetchImpl` seam) returning a
canned graph / a 404, asserting `handleProjectGraph` maps them to
`exists:true`/`exists:false`. Follow the existing share-handler tests as the model.

**Verify:**
```bash
npm test -w apps/overleaf-extension
# with adapter + companion running:
curl -s "localhost:31245/project/graph?overleafProjectId=<overleafId>" | jq '{exists, n: (.nodes|length)}'
```

---

### PR3 — Shared layout module (refactor, no behavior change)

Extract the framework-free graph logic so the extension and the standalone share
one source of truth instead of two copies.

**New file:** `apps/overleaf-extension/shared/blueprintLayout.mjs`

Move verbatim from `BlueprintGraph.tsx`:
- constants `NODE_W`/`NODE_H`/`H_GAP`/`V_GAP`/`PAD` (`:13-17`), `STATUS_LABEL` (`:19-25`)
- `statusLabel(node)` (`:30-33`), `statusClass(node)` (`:37-40`)
- `computeLayout(graph)` (`:57-101`) and `truncate` (`:103-105`)

Export them as plain JS (drop TS types; the `.tsx` side keeps its `import type`s
for `GraphNode`/`ProjectGraph`). Keep the `Placed`/`Layout` shapes documented in
a comment.

**Edit:** `apps/lea-standalone/src/app/components/BlueprintGraph.tsx` — delete the
moved bodies, import from the shared module:
```ts
import { computeLayout, statusLabel, statusClass, STATUS_LABEL, NODE_W, NODE_H, truncate } from '../../../../overleaf-extension/shared/blueprintLayout.mjs';
```
(Confirm the relative path resolves under Vite; if the cross-app import is
awkward, the fallback is to keep the port in the extension and leave `.tsx`
unchanged — note this decision in the PR. The extraction is *recommended*, not
required, per the spec.)

**Verify (this is a no-op refactor — prove the standalone is unchanged):**
```bash
npm run typecheck -w apps/lea-standalone
npm run test:frontend -w apps/lea-standalone
```
Then run `/verify` or drive the standalone Blueprint tab in the browser and
confirm the graph renders identically (shapes, colors, layout, legend, node
click). Use the `run` skill to launch the UI.

---

### PR4 — Extension: Blueprint view in the Lean pane

**New file:** `apps/overleaf-extension/extension/blueprintPaneView.mjs`
(lazily imported, like `leanPaneView.mjs`). A pure renderer:

```js
import { computeLayout, statusLabel, statusClass, STATUS_LABEL, NODE_W, NODE_H, truncate }
  from "../shared/blueprintLayout.mjs";

// buildBlueprintSvg(graph) -> SVGElement   (edges + nodes, arrowhead marker)
// buildBlueprintLegend()   -> HTMLElement
// buildNodeDetail(node)    -> HTMLElement   (key/kind/status/lean/statement; NO session links)
// renderBlueprintView(container, graph, { onSelectNode }) -> void
```

Port the SVG draw from `BlueprintGraph.tsx:152-232` using
`document.createElementNS("http://www.w3.org/2000/svg", …)`: edges as `<path>`
with the bezier `d` and the `bp-arrow` `<marker>`; each node a `<rect rx=7>`
(definition) or `<ellipse>` (lemma/theorem) plus two `<text>` labels; `click`
toggles selection. Node detail drops the "Worked on by" session list
(`:260-275`) — no host surface in Overleaf.

**Edit:** `apps/overleaf-extension/extension/content.js`

1. **Pane header toggle.** Add an **Items | Blueprint** segmented control to the
   Lean pane header (the manifest-driven pane; the toggle sits above the tree).
   Default "Items" (current behavior untouched). Store the active view in the
   pane's existing view state.
2. **Fetch on activate.** When "Blueprint" is selected (and on the pane's
   existing manifest-refresh signal), GET
   `${companionBase}/project/graph?overleafProjectId=${encodeURIComponent(projectId)}`
   — reuse the `projectId` resolution already used by `/share/github`
   (`content.js:615`) and `resolveOverleafProjectId` (`content.js:1001`).
3. **Render states** (match `BlueprintGraph.tsx:138-150`):
   - loading → "Loading graph…"
   - `exists:false` → "No Lea project for this document yet — formalize a theorem to start one."
   - `nodes.length === 0` → the empty-blueprint hint
   - populated → `renderBlueprintView(...)`
   - fetch error → error line
4. Lazy-import `blueprintPaneView.mjs` on first activation (pattern of the other
   lazily-imported `.mjs` modules).

**Edit:** `apps/overleaf-extension/extension/content.css`

Add a `bp-*` block mirroring `src/styles/lea-v2.css:585-599` and the node/edge/
legend/detail rules there — **reuse the same class names** so the visual language
matches the standalone; retheme colors to the pane palette variables. Put the SVG
in a scrollable canvas (`overflow:auto`) — no zoom/pan in v1.

**Verify:** load the unpacked extension in Overleaf against a project that has a
blueprint with a few nodes across statuses (`planned`/`stated`/`ready`/`proved`,
one SafeVerify-audited). Confirm: toggle switches views, shapes/colors/edges
match the standalone, node click shows detail, empty/error states render, and the
"Items" view is byte-for-byte unchanged when the toggle is off Blueprint.

---

## Sequencing & risk

- **PR1 → PR2 → PR4** is the critical path; **PR3** can land before PR4 or be
  folded into it. PR2 depends on PR1 (route must exist); PR4 depends on PR2
  (companion route) and PR3 (shared module, if extracted).
- **Lowest-risk first:** PR1 and PR2 are additive (new routes, no existing path
  touched) and fully `curl`-verifiable. PR3 is the only refactor of shipped code
  — gated by typecheck + the standalone rendering identically. PR4 is additive to
  the pane (new toggle; "Items" untouched).
- **Main porting risk** is in PR4: SVG-by-`createElementNS` is verbose and easy
  to get subtly wrong (namespace, marker refs, text baseline). Mitigation: PR3
  makes the *layout math* shared and already-tested; only the draw calls are new,
  and they're a direct transliteration of `BlueprintGraph.tsx:152-232`.
- **No new state, no writes, no live streaming** — the viewer is a pure function
  of a snapshot GET, so there's nothing to keep in sync and no migration.

## Definition of done

- `curl …/by-slug/<slug>/graph` returns derived nodes; unknown slug → 404 (PR1).
- `curl …:31245/project/graph?overleafProjectId=…` returns the graph, 404 → empty (PR2).
- Standalone Blueprint tab renders identically after the extraction (PR3).
- In Overleaf, the Lean pane's Blueprint toggle shows the derived graph with
  matching shapes/colors/edges, a working node-detail panel, and correct
  loading/empty/error states; the Items view is unchanged (PR4).
- Adapter pytest + companion `node --test` + standalone typecheck/frontend tests
  all green.

## Out of scope (from the spec — restated so PRs don't creep)

Editing/authoring from Overleaf · Markdown sub-view · live status streaming ·
node→session deep-links · zoom/pan/export. The deep-link-to-standalone fallback
stays available only as a possible per-node "Open in Lea UI" affordance via the
existing `uiBridge.js` tab, not a v1 deliverable.
