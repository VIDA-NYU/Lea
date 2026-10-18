# Lea Standalone

`apps/lea-standalone` is the canonical local Lea UI and backend. It contains:

- a React + Vite web app on `http://localhost:5173`;
- a FastAPI adapter on `http://127.0.0.1:8001`;
- a separate singleton workspace worker, which imports the vendored prover at
  `prover/` as a Python library.

The adapter admits typed SQL commands; its workers drive the prover through
`lea.interface.run_events(...)`. Timeline, progress, source publications and
application views are read from SQLite. Workspace changes and checks return
receipts; the browser observes their committed outcomes. The live Monaco editor
publishes immutable buffers and uses queued diagnostics, hover, completion and
plain goals. Results are read from SQL; direct LSP access is rejected. Interactive
widgets, navigation and code actions are unavailable in this cutover.

See the [Stage 6 delivery record](../../docs/STAGE6-database-ui-cutover.md) for
companion import, limits, qualification and rollback. The
[Stage 7 editor record](../../docs/STAGE7-queued-editor.md) covers language features,
queue limits and supported-runtime qualification.
The [Stage 8 runtime guide](../../docs/STAGE8-process-boundary.md) governs process
ownership, bootstrap, strict API isolation, health and compatible rollback.

## Run The VIDA Image

From this directory:

```sh
docker compose pull
docker compose up
```

Compose expects matching `leaui-api`, `leaui` (worker) and `leaui-companion` images.
The migration job runs before API and worker startup. The API image has no
workspace/compiler/config tree and mounts only the database directory. These
Stage 8 image targets require an explicit release publication before pulling a
hosted version; build the checkout below for development. Open `http://localhost:8001`;
the companion is exposed on `:31245`. Runtime data and proofs persist in the
host directories declared by `docker-compose.yml`.

Keep the `lea-workspace-ownership` Docker local volume as well. Worker and
migration share its enforced lock and persistent identity. Every deployment that
shares a database/workspace must use the same ownership volume; independent
workspaces can set `LEA_WORKSPACE_OWNERSHIP_VOLUME` to distinct names. Host bind
mounts that do not enforce locks are rejected. See the
[production qualification and rollback guide](../../docs/STAGE8-release-qualification.md).

To build the same image from the checkout instead, use:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

## First-Time Setup

From the monorepo root, the recommended setup is:

```sh
npm run setup
```

For this app only:

```sh
npm run setup -- --target ui
```

Manual app-local setup is:

```sh
npm install
cp config/lea.local.example.toml config/lea.local.toml
npm run setup:api
```

`setup:api` creates the adapter virtualenv, installs the editable prover package,
sets up the prover environment, and downloads the pinned Mathlib cache so the
first proof check does not compile Mathlib from scratch.

## Development

Start the adapter and web UI together:

```sh
npm run dev
```

The launcher:

- checks local prerequisites;
- starts the FastAPI adapter on `http://127.0.0.1:8001`;
- waits for `/api/health`;
- starts Vite with `--host 0.0.0.0` on `http://localhost:5173`;
- stops both child processes on `Ctrl+C`.

To start the API/worker backend pair without Vite:

```sh
npm run start:api  # FastAPI adapter only
npm run dev:web    # Vite web server only
```

From the monorepo root, `npm run start:adapter` is equivalent to this app's
`npm run start:api`.

## Configuration

The adapter reads bootstrap connections and credentials from
`config/lea.local.toml`. Non-secret policy is imported once into SQLite; subsequent
Settings changes use regulated commands. Editing the old TOML model/limit fields
after import does not replace database policy. Credentials remain protected
infrastructure and appear only as redacted status in SQL views.

Bootstrap example:

```toml
lea_root = "prover"
model = "gemini/gemini-3.1-pro-preview"
max_turns = 20
max_spend_usd = 25.00
narrate_tool_steps = true
openai_api_key = ""
anthropic_api_key = ""
google_api_key = ""
# Portkey AI gateway: model = "portkey/@provider-slug/model"
PORTKEY_API_KEY = ""
PORTKEY_BASE_URL = "https://your-gateway.example.edu/v1"   # omit for Portkey's hosted service
```

Provider keys are exported into the adapter process environment when config is
loaded, because LiteLLM reads provider credentials from environment variables.
The returned `LeaConfig` object does not carry secrets.

The current interactive UI gates impactful tools (`bash`, `write_file`,
`edit_file`) with allow/deny/always-for-session prompts. Overleaf-originated runs
are marked autonomous and skip this gate.

## Using The UI

- Chat messages and status chips show the prover's live reasoning, tool calls,
  checks, and terminal status.
- The proof canvas shows database-backed file snapshots from each `code_step`; use the
  stepper to inspect earlier proof states.
- Human edits in the canvas write to the same working tree, commit as user-authored
  steps, and can include an edit note for the next run.
- `lean_check` checks the current file without starting an agent run and
  back-fills the latest step's verdict.
- SafeVerify audits a finished proof with kernel replay and axiom checks.
- Projects create shared proof repos under `prover/workspace/proofs/Lea/<Project>`
  so sessions can import sibling lemmas.
- Project Instructions, Memory, Blueprint, uploads and file views use SQL content
  and worker observations; workers materialize managed files in the project repo.
- The Stats page reads persisted usage and cost from SQLite.

## Health Check

Run:

```sh
npm run doctor
```

Manual endpoint checks:

```sh
curl http://127.0.0.1:8001/api/health
curl http://127.0.0.1:8001/api/sessions
curl -I http://127.0.0.1:5173/
```

If `prover/` is missing, the checkout is incomplete. If a run appears to sit on
`lean_check` for minutes, run `npm run doctor`; a failing Mathlib cache check
usually means `npm run setup:api` needs to finish successfully.

## Data

- App database: `data/lea-interface.sqlite3`
- Adapter event/log data: `data/`
- Local adapter config: `config/lea.local.toml`
- Vendored prover: `prover/`
- Proof repos and project repos: `prover/workspace/proofs/`
- Overleaf LaTeX mirror: `prover/workspace/context/overleaf/`

Do not commit `config/lea.local.toml`; rotate any API key that is pasted into
logs or chat.

## Tests

Frontend unit tests:

```sh
npm run test:frontend
```

TypeScript check:

```sh
npm run typecheck
```

Adapter tests:

```sh
cd adapter
./.venv/bin/python -m pytest
```

## Upgrading An Existing Checkout (database migrations)

The database is **versioned with real Alembic migrations** and is **not** disposable —
since v2.3 SQL owns your proof content, so "just reset it" means "delete your proofs."
Do **not** `npm run reset:local` to get past a schema change; that wipes sessions and
proofs.

Instead, just pull and start the adapter — pending migrations apply automatically on
startup, after snapshotting your DB first:

```sh
git pull
npm run setup            # only if dependencies changed; safe to run anyway
npm run start:adapter    # (or npm run dev:ui) — migrations run on startup
```

Full details — how it works, verifying the revision, manual upgrades, rollback from a
snapshot, and the revision history — are in **[docs/MIGRATIONS.md](docs/MIGRATIONS.md)**.
