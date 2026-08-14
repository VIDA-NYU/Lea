---
title: Install Lea
description: Run Lea from VIDA's prebuilt Docker image, or use a local development install. Includes the Overleaf extension, first-proof walkthrough, and troubleshooting.
updated: 2026-08-13
---

Lea runs on your own machine: the web client, adapter, companion, prover, Lean, and Mathlib.
VIDA hosts the downloadable container image, but there is no Lea account or hosted runtime.
The only runtime data that leaves your computer is the prompt sent to your chosen model provider.

Pick **Docker** for both applications without installing Lea's local toolchains. Pick the
**local install** if you plan to develop Lea itself.

> **Note.** The Docker image downloads about 3.7 GB and occupies about 10.7 GB after extraction.
> Keep at least 20 GB free in Docker's storage for extraction. It happens once.

## Before you start

You need one API key from a model provider. You can select any model provider and model in the
Settings pane, and switch at any time.

| Provider | Where to get a key | Env var |
| --- | --- | --- |
| OpenAI | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | `OPENAI_API_KEY` |
| Anthropic | [console.anthropic.com](https://console.anthropic.com/settings/keys) | `ANTHROPIC_API_KEY` |
| Google Gemini | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | `GEMINI_API_KEY` |

> **Note.** These three are the common ones, but you can use any model provider and model from
> the Settings pane. [LiteLLM's provider pages](https://docs.litellm.ai/docs/providers) give the
> model ID and env var for the rest — DeepSeek, for example, is `deepseek/deepseek-v4-flash`
> with `DEEPSEEK_API_KEY`.

You do not need to set the environment variable if you would rather paste the key into the app.
Both work; the Settings pane is the simpler path.

You can even use open-weight models you host yourself. For example, for a model served by vLLM,
point Lea at your server on the way in:

```bash
export HOSTED_VLLM_API_BASE=http://localhost:<PORT>/v1 && ./start-dev.sh
```

Then add the model `hosted_vllm/<your-model>` and its key in the Settings pane.

**Supported platforms.** macOS (Apple Silicon and Intel) and Linux for the local install; Docker
works anywhere Docker Desktop does. Windows users should use Docker or WSL2.

> **Note.** If `git clone` reports that Git or command-line developer tools are missing, install
> them with the command for your system, let the installer finish, and then retry the clone step.

```bash
# macOS — installs Apple's Command Line Tools, including Git
xcode-select --install

# Ubuntu, Debian, or WSL2
sudo apt-get update
sudo apt-get install -y git
```

Run `git --version` to confirm that Git is available before continuing.

## Option A — Docker, no toolchain

The standalone UI, adapter, Overleaf companion, Lean, and a baked Mathlib cache run in one
container. There is no Node, Python, Lean, or Mathlib toolchain to install locally; after cloning
the repository, Docker handles the rest.

1. **Install Docker Desktop** from [docker.com](https://www.docker.com/products/docker-desktop/)
   and open it once. Wait until it reports *running*, then check:

```bash
docker --version
```

2. **Clone the repo and enter the app folder:**

```bash
git clone https://github.com/VIDA-NYU/Lea.git
cd Lea/apps/lea-standalone
```

3. **Download and start:**

```bash
docker compose pull
docker compose up
```

The VIDA image supports `linux/amd64` and `linux/arm64`; Docker selects the correct variant.

4. **Open [http://localhost:8001](http://localhost:8001).** The app boots without a key.

5. **Add your key.** Open **Settings**, choose a model, paste the matching API key, save. The key
   is validated against the provider immediately, so a typo tells you right away.

Session metadata, your key, proof repositories, project registry, and Overleaf state persist
under `apps/lea-standalone/{data,config,proofs,projects,overleaf-state}`. Stop with `Ctrl+C`;
`docker compose down` and image updates preserve these directories.

To use Overleaf, open `chrome://extensions`, enable Developer mode, choose **Load unpacked**,
and select `apps/overleaf-extension/extension`. Leave its companion URL at
`http://127.0.0.1:31245`; the container already runs that service.

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
git clone https://github.com/VIDA-NYU/Lea.git
cd Lea
```

3. **Bootstrap and provision.** For the leanest install — web client only, skipping the
   SafeVerify audit build:

```bash
./install.sh --target ui --skip-verify
```

Or the full stack, both applications with SafeVerify:

```bash
./install.sh
```

The first run downloads Mathlib and takes several minutes.

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

The extension needs the local install above, and the adapter running on `:8001`.

1. **Start the companion** (from the monorepo root):

```bash
npm run dev:overleaf
```

It listens on `http://127.0.0.1:31245` and expects the adapter at `LEA_API_BASE_URL`.

2. **Load the extension in Chrome:**

- Open `chrome://extensions`
- Enable **Developer Mode**
- Choose **Load unpacked** and select `apps/overleaf-extension/extension/`

3. **Check the options page.** Open the extension's options and confirm:

- Companion URL — `http://127.0.0.1:31245`
- Lea repo path — `apps/lea-standalone/prover`
- Lea UI base URL — `http://localhost:5173`

4. **Mark a theorem** in your Overleaf source with a `% lea:` comment:

```tex
\begin{theorem}\label{thm:finite-tree-leaves}
% lea: formalize label=finite_tree_leaves
Every finite tree has at least two leaves.
\end{theorem}
```

The `label=` value becomes the Overleaf identifier and the fallback Lean declaration name, so it
must be a valid Lean identifier: letters, digits and underscores, no leading digit.

A badge appears on the marked block. **Formalize** starts an autonomous run; **View in Lea UI**
deep-links to the same session in the web client.

### Declaring dependencies and hints

`uses={…}` names earlier theorems this one builds on, by their Overleaf labels — not their Lean
names. Each referenced theorem must already be formalized, or at least have a saved stub, before
the run starts; otherwise the extension blocks the run and tells you which one to do first.

`context={…}` passes natural-language guidance for this theorem: proof strategy, notation, a
lemma worth trying, or a warning about how to read the statement.

```tex
\begin{proposition}\label{prop:main-bound}
% lea: formalize label=main_bound uses={auxiliary_bound, monotonicity_lemma} context={Start from auxiliary_bound, then apply monotonicity_lemma to compare the two sides.}
The desired main bound holds.
\end{proposition}
```

Marked blocks are recognized inside `theorem`, `lemma`, `proposition`, `corollary` and
`definition` environments. For a custom environment — `claim`, `conjecture`, a journal-specific
name — use an inline tag instead, which states its own kind:

```tex
\usepackage{lea-tags}
...
\begin{claim}\label{clm:even-square}
\leatheorem{label=even_square, uses={even_def}, context={Use the parity definition first.}}
If $n$ is even, then $n^2$ is even.
\end{claim}
```

`lea-tags.sty` is available from the extension's options page, either as a download or as a
copy-paste preamble snippet that needs no `\usepackage` line. Use a comment marker *or* a tag per
block, never both — the extension reports that as a `duplicate_marker` error.

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
[open an issue](https://github.com/VIDA-NYU/Lea/issues) — a transcript link and the
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
