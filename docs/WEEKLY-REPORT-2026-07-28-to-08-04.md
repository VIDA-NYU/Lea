# Weekly Work Report — Jul 28 – Aug 4, 2026

**Author:** Daniel Arturi (`darturi`)
**Repository:** `lea-ecosystem` monorepo
**Reported branches:** `issue_address`, `usage_freeform`,
`multi_formalization_sessions`, and `walkthrough_bugs`, integrated into `main`
**Scope:** 23 authored commits in the reporting window (20 direct + 3 PR
merges), excluding all uncommitted work on `share_two_way`. The committed net
change after the prior report was 177 files, 15,863 insertions, and 895
deletions.

Last week turned sub-agents into a streamed, parallel execution model, brought
the project Blueprint to Overleaf, unified the two front ends visually, and
closed a large audit-remediation pass. This reporting period shifted from
platform hardening to the mathematical work model and the day-to-day Overleaf
experience.

The largest change separated **conversation continuity** from **formalization
identity**. One Lea session can now discuss and revise several theorems or
definitions without allowing the newest attempt to overwrite the status,
history, or current source of earlier work. Formalizations became first-class
records with independent file attribution and verification evidence, while
sessions remained the durable conversation and run timeline.

At the same time, the Overleaf extension became much more explicit about what
the user can trust. It detects when a formalization is stale, lets a user record
that they personally checked an eligible proof, exposes dependency and blocking
information in the Lean pane, and presents batch progress and model selection
more clearly. The prover also moved from broad `import Mathlib` proofs to
targeted module imports, and the shared model stack gained working support for
the new GPT-5.6 family.

---

## 1. Sessions and formalizations became independent concepts

The previous product model implicitly treated a session as one theorem: a
session had one apparent status, one latest file, and one proof identity. That
worked for a single request but broke down as soon as a conversation moved from
theorem A to theorem B. A failed attempt on B could make the session look failed
even though A remained a valid project artifact.

The new model makes the distinction explicit:

```text
Project
├── Formalizations
│   ├── theorem A
│   └── theorem B
└── Sessions
    └── one continuous conversation
        ├── runs focused on A
        ├── project-level discussion
        └── runs focused on B
```

A **session** still owns messages, approvals, usage, runs, and the ordered
conversation. A **formalization** is now the stable mathematical work item: it
may represent a theorem, definition, lemma, counterexample, or draft, and it
retains its identity before or after a checked artifact exists.

### Schema and derived state

Migration `0008_multi_formalization_sessions` added first-class
`formalizations`, session membership, formalization-file associations, and
append-only verification events. Runs may name one focused formalization, and
timeline steps and artifacts can carry explicit formalization attribution.

The implementation preserved the repository's existing storage rules:

- there is no mutable formalization-status column;
- validity is derived from current file/check/verification evidence;
- queued or running activity is a separate overlay rather than proof truth;
- Git remains the owner of proof content;
- SQLite stores identity, relationships, runs, and evidence; and
- old clients may omit the new scope fields and continue creating unfocused
  runs.

This also avoids turning the existing artifact index into something it is not.
Artifacts remain evidence of checked declarations; a formalization may exist
while it is still planned, running, or failing.

### Focused runs and project-level discussion

Run creation can now atomically create or focus a formalization. The prover
receives the focused work item's statement, current files, source provenance,
and project context, while a null focus continues to mean a general project
discussion. One top-level run per session remains the admission rule; the
feature did not introduce simultaneous runs inside one conversation.

The standalone UI added a formalization rail and scope-aware composer. A user
can select an existing formalization, begin a new one, or return to project-wide
discussion without splitting the transcript. The canvas, project window,
search, deep links, and proof-session store all learned the same scope rather
than introducing a second competing state container.

### Current source across several conversations

A session timeline is immutable history, not the canonical current copy of a
formalization. If another conversation updates the same theorem, selecting that
formalization now opens the newest project-wide snapshot by default while still
making the older conversation snapshot available as history.

Revision tokens protect manual edits from overwriting work that changed in
another conversation. Check and SafeVerify evidence remain current only when
they refer to the current file revision. This made cross-session updates
consistent without adding a mutable "current content" pointer.

The same identity was propagated through the Overleaf companion. Pressing
**Reformalize** now starts another run in the existing session instead of
creating a new conversation for every retry.

The complete product and implementation records are in
[`docs/FEATURE-multi-formalization-sessions.md`](FEATURE-multi-formalization-sessions.md)
and
[`docs/PLAN-multi-formalization-sessions.md`](PLAN-multi-formalization-sessions.md).

