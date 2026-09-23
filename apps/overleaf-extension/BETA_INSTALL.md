# Beta Install Guide

These instructions install the beta version of the Overleaf Lea Formalizer Chrome extension from the pinned beta tag of the LeaEcosystem monorepo. All commands are run from the monorepo root unless noted.

## What You Need

- Google Chrome
- Git
- Node.js 20 or newer
- `uv`
- Lean and Lake available on your `PATH`
- An OpenAI API key

If you are not sure whether the command-line tools are installed, continue with the steps below. The `doctor` command will report anything missing.

## 1. Clone The Beta

Replace `beta-2026-06-16` if you were given a newer beta tag.

```sh
git clone --branch beta-2026-06-16 --recurse-submodules https://github.com/darturi/UnifiedLeaEcosystem.git
cd UnifiedLeaEcosystem
```

If you already cloned without submodules, the setup command below initializes
the shared Lea submodule for you.

## 2. Run Setup

```sh
npm run setup
```

This installs workspace Node dependencies, prepares the local Lea checkout,
installs the Lea API dependencies, fetches Lean dependencies, downloads the
Mathlib cache, and writes local settings files.

The first setup may take a while because Lean and Mathlib artifacts are large.

## 3. Add Your API Key

Open the `.env` file in the repository root and replace the placeholder key:

```text
OPENAI_API_KEY=your_openai_key_here
```

with:

```text
OPENAI_API_KEY=sk-...
```

Leave these defaults unless Daniel tells you otherwise:

```text
LEA_API_BASE_URL=http://127.0.0.1:8001
LEA_PROVIDER=openai
LEA_MODEL=o4-mini
LEA_MAX_TURNS=20
LEA_JOB_TIMEOUT_SECONDS=900
```

## 4. Check Your Install

```sh
npm run doctor
```

Every required check should show a checkmark. If anything shows an `x`, fix that item and run `npm run doctor` again.

## 5. Start The Local Services

Open two terminal windows or tabs in the repository root.

In the first terminal, start the shared Lea adapter:

```sh
npm run start:adapter
```

In the second terminal, start the Overleaf companion:

```sh
npm run dev:overleaf
```

Keep both terminals running while you use the extension.

## 6. Load The Chrome Extension

1. Open Chrome.
2. Go to `chrome://extensions`.
3. Turn on Developer Mode.
4. Click Load unpacked.
5. Select the `apps/overleaf-extension/extension` folder inside this repository.
6. Open the extension options page.
7. Confirm the companion URL is:

```text
http://127.0.0.1:31245
```

8. Confirm the Lea repo path points to `apps/lea-standalone/prover`.

## 7. Use It In Overleaf

Open an Overleaf project and write theorem blocks like this:

```tex
\begin{theorem}\label{thm:finite-tree-leaves}
% lea: formalize
Every finite tree has at least two leaves.
\end{theorem}
```

Lea creates a stable internal label automatically. The `label=...` field is
deprecated but remains supported for existing documents. If used, it must be
a valid Lean identifier: letters, digits, and underscores, with no leading digit.

You can add dependencies and guidance on the same marker line:

```tex
\begin{lemma}
% lea: formalize label=main_bound uses={aux_bound, mono_lemma} context={Apply aux_bound, then monotonicity.}
The desired main bound holds.
\end{lemma}
```

Or split metadata across adjacent marker comments:

```tex
\begin{corollary}
% lea: formalize
% lea: label=main_corollary
% lea: uses={main_bound}
% lea: context={Apply the main bound directly.}
The corollary follows.
\end{corollary}
```

The older `\theorem[label=...]{...}` command syntax still works temporarily for
existing beta documents, but new documents should use `% lea:` comment markers.

## Updating To A New Beta

If Daniel gives you a new beta tag, run these commands from inside the repository:

```sh
git fetch --tags origin
git checkout beta-NEW-TAG-HERE
npm run setup
npm run doctor
```

Then restart both local services:

```sh
npm run start:adapter
npm run dev:overleaf
```

## Troubleshooting

If setup or doctor fails, send Daniel:

- the command you ran
- the full terminal output
- whether you are on macOS, Windows, or Linux
- the output of:

```sh
git rev-parse --short HEAD
git -C apps/lea-standalone/prover rev-parse --short HEAD
npm run doctor
```
