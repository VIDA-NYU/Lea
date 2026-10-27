<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)"
            srcset="public/assets/img/lea-wordmark-dark.svg">
    <img src="public/assets/img/lea-wordmark-light.svg" alt="Lea" width="220">
  </picture>
</p>

<p align="center">
  <a href="https://discord.gg/58ZWUXU8FM">
    <img alt="Join the Lea Discord"
         src="https://img.shields.io/badge/Discord-5865F2?style=flat-square&logo=discord&logoColor=white">
  </a>
</p>

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

The FastAPI API on **`http://127.0.0.1:8001`** reads application state from SQLite
and admits regulated commands. A separate singleton worker owns the workspace,
the vendored prover, compiler processes, recovery and publication. Restarting the
API leaves worker tasks running; worker downtime leaves database views readable.

SQLite owns the timeline, proof bytes, source publications and results. Git
records workspace provenance. Both front ends observe committed database state;
Lea cannot change their selection or navigate them. See the governing
[database boundary specification](docs/FEATURE-database-mediated-lea.md) and
[Stage 8 runtime and upgrade guide](docs/STAGE8-process-boundary.md).

## Quick start

You supply one provider API key; everything else is provisioned for you. Get a
key from whichever provider you'll use, then keep it handy for the last step:

- **OpenAI** (default) — https://platform.openai.com/api-keys
- **Anthropic** — https://console.anthropic.com/settings/keys
- **Gemini** — https://aistudio.google.com/apikey
- **Portkey gateway** — a Portkey API key from your institution's (or Portkey's
  hosted) AI gateway; see [Portkey gateways](#portkey-gateways) below

These three are the common ones, but you can use any model provider and model from
the Settings pane. [LiteLLM's provider pages](https://docs.litellm.ai/docs/providers)
give the model ID and env var for the rest — DeepSeek, for example, is
`deepseek/deepseek-v4-flash` with `DEEPSEEK_API_KEY`.

You can even use open-weight models you host yourself. For example, for a model
served by vLLM, point Lea at your server on the way in:

```sh
export HOSTED_VLLM_API_BASE=http://localhost:<PORT>/v1 && ./start-dev.sh
```

Then add the model `hosted_vllm/<your-model>` and its key in the Settings pane.

Then pick one of the two paths below.

### Option A — Docker (no local toolchain) ⭐ easiest

For a matched, reproducible installation, download **Lea.zip** from the
[latest release](https://github.com/VIDA-NYU/Lea/releases/latest). It includes
`compose.yaml` with immutable image digests, the matching Overleaf extension,
installation and upgrade instructions, and checksums. Extract it into a permanent
directory, then run `docker compose -f compose.yaml pull` and
`docker compose -f compose.yaml up`. Open http://localhost:8001 when ready.
Keep an existing installation's data directories and volumes when upgrading;
follow the included instructions to select the existing Compose project directory.

Runs the standalone UI, shared adapter, Overleaf companion, Lean, and Mathlib
using VIDA's matching prebuilt images. Docker Compose starts the services together.

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
running Docker stack; no separate local toolchain is required.

### Option B — Local install (macOS / Linux)

Needs three toolchains — **Node 22.13 or newer**, **[uv](https://docs.astral.sh/uv/)**, and
the **[Lean toolchain (elan)](https://leanprover.github.io/)**. The bundled
bootstrap installs the missing ones for you.

1. **Install Node 22.13 or newer** if you don't have it (https://nodejs.org, or `nvm install 22`).
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
   matching `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` / `PORTKEY_API_KEY`) — or skip
   this and paste it into the app's Settings pane after it starts.
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
refreshes the Lean/Mathlib cache, and initializes database-owned application policy.

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

### Portkey gateways

Models served through a [Portkey](https://portkey.ai) AI gateway — Portkey's
hosted service, or a self-hosted one such as a university's — are a first-class
provider. Portkey's endpoint is OpenAI-compatible, and Lea drives it through the
same LiteLLM layer as every other provider.

1. Save your **Portkey API key** (`PORTKEY_API_KEY`) in Settings → API keys, and
   the gateway's `/v1` root as the **Gateway URL** (`PORTKEY_BASE_URL`, e.g.
   `https://ai-gateway.example.edu/v1`). Leave the URL blank for Portkey's hosted
   service. Both can also go in the root `.env`.
2. Pick the model as `portkey/<catalog-name>`, where the catalog name is exactly
   the model string your gateway expects — usually Portkey's model-catalog form
   `@provider-slug/model`, for example
   `portkey/@vertexai-jdoe/anthropic.claude-opus-4-8`. Type it into the model
   picker; only the leading `portkey/` is stripped before the request.

Claude models take the gateway's native Anthropic Messages route rather than
its OpenAI-compatible one, so Anthropic prompt caching works through the gateway
exactly as it does against Anthropic directly (the system prompt and tool schemas
are cached once per run, and each turn re-reads the conversation so far at the
cached rate). A gateway that lacks that route falls back to the OpenAI-compatible
dialect with a warning; caching is unavailable there.

Optional routing headers a gateway may require can be supplied as environment
variables: `PORTKEY_VIRTUAL_KEY`, `PORTKEY_CONFIG`, `PORTKEY_PROVIDER` (the last
defaults to the `@provider-slug` from the catalog name on the native route). Cost
is estimated from the upstream model named inside the catalog string, cache reads
and writes priced at their own rates; a model LiteLLM cannot price reports `$0.00`.

### Model catalog updates

Both interfaces use the adapter's shared model catalog. It loads saved data
immediately and refreshes in the background once a week. Startup refreshes only
missing or stale data, so restarting an app does not reset the weekly schedule.
Configured OpenAI, Anthropic, Gemini, and Portkey accounts are queried through
their model-list endpoints; public LiteLLM metadata supplements capabilities.
Saving a provider key or gateway URL requests another refresh. Each picker also
has **Refresh models**, the last update time, and any source-specific warnings.

Failed or incomplete requests retain the last successful list. Account lists
are scoped to their credentials and endpoint, and cached under the adapter's
data directory in `model-catalog.json`, which persists in the Docker data volume.
Keys are never included in catalog responses or the cache. New entries with
unknown capabilities are labeled **Compatibility unverified**; metadata alone
does not establish account access or a successful proof run. Existing selections
and active proofs keep their chosen model when the catalog changes.

For offline/reproducible environments, set `LEA_MODEL_CATALOG_OFFLINE=true`.
This disables background discovery and serves saved or bundled data. Normal
catalog updates use metadata requests rather than inference requests.

The `Model catalog maintenance` GitHub Actions workflow checks the bundled
fallback and LiteLLM dependency updates once a week. It opens or updates a
maintenance PR after catalog regressions pass. Repository administrators must
enable **Allow GitHub Actions to create and approve pull requests** in workflow
permissions for PR creation. Runtime discovery works independently of these PRs.

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

## Set Up The Overleaf Extension

The Docker release includes the Overleaf companion. Start its Compose stack and
load the extension bundled with the same release. For a
[local install](#option-b--local-install-macos--linux), if you previously ran the
UI-only setup, add the Overleaf side from the monorepo root:

```sh
npm run setup -- --target overleaf
```

Then:

1. **Start the local stack:**

   ```sh
   ./start-dev.sh
   ```

   This starts the adapter, standalone UI, and Overleaf companion. Keep the
   terminal open while using the extension.

2. **Load the extension in Chrome:**

   - Open `chrome://extensions`.
   - Enable **Developer mode**.
   - Click **Load unpacked** and select
     `apps/overleaf-extension/extension/` from this repository.

3. **Check the extension settings.** Open **Details** for the extension, choose
   **Extension options**, and confirm:

   - Companion URL: `http://127.0.0.1:31245`
   - Local Lea repo path: the absolute path to
     `apps/lea-standalone/prover`
   - Lea API URL: `http://127.0.0.1:8001`

   Setup writes these defaults for you. The options page can also load the
   current values from the companion.

4. **Mark a theorem in Overleaf** with a `% lea:` comment:

   ```tex
   \begin{theorem}\label{thm:finite-tree-leaves}
   % lea: formalize label=finite_tree_leaves
   Every finite tree has at least two leaves.
   \end{theorem}
   ```

   The `label=` value is required and must be a valid Lean identifier: letters,
   digits, and underscores, with no leading digit. Open the document in Overleaf;
   the extension adds a badge to the marked block. Choose **Formalize** to start
   the run or **View in Lea UI** to open its proof timeline.

For dependency markers, context hints, custom LaTeX environments, and other
extension details, see the
[Overleaf extension guide](apps/overleaf-extension/README.md).

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
- `apps/lea-standalone/config/lea.local.toml` retains bootstrap connections and
  infrastructure credentials. Non-secret model, turn and spend policy is imported
  once into SQLite; Settings updates use regulated worker commands.
- The default SQLite database is
  `apps/lea-standalone/data/lea-interface.sqlite3`.
- Local proof repos live under
  `apps/lea-standalone/prover/workspace/proofs/`.
- Companion jobs, associations, batches, bounded logs and preferences live in
  SQLite. The former `apps/overleaf-extension/.overleaf-lean-stub/` directory is a
  bounded, idempotent migration source; keep its originals and the digest archives
  beside the database until migration verification is complete.
- The optional companion uses only API/UI connection settings. It needs no
  workspace path, provider keys or writable job directory. The adapter-owned
  business worker requires Node 22.13 or newer for SQLite support.

See the [Stage 6 cutover record](docs/STAGE6-database-ui-cutover.md) for publication,
migration checks, explicit limits and rollback. The [Stage 7 editor record](docs/STAGE7-queued-editor.md)
covers immutable buffers, queued language requests and SQL results; direct LSP
access is retired. The [Stage 8 runtime guide](docs/STAGE8-process-boundary.md)
covers separate API/worker ownership; [production qualification](docs/STAGE8-release-qualification.md)
records native AMD64/ARM64 images and same-data strict binary rollback.

`LEA_SHARED_DATA_DIR` can relocate the adapter data directory for local
experiments, but the normal current setup uses the standalone app's data
directory. The older `LEA_SHARED_STATE` recorder path is legacy-only for the
retired rollback flavor; on the current `/api` path, the adapter records runs
directly.
