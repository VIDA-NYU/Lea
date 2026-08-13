# LeaEcosystem

LeaEcosystem is an npm-workspaces monorepo that wraps
[Lea](https://github.com/darturi/lea-prover), a Lean 4 theorem-proving agent,
behind two local front ends and one shared backend.

- **`apps/lea-standalone/`** (`lea-interface`) — the React + Vite UI and the
  FastAPI adapter that drives Lea, streams proof progress, and persists the
  proof timeline.
- **`apps/overleaf-extension/`** (`overleaf-lean-stub-mvp`) — a Chrome extension
  plus local Node companion that formalizes labeled theorem blocks from an
  Overleaf document.
- **`packages/lea-model-catalog/`** — the shared provider/model catalog consumed
  by both apps.

The architectural center is the FastAPI adapter on **`http://127.0.0.1:8001`**.
It imports the vendored prover at `apps/lea-standalone/prover/` in-process and
maps the prover's typed events onto the browser's SSE stream. There is no
separate Lea API server to start, and no separate prover checkout to initialize.

The adapter owns the persistent local timeline: SQLite stores sessions, runs,
messages, usage, and code-step metadata; git stores proof content under the
prover workspace. Overleaf-created formalizations go through the same adapter, so
they appear in the standalone UI and can be opened with a `?session=<id>` link.

## Website

The public site (landing page, install guide, blog) lives in
[`public/`](public/README.md) and is a dependency-free static site.

```sh
npm run site:dev     # preview at http://localhost:4321, rebuilding on save
npm run site:build   # write public/dist
npm run site:test    # unit tests for the Markdown renderer
```

Adding a blog post means adding one Markdown file to `public/content/blog/`.
Pushing to `main` publishes to GitHub Pages via `.github/workflows/site.yml`.

## Quick start

You supply one provider API key; everything else is provisioned for you. Get a
key from whichever provider you'll use, then keep it handy for the last step:

- **OpenAI** (default) — https://platform.openai.com/api-keys
- **Anthropic** — https://console.anthropic.com/settings/keys
- **Gemini** — https://aistudio.google.com/apikey

Then pick one of the two paths below.

### Option A — Docker (no local toolchain) ⭐ easiest

Runs the standalone UI, shared adapter, Overleaf companion, Lean, and Mathlib in
one prebuilt container published by VIDA. The only thing you install is Docker.

1. **Install Docker Desktop** and start it: https://www.docker.com/products/docker-desktop/
   (verify with `docker --version`). Keep at least **20 GB free** in Docker's
   storage: the image downloads about 3.7 GB and occupies about 10.7 GB after
   extraction because Lean and Mathlib are included.
2. **Clone this repo and enter the app folder:**
   ```sh
   git clone https://github.com/VIDA-NYU/Lea.git
   cd Lea/apps/lea-standalone
   ```
3. **Download and start it:**
   ```sh
   docker compose pull        # downloads ghcr.io/vida-nyu/leaui:main
   docker compose up
   ```
   The image supports Intel/AMD and ARM64 machines, so no architecture flags are needed.
4. **Open** http://localhost:8001 in your browser.
5. **Add your key:** open the **Settings** pane and paste in your API key (no key
   is needed to boot). You're ready to prove.

Session metadata, your saved key, proof repositories, project registry, and
Overleaf job state persist on your machine under
`apps/lea-standalone/{data,config,proofs,projects,overleaf-state}`. Stop with
`Ctrl+C`; `docker compose down` and image updates preserve these directories.

To use Overleaf, load `apps/overleaf-extension/extension` as an unpacked Chrome
extension. Its default companion URL (`http://127.0.0.1:31245`) points to the
same running container; no separate local toolchain is required.

### Option B — Local install (macOS / Linux)

Needs three toolchains — **Node 22**, **[uv](https://docs.astral.sh/uv/)**, and
the **[Lean toolchain (elan)](https://leanprover.github.io/)**. The bundled
bootstrap installs the missing ones for you.

1. **Install Node 22** if you don't have it (https://nodejs.org, or `nvm install 22`).
   Check with `node --version`.
2. **Clone this repo:**
   ```sh
   git clone https://github.com/VIDA-NYU/Lea.git
   cd Lea
   ```
3. **Bootstrap + provision** (installs `uv` and `elan` if absent, then sets
   everything up). For the leanest test install, use the UI-only, no-SafeVerify
   variant:
   ```sh
   ./install.sh --target ui --skip-verify
   ```
   Or `./install.sh` alone for the full stack (UI + Overleaf, with SafeVerify).
   The first run downloads Mathlib and can take several minutes.
4. **Add your key** to the root `.env` file (set `OPENAI_API_KEY=...`, or the
   matching `ANTHROPIC_API_KEY` / `GEMINI_API_KEY`) — or skip this and paste it
   into the app's Settings pane after it starts.
5. **Start the app:**
   ```sh
   ./start-dev.sh
   ```
   Then open http://localhost:5173. Stop everything with `Ctrl+C`.

Already have Node, `uv`, and `elan`? You can skip `install.sh` and run
`npm run setup` directly (see [Setup](#setup)) — it runs a **preflight check**
first and prints exact install commands for anything missing.

> `--target ui --skip-verify` is the leanest install: it skips the Overleaf side
> and the second Mathlib download that SafeVerify's `/verify` audit needs (the
> audit then reports "unavailable"; nothing else is affected). Run
> `npm run doctor` any time to health-check the install.

## Setup

Run setup from the monorepo root:

```sh
npm run setup
```

This checks prerequisites, installs workspace Node dependencies, writes root
`.env` defaults, builds the standalone adapter/prover Python environments,
refreshes the Lean/Mathlib cache, and writes Overleaf companion settings.

To provision only one side, or trim the install:

```sh
npm run setup -- --target ui       # standalone UI only
npm run setup -- --target overleaf # Overleaf companion only
npm run setup -- --skip-verify     # skip SafeVerify (second Mathlib build)
```

To refresh Lean dependencies and the Mathlib cache:

```sh
npm run update-lean-deps
```

After setup, add provider keys either in the standalone Settings page or in the
monorepo root `.env`. The default `.env` points both apps at the adapter:

```text
LEA_ROOT=apps/lea-standalone/prover
LEA_API_BASE_URL=http://127.0.0.1:8001
LEA_API_FLAVOR=api
LEA_UI_BASE_URL=http://localhost:5173
OVERLEAF_COMPANION_URL=http://127.0.0.1:31245
```

## Running

Start only the shared backend:

```sh
npm run start:adapter
```

Start the standalone UI in development mode:

```sh
npm run dev:ui
```

`dev:ui` starts the adapter on `:8001` and Vite on `:5173`.

Start the Overleaf companion:

```sh
npm run dev:overleaf
```

The companion listens on `http://127.0.0.1:31245` and expects the adapter to be
reachable at `LEA_API_BASE_URL`.

## Common Commands

```sh
npm run doctor       # health-check both apps
npm test             # Overleaf tests, then standalone frontend tests
npm run reset:local  # clear local run state; keeps installed dependencies
```

Preview a reset:

```sh
npm run reset:local -- --dry-run
```

Adapter Python tests are not included in root `npm test`. Run them from the
adapter directory:

```sh
cd apps/lea-standalone/adapter
./.venv/bin/python -m pytest
```

## Data And Configuration

- Root `.env` supplies shared defaults for both apps. Shell-exported values still
  win.
- `apps/lea-standalone/config/lea.local.toml` is the adapter's runtime config and
  the source of truth for model, turn limit, spend cap, and provider keys edited
  through the standalone Settings page.
- The default SQLite database is
  `apps/lea-standalone/data/lea-interface.sqlite3`.
- Local proof repos live under
  `apps/lea-standalone/prover/workspace/proofs/`.
- Overleaf companion job logs and local settings live under
  `apps/overleaf-extension/.overleaf-lean-stub/`.

`LEA_SHARED_DATA_DIR` can relocate the adapter data directory for local
experiments, but the normal current setup uses the standalone app's data
directory. The older `LEA_SHARED_STATE` recorder path is legacy-only for the
retired rollback flavor; on the current `/api` path, the adapter records runs
directly.
