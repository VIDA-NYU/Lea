---
title: Install Lea
description: Get Lea running on your machine — Docker in one command, or a local install with the Overleaf extension. Includes first-proof walkthrough and troubleshooting.
updated: 2026-08-11
---

Lea runs entirely on your own machine: the web client, the adapter, the prover, Lean and
Mathlib. Nothing is hosted by us, and there is no account to create. The only thing that leaves
your computer is the prompt you send to whichever model provider you choose.

Pick **Docker** if you want the standalone web client with nothing else to install. Pick the
**local install** if you want the Overleaf extension too, or if you plan to work on Lea itself.

> **Note.** First-time setup downloads Lean and Mathlib. That is several gigabytes and can take
> a while on a slow connection. It happens once.

## Before you start

You need one API key from a model provider. Lea ships with 13 models across three families and
you can switch between them at any time from the Settings pane.

| Provider | Where to get a key | Env var |
| --- | --- | --- |
| OpenAI | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | `OPENAI_API_KEY` |
| Anthropic | [console.anthropic.com](https://console.anthropic.com/settings/keys) | `ANTHROPIC_API_KEY` |
| Google Gemini | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | `GEMINI_API_KEY` |

You do not need to set the environment variable if you would rather paste the key into the app.
Both work; the Settings pane is the simpler path.

**Supported platforms.** macOS (Apple Silicon and Intel) and Linux for the local install; Docker
works anywhere Docker Desktop does. Windows users should use Docker or WSL2.

## Option A — Docker, no toolchain

The whole standalone app in one container: UI, adapter, Lean and a baked Mathlib cache. The only
thing you install is Docker itself.

1. **Install Docker Desktop** from [docker.com](https://www.docker.com/products/docker-desktop/)
   and open it once. Wait until it reports *running*, then check:

```bash
docker --version
```

2. **Clone the repo and enter the app folder:**

```bash
git clone https://github.com/darturi/UnifiedLeaEcosystem.git
cd UnifiedLeaEcosystem/apps/lea-standalone
```

3. **Build and start:**

```bash
docker compose build    # slow the first time — it downloads and bakes Mathlib
docker compose up       # every later start jumps straight to here
```

The build targets your machine's own CPU, so there are no architecture flags to pass.

4. **Open [http://localhost:8001](http://localhost:8001).** The app boots without a key.

5. **Add your key.** Open **Settings**, choose a model, paste the matching API key, save. The key
   is validated against the provider immediately, so a typo tells you right away.

Sessions, proofs and your key persist on your machine under `apps/lea-standalone/data` and
`apps/lea-standalone/config`, so they survive restarts. Stop with `Ctrl+C`, or `docker compose down`.

> **Note.** Docker covers the standalone web client only. The Overleaf extension side-loads a
> Chrome extension and needs the local install below.

On macOS there is also a double-clickable `start-lea.command` in `apps/lea-standalone/` that does
all of the above and opens your browser — useful for handing the app to someone who does not live
in a terminal.

## Option B — Local install

This path gives you both applications and a live editable checkout. It needs three toolchains:
**Node 22**, **[uv](https://docs.astral.sh/uv/)** and the **[Lean toolchain (elan)](https://leanprover.github.io/)**.
The bundled bootstrap installs `uv` and `elan` for you if they are missing.

1. **Install Node 22** if you do not have it — [nodejs.org](https://nodejs.org), or `nvm install 22`:

```bash
node --version   # want v22 or newer
```

2. **Clone the repo:**

```bash
git clone https://github.com/darturi/UnifiedLeaEcosystem.git
cd UnifiedLeaEcosystem
```

3. **Bootstrap and provision.** Install the full stack: the standalone web client, Overleaf companion, and SafeVerify audit build:

```bash
./install.sh
```

The first run downloads Mathlib and takes several minutes.

> **Note.** For a faster UI-only install, use `./install.sh --target ui --skip-verify`.
> `--target ui` skips Overleaf setup, so this install does not support the Overleaf extension.
> `--skip-verify` skips the SafeVerify audit build, leaving `/verify` unavailable.
> Re-run `./install.sh` without these flags later to enable both.

4. **Add your key** to the root `.env` (`OPENAI_API_KEY=…`, or the matching `ANTHROPIC_API_KEY` /
   `GEMINI_API_KEY`) — or skip this and paste it into Settings once the app is running.

5. **Start everything:**

```bash
./start-dev.sh          # keeps previous sessions and proofs
./start-dev.sh --fresh  # clears sessions, proofs and logs first
```

Open [http://localhost:5173](http://localhost:5173). `Ctrl+C` stops the whole stack.

### If you already have the toolchains

Skip `install.sh` and provision directly. A preflight check runs first and prints exact install
commands for anything missing:

```bash
npm run setup                      # everything
npm run setup -- --target ui       # standalone web client only
npm run setup -- --target overleaf # Overleaf companion only
npm run setup -- --skip-verify     # skip SafeVerify's second Mathlib build
```

`--skip-verify` is worth understanding: SafeVerify is the audit that turns *proved* into
*verified*, and building it downloads Mathlib a second time. Skipping it makes the install
markedly faster and leaves `/verify` reporting "unavailable"; nothing else changes. You can
build it later by re-running setup without the flag.

### What runs where

| Service | Port | Started by |
| --- | --- | --- |
| FastAPI adapter — the shared backend | 8001 | `npm run start:adapter`, or `dev:ui` |
| Vite dev server — the web client | 5173 | `npm run dev:ui` |
| Overleaf companion | 31245 | `npm run dev:overleaf` |

There is no separate prover server. The adapter imports the prover in-process, which is why
there is one backend and not two.

## Overleaf extension

The extension needs the adapter on `:8001` and the companion on `:31245`.
If you followed the local-install steps and started `./start-dev.sh`, both are already running.
Keep Lea running while using the extension.

> **Note.** If you started the web client separately with `npm run dev:ui`, start the companion
> with `npm run dev:overleaf` from the monorepo root in another terminal.

**Load the extension in Chrome:**

1. Open `chrome://extensions`.
2. Enable **Developer Mode**.
3. Choose **Load unpacked** and select `apps/overleaf-extension/extension/`.

## Your first proof

With the app open, paste a statement into the composer and send it:

```text
Prove that the square root of 2 is irrational.
```

What to watch for:

- The **transcript** on the left narrates what the agent is doing and why.
- The **canvas** on the right shows the Lean file as it is written, step by step. Use the step
  arrows to walk back through earlier versions.
- A green `lean_check: 0 errors` means the file elaborates — the *proved* state.
- **Run SafeVerify** runs the audit that turns that into *verified*. Until it clears, the
  blueprint shows the node with a dashed outline and the label `audit pending`.

You can edit the Lean file by hand at any point and hand it back. The ledger records who wrote
each step, so the division of labour stays visible afterwards.

### Working in a project

A **project** is a workspace that outlives a single proof. It fixes a Lean namespace and carries
three documents the agent reads on every run:

- **Instructions** — your rules and conventions for this development.
- **Memory** — durable facts, what worked, what failed, dead ends to avoid. Both you and Lea
  append to it.
- **Blueprint** — the decomposition into interdependent lemmas. A node becomes *ready* when
  everything it depends on is discharged; its status is resolved from the latest Lean verdict,
  never stored separately.

Use a project whenever you are formalizing more than one theorem, and always for a paper.

## Keeping it working

```bash
npm run doctor              # health-check both applications
npm run update-lean-deps    # lake update + refresh the Mathlib cache
npm run reset:local         # clear local run state; keeps installed dependencies
npm run reset:local -- --dry-run   # preview exactly what a reset would remove
```

Run `doctor` first whenever something behaves oddly. It checks ports, toolchains, the Python
environment and the Lean workspace, and prints what to fix.

> **Warning.** `reset:local` deletes local proofs and session history. Since v2.3 the database
> owns your proof content — resetting is not a harmless "clear the cache". Use `--dry-run` first.

## Troubleshooting

**`docker compose up` fails complaining about a missing env file.** Older checkouts required
`lea.env` to exist before the container would start. Pull the latest, or create an empty
`lea.env` — the container boots keyless by design and asks for the key in Settings.

**The build fails at `lake build` with `no such file or directory: proofs/Lea.lean`.** The
workspace's default Lake target expects a root module in `proofs/`, which is where the agent
writes at runtime and is therefore empty at build time. The image build no longer runs
`lake build` for this reason; if you hit it in a custom build, run `lake exe cache get` instead —
prebuilt Mathlib oleans are all that is needed.

**Mathlib is downloading from source and taking forever.** It should not compile from source.
`lake exe cache get` fetches prebuilt oleans, and they are architecture-independent, so the same
cache serves arm64 containers. Check your network can reach the Mathlib cache, then re-run
`npm run update-lean-deps`.

**`/verify` says "unavailable".** You installed with `--skip-verify`. Re-run `npm run setup`
without the flag to build SafeVerify.

**The first `lean_check` takes ~90 seconds, then later ones are fast.** Expected. Lea keeps a
Lean language server warm per Lake root; the first check pays for loading Mathlib's oleans and
later in-place edits reuse it. If every check is slow, the daemon is probably being restarted —
check for `LEA_DISABLE_LSP=1` in your environment.

**The extension shows no badges on an Overleaf document.** Confirm the companion is running on
`:31245`, that the adapter is up on `:8001`, and that your marker sits inside a recognized
environment. A tag command with no `\usepackage{lea-tags}` reports `tag_package_not_loaded` —
fix that before compiling, since an undefined command fails the whole Overleaf build.

**A formalization is blocked on an upstream theorem.** That is `uses={…}` doing its job: the
named theorem is not formalized yet. Do it first, or drop it from `uses`.

Still stuck? Ask in [Discord](https://discord.gg/CtEJvUTjm) or
[open an issue](https://github.com/darturi/UnifiedLeaEcosystem/issues) — a transcript link and the
output of `npm run doctor` make it much faster to help.

## Where things live

| What | Path |
| --- | --- |
| Shared configuration | root `.env` |
| Adapter runtime config, including keys | `apps/lea-standalone/config/lea.local.toml` |
| Sessions, timeline and proof content | `apps/lea-standalone/data/lea-interface.sqlite3` |
| Lean workspace and generated proofs | `apps/lea-standalone/prover/workspace/proofs/` |
| Project records | `apps/lea-standalone/prover/workspace/projects/` |
| Overleaf companion logs and settings | `apps/overleaf-extension/.overleaf-lean-stub/` |

Shell-exported variables always win over `.env`. The config file is the source of truth for
model, turn limit, spend cap and provider keys edited through the Settings pane.

## Uninstalling

Everything Lea creates lives inside the checkout, plus the Lean toolchain in `~/.elan` and the
`uv` cache. Delete the repository directory to remove the applications, their data and the
generated proofs. Remove `~/.elan` as well if you have no other Lean projects.
