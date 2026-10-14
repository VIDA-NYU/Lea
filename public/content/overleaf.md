---
title: Overleaf extension usage guide
description: Mark mathematical content, formalize it from the Lean pane, review the results, and refine them with chat or manual edits.
updated: 2026-10-13
---

Lea works alongside your Overleaf source. You choose which statements or definitions to
formalize, inspect the generated Lean, and decide whether the result matches your mathematics.

Complete the [installation instructions](/install/#overleaf-extension) first, and keep Lea
running while you work. Both `./start-dev.sh` and the Docker setup start the companion for you.
The standard connection settings are already configured; you can start using the extension
without opening its options page.

## Open the Lean pane

Open or reload your Overleaf project after loading the extension. Click the floating **L**
button, whose tooltip reads **Lean project pane**. The pane lists Lea-marked mathematical
items across the project's `.tex` files, grouped by file and section.

Expand an item to see its source statement, generated Lean, progress, and available actions.
**Go to source** jumps to its block in the Overleaf editor. The pane's refresh button reloads
the project inventory; you can resize or close the pane without changing your document.

If your document already contains Lea tags or comment markers, go straight to
[formalizing an item](#formalize-an-item). Ordinary LaTeX labels alone do not activate Lea.

## Mark your mathematical content

Use either inline Lea tags or `% lea:` comments to identify the blocks you want Lea to work on.
You can keep your existing theorem environments and document layout.

### Inline tags

If you already use `lea-tags`, keep using those commands. Otherwise, click **Add Lea tags**
below the **Items / Blueprint** tabs in the Lean pane. The extension creates `lea-tags.sty`
in the project root, loads it in the configured main document, and verifies that the
changes were saved. Recompile your project once setup finishes.

Setup uses your existing Overleaf session and works even when the Lea companion is
unavailable. Repeating it does not duplicate the file or package line. Existing styles
and inline command definitions are preserved.

If automatic setup cannot finish, its message identifies what remains. Use **Download
style** to upload the package manually and **Copy package line** to add this line before
`\begin{document}` in your main document:

```tex
\usepackage{lea-tags}
```

The extension's options page still offers **Download style** under **Inline Lea tags**.
Its **Copy preamble** alternative lets you paste the command definitions into your
preamble instead of adding a package file. For a customized style or a preamble that
loads definitions from other files, review the existing setup before adding another load.

Place a tag inside the mathematical block:

```tex
\begin{theorem}\label{thm:even-square}
\leatheorem{label=even_square}
If $n$ is an even integer, then $n^2$ is even.
\end{theorem}
```

The tag's metadata is invisible in the PDF. It tells Lea what kind of item the block contains,
so it also works inside custom environments such as `claim` or `conjecture`.

| Content | Tag |
| --- | --- |
| Theorem | `\leatheorem{…}` |
| Lemma | `\lealemma{…}` |
| Proposition | `\leaproposition{…}` |
| Corollary | `\leacorollary{…}` |
| Definition | `\leadefinition{…}` |

For a definition, Lea generates a Lean definition rather than a proof obligation. For example:

```tex
\begin{definition}\label{def:even}
\leadefinition{label=even_integer}
An integer is even if it is twice another integer.
\end{definition}
```

Tags can also take the statement as a second argument, without an enclosing environment:

```tex
\leatheorem{label=even_square}{If $n$ is an even integer, then $n^2$ is even.}
```

Metadata can span multiple lines, and `%` comments may separate the arguments:

```tex
\leatheorem{
  context={Use the parity definition first.}
}% The statement follows on the next line.
{If $n$ is an even integer, then $n^2$ is even.}
```

Commented-out tags and command definitions are ignored. `leacode` metadata follows
the same rules; its body is preserved as a verbatim code listing.

### Comment markers

Comments work without `lea-tags.sty` and do not change the rendered document:

```tex
\begin{theorem}\label{thm:even-square}
% lea: formalize label=even_square
If $n$ is an even integer, then $n^2$ is even.
\end{theorem}
```

Use `% lea: define label=…` inside a `definition` environment. Comment markers recognize
`theorem`, `lemma`, `proposition`, `corollary`, and `definition`; use an inline tag for a custom
environment. Add one Lea marker per block: a comment and a tag in the same block produce a
duplicate-marker error.

### Names, dependencies, and hints

`label=` gives an item a stable Lea identifier. Explicit labels use letters, digits, and
underscores and cannot start with a digit. The label is distinct from the ordinary LaTeX
`\label{…}`. It is optional in the current extension: Lea generates an identifier when you
omit it, so `\leatheorem{}` is sufficient to mark a block.

Use an explicit label when you want to refer to the item from another Lea marker. Optional
`uses={…}` fields name supporting items, and `context={…}` supplies guidance:

```tex
\leatheorem{label=even_square, uses={even_integer}, context={Write n as twice an integer.}}
{If $n$ is an even integer, then $n^2$ is even.}
```

The same fields work in comment markers. Keep the statement and relevant proof text in your
document; Lea uses the source context as part of formalization. An adjacent `proof` environment
can supply the author's proof to follow.

## Formalize an item

1. Open the Lean pane and find the item you want to work on.
2. Click **Formalize**. A separate stub-generation step is optional.
3. Expand the item to follow progress and inspect the generated Lean.
4. Review the checks and source findings before accepting the result.

You can also click a marked block or its badge in the source editor to open its actions, then
choose **Formalize** or **Show in Lean pane**.

The gear button opens **Lea settings and usage**. Use it to choose a model, add a missing API
key, or adjust the turn and spend limits. If you already configured your model and key in Lea,
you can use those shared settings. The extension shows an **Open Settings** prompt when the
selected model still needs credentials.

**Stub**, available in an item's other actions, generates a theorem skeleton to work from.
A stub with unfinished proof obligations is not a completed proof. Definitions use
**Formalize** directly.

## Read the checks and status

The pane presents two separate results:

| Indicator | What to review |
| --- | --- |
| **Lean Check** | The formal checking result for the generated Lean artifact. Pending, incomplete, or failed work still needs attention. |
| **Lea Status** | Lea's assessment of the source and formalization: confidence, assessment scope, findings, corrections, and remaining obligations. |

Expand **Lea Status details** to read the explanation and **Update history** to see earlier
assessments. Confidence may refer only to the source or to a partial artifact; check the scope
before interpreting it as an assessment of a finished proof. High confidence does not replace
formal checking or your review of whether the Lean statement matches the intended claim.

Source edits, new Lean revisions, or changed dependencies can make a previous assessment
outdated. The pane labels these changes and retains the last assessed version for inspection.
If Lea finds a checked counterexample, the item is marked **Claim disproved** rather than
presented as a proof of the original statement.

After personally reviewing an eligible checked result, use the **✓** control to record your
approval of that exact proof and its dependencies. This is a personal review mark. Changes to
the reviewed content can invalidate it.

## Pause, resume, and resolve blockers

Use **Pause** in the live status view to interrupt an individual run. A paused item offers
**Resume** when continuation is available. If a limit stopped the run, adjust the relevant
setting before resuming.

The setting **Pause formalization when Lea needs author input** can pause individual runs
when the source needs clarification. Read the finding, correct or clarify the source, and
resume. When an eligible item has no associated author proof, **Continue best effort** may
be offered alongside **Resume faithfully**. Best effort authorizes Lea to proceed with
disclosed inferred context for that continuation; review the resulting assumptions and
corrections. Source findings remain visible even when the pause setting is off.

If Lea needs a supporting mathematical result, the item can wait while that result is proved
or reused. **View supporting result** opens the shared mathematics details, including its
status and attempts. Formalization continues automatically when the required result is
verified, unless the wait is stopped or needs attention. Use **Refresh status** to reconnect,
or **Stop waiting** to disable automatic continuation. **Continue when ready** enables it again.

## Refine the result

Use **Chat** to discuss an item with Lea from inside the pane. The chat and its Lean revisions
belong to the same session available through **Open in LeaChat**. Give corrections, ask about
the proof strategy, or request a change while retaining the item's context.

To change the generated Lean yourself, expand the item and choose **Edit** under its other
actions. **Save** records the change and checks it with Lean. It also checks affected
dependents when needed, and shows the impact. A manual save does not start a new agent run.
Use **Cancel** to discard the draft.

If an edit breaks downstream items, inspect the reported errors. **Repair with Lea** repairs
an affected item; **Repair all** handles the offered group. Review any changed declaration
headers against the original statements. After changing the LaTeX statement or marker
metadata, use **Re-formalize** to bring the Lean artifact back into sync.

## Work across the project

**Formalize all** runs eligible items in dependency order. The batch panel shows the current
item, queued work, and outcomes. **Stop** interrupts the batch; a paused batch offers
**Continue remaining** after you address its blocker. **Stub all** is available for eligible
theorems if you want skeletons before completing proofs.

Formalize all keeps source findings visible without pausing for author-input findings.
Review the **Formalize all report** when the batch settles. Earlier saved reports are
available under **Past Formalize all reports** in the pane.

Switch from **Items** to **Blueprint** to inspect the project's dependency graph.
**Generate from formalized theorems** adds a starter graph from recorded work. Select a node
to inspect it and use its available source or session links.

The project header shows the Lea project name and Lean namespace. **Edit project name**
lets you rename the project and preview any namespace change before saving.

## Export or share your work

Open **Share** in the pane header and choose **Download .zip** to export the Lean project.
The same panel lets you configure a GitHub remote and **Push to GitHub**, or **Add Lean files
from GitHub** to preview an import. Review the import preview before confirming it; imported
proofs are checked before their results are treated as ready.

## If something does not appear

- **No L button:** confirm the extension is enabled in Chrome, then reload the Overleaf tab. Reload the tab again after reloading or updating the extension itself.
- **No mathematical items:** add a Lea tag or comment marker. A normal `\label{…}` alone is not enough. Check the pane for malformed-marker diagnostics.
- **A malformed-tag warning:** check for a missing metadata argument or an unclosed brace. Metadata may span lines, and comments between arguments are allowed. After updating or reloading the extension, refresh the Overleaf tab; the existing `lea-tags.sty` can stay in place.
- **Undefined Lea commands in the PDF build:** click **Add Lea tags** in the Lean pane, then recompile. If setup needs manual completion, follow its message or paste the definitions from **Copy preamble**. Comment markers need no package.
- **Tag setup stops or needs review:** keep the project in Code mode while setup runs. Switching files stops the operation. Existing custom styles are never replaced; use the download and package-line controls to finish manually when needed.
- **Connection errors:** keep the adapter and companion running. The standard companion address is `http://127.0.0.1:31245`; the adapter uses `:8001`. Check the extension's options only if you changed the defaults or need to troubleshoot.
- **An unavailable action:** expand the item and read its status. An active run, pending import check, malformed marker, or dependency wait can change which actions are offered.

For startup and maintenance commands, return to the [install guide](/install/).
