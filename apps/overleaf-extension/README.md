# Overleaf Lea Formalizer

This app is the Overleaf side of the LeaEcosystem monorepo. It provides a Chrome
extension plus a local Node companion that sends marked theorem blocks from
Overleaf to the shared Lea adapter.

The default path is:

```text
Overleaf page -> Chrome extension -> companion (:31245) -> FastAPI adapter (:8001)
```

The API admits durable SQL commands and a separate worker drives the vendored prover.
The optional companion forwards database operations and observes SQL receipts;
it needs no workspace access, provider keys or writable job storage. The adapter
workspace worker owns a private Node business process (Node 22.13 or newer).

The extension publishes immutable editor snapshots with a document/tab generation
and sequence before requesting execution. Source callbacks and pending-source
polling are retired. A competing tab or changed source receives an actionable
conflict; accepted work never silently adopts a newer source snapshot.

See the [Stage 6 cutover record](../../docs/STAGE6-database-ui-cutover.md) for the
legacy-state import, limits, qualification and rollback procedure.
The [Stage 8 runtime guide](../../docs/STAGE8-process-boundary.md) defines the final
API/worker split and strict client capability negotiation.

For the VIDA-hosted Docker installation and Chrome side-load steps, see
[BETA_INSTALL.md](BETA_INSTALL.md). The image already runs both the adapter and
the companion; only the unpacked extension is loaded separately in Chrome.

## Pieces

- `extension/`: Chrome MV3 extension injected into Overleaf.
- `companion/server.mjs`: optional stateless HTTP compatibility transport.
- `companion/worker.mjs`, `workerService.mjs`, `databaseState.mjs`: private adapter-owned
  business worker; orchestration records and diagnostics are committed to SQL.
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
% lea: formalize
Every finite tree has at least two leaves.
\end{theorem}
```

Lea assigns a stable internal label automatically. An existing LaTeX
`\label{...}` helps Lea keep that identity and choose a Lean declaration name;
neither label is required to start formalizing. The older `label=...` field is
deprecated but still supported for existing documents and references. If used,
it must be a valid Lean identifier: letters, digits, and underscores, with no
leading digit.

Initially supported environments are `theorem`, `lemma`, `proposition`, and
`corollary`. Unmarked environments are ignored.

### `uses={...}`

Use `uses={...}` when a theorem should depend on earlier theorems from the same
Overleaf project. Values may be existing Lea labels or unique LaTeX labels
(such as `uses={thm:finite-tree-leaves}`); they are not Lean theorem names. Each
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
% lea: formalize context={Use the assumption that n is even, rewrite n as 2 * k, then use ring_nf.}
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
\leatheorem{uses={even_def}, context={Use the parity definition first.}}
If $n$ is even, then $n^2$ is even.
\end{claim}
```

`claim` is not a recognized environment name, but `\leatheorem{...}` states
its own kind directly, so the extension still finds and formalizes it. The
`uses=`/`context=` fields work exactly like the comment-marker fields above.
The deprecated `label=` field remains accepted.

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
\leatheorem{uses={right_triangle}}
{In a right triangle, the square of the hypotenuse equals the sum of the
squares of the other two sides.}
```

Metadata may span multiple lines, and whitespace or `%` comments may separate
any of the arguments. For example:

```tex
\leatheorem{
  context={Use the parity definition first.}
}% The statement follows on the next line.
{If $n$ is even, then $n^2$ is even.}
```

Commented-out tags and command definitions are ignored by the extension.
The same multiline metadata rules apply to `\begin{leacode}{...}`.
One caveat that follows directly from that: a tag with **no** intended body,
placed with only whitespace or comments before an unrelated `{...}` group, will
have that group absorbed as its body. If you don't want a second argument,
keep actual prose or another command between the tag and the next braced
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
PORTKEY_API_KEY=          # Portkey AI gateway; models are portkey/@provider-slug/model
PORTKEY_BASE_URL=         # the gateway's /v1 root (blank = Portkey's hosted service)
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

Retries retain prior proof bytes and canonical conversation history. Source
changes invalidate current verification. Replacements follow the existing staged
publication and human-review rules.

## Usage And Session Links

Usage, job history and sessions come from SQL projections. An unavailable adapter
is an unavailable observation; it never enables a second JSON authority.

The extension opens Lea sessions through:

```text
http://localhost:5173/?session=<session_id>
```

Set `LEA_UI_BASE_URL` if your Vite server is running elsewhere.

## Export And GitHub Sharing

The Lean pane header has two actions that operate on the document's Lea project
(the same repo the Lea UI's Filesystem tab shows):

- **Export** downloads the whole project as a zip (source + `.lea/` assets;
  `.git`/`.lake` excluded). `POST /project-export` queues preparation; download
  bytes are retained in SQL for 24 hours and can be read without workspace access.
- **Share** opens a panel to save a GitHub remote (`https://github.com/you/repo`)
  and push the project's commits to its `main` branch. Pushing requires a GitHub
  token, set either in the extension's settings popover ("GitHub sharing") or in
  the Lea UI's Settings — it's the same token, stored only in the adapter's
  protected adapter infrastructure and represented by redacted SQL status. Raw
  credentials never enter command payloads, companion records or Chrome storage.

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
