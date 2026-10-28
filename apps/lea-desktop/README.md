# Lea Desktop (proof of concept)

Lea as a native desktop application: its own window, dock icon and menu bar,
with the backend started and stopped by the app. The UI is the unchanged
`apps/lea-standalone` React app; the backend is the unchanged adapter, worker
and Overleaf companion. Only the launcher is new.

This is a POC. It runs from the repository checkout and assumes the local
toolchain install (`npm run setup`) has been done. Packaging, first-run
provisioning of Lean and Mathlib, signing and auto-update are not here yet;
see "What is missing" below.

## Run it

```sh
# once, from the monorepo root
npm install
npm run build -w apps/lea-standalone     # the app serves this dist/ when it owns the backend

# start the app
npm start -w apps/lea-desktop
```

On launch the app looks for a Lea adapter on `127.0.0.1:8001`:

- **A stack is already running** (`./start-dev.sh` or `npm run dev:ui` in a
  terminal): the app attaches to it and opens the Vite UI on `:5173`. Nothing is
  started or stopped by the app.
- **Nothing is running**: the app starts the stack itself, in the same order as
  `scripts/supervisor.mjs` — `bootstrap.py`, the worker, the API (serving the
  built UI on `:8001`), and the Overleaf companion on `:31245` — shows progress
  on a splash screen, and opens the UI once `/api/ready` answers. Quitting the
  app stops everything it started.

Force one mode with `LEA_DESKTOP_ATTACH=1` (attach or fail) or
`LEA_DESKTOP_ATTACH=0` (always own the stack); `npm run start:attach` and
`npm run start:spawn` are shortcuts.

Backend logs go to the app's log folder (**Help → Show logs folder**;
`~/Library/Logs/Lea` on macOS), one file per process.

## What the shell adds

- Native menu bar with the usual Edit/View/Window roles, **Settings…** (⌘,),
  **Home** and **Statistics** items that navigate the UI, an **Overleaf** menu
  (open Overleaf, extension install guide, companion status), and **Help →
  Backend status…**.
- External links open in the system browser; only the local UI stays in the
  window.
- Window size and position are remembered. Closing the window on macOS keeps
  the app and its backend running (so the Overleaf companion stays up); the
  dock icon reopens it.
- Single-instance: a second launch focuses the existing window.
- `lea://` deep links (`lea://session/<id>`, `lea://formalization/<id>`,
  `lea://settings`). macOS only honours the scheme for a packaged app, so this is
  wired but inert under `electron .`.
- The worker's private Node child and the companion run on the app's own
  binary (`ELECTRON_RUN_AS_NODE`), so a Finder launch needs no Node on PATH.
  `PATH` is extended with the usual elan, uv and Homebrew locations for the same
  reason.

## Smoke test

```sh
LEA_DESKTOP_SCREENSHOT=/tmp/lea-desktop.png npm start -w apps/lea-desktop
```

Starts the app, waits for the UI to settle (`LEA_DESKTOP_SCREENSHOT_DELAY_MS`,
default 6000), writes a PNG of the window and quits. Useful for CI and for
checking the shell without a person at the screen.

## Troubleshooting

- **"This UI requires the database-only API and separate Lea worker. Update the
  services together."** An adapter from before your last `git pull` is still
  running on `:8001` (for example a `./start-dev.sh` left open in a terminal),
  and the current UI refuses it. Stop that stack (Ctrl+C there, or `kill` its
  supervisor) and launch the app again; with nothing on `:8001` it starts a
  current stack itself.
- **"The requested module 'electron' does not provide an export named 'app'"**
  when running the Electron binary by hand: `ELECTRON_RUN_AS_NODE=1` is set in
  your environment (VS Code's extension host sets it), which turns Electron into
  plain Node. `npm start` goes through `scripts/launch.mjs`, which strips it.
- **Port 8001 busy but no healthy adapter:** something else holds the port;
  `lsof -nP -iTCP:8001 -sTCP:LISTEN` shows what.

## Files

- `src/main.mjs` — app lifecycle, window, deep links, quit → backend stop.
- `src/backend.mjs` — attach-or-spawn logic; reuses the repo's
  `scripts/process-supervisor.mjs`, `service-env.mjs`, `env.mjs` and
  `dev-stack-preflight.mjs`.
- `src/menu.mjs` — the native menu.
- `src/preload.cjs` — the sandboxed bridge (`window.leaDesktop`).
- `src/splash.html` — startup progress page.
- `scripts/make-icon.py` — regenerates `assets/icon.{svg,png,icns}` from the
  wordmark (macOS tools only).

## What is missing (next steps toward a shippable app)

1. **Run from an app-data directory instead of the checkout.** Resolve the
   adapter, prover and workspace from `app.getPath('userData')`; today the
   backend's own path assumptions point at the repo.
2. **First-run bootstrap.** Provision Python (via a bundled `uv`), the Lean
   toolchain and the Mathlib cache into app data with a progress screen; this
   is `install.sh` + `docker/prepare-mathlib.sh` behind a UI.
3. **Packaging and updates.** electron-builder, Developer ID signing and
   notarization, `electron-updater` fed from GitHub Releases. `assets/icon.icns`
   is ready for that.
4. **Title bar.** `titleBarStyle: 'hiddenInset'` plus a drag region in the UI
   for the VS Code look; left default here so nothing overlaps the UI.
5. **Windows.** The native path is POSIX-only today (venv paths, process groups,
   exclusive file locks); Windows stays on Docker until that is ported.
