# PLAN — UI redesign (canvas, files, blueprint first, project first)

Audience: Lea engineers and the agents implementing the work packages below.
Mockup: the Design canvas "Lea Canvas Mockup", frames 1–15. The artboard
sources are plain HTML with inline styles and are the reference for every
measurement, label and state in this plan; the exact rendering is not sacred,
the behaviour and the information shown are.

Decisions already taken (do not reopen):

- **Project first.** Every session lives in a project. "New proof" creates a
  project named from the statement; there are no loose sessions in the UI.
- **Blueprint first.** `/blueprint` drafts the decomposition before any
  proving; sessions are bound to blueprint nodes; node → file is explicit.
- **One surface.** The canvas is always the editor; files, the blueprint and
  the proof are documents in it. No separate Filesystem or Blueprint tab.
- **The LeanTrail-style paper view (frames 16–17) is out of scope** for now.

## 1. How the work is split

Five packages run in parallel, each in its own git worktree on a branch off
`desktop-app`. The split follows file ownership so that merges are mechanical.
A package edits only the files it owns; anything else it needs goes through
the contracts in §2 or is written down in its final report for the integrator.

| Package | Frames | Owns (create or edit) | Must not touch |
|---|---|---|---|
| **canvas** | 1, 2, 3, 4 | `components/Canvas.tsx`, `components/LiveEditor.tsx`, `styles/canvas.css`, new files under `components/canvas/` | App.tsx, ChatThread.tsx, Sidebar.tsx, lea-v2.css |
| **files** | 5, 6, 7 | `components/files/*`, `styles/files.css`, `lib/files*.{js,ts}` + tests, `stores/files.ts` (optional) | Canvas.tsx, App.tsx, lea-v2.css |
| **blueprint** | 8, 9, 10 | `components/blueprint/*`, `styles/blueprint.css`, `lib/slashCommands.js`, `lib/slashCommandRunner.ts`, `lib/blueprint*.{js,ts}` + tests, `packages/lea-blueprint/blueprintLayout.mjs` (layout only) | Canvas.tsx, App.tsx, ChatThread.tsx (NodeChip is already mounted), lea-v2.css |
| **shell** | 11, 12 | `components/shell/*`, `components/Sidebar.tsx`, `components/ProjectsHub.tsx`, `components/ProjectWindow.tsx`, `styles/shell.css`, **App.tsx view routing only** (the `home` / `project-home` branches and the start-project callback), `hooks/useLayout.ts` | Canvas.tsx, ChatThread.tsx, lea-v2.css |
| **desktop** | 13, 14, 15 | `apps/lea-desktop/**`, `src/desktop.d.ts`, `stores/desktop.ts`, `lib/desktopActivity.js` + test, the update button component (new file `components/shell/DesktopUpdateButton.tsx`) | everything else in the UI; the shell package mounts the button in the new Sidebar footer at integration |

Paths are relative to `apps/lea-standalone/src/app/` unless stated.

Shared files that every package may *read* but only the integrator edits:
`App.tsx` outside the shell package's region, `ChatThread.tsx`, `lea-v2.css`,
`lib/api.ts`, `lib/types.ts`, `stores/*` that exist today. If a package needs a
new API call or type, it adds it to its own `lib/<package>Api.ts` wrapper over
`fetch` with the same conventions as `lib/api.ts`, and says so in its report.

## 2. Contracts

The foundation commit (`ui: foundation for the redesign work packages`) added
these; packages build on them and may extend their own slice.

**`stores/workbench.ts`** — the shared store. Slices by owner:

| Slice | Fields | Owner |
|---|---|---|
| files | `openPath`, `openFile()`, `filesRailOpen`, `setFilesRailOpen()`, `toggleFilesRail()`, `quickOpenOpen`, `setQuickOpenOpen()` | files |
| blueprint | `selectedNodeKey`, `selectNode()`, `nodeBindings` (sessionId → node key, persisted), `bindSessionToNode()` | blueprint |
| canvas | `focusMode`, `setFocusMode()`, `drawerTab`, `setDrawerTab()` | canvas |

`BLUEPRINT_PATH = '.lea/blueprint.md'`: opening this path shows the blueprint.

**Components with fixed props** (stubs exist; replace the body, keep the
signature, extend props only by adding optional ones):

- `components/files/FilesRail.tsx` — `FilesRail({ projectId })`. Canvas mounts
  it as its left column when `filesRailOpen`.
- `components/files/PathCrumbs.tsx` — `PathCrumbs({ projectId, path })`.
  Canvas mounts it under the title in its header.
