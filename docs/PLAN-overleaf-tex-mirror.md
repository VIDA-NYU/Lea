# Plan — Mirror Overleaf `.tex` into the project's files

**Goal.** When a formalization is requested from the Overleaf extension, copy the
Overleaf project's `.tex` files into the matching adapter project **as if they had
been uploaded via the project's Files tab** (`.lea/files/`), and keep that copy up
to date as the document changes.

**Decisions locked with the user.**
- **Scope:** mirror **all `.tex` files** associated with the project — the TeX
  sources only, *not* the whole project (no images, `.bib`, or other assets).
- **Cadence:** keep the mirror current with a **background sync** as the user edits, so
  the formalize click does only a cheap up-to-date check that is **usually a no-op** —
  the sync cost is moved *off* the formalize critical path rather than paid on every run
  (see Section 4).
- **Legacy path:** **replace** the existing separate LaTeX-context mechanism —
  delete-and-replace, with **no backward compatibility** (no data migration, no
  fallback to the old setting or on-disk format).

---

## 1. How it works today (grounding)

The pieces already exist; they're just wired to the wrong place.

**Overleaf → adapter project is already a 1:1 mapping.** On formalize the companion
calls the adapter's `POST /api/runs` with `project_slug = slugProjectId(overleafProjectId)`
(`leaApiClient.startApiRun`). The adapter's `store.get_or_create_project(slug)` then
resolves (or creates) **one project per Overleaf document**, whose **slug is the
Overleaf project ID** (e.g. `6a1dbf78551016819f930107`). That is exactly the project
the user uploads files to in the Files tab — confirming the link.

**The Files tab already feeds Lea.** `uploads.save_upload` writes bytes into the
project repo's `.lea/files/`, git-commits them (`gitstore`), and indexes a
`project_files` row. `projects.compose_context_message` lists `.lea/files/` to the
prover on every run and tells it to read them. `.tex` is a Tier-1 (native-text)
type — readable as-is, no extraction. **So any file placed in `.lea/files/` is
automatically surfaced to Lea.** This is the channel we want.

**The legacy `.tex` copy is off to the side.** The companion's "LaTeX context" mode
(`leaLatexContextMode`: `off` | `active_file`) writes the *active editor file* to
`…/workspace/context/overleaf/<slug>/tex/active.tex` + a `manifest.json`
(`writeActiveLatexContext`), and `buildLeaPrompt` injects a pointer to that manifest.
This bypasses the project repo, the DB index, and git. It's also off by default —
matching the user's "this version doesn't do that yet."

**The extension only sees the active file.** `pageBridge.js` hooks Overleaf's
CodeMirror via `UNSTABLE_editor:extensions` and can read `view.state.doc.toString()`
— the **open document only**. It has no access to the project's other files. This is
the main new capability the "all `.tex` files" decision requires.

### Two constraints that shape the design
1. **The upload pipeline never overwrites.** `uploads.sanitize_filename` appends
   `-2`, `-3`, … on collision. A *maintained mirror* needs upsert-by-path semantics,
   not new-file-on-every-sync.
2. **The extension can't read non-open files yet.** Getting "all `.tex`" means adding
   a way to enumerate + fetch the whole project's `.tex` content from the page.

---

## 2. Target design

```
Overleaf page (extension)                Companion (:31245)            Adapter (:8001)
─────────────────────────                ──────────────────            ───────────────
BACKGROUND (debounced, as you edit):
collect .tex (paths+content) + hash ─►  POST /mirror-tex          ──►  resolve project by slug
  skip if hash unchanged               (forwards file set)             (get_or_create_project)
                                                                       reconcile .lea/files/overleaf/**
                                                                         · upsert changed / delete removed
                                                                         · update project_files rows (sync)
                                                                         · git commit (DEFERRED, off path)

ON FORMALIZE (fast path):
send current hash only        ──────►   POST /formalize           ──►  POST /api/runs (project_slug)
  match → no-op (≈0 latency)                                           compose_context_message lists
  mismatch → top-up sync first                                         the mirrored .tex to Lea
```

The mirror is kept current **in the background as the document changes**, so by the time
a run starts the files are already in place. Formalize only verifies freshness (a
content-hash check) and tops up on a mismatch; the composed context the prover sees
always includes the current `.tex`. The adapter writes + indexes **synchronously** but
**defers the git commit** (the slow step) off the run-start path — the inventory reads
files from disk, not git.

