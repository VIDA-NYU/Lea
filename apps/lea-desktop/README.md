# Lea Desktop

Lea as a native desktop application: its own window, dock icon and menu bar,
with the backend provisioned, started and stopped by the app. The UI is the
unchanged `apps/lea-standalone` React app; the backend is the unchanged
adapter, worker and Overleaf companion. Only the launcher is new.

## For users

Download the DMG for your Mac (Apple Silicon or Intel) from the GitHub
release, drag Lea to Applications, and open it. Until builds are signed,
right-click → Open the first time.

The first launch provisions everything Lea needs into
`~/Library/Application Support/Lea` and shows progress while it does:

| Step | What happens | First run |
|---|---|---|
| Lea runtime | copies the backend out of the app bundle | seconds |
| Python environment | bundled `uv` installs Python 3.13 and the locked dependencies | ~300 MB |
| Lean toolchain | reuses `~/.elan` if it has the pinned Lean, else installs elan + Lean | ~300 MB |
| Mathlib library | `lake exe cache get` + build check, exactly like the Docker image | several GB |
| Proof verifier | builds SafeVerify against the shared Mathlib (optional) | minutes |
| Starting Lea | database bootstrap, worker, API, Overleaf companion | seconds |

Later launches take seconds. Then add your provider key in **Settings** (⌘,)
and start proving. Self-hosted models (vLLM, Ollama, any OpenAI-compatible
server) and any environment variable the backend reads are set there too, under
**Providers → Self-hosted servers** and **Environment**; nothing needs
exporting. Git must be installed (macOS offers the Command Line Tools
automatically the first time it is needed).

Your data lives outside the app and survives updates and reinstalls:

```
~/Library/Application Support/Lea/
  data/            sessions, timeline, backups (SQLite)
  config/          lea.local.toml with your keys
  app/prover/workspace/{proofs,projects,.lake}   proof repositories and the Mathlib cache
  venv/ python/ elan/ uv-cache/                  provisioned toolchains
~/Library/Logs/Lea/   one log per backend process
```

Quitting the app stops the backend. Closing the window keeps it running (so
the Overleaf extension keeps working); the dock icon reopens it.

### Overleaf

The app runs the companion on `127.0.0.1:31245`, where the Chrome extension
looks by default. **Overleaf → Install the Chrome extension…** has the steps.
"Open in Lea" from Overleaf uses `lea://` links and focuses the app.

### Notifications, Report a Problem, updates

- **Notifications and the dock badge.** When a run finishes while the Lea
  window is not in front, a system notification names the session and the
  outcome (proof complete, Lean error, needs review, counterexample); clicking
  it opens that session. The dock badge is the number of sessions with a live
  run and clears at zero. **Lea → Notify When a Run Finishes** turns the
  notifications off (`<app support>/preferences.json`). The UI derives both
  from its session list; the app only decides whether to show them.
- **Help → Report a Problem…** (also the splash page's button when startup
  fails) saves a zip where you choose: the tail of every log in
  `~/Library/Logs/Lea`, the provisioning log, `state.json`, the workspace's
  `lean-toolchain` and `lake-manifest.json`, an environment summary and a copy
  of `lea.local.toml` with every secret replaced by `"<redacted>"` (all of
  `[keys]` and `[environment]`, any key with key/token/secret/password/
  credential/auth in its name, commented-out keys, user:password in URLs). It
  never includes the database or your proofs. The dialog then offers **Open a
  GitHub issue** (prefilled with version and system; attach the zip), **Reveal
  in Finder** and **Done**.
- **Updates.** The app checks the `desktop-latest` release 15 s after launch
  and every 6 h (or **Check for Updates…**). An update shows as one button in
  the sidebar footer with a popover explaining the steps the first time:
  **Update to x.y.z** downloads in the background, the button becomes
  **Downloading… 43%**, then **Restart to update**; **Later** keeps the button,
  **Skip this version** hides it for that version. A build that cannot update
  in place — on macOS one whose signature is missing or ad-hoc, which is every
  unsigned build — shows **Download Lea x.y.z** instead and opens the DMG for
  your architecture; drag it to Applications. A failed background check stays
  quiet; a failed download shows **Update failed · retry**.