- `components/files/QuickOpen.tsx` — `QuickOpen({ projectId })`. App mounts it
  once at the root; it renders only while `quickOpenOpen`. The files package
  owns the ⌘P key binding (register it inside QuickOpen with a `keydown`
  listener on `window`, guarded against inputs).
- `components/blueprint/BlueprintView.tsx` — `BlueprintView({ projectId, onOpenSession })`.
  Canvas renders it instead of the editor when `openPath === BLUEPRINT_PATH`.
- `components/blueprint/BlueprintStrip.tsx` — `BlueprintStrip({ projectId, sessionId })`.
  Canvas mounts it above the editor; it renders null for an unbound session.
- `components/blueprint/NodeChip.tsx` — mounted in ChatThread's header already.
- `components/shell/Home.tsx` — `Home({ onOpenProject, onStartProject, onNewProject })`.
- `components/shell/ProjectHome.tsx` — `ProjectHome({ projectId, onOpenSession, onNewSession, onOpenBlueprint, onOpenFiles })`.

**Where the canvas gets its project.** `useSessions` has the selected session;
`session.project_id` is the project. With project first every session has
one; while that is not yet true in the database, treat a missing project as
"no rail, no crumbs, no blueprint".

**Per-file status** (`FileStatus` in the store): derive it from the project
tree plus the latest check per file. Today the tree has no status field; the
files package computes it client-side from `getProjectFile` / code steps and
documents what the backend should add (one field, `check_status`, per entry).

**Styling.** Each package has its own stylesheet, imported by its components.
Use the tokens from `lea-v2.css` (`--bg`, `--panel`, `--line`, `--ink`,
`--ink-2`, `--muted`, `--accent`, `--green`, `--amber`, …). Do not edit
`lea-v2.css`; if a token is missing, define it in your stylesheet under your
own prefix and list it in the report.

**Status colours** (keep them identical across packages): proved `--green`,
sorry `--amber`, error `#b4473b`, running `#3b5fa8`, unchecked outline
`#a9a9b2`, ready outline `#3b5fa8`. Never hue alone: pair with a label or icon.

## 3. Package briefs

### canvas (frames 1–4)

Replace the History/Live toggle and the footer blob with one surface:

- Header: theorem name (mono) over `PathCrumbs`; a single status chip that
  merges compile, sorry and verify (`Checking…`, `1 error · line 14`,
  `Proved · checked`, `Proved · verified`); icon buttons lean_check,
  SafeVerify, more, hide canvas; a rail toggle at the far left that flips
  `filesRailOpen`.
- A 36px timeline strip: previous/next step, dots per code step (red for a
  failed check, blue ring for the current), "Step n of m · turn t · Lea",
  `+a −b this step`.
- Editor: gutter marks for lines changed in the current step, the agent's
  caret marker ("Lea · editing") on the line being edited, error lines
  tinted with a squiggle, `sorry` lines tinted amber.
- Drawer (40px collapsed, expandable): Goals / Problems / Verify tabs from
  `drawerTab`; Problems lists errors with jump links; Verify shows the
  SafeVerify verdict and audit output.
- Empty state (frame 1): three cards, Ask Lea / Open a project file / Paste
  Lean, and a New file button.
- Focus mode (frame 4): `focusMode` collapses the chat to a rail; the proof
  takes the width. App already supports a collapsed canvas; focus is the
  mirror image and lives in the canvas package (a class on `.main-area`
  is acceptable; coordinate the class name in the report).
- Mount `FilesRail`, `PathCrumbs`, `BlueprintView` and `BlueprintStrip` at the
  places above, using the stubs as they are.

### files (frames 5–7)

- `FilesRail`: project switcher (lists projects from `useProjects`), "Go to
  file" box (opens QuickOpen), "This session" group (files from the session's
  code steps with +/− line deltas), the project tree from `getProjectTree`
  with status dots and a "Lea" marker on the file being edited, a legend, and
  a right-click menu: Open, Open to the side (may be disabled), Ask Lea about
  this file, Prove the sorries here, Reveal in Finder (desktop only, via
  `window.leaDesktop`), Copy path, Rename…, Delete….
  "Ask Lea about this file" and "Prove the sorries here" put a context chip in
  the composer: expose this as a store field `composerContext: { path } | null`
  in `stores/files.ts` for the integrator to wire into ChatThread.
- `PathCrumbs`: one button per segment; a folder crumb opens a menu of its
  siblings with status dots; ← → walks folders, ↑ ↓ files, ⏎ opens.