### Identity & storage model
- Mirrored files live under a **dedicated subtree**: `.lea/files/overleaf/<relative
  path>` (e.g. `.lea/files/overleaf/sections/intro.tex`). This preserves Overleaf's
  structure, keeps mirrored files visually grouped, and makes them **incapable of
  clobbering user uploads** (which sit at `.lea/files/<name>`).
- `project_files.kind = "overleaf"` (new value alongside `upload`/`blueprint`/`extract`).
  `kind` is free text with a default, so **no schema migration is required** — only a
  comment update in `db.py` and the new value in code.
- `filename` stores the Overleaf-relative path; `stored_path` stores the repo-relative
  path. Reconciliation keys on `(project_id, kind="overleaf", stored_path)`.
- Track a **content hash** of the mirrored set so an unchanged sync is a no-op (no
  write, no commit, no DB churn) — the basis for the formalize fast path (Section 4).
- Write + index synchronously, but **defer the `git commit`** to a background task; the
  composed-context inventory reads files from disk, so the run never waits on git.

---

## 3. Work breakdown

### A. Adapter — the mirror sink (most important, lowest risk)

**`store.py`**
- `get_project_file_by_path(project_id, stored_path)` — lookup for upsert.
- `update_project_file(file_id, …)` / `upsert_project_file(...)` — overwrite an
  existing row's metadata.
- `list_project_files(project_id, kind="overleaf")` — to compute the reconcile diff
  (which mirrored files to delete).
- Allow `kind="overleaf"`.

**`uploads.py`** — add a reconcile entry point:
- `sync_overleaf_tex(project, proofs_root, files: list[{path, content}]) -> summary`
  - ensure repo exists: `GitStore(proofs_root).init_repo(repo)` (idempotent, same as
    the bridge) and `mkdir` the `overleaf/` subtree.
  - validate each path (reuse the path-escape guard; restrict to `.tex`).
  - for each incoming file: write under `.lea/files/overleaf/<path>` **only if changed**;
    upsert the `project_files` row (`kind="overleaf"`).
  - delete mirrored rows + files whose path is **absent** from the incoming set
    (handles Overleaf renames/deletes).
  - short-circuit to a no-op when the incoming set hashes equal to the last mirror;
  - **defer** the single `git commit_all` to a background task (FastAPI `BackgroundTasks`
    / thread) so the endpoint returns as soon as files are on disk + indexed; return
    `{written, updated, deleted, unchanged}`.

**`projects.py` — `compose_context_message`**
- Include the mirrored subtree in the inventory. Either recurse `.lea/files/` or add a
  dedicated **"## Overleaf source (LaTeX)"** section listing `.lea/files/overleaf/**`,
  so the prover is told the source TeX is available and where. (The current code only
  `iterdir()`s the top level, so this is a required change for the files to surface.)

**`routes/projects.py`** — new endpoint:
- `POST /api/projects/by-slug/{slug}/mirror` (resolve via `get_or_create_project`,
  matching how `/api/runs` resolves a project by slug — the companion then only needs
  the Overleaf project ID). Body: `{ source: "overleaf", files: [{path, content}] }`.
  Delegates to `uploads.sync_overleaf_tex`; returns the summary.
- *(Alternative: a top-level `POST /api/overleaf/mirror` with `{overleaf_project_id,
  files}`. Pick one; the by-slug route keeps it inside the existing projects router.)*

### B. Companion — forward the file set, drop the side directory

**`leaApiClient.mjs`**
- `mirrorProjectTexFiles({ fetchImpl, baseUrl, slug, files })` → `POST …/mirror`.

**`server.mjs`**
- New handler `handleMirrorTex` (replaces `handleLatexContext`): receives a `.tex` set
  (with its content hash) from the extension's **background sync** and forwards it to the
  adapter mirror endpoint, resolving the slug with `slugProjectId(overleafProjectId)`.
- In `handleFormalize`: do **not** unconditionally re-sync. The formalize request carries
  the current `.tex` hash; if it matches the last mirrored hash the companion **goes
  straight to the run** (the common case → ~0 added latency). Only on a mismatch does it
  top-up via the mirror before `POST /api/runs`.
