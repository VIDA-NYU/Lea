# Overleaf Lea Formalizer

This app is the Overleaf side of the LeaEcosystem monorepo. It provides a Chrome
extension plus a local Node companion that sends labeled theorem blocks from
Overleaf to the shared Lea adapter.

The default path is:

```text
Overleaf page -> Chrome extension -> companion (:31245) -> FastAPI adapter (:8001)
```

The adapter then drives the vendored prover at `apps/lea-standalone/prover/`
in-process. The companion does not start its own prover backend.

For the VIDA-hosted Docker installation and Chrome side-load steps, see
[BETA_INSTALL.md](BETA_INSTALL.md). The image already runs both the adapter and
the companion; only the unpacked extension is loaded separately in Chrome.

## Pieces

- `extension/`: Chrome MV3 extension injected into Overleaf.
- `companion/`: local HTTP service that validates theorem payloads, starts Lea
  runs, tracks jobs, and reads adapter-persisted usage.
- `shared/`: theorem parser and Lean path helpers used by the companion and tests.
- `tests/`: Node test suite.

Generated Lean work happens under:

```text
apps/lea-standalone/prover/workspace/
```

## Theorem Syntax

The extension looks for `% lea:` comments inside ordinary theorem-like LaTeX
environments. Mark only the blocks you want Lea to formalize:

```tex
\begin{theorem}\label{thm:finite-tree-leaves}
% lea: formalize label=finite_tree_leaves
Every finite tree has at least two leaves.
\end{theorem}
```

The `label=...` value is required. It is used as the Overleaf theorem identifier
and fallback Lean declaration name. It must be a valid Lean identifier: letters,
digits, and underscores, with no leading digit.

Initially supported environments are `theorem`, `lemma`, `proposition`, and
`corollary`. Unmarked environments are ignored.

### `uses={...}`

Use `uses={...}` when a theorem should depend on earlier theorems from the same
Overleaf project. Values are Overleaf labels, not Lean theorem names. Each
referenced theorem must already be formalized, or at least have a saved sorry
stub from an older build, before Lea starts the new run.

```tex
\begin{lemma}
% lea: formalize label=my_next_theorem uses={my_prior_theorem, another_prior_theorem}
Prove this using earlier project results.
\end{lemma}
```

If a referenced label cannot be resolved, the extension blocks the run and
reports which theorem needs to be formalized first.

### `context={...}`

Use `context={...}` to pass natural-language guidance to Lea for this theorem:
proof strategy, notation hints, suggested lemmas, or warnings about how to
interpret the statement.

```tex
\begin{theorem}
% lea: formalize label=even_square context={Use the assumption that n is even, rewrite n as 2 * k, then use ring_nf.}
If n is even, then n^2 is even.
\end{theorem}
```

You can combine all fields:

```tex
\begin{proposition}\label{prop:main-bound}
% lea: formalize label=main_bound uses={auxiliary_bound, monotonicity_lemma} context={Start from auxiliary_bound, then apply monotonicity_lemma to compare the two sides.}
The desired main bound holds.
\end{proposition}
```

Multiline marker metadata is also supported when the `% lea:` lines are adjacent:

```tex
\begin{corollary}
% lea: formalize
% lea: label=main_corollary
% lea: uses={main_bound}
% lea: context={Apply the main bound directly.}
The corollary follows.
\end{corollary}
```

### Deprecated legacy syntax

The older custom command syntax still works temporarily for existing beta
documents, but new documents should use `% lea:` comment markers:

```tex
\theorem[label=my_theorem_name]{
  Every finite tree has at least two leaves.
}
```

## Inline Tag Syntax

`% lea:` comments only work inside `theorem`, `lemma`, `proposition`,
`corollary`, and `definition` environments. If your document defines its own
theorem-like environment (`claim`, `conjecture`, `fact`, a journal-specific
name, ...), use an inline Lea tag instead. Tags are invisible LaTeX commands
from a small package, `lea-tags.sty`, available from the extension's options
page (download link or a copy-paste preamble snippet -- no `\usepackage` line
needed if you use the snippet).

