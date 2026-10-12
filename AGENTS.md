# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

> Not to be confused with the `AGENTS.md` one directory up (`lea-agent/AGENTS.md`),
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

- **SQLite owns proof history and metadata.** `artifact_blobs` retains immutable
  proof snapshots; `timeline` references those blobs. Git remains useful for
  working repositories, uploads and export, but is not the only proof-history copy.
- A session has **no stored status**. Its current certification is derived from
  the relevant timeline verdict and matching source/dependency check evidence.
- The streamed SSE payload carries the file snapshot for the live canvas; the
  `lean_check` verdict attaches only to the immutable blob actually checked.
  Historical `check_status` and derived `check_current` are separate projections.

`autonomous` runs (the Overleaf path, `D19`) disable the per-tool approval gate and
use the non-interactive prompt variant; interactive UI runs keep the approval gate.

The prover is vendored at `apps/lea-standalone/prover/` and runs in-process.
Container builds use the monorepo root and serve both backends. Use the canonical
`adapter/run_api.py` launcher; readiness is `/api/ready`.

## Ports

| Service                          | Port  | Started by            |
|----------------------------------|-------|-----------------------|
| FastAPI adapter (the backend)    | 8001  | `npm run start:adapter` / `dev:ui` |
| Vite web dev server (the UI)     | 5173  | `npm run dev:ui`      |
| Overleaf companion server        | 31245 | `npm run dev:overleaf`|

## Commands (run from the monorepo root)

```bash
npm run setup                 # full provision: node deps, prover venv, Lean/Mathlib cache, .env, overleaf settings
npm run setup -- --target ui  # or --target overleaf — provision just one app
npm run update-lean-deps      # lake update + refresh the Mathlib cache

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
                      db.py, store.py, proof_evidence.py, config.py, routes/{runs,sessions,settings}.py
  adapter/tests/      pytest suites (test_bridge, test_db_*, test_routes_*, ...)
  src/app/            React UI (App.tsx, components/, stores/ [zustand], hooks/, *.test.mjs)
  prover/             vendored Lea prover (its own lea/ package + workspace/ Lake project)
  scripts/            dev.mjs (launcher), doctor.mjs, setup-api.mjs, reset-local-state.mjs
apps/overleaf-extension/
  extension/          Chrome extension (manifest.json, content.js, background.js, options.*)
  companion/          Node companion server (server.mjs, leaApiClient.mjs, config.mjs, doctor.mjs)
  shared/             theoremParser.mjs, leanStub.mjs (label/theorem parsing, path generation)
packages/lea-model-catalog/   models.json + index.mjs (shared model list)
scripts/              monorepo setup.mjs, env.mjs (.env read/patch), reset-local-state.mjs
```

## When editing

- **Keep the two apps talking to one backend on :8001.** Don't reintroduce a
  separate prover HTTP server — the adapter drives the prover in-process.
- **Don't store derived state.** Session status and check freshness are computed
  from immutable recorded verdicts and current evidence. Preserve SQL proof blobs,
  timeline history and the append-only evidence table.
- The shared model list is consumed by both Node apps via `packages/lea-model-catalog`
  and by the adapter's `models_catalog.py` — keep them consistent.
- Changes to the vendored prover under `apps/lea-standalone/prover/` are picked up
  live (editable `uv` path dep); follow that package's own `DESIGN.md` guardrails
  (never modify a theorem statement, no `sorry`/`axiom`/`native_decide` in final
  proofs, Mathlib-search budget, etc.).

## Imported Claude Cowork project instructions