- **Remove the legacy mechanism (no back-compat):** delete `writeActiveLatexContext`,
  `readExistingLatexContext`, `buildLatexContextInfo`, `maybePrepareLatexContext`,
  `upsertProjectLatexContextEntry`, the manifest writes, the `/latex-context` route,
  and the `latexContextGuidance` block in `buildLeaPrompt`. No migration of existing
  `workspace/context/overleaf/…` data — those dirs are simply orphaned (optional
  one-time delete).
- Replace the `leaLatexContextMode` setting with a simple **on/off** toggle ("Mirror
  Overleaf `.tex` into the project"). The old `off`/`active_file` values and their
  plumbing are deleted outright, not migrated.

**`shared/leanStub.mjs`**
- Remove `buildLeaLatexContextRoot` / `…ManifestPath` / `…ActiveTexPath` (legacy).
  Keep `slugProjectId`.

### C. Extension — capture all `.tex` files (the genuinely new capability)

The page bridge can read only the open document, so we need a way to read the project's
*other* `.tex` files. The spike (Section 9) settled the mechanism:

- **Architecture is fine.** `pageBridge.js` is injected as a `web_accessible_resource`
  into the page's **main world**, so it can both touch Overleaf's client internals
  *and* make **authenticated same-origin requests** like the page's own JS — no new
  extension permission needed. `content.js` (isolated world) keeps talking to the
  localhost companion (already permitted) and bridges to `pageBridge` via `postMessage`.
- **Recommended (robust, version-agnostic): the authenticated project ZIP.** From the
  page world, `fetch('/project/<id>/download/zip', {credentials:'include'})` returns the
  source archive regardless of editor build (Angular or React/CM6). Unzip in-page
  (bundle **JSZip**), **keep only `*.tex`**, and **overlay the live active-editor
  buffer** so the file being edited is current. Output is exactly "just the `.tex`" —
  the only cost is that the request downloads the whole archive (trivial for a math
  paper; some MB for asset-heavy projects). Reading non-open doc *contents* is the hard
  part, and the ZIP is the one mechanism guaranteed to deliver it.
- **Optional optimization (only if download size becomes a problem):** enumerate `.tex`
  paths from `window._ide.fileTreeManager` (or the file-tree DOM) and read each via a
  per-doc REST endpoint, avoiding the binary payload. Lighter, but editor-version
  dependent — defer until measured need. See Section 4 for the full latency strategy.

Wiring (latency-driven — see Section 4):
- `pageBridge.js` / `content.js`: add `collectProjectTexFiles()` → `[{path, content}]`
  (`.tex` only); overlay `latestActiveTex` for the active path; compute a **content hash**
  of the set.
- **Background sync, not on-click.** Run collect+mirror on a **debounce after edits**
  (and on project open / tab focus), reusing the existing `scheduleLatexContextSync`
  machinery and the `latestSyncedLatexContext` change-guard generalized to the whole set.
  Skip the network entirely when the hash is unchanged.
- On **formalize**, send only the current hash; the companion runs the fast path above.
  This keeps the formalize click ≈ free in the common case while guaranteeing freshness.
- Bundle JSZip as an extension asset; update the settings toggle copy to an on/off mirror switch.

---

## 4. Latency & sync strategy

The mirror's cost is real but almost entirely **schedulable**. Only one thing is
irreducible: when a `.tex` actually changes, the changed bytes must reach the adapter
once before a run that depends on them. Everything else is removed by changing *when*
and *how* we move them.

- **Amortize off the formalize click (primary win).** Mirror continuously in the
  **background** as the user edits (debounced), not on the Formalize button. By run-start
  the files are already in place, so formalize adds ≈ 0.
- **Detect "unchanged" and skip (primary win).** Hash the `.tex` set; an unchanged sync
  does no fetch-forward work, no adapter write, no commit. Re-formalizing an unedited doc
  is free.
- **Defer the git commit.** The adapter writes + indexes synchronously but commits in the
  background; the composed context reads files from disk, so the run never waits on git
  (~0.1–0.4 s removed from the path).
- **Incremental fetch (spike-gated upgrade).** Overleaf's client holds a realtime
  websocket carrying edit events for *every* doc. Tapping it lets the extension track a
  **dirty set** and re-read only changed docs — and skip the fetch entirely when idle.
  Strongest reduction, but socket access is editor-build-dependent (same fragility as
  `_ide`); add only behind the live spike (Section 9).
- **Client-memory reads (spike-gated upgrade).** Docs already open this session live in
  the Overleaf client model and are readable with no network round trip; fetch only the
  cold ones. Same version-dependence caveat.

**Net effect.** Common case (formalizing a doc you've been editing): the background sync
already ran and the hash matches → **near-zero** added latency. Worst case (first sync of
a changed project): one ZIP fetch (~0.3–1.5 s typical) **in the background**, commit off
the critical path. The genuine floor — one transfer of changed bytes per change — never
lands on the formalize click.

**Tradeoff.** Background syncing spends some idle bandwidth on sessions where the user
never formalizes; the on/off toggle (Section 3B) disables mirroring entirely for users
who don't want it. The incremental options shrink even that idle cost but depend on the
spike.

---

## 5. Implementation phases

1. **Adapter sink (no UI dependency).** Add `sync_overleaf_tex` (with hash short-circuit
   + **deferred commit**) + store helpers + the mirror route + inventory change.
   Unit-test in isolation. *Testable via curl before any extension work.*
2. **Companion rewire.** Add the client + `handleMirrorTex`, the **formalize hash
   fast-path** (skip re-sync when unchanged), delete the legacy context code and prompt
   pointer. Test with a mocked `.tex` set.
3. **Extension capture + background sync.** Implement `collectProjectTexFiles` +
   active-buffer overlay + set hashing; drive it from a **debounced background sync**
   (not the click) and send only the hash on formalize. (Gated by the retrieval spike,
   Section 9.)
4. **Optional latency upgrades (spike-gated).** Incremental websocket dirty-tracking
   and/or client-memory reads, only if measured cost warrants.
5. **Cleanup + polish.** Settings toggle copy, surface mirrored files as a
   "managed / from Overleaf" kind in the Files UI (read-only, not user-deletable), docs.

---

## 6. Edge cases to handle

- **Active file unsaved / ZIP lag** → overlay the live editor buffer over the ZIP set.
- **Subfolders** → preserve relative path under `.lea/files/overleaf/`.
- **Rename / delete in Overleaf** → reconcile deletes mirrored files now absent.
- **Collision with a user upload** → impossible: mirror lives under `overleaf/` and is
  `kind="overleaf"`; user uploads stay at the `.lea/files/` top level.
- **Unprovisioned project** (created tag-only by `get_or_create_project`, no on-disk
  repo) → the mirror endpoint `init_repo`s + `mkdir`s before writing.
- **No empty commits / unchanged sync** → hash short-circuit: no write, no commit.
- **Background sync racing a formalize** → both go through the idempotent reconcile; the
  formalize fast-path compares against the last *acknowledged* mirrored hash, and on a
  mismatch top-up runs (and completes) before `POST /api/runs`.
- **Formalize mid-flight background sync** → if a deferred commit is still pending, the
  files are already on disk (synchronous write), so the run's context is correct
  regardless of commit timing.
- **Toggle off** → no background sync and no mirror; existing mirrored files are left in
  place (or cleared on next run — decide in polish).
- **`.tex` only** → all Tier-1 text; no extraction, no binary, well under the size cap.
- **Project title** → Overleaf projects currently get the raw ID as title; optionally
  pass the document name through for a friendlier Files-tab label (minor).

---

## 7. Testing & verification

- **Adapter (pytest):** new `tests/test_mirror.py` (or extend `test_uploads.py`) —
  upsert, **no-op when hash unchanged**, delete-on-absent, **deferred commit** (files +
  index present before the commit lands), path-escape guard; `test_routes_projects.py`
  for the endpoint (get-or-create + summary); a `compose_context_message` test asserting
  mirrored `.tex` appears in the inventory.
- **Companion (node --test):** `leaApiClient.test.mjs` for the mirror client;
  `companion.test.mjs` asserting the **formalize hash fast-path** skips the mirror when
  unchanged and tops up on mismatch, and that the legacy context code is gone.
- **Extension:** unit-test the pure collect/overlay/hash function in `shared/`; verify
  the **debounced background sync** fires on edit and is skipped when the hash is
  unchanged; manual end-to-end on a real Overleaf project.
- **Latency check:** time the formalize fast-path (expect ≈ no added latency on an
  unchanged doc) and one cold background sync; confirm the run does not block on the git
  commit.
- **Manual acceptance:** edit a doc → background sync mirrors it → mirrored `.tex` appear
  in the Files tab (as an "overleaf" kind) and in Lea's composed context → formalize is
  instant → rename/delete in Overleaf reconciles on the next sync.

---

## 8. Open risks

- **(Resolved by the spike)** Reading non-open `.tex` from the page — the authenticated
  project-ZIP mechanism works regardless of editor build (Section 9). One live
  confirmation still pending (see Section 9) but the approach no longer blocks.
- **(Low, mitigated)** ZIP download size on asset-heavy projects — now off the formalize
  path (background sync) and skipped when unchanged (hash); the spike-gated incremental
  fetch removes even the idle cost if needed.
- **(Low)** Inventory recursion change in `compose_context_message` must not disturb the
  existing top-level upload listing or the Tier-2 sidecar handling.
- **(Low)** Deferred commit means a brief window where mirrored files are on disk +
  indexed but not yet committed; acceptable because the run reads from disk and git is
  not the source of truth for *mirrored source* (unlike proof bytes).

---

## 9. Spike result — extension `.tex` retrieval

**Question:** can the extension read *all* the project's `.tex` (not just the open
file) on the current Overleaf, and with what permissions?

**Method:** read the extension manifest + bridge code; researched the current Overleaf
client and the source-download endpoint (`pyoverleaf`, the `download/zip` gist, the
"Plumbing into Overleaf editor logic" write-up on `window._ide`). Live page probing was
**not** possible this session — no Chrome browser is connected to the account.

**Findings.**
1. **Permissions/architecture already support it.** `manifest.json` injects
   `pageBridge.js` as a `web_accessible_resource` into the page **main world**, which
   already reaches Overleaf's CodeMirror via `UNSTABLE_editor:extensions`. Main-world
   code can issue authenticated same-origin requests exactly like Overleaf's own JS, so
   **no new `host_permissions` entry is required** to read `.tex` page-side. The only
   declared host permission is localhost (the companion), which still covers
   `content.js → companion`.
2. **The ZIP endpoint is the canonical, build-agnostic source read.**
   `GET /project/<id>/download/zip` (cookie-authenticated) returns all source files and
   is what `pyoverleaf` / `underleaf` / community scripts use. Filtering `*.tex` yields
   exactly the desired set. This does **not** depend on `window._ide`, so it survives
   the Angular→React/CM6 editor change.
3. **`window._ide.fileTreeManager` exists historically** and cleanly *lists* files, but
   the documented usage only reads the **open** doc's content — it does not clearly
   expose non-open doc contents, and its shape on the redesigned editor is unconfirmed.
   Good for a future *listing* optimization, not for content today.

**Conclusion.** Build on the **authenticated project ZIP** (page-world fetch + JSZip +
`.tex` filter + live-buffer overlay). Robust, permission-clean, version-proof; the only
tradeoff is downloading the whole archive per formalize, which is optimizable later.

**One check still worth doing live** (paste into DevTools on an open Overleaf project,
or have me run it once the Chrome extension is connected):

```js
const id = location.pathname.match(/\/project\/([^/]+)/)[1];
const t0 = performance.now();
const r = await fetch(`/project/${id}/download/zip`, { credentials: "include" });
const blob = await r.blob();
console.log("zip:", r.status, r.headers.get("content-type"), blob.size,
            `${(performance.now() - t0).toFixed(0)}ms`);          // baseline fetch cost
console.log("_ide?", !!window._ide, "fileTree?", !!window._ide?.fileTreeManager);  // listing opt?
console.log("socket?", !!(window._ide?.socket || window._ide?.connectionManager)); // dirty-track opt?
```

Expected: `zip: 200 application/zip <bytes> <ms>`. The `ms` is the real dominant latency
term to design around (Section 4); the `_ide`/`socket` lines tell us whether the
spike-gated optimizations (per-doc listing, websocket dirty-tracking) are available on
this build.