```tex
\usepackage{lea-tags}
...
\begin{claim}\label{clm:even-square}
\leatheorem{label=even_square, uses={even_def}, context={Use the parity definition first.}}
If $n$ is even, then $n^2$ is even.
\end{claim}
```

`claim` is not a recognized environment name, but `\leatheorem{...}` states
its own kind directly, so the extension still finds and formalizes it. The
`label=`/`uses=`/`context=` fields work exactly like the comment-marker
fields above.

Named tag commands: `\leatheorem`, `\lealemma`, `\leaproposition`,
`\leacorollary` (all theorem-kind), and `\leadefinition` (definition-kind).
There's also a generic form for explicit kind control:

```tex
\begin{conjecture}
\lea{kind=definition, label=even_nat, context={Use Nat parity, not Int parity.}}
A natural number $n$ is even if there exists $k$ with $n = 2k$.
\end{conjecture}
```

### Standalone tags: no enclosing block at all

Give a tag a second argument and it needs no enclosing environment -- the
argument both renders as the statement and is what's sent to Lea:

```tex
\leatheorem{label=pythagorean, uses={right_triangle}}
{In a right triangle, the square of the hypotenuse equals the sum of the
squares of the other two sides.}
```

The two arguments don't need to be on the same line -- whitespace (including
blank lines) between them is fine, matching ordinary TeX argument scanning.
One caveat that follows directly from that: a tag with **no** intended body,
placed with nothing but whitespace before an unrelated `{...}` group, will
have that group absorbed as its body. If you don't want a second argument,
keep something other than blank lines between the tag and the next braced
group (the normal case of actual prose in between is never a problem).

If you load `lea-tags.sty` but the extension finds a tag command with nothing
defining it (a missing or misspelled `\usepackage`), it reports a
`tag_package_not_loaded` warning -- check this before compiling, since an
undefined tag command will otherwise fail the whole Overleaf build.

A comment marker and a tag inside the same environment is an error
(`duplicate_marker`); use one or the other per target. Both syntaxes are
permanently supported side by side -- comments stay the simplest path for
ordinary `theorem`/`lemma`/`definition` blocks, tags exist for everything
else.

## Setup

Run setup from the monorepo root:

```sh
npm run setup
```

For only the Overleaf app:

```sh
npm run setup -- --target overleaf
```

The unified setup installs workspace Node dependencies, delegates adapter/prover
setup to `apps/lea-standalone`, downloads the Lean/Mathlib cache, writes root
`.env` defaults, and writes `.overleaf-lean-stub/settings.json`.

Current default environment values:

```text
LEA_ROOT=apps/lea-standalone/prover
LEA_API_BASE_URL=http://127.0.0.1:8001
LEA_UI_BASE_URL=http://localhost:5173
OVERLEAF_COMPANION_URL=http://127.0.0.1:31245
```

Add provider keys in the standalone Settings page, the extension settings UI, or
the monorepo root `.env`:

```text
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
GOOGLE_API_KEY=
LEA_JOB_TIMEOUT_SECONDS=900
```

Check setup:

```sh
npm run doctor
```

Fix anything marked as failing before starting a formalization.

## Running

Start the shared adapter from the monorepo root:

```sh
npm run start:adapter
```

Or, from `apps/overleaf-extension`, use the workspace helper:

```sh
npm run start:lea-api
```

Then start the companion:

```sh
npm run dev:overleaf       # from the monorepo root
npm start                  # from apps/overleaf-extension
```

The companion listens on:

```text
http://127.0.0.1:31245
```

## Configure The Extension

1. Open `chrome://extensions`.
2. Enable Developer Mode.
3. Load the unpacked extension from `apps/overleaf-extension/extension/`.
4. Open the extension options page.
5. Confirm the companion URL is `http://127.0.0.1:31245`.
6. Confirm the Lea repo path points to `apps/lea-standalone/prover`.
7. Confirm the Lea UI base URL is `http://localhost:5173`.