- `QuickOpen`: ⌘P; fuzzy match over file names (pure function in
  `lib/fuzzy.js` with tests), then declarations (from the LSP when available,
  else a regex scan of `theorem|lemma|def` over fetched files, cached), then
  other projects; `#`, `>`, `:` prefixes; ⌘⏎ adds the hit as composer context.
- Opening a file: `openFile(path)`; the canvas package reads it.

### blueprint (frames 8–10)

- `BlueprintView`: Graph and Markdown toggle; By level / By file grouping
  (by file draws a lane per file, dashed "no file yet" lane); node styles per
  status from `getProjectGraph` (planned dashed, ready blue outline, stated
  amber, proved green, failed red) plus a running badge when a bound session
  is live; the node detail panel (statement, kind, lean, file:line link via
  `openFile`, checked-at, Uses and Unblocks with status, Sessions with the
  running one first, actions Open running session / Ask Lea / Prove).
- Proposal state: when the blueprint is empty and the user runs `/blueprint`,
  call `POST /api/projects/{id}/blueprint/generate` (exists, 202 + command
  receipt; poll `/api/workspace/commands/{id}`), then show the result as a
  proposal with per-node checkboxes and Accept / Edit markdown / Discard.
  Accept writes the markdown through `putProjectDoc`. Note in the report what
  the backend must add for a true unsaved proposal.
- Slash commands in `lib/slashCommands.js` + `slashCommandRunner.ts`:
  `/blueprint` (open or draft), `/blueprint next` (one session per ready
  node: `createSessionInProject` + a run whose first prompt names the node
  and its file; bind with `bindSessionToNode`), `/blueprint prove <node>`.
- `BlueprintStrip` and `NodeChip` real implementations; a "node proved" card
  is out of scope (needs backend events).
- Keep `packages/lea-blueprint/blueprintLayout.mjs` the single layout source;
  extend it for lanes so the Overleaf extension can reuse it later.

### shell (frames 11–12)

- `Home`: the sidebar lists projects only (no Chats group); New project
  button; search box (opens the existing SearchOverlay); "What do you want to
  prove?" box that calls `onStartProject(statement)`; project cards with a
  five-colour blueprint bar from `getProjectGraph` counts, session count,
  last activity, running badge; recent sessions with project and node.
- `ProjectHome`: header (name, namespace, running badge, New session,
  Blueprint, Share, more), progress card, Up next (ready → Prove, sorry →
  Prove the sorry, blocked greyed with reason), Sessions with node ownership,
  Files (status dots), Overleaf card (linked state and the unlinked state with
  a URL field; Sync now can call the companion's existing endpoints or be a
  stub that says so), Git, Instructions, Memory.
- `Sidebar`: projects only; an open project expands to Overview, Blueprint,
  nodes with their sessions nested, Files, Sessions.
- App.tsx routing: `home` is the default when no session is selected;
  `project-home` replaces the `project` view's Overview; keep the old views
  reachable until the integrator removes them. `onStartProject` =
  `createAndOpen(title from statement)` → `createSessionInProject` → the
  existing submit path with `/blueprint` as the first message (ask the
  blueprint package's command to run; if it is not merged yet, submit the
  statement as the first message and leave a TODO).
- Mount `DesktopUpdateButton` (from the desktop package; import it only if the
  file exists at integration, otherwise leave a clearly marked slot) in the
  footer.

### desktop (frames 13–15)

Finish what is parked on `wip/desktop-features` (merge it into your branch
first): notifications when a run finishes while the window is not in front,
dock badge with the running count, Help › Report a Problem… (zip of logs and a
redacted config, prefilled issue link), the update button and its states,
`LEA_DESKTOP_FAKE_UPDATE` for testing. Type-check, run the unit tests, run
the app in dev mode with the fake update and the smoke hooks, fix what breaks.
Move the update button into `components/shell/DesktopUpdateButton.tsx` so the
shell package can mount it; keep the Sidebar edit minimal since Sidebar is
being rewritten.

## 4. Verification each package runs before reporting

```
npm run typecheck -w apps/lea-standalone
npm run test:frontend -w apps/lea-standalone
npm run build -w apps/lea-standalone        # once, at the end
```

Pure logic goes in `.js`/`.mjs` modules with `node --test` tests next to the
existing ones in `src/app/*.test.mjs`. No new dependencies without saying so
in the report.

## 5. Integration

The integrator merges `ui/canvas`, `ui/files`, `ui/blueprint`, `ui/shell`,
`ui/desktop` into `desktop-app` in that order, wires the composer context and
the update button, runs the three commands above plus the Electron smoke
screenshot, and does the visual pass against the mockup.