## For developers

```sh
npm install                                 # once, from the monorepo root
npm start -w apps/lea-desktop               # dev mode: backend from this checkout
```

Dev mode uses the checkout's adapter venv and Lean workspace (`npm run setup`
must have been run). If a stack is already running on `:8001` (for example
`./start-dev.sh`), the app attaches to it instead of starting its own; force
one behaviour with `LEA_DESKTOP_ATTACH=1|0`.

To exercise the packaged behaviour without packaging:

```sh
npm run build:backend -w apps/lea-desktop   # assembles build/backend from git-tracked files (+ built UI)
npm run fetch:uv -w apps/lea-desktop        # pinned uv binary into build/bin/<arch>/
npm run start:managed -w apps/lea-desktop   # provisions into ~/Library/Application Support/Lea
```

Add `-- --user-data-dir=/some/dir` to provision somewhere disposable.

### Packaging

```sh
npm run dist:mac -w apps/lea-desktop        # arm64: release/Lea-<version>-arm64.{dmg,zip}
npm run dist:mac:x64 -w apps/lea-desktop
```

`electron-builder.yml` has the configuration. Builds are unsigned unless
`CSC_LINK`/`CSC_KEY_PASSWORD` are set; add `-c.mac.notarize=true` with
`APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD` and `APPLE_TEAM_ID` to notarize.
`.github/workflows/desktop-release.yml` does both architectures on GitHub's
macOS runners and, for a `desktop-v*` tag, publishes a release with the
`latest-mac.yml` feed that `electron-updater` reads. In-place updates only
work once builds are signed (`src/updater.mjs` runs `codesign -dv` on the
bundle at launch); an unsigned or ad-hoc-signed app is download-only: its
update button opens the DMG instead, and a feed that does not exist yet is
logged and otherwise stays quiet.

### How the managed runtime works

`scripts/build-backend.mjs` assembles the backend the same way the Dockerfile
does (`adapter/`, `prover/`, `dist/`, `apps/overleaf-extension/…`,
`packages/…`, `scripts/env.mjs`, `config/`), taking only git-tracked files, and
writes a `manifest.json` with a content id and the file list. The app bundles
that tree under `Contents/Resources/backend`.

On launch `src/provision.mjs` syncs it into `<userData>/app`: files from the
manifest are copied, files from the previous manifest that disappeared are
removed, and everything else in that tree is left alone. That is what makes
`prover/workspace/{.lake,proofs,projects}` and SafeVerify's build persistent
across updates. Then it runs `uv sync --frozen` (venv outside the tree),
ensures elan and the toolchain from `lean-toolchain`, runs the Mathlib cache
steps from `docker/prepare-mathlib.sh`, and builds SafeVerify with its
`.lake/packages` symlinked to the workspace's, as the Dockerfile does.

`src/backend.mjs` then runs `bootstrap.py`, the worker, the API (serving
`dist/`) and the companion through the repo's `scripts/process-supervisor.mjs`,
with `LEA_SHARED_DATA_DIR`, `LEA_CONFIG_FILE` and `LEA_ROOT` pointing at app
support. Both Node children (the worker's private companion worker and the
companion server) run on the app's own binary via `ELECTRON_RUN_AS_NODE`.
The four shared modules the shell needs (`process-supervisor`, `service-env`,
`env`, `dev-stack-preflight`) are imported live from `<repo>/scripts` in dev
and from `Contents/Resources/shared` when packaged.

### Smoke test

```sh
LEA_DESKTOP_SCREENSHOT=/tmp/lea.png LEA_DESKTOP_SCREENSHOT_VIEW=settings npm start -w apps/lea-desktop
```

Starts the app, waits for the UI (`LEA_DESKTOP_SCREENSHOT_DELAY_MS`, default
6000), optionally drives a native-menu navigation, writes a PNG of the window
and quits. Failures never block on a dialog in this mode. Three more variables
exercise the desktop features without a release or a human at the screen:

| Variable | Effect |
|---|---|
| `LEA_DESKTOP_FAKE_UPDATE=0.2.0` | dev only: pretends 0.2.0 is available, download-only (what an unsigned build sees). `0.2.0:inapp` pretends the in-app flow and simulates the download when the button is clicked, so all four button states can be seen. |
| `LEA_DESKTOP_SMOKE_REPORT=1` | writes the Report a Problem zip to the temp folder without the save dialog and logs `smoke diagnostics: <path>`. |
| `LEA_DESKTOP_SMOKE_NOTIFY=1` | sends a notification through the page bridge and logs `smoke notification shown: <bool>` (`false` while the window is in front, which is the rule). |
| `LEA_DESKTOP_SMOKE_UPDATE_INSTALL=1` | does what the update button does 2.5 s in; with `LEA_DESKTOP_FAKE_UPDATE=0.2.0:inapp` the button walks downloading → **Restart to update** before the screenshot. |

The log is `~/Library/Logs/Lea/lea-desktop.log`. If a packaged Lea is already
running, add `-- --user-data-dir=/some/dir` so the dev instance gets its own
single-instance lock; if an adapter already answers on `:8001` the app attaches
to it and serves either a Vite dev server on `:5173` (`npm run dev:web -w
apps/lea-standalone`, so the UI is this checkout's) or that adapter's own build.

```sh
npm test -w apps/lea-desktop                 # node --test: config redaction
```

### Files

- `src/main.mjs` — lifecycle, window, deep links, notifications and dock
  badge, Report a Problem, quit → backend stop.
- `src/layout.mjs` — paths for dev (checkout) and managed (app support) modes.
- `src/provision.mjs` — first-run / after-update provisioning with progress.
- `src/backend.mjs` — attach-or-spawn; reuses the repo's supervisor modules.
- `src/updater.mjs` — electron-updater state machine, in-app vs download-only.
- `src/diagnostics.mjs` — the Report a Problem zip; `src/redact.mjs` the pure
  config redaction it uses (`test/redact.test.mjs`).
- `src/prefs.mjs` — the shell's own preferences (`preferences.json`).
- `src/menu.mjs`, `src/preload.cjs`, `src/splash.html`.
- `scripts/build-backend.mjs`, `scripts/fetch-uv.mjs`, `scripts/launch.mjs`,
  `scripts/make-icon.py`.
- `electron-builder.yml`, `build-resources/entitlements.mac.plist`.

## Troubleshooting

- **"This UI requires the database-only API and separate Lea worker."** An
  adapter from before your last `git pull` is still running on `:8001`. Stop it
  and launch again.
- **"does not provide an export named 'app'"** when running the Electron binary
  by hand: `ELECTRON_RUN_AS_NODE=1` is set in your environment (VS Code's
  extension host sets it). `npm start` goes through `scripts/launch.mjs`, which
  strips it. The same variable makes a packaged app launched with `open -a`
  from such a shell exit within seconds and log
  `codesign_util.cc ... task_name_for_pid`, because `open` forwards the caller's
  environment; use `env -u ELECTRON_RUN_AS_NODE open -a Lea`. Finder launches
  are unaffected.
- **Provisioning failed.** The splash offers **Open logs**; the step that
  failed is named there. Mathlib and Python steps need network. Deleting
  `~/Library/Application Support/Lea/state.json` forces the Mathlib and
  SafeVerify checks to run again; deleting the whole folder starts from scratch.
- **Port 8001 busy but no healthy adapter:** `lsof -nP -iTCP:8001 -sTCP:LISTEN`.
- **A packaged app shows "A JavaScript error occurred in the main process"**
  instead of logging: that is how Electron reports an uncaught startup error
  when packaged. Run `release/mac-arm64/Lea.app/Contents/MacOS/Lea` from a
  terminal to see it on stderr.
- **Screenshot smoke test fails with `UnknownVizError`:** the window could not
  be rendered (locked or sleeping display, or hidden behind a modal). Rerun with
  the screen awake.

## Not yet

- Linux (AppImage) and Windows. The native path is POSIX-only today (venv
  paths, process groups, exclusive file locks); Windows stays on Docker.
- Signed and notarized builds (configuration is in place; needs the Apple
  Developer ID secrets).