## Workflow

When a theorem is visible in Overleaf, the extension adds status badges and a
popover. The main action is **Formalize**, which asks the companion to create an
autonomous adapter run. The adapter records the session, run, messages, code
steps, and usage, so **View in Lea UI** can deep-link to the standalone proof
timeline.

## Generated Lean Work

Project records and proof files are written in the standalone prover workspace:

```text
apps/lea-standalone/prover/workspace/projects/
apps/lea-standalone/prover/workspace/proofs/Lea/<ProjectName>/
```

Optional mirrored Overleaf context is written under:

```text
apps/lea-standalone/prover/workspace/context/overleaf/<ProjectSlug>/
  manifest.json
  tex/active.tex
```

Formalization prompts include only the manifest path. Lea can open the mirrored
`.tex` file if it decides the additional context is useful.

Temporary retry behavior: when a failed theorem is retried, the companion removes
the prior generated proof file and matching project markdown theorem entry before
asking Lea to run again. This prevents stale failed artifacts from interfering
with artifact mapping.

## Usage And Session Links

Usage comes from the standalone adapter's persisted stats endpoint. The
companion falls back to its local job index only when the adapter is unreachable.

The extension opens Lea sessions through:

```text
http://localhost:5173/?session=<session_id>
```

Set `LEA_UI_BASE_URL` if your Vite server is running elsewhere.

## Export And GitHub Sharing

The Lean pane header has two actions that operate on the document's Lea project
(the same repo the Lea UI's Filesystem tab shows):

- **Export** downloads the whole project as a zip (source + `.lea/` assets;
  `.git`/`.lake` excluded), streamed from the adapter through the companion's
  `GET /project-export`.
- **Share** opens a panel to save a GitHub remote (`https://github.com/you/repo`)
  and push the project's commits to its `main` branch. Pushing requires a GitHub
  token, set either in the extension's settings popover ("GitHub sharing") or in
  the Lea UI's Settings — it's the same token, stored only in the adapter's
  `config/lea.local.toml` and never persisted by the companion or Chrome.

Both actions resolve the project by the document's slug and never create one: a
document with no formalized theorems yet has nothing to export. If a push is
rejected because the GitHub repo has diverged, the error suggests asking Lea to
reconcile the histories from a session — same recovery path as the Lea UI.

## Optional LaTeX Context

The extension can mirror the active Overleaf editor buffer into the Lea workspace
so the agent can inspect surrounding LaTeX when notation or exposition matters.
This is off by default and can be enabled from extension settings.

The mirror currently tracks the active editor file, not the full Overleaf file
tree.

## Lean Pane Math Rendering

The Lean pane renders inline and display mathematics with a locally packaged
KaTeX build. Standard KaTeX notation such as `\triangleq`, fractions, roots,
matrices, and aligned equations is supported without a CDN. The pane also
defines the common `\RR`, `\CC`, `\NN`, `\QQ`, and `\ZZ` aliases. If an
expression uses an unsupported project-specific command, the pane falls back to
readable source text instead of failing the whole item.

The unpacked extension owns a synchronized copy of KaTeX's browser assets. After
changing the KaTeX dependency, refresh and verify that copy with:

```sh
npm run sync:katex-assets -w apps/overleaf-extension
npm run check:katex-assets -w apps/overleaf-extension
```

## Tests

Run the Overleaf test suite:

```sh
npm test
```

From the monorepo root, `npm test` runs this suite first and then the standalone
frontend suite.

## Reset Local Run State

From the monorepo root:

```sh
npm run reset:local
```

This clears Lea project entries, proof entries, mirrored Overleaf LaTeX context,
companion job logs, companion indexes, and the standalone SQLite database while
keeping installed dependencies and caches.

Preview first:

```sh
npm run reset:local -- --dry-run
```
