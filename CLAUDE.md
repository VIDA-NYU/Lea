# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> Not to be confused with the `CLAUDE.md` one directory up (`lea-agent/CLAUDE.md`),
> which documents the standalone prover checkouts. **This** repo is the
> `lea-ecosystem` monorepo that wraps the prover behind two front ends.

## What this is

`lea-ecosystem` is an npm-workspaces monorepo that unifies two front ends for
[Lea](https://github.com/darturi/lea-prover) (a Lean 4 theorem-proving agent)
around one shared backend:

- **`apps/lea-standalone/`** (npm name `lea-interface`) — a React + Vite UI plus a
  FastAPI **adapter** that drives the prover and streams proof progress; persists
  chat/code history to SQLite.
- **`apps/overleaf-extension/`** (npm name `overleaf-lean-stub-mvp`) — a Chrome
  extension + a local Node **companion** server that formalizes labeled theorem
  blocks straight from an Overleaf document.
- **`packages/lea-model-catalog/`** — the provider/model list (`models.json` +
  `index.mjs`) both apps import.
- **`apps/lea-standalone/prover/`** — the **vendored** Lea prover (a Python/`uv`
  package, `lea-prover`), copied in-repo (no git submodule). It has its own
  `DESIGN.md`, `lea/` package, and `workspace/` Lake project.

## The one architectural idea to internalize

The FastAPI adapter **imports the vendored prover in-process as a library** — there
is **no HTTP boundary** between the adapter and the prover, and no separate "Lea
API" server. `apps/lea-standalone/adapter/app/bridge.py` calls the prover's
`lea.interface.run_events(...)` and pattern-matches its *typed* events
(`AssistantTextDelta`, `TurnStarted`, `ToolCalled`, `FileChanged`, `CheckResult`,
`UsageUpdated`, `Finished`, `ToolApprovalRequested`, …) onto the browser's SSE
stream. Both front ends talk to this single adapter on **:8001**.

Persistence is split deliberately (see design-decision tags `D1`/`D6`/`D7`/`D8`/`D14`
sprinkled through the adapter source, documented in `docs/`):

- **SQL owns proof content** (inverted in v2.3 — git used to own it). Every
  `FileChanged` preserves the captured write as a content-addressed
  blob (`artifact_blobs`); the `timeline` row points at it. Git is now only
  *transport* for non-proof assets (uploads, project files), never the store.
- **One `timeline` table** holds messages and code steps (`adapter/app/db.py`
  defines it via Alembic revisions in `adapter/migrations/`). Its autoincrement
  `id` IS the ordering key — the old per-session `seq` counter was a
  read-modify-write across two tables that silently issued duplicates under
  concurrent writers. A session has **no stored status** — it's *derived* from the
  latest code row's verdict and current source/dependency evidence.
- The streamed SSE payload and the stored row carry the **same bytes**, so the live
  canvas and a reload can't disagree. An append-only `proof_check_evidence` row
  binds a verdict to the checked blob and compiler environment. Backfill requires
  matching captured bytes; current success also requires fresh dependencies.
- **Migrations are real** (`adapter/migrations/`, Alembic). The DB is no longer
  disposable — "just reset it" now means "delete the user's proofs". Any pending
  migration snapshots the DB first (`adapter/app/backup.py` → `data/backups/`).
- `check_current` and stale reasons are derived on read. Evidence-less history
  remains available, but requires an explicit recheck for current certification.

`autonomous` runs (the Overleaf path, `D19`) disable the per-tool approval gate and
use the non-interactive prompt variant; interactive UI runs keep the approval gate.

### Doc pointers

The root `README.md` and `apps/lea-standalone/README.md` were rewritten for the
in-process architecture and are current (verified 2026-07-10). For a deeper map
see `docs/SYSTEM-REPORT-overleaf-architecture.md`; per-feature design docs are
the `FEATURE-`/`PLAN-` files in `docs/`. When prose and code disagree, trust
`scripts/setup.mjs`, `package.json`, and the adapter source.

## Ports

| Service                          | Port  | Started by            |
|----------------------------------|-------|-----------------------|
| FastAPI adapter (the backend)    | 8001  | `npm run start:adapter` / `dev:ui` |
| Vite web dev server (the UI)     | 5173  | `npm run dev:ui`      |
| Overleaf companion server        | 31245 | `npm run dev:overleaf`|

## Commands (run from the monorepo root)

```bash
./install.sh                  # one-shot bootstrap: installs uv+elan if absent, then runs setup
npm run setup                 # full provision: node deps, prover venv, Lean/Mathlib cache, .env, overleaf settings
                              #   (runs scripts/preflight.mjs first — fails early with install hints if node/uv/lake missing)
npm run setup -- --target ui  # or --target overleaf — provision just one app
npm run setup -- --skip-verify # skip the SafeVerify build + its 2nd Mathlib download (/verify -> "unavailable")
npm run update-lean-deps      # lake update + refresh the Mathlib cache

# Zero-toolchain path (standalone UI only): build the self-contained image locally.
cd apps/lea-standalone && docker compose build && docker compose up   # open http://localhost:8001, add key in Settings

npm run start:adapter         # start ONLY the shared FastAPI backend (:8001)
npm run dev:ui                # adapter (:8001) + Vite (:5173) — the standalone UI
npm run dev:overleaf          # Overleaf companion (:31245); expects the adapter running

./start-dev.sh                # start the whole stack (UI + overleaf); keeps previous session data
./start-dev.sh --fresh        # ...clearing previous sessions/proofs/logs first

npm run doctor                # health-check both apps
npm run reset:local           # clear local run state (proofs, logs, SQLite); keeps Lea deps
npm run reset:local -- --dry-run   # preview what reset would remove
```

## Tests

There is no single test runner; each workspace has its own. `npm test` (root) runs
the Overleaf suite then the UI frontend suite, but **not** the adapter's Python tests.

```bash
# Adapter (Python / pytest) — the backend logic. Run inside the app's venv:
cd apps/lea-standalone/adapter && ./.venv/bin/python -m pytest          # all
cd apps/lea-standalone/adapter && ./.venv/bin/python -m pytest tests/test_bridge.py   # one file
cd apps/lea-standalone/adapter && ./.venv/bin/python -m pytest tests/test_db_messages.py::<name>  # one test

# UI frontend (Node built-in test runner, .mjs unit tests):
npm run test:frontend -w apps/lea-standalone        # node --test src/app/*.test.mjs

# Overleaf companion (Node built-in test runner):
npm test -w apps/overleaf-extension                 # node --test tests/*.test.mjs

# The vendored prover has its own test suites — see apps/lea-standalone/prover/
# (run as `python -m <module>`, not pytest; see that dir's DESIGN.md / README.md).
```

`apps/lea-standalone/typecheck` (`tsc --noEmit`) checks the React/TS code.

## Configuration

- **One env file** at the monorepo root drives everything: `cp .env.example .env`.
  Shell-exported vars override `.env`; older app-local files are read only as
  migration fallbacks. The prover reads its provider keys from this same config
  (`D1·cfg`) — there is no separate key plumbing.
- Key vars (defaults in `scripts/setup.mjs`): `LEA_ROOT=apps/lea-standalone/prover`,
  `LEA_API_BASE_URL=http://127.0.0.1:8001`, `LEA_PROVIDER`, `LEA_MODEL`,
  `LEA_MAX_TURNS`, `LEA_JOB_TIMEOUT_SECONDS`, `LEA_NARRATE_TOOL_STEPS`.
- **Overleaf sessions:** the companion starts autonomous runs through the same
  adapter, so the adapter records their sessions, timelines, and usage in the
  normal UI database. `LEA_SHARED_DATA_DIR` can relocate that data directory for
  local experiments; the old separate shared-state recorder is legacy-only.
- The adapter's local config lives at `apps/lea-standalone/config/lea.local.toml`
  (gitignored). App DB: `apps/lea-standalone/data/lea-interface.sqlite3` (gitignored).

## Layout cheat-sheet

```
apps/lea-standalone/
  adapter/app/        FastAPI backend: main.py (wiring), bridge.py (prover seam),
                      db.py, gitstore.py, store.py, config.py, routes/{runs,sessions,settings}.py
  adapter/tests/      pytest suites (test_bridge, test_db_*, test_routes_*, ...)
  src/app/            React UI (App.tsx, components/, stores/ [zustand], hooks/, *.test.mjs)
  prover/             vendored Lea prover (its own lea/ package + workspace/ Lake project)
  scripts/            dev.mjs (launcher), doctor.mjs, setup-api.mjs, reset-local-state.mjs
apps/overleaf-extension/
  extension/          Chrome MV3 extension: content.js (badges/popovers/Lean pane, IIFE),
                      pageBridge.js (in-page CodeMirror plugin), uiBridge.js (Lea UI tab),
                      background.js, options.*, and lazily-imported .mjs modules
                      (targetParserCore, leanPaneView, zipTex, editorHookWatchdog)
  companion/          Node companion server: server.mjs (routes+orchestration),
                      leaApiClient.mjs (adapter client), jobStore.mjs, config.mjs,
                      chatPrompt.mjs, cascadeVerify.mjs, leanDependencyGraph.mjs,
                      leanSignatureDiff.mjs, doctor.mjs
  shared/             theoremParser.mjs, leanStub.mjs, leanPaneManifest.mjs
packages/lea-model-catalog/   models.json + index.mjs (shared model list)
scripts/              monorepo setup.mjs, env.mjs (.env read/patch), reset-local-state.mjs
```

## When editing

- **Keep the two apps talking to one backend on :8001.** Don't reintroduce a
  separate prover HTTP server — the adapter drives the prover in-process.
- **Don't store derived state.** Session status is computed from the latest code
  row's verdict — never stored.
- **Proof bytes live in the DB, not git** (v2.3 inverted this; older docs and
  comments may still say the opposite). Don't reintroduce a git pointer for proof
  content: a pointer is a claim about a second store that nothing verifies, and at
  least one shipped row pointed at a commit whose tree never held the file.
- **Schema changes go through a new Alembic revision** — never edit an applied one,
  and never hand-`ALTER` at startup.
- The shared model list is consumed by both Node apps via `packages/lea-model-catalog`
  and by the adapter's `models_catalog.py` — keep them consistent.
- Changes to the vendored prover under `apps/lea-standalone/prover/` are picked up
  live (editable `uv` path dep); follow that package's own `DESIGN.md` guardrails
  (never modify a theorem statement, no `sorry`/`axiom`/`native_decide` in final
  proofs, Mathlib-search budget, etc.).