## 2. Overleaf gained freshness and human-review signals

The Lean pane previously showed the latest known proof state but could not
always distinguish "this proof passed" from "this proof passed for an older
version of the source." The new staleness model binds formalization evidence to
the inputs that produced it.

Freshness now reacts to more than the theorem's visible statement. The revision
also covers its target kind, `uses` dependencies, `context` guidance, comments,
the current Lean artifact, and transitively imported project-local Lean files.
Changing explanatory comments matters because those comments may contain the
mathematical assumptions or guidance Lea used to produce the proof.

The source badge and Lean pane now mark stale work and keep **Reformalize**
available as the recovery action. A source change does not silently leave a
green proof looking current.

### “I checked that myself”

A separate checkmark lets the user record that they personally audited a proof
or definition. This is intentionally a human note, not a second verification
engine:

- it does not alter Lea prompts, runs, compile checks, or formalization status;
- it is not written into the LaTeX or Lean source;
- it is stored in `chrome.storage.local` and shared across open Overleaf tabs;
- it is available only for current, checked, sorry-free work whose project
  dependencies are also free of sorry stubs; and
- it is removed when the source, artifact, dependencies, or eligibility change.

Binding the note to an opaque revision prevents a prior approval from
reappearing if the source changes away and later changes back. Counterexamples,
stubs, failed items, and active runs remain ineligible. The detailed contract is
recorded in
[`docs/FEATURE-overleaf-human-approval.md`](FEATURE-overleaf-human-approval.md).

## 3. Lea received better project context and targeted imports

Several fixes improved what Lea can see before attempting a proof. The Overleaf
mirror expanded from `.tex` alone to the full source set needed to understand a
document: `.tex`, `.sty`, and `.cls`. Project context now inventories those
files, identifies likely root documents, records `\input` / `\include`
relationships, and distinguishes small corpora that should be read in full from
larger projects that should be searched selectively.

The formalization prompt can include:

- the exact mirrored source path;
- a bounded surrounding-source excerpt;
- declared `uses` dependencies and formalization guidance;
- whether mirroring is unavailable, so stale mirror content must be ignored;
  and
- the existing project Lean modules available for reuse.

Run-relative paths are now resolved against the activation's working directory
rather than the adapter process's stable current directory. `read_file` and
`lean_check` therefore find the intended project files during concurrent runs
while retaining the workspace/Lake-root containment boundary.

### Targeted Lean imports

Generated proof files may no longer use the umbrella `import Mathlib`. The agent
prompt tells Lea to import a small domain-appropriate module set, and the write,
edit, and agent-facing check tools enforce that policy so a broad import cannot
be smuggled into a successful final result.

A new read-only `suggest_imports` tool runs Mathlib's import analysis on a
disposable copy of an already-compiling file and returns a replacement direct
import block. This gives Lea a mechanical way to tighten imports without
spending proof-search turns manually guessing a theoretically minimal set.

Import parsing ignores line comments and nested block comments, so a commented
`import Mathlib` cannot be mistaken for a real dependency. SafeVerify now builds
its trusted target prelude from the proof's actual direct imports under trusted
roots (`Init`, `Lean`, `Std`, `Batteries`, `Mathlib`, and project `Lea` modules)
instead of forcing every target through the full Mathlib barrel. The result is
faster Lean checking and a verification target aligned with the proof's real
dependency closure.

## 4. The Lean pane was polished through walkthrough testing

Live walkthroughs produced a concentrated set of UI and workflow fixes across
Aug 3–4.

### Mathematical rendering and batch progress

The extension now bundles KaTeX and its fonts, allowing the Lean pane to render
mathematical fragments consistently without a network dependency. The renderer
and styles were extended for cases that had looked incomplete during testing,
with dedicated tests for math rendering and theme parity.

The **Formalize all** queue received a larger progress-display pass. Running,
queued, paused, skipped, completed, and failed work is easier to distinguish,
while the existing dependency-ordered and sequential batch behavior remains
unchanged.

### Clearer identity, dependency, and blocking information

- Namespace changes are presented more cleanly instead of surfacing as noisy
  raw transitions.
- Renaming a project now updates the Lean code shown in the pane as well as the
  project record.
- Each item can display its `uses` dependencies directly in the pane.
- A formalization blocked by an unformalized upstream theorem says so
  explicitly.
- Spend-cap blocks have their own message instead of collapsing into a generic
  failure.
- General errors were reorganized into clearer, context-appropriate pane
  states.

The standalone UI also fixed an Enter-key failure path that could leave the
React surface blank until a browser refresh.

### Model selection

The extension's limited static dropdown was replaced with a fuller model-picker
surface backed by the shared model catalog. The options page and in-pane picker
now expose the available provider families and model choices more consistently,
and selection/error handling received companion and frontend regression tests.

## 5. New OpenAI models now run through the complete stack

The shared catalog added GPT-5.6 Sol, GPT-5.6 Terra, and GPT-5.6 Luna alongside
the existing OpenAI, Gemini, and Anthropic choices. Merely listing the models
was not enough: walkthrough testing showed the new OpenAI entries reached the
picker but failed in the provider execution path.

The adapter bridge, prover agent, and provider streaming layer were updated so
the newer model responses are normalized into Lea's existing typed event stream.
Settings validation and model metadata remain shared across the standalone UI,
adapter, and Overleaf companion rather than introducing an extension-only model
list. Provider-stream and run-event tests cover the corrected path.

## 6. Documentation and a worked formalization review

Documentation added or materially updated during the period:

- [`docs/FEATURE-multi-formalization-sessions.md`](FEATURE-multi-formalization-sessions.md)
  and
  [`docs/PLAN-multi-formalization-sessions.md`](PLAN-multi-formalization-sessions.md)
  — the product model, constraints, schema, rollout, and implementation record
  for first-class formalizations.
- [`docs/FEATURE-overleaf-human-approval.md`](FEATURE-overleaf-human-approval.md)
  — the semantics and revision binding of the user-audit checkmark.
- [`docs/FEATURE-overleaf-lean-pane.md`](FEATURE-overleaf-lean-pane.md) — updated
  for freshness and the expanded pane behavior.
- [`docs/WEEKLY-REPORT-2026-07-21-to-07-27.md`](WEEKLY-REPORT-2026-07-21-to-07-27.md)
  — the preceding week's report.
- [`docs/AUDIT-quantum-allocation-formalization-2026-08-03.md`](AUDIT-quantum-allocation-formalization-2026-08-03.md)
  and [`docs/quantum-allocation-overleaf/`](quantum-allocation-overleaf/) — a
  worked multi-file LaTeX example and formalization audit covering allocation
  definitions and the QUBO transformation.

The period's work was integrated through three pull requests:

- **PR #30 (`issue_address`)** brought the prior report and the completed audit
  line onto the integration branch. The audit implementation commits themselves
  were dated before this report's rolling eight-day cutoff and are described in
  the preceding report rather than counted again here.
- **PR #31 (`multi_formalization_sessions`)** integrated the formalization data
  model, standalone UI, Overleaf identity propagation, targeted imports, and
  queue/rendering follow-ups.
- **PR #32 (`walkthrough_bugs`)** integrated the Aug 3 walkthrough fixes and the
  corrected GPT-5.6 execution path.

---

## Status of the work

- **Integrated on `main`:** staleness and human approval, multi-formalization
  sessions, cross-conversation revision consistency, targeted imports, expanded
  project context, KaTeX rendering, batch-progress polish, Lean-pane walkthrough
  fixes, the fuller model picker, and GPT-5.6 provider support.
- **Architecture preserved:** both front ends still use the adapter on `:8001`,
  which imports the vendored prover in process. No second prover service or
  competing source of proof truth was introduced.
- **Current branch exclusion:** `share_two_way` was created from `main` after PR
  #32 and has zero commits of its own. Its modified and untracked GitHub-import
  work is outside this report and was not used to describe or measure the
  period.
- **Working tree before this report:** already dirty with the excluded
  `share_two_way` work. This report is the only file added for this request.

## Test posture

The committed diff touched **35 test files**, adding approximately **3,557 test
lines** and removing 70. Coverage was added or extended for:

- formalization services, routes, session detail, run focus, project search,
  canonical snapshots, and revision conflicts;
- Overleaf mirror upserts and run-relative sandbox reads;
- human approval eligibility and invalidation;
- source/comment staleness and theorem parsing;
- companion identity propagation and reformalization reuse;
- Lean-pane batch states, dependency display, editing, math rendering, model
  selection, and theme parity;
- targeted-import enforcement, import suggestions, and SafeVerify import
  derivation; and
- new-model provider streaming and typed run events.

No fresh full-suite run was performed while preparing this report because the
current working tree contains substantial excluded `share_two_way` changes. The
test posture above describes the committed regression coverage and deliberately
does not claim a new passing-suite count from a mixed tree.

---
