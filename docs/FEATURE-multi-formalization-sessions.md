# Feature: Multi-Formalization Sessions

> **Status:** Implemented — 2026-07-28

## Summary

Allow one Lea UI session to discuss, create, revise, and compare multiple
formalizations in the same project without collapsing those formalizations into
one status, one file, or one implied proof identity.

A **session** should remain the continuous conversation between a user and Lea.
A **formalization** should become a first-class mathematical work item with its
own identity, files, history, compile verdict, verification result, and project
relationships.

The user should be able to move naturally from:

> Prove `continuous_mul`.

to:

> Now formalize `compact_image`, using the previous result.

without starting a new chat and without causing the second theorem's status to
replace the first theorem's status.

## Core Principle

Conversation continuity and formalization identity are independent.

- One session may discuss zero, one, or many formalizations.
- One formalization may be discussed or revised from one or many sessions.
- A project contains both conversations and formalizations.
- Run lifecycle belongs to a run/session.
- Mathematical and Lean validity belongs to a formalization.

The product must not require a new session merely to preserve independent proof
status.

## Terminology

### Project

The shared Lean namespace, repository, project context, instructions, memory,
blueprint, and files. Project sessions continue to use the same project working
directory and may import sibling declarations.

### Session

A durable chat conversation and ordered timeline. A session owns messages,
approvals, usage, runs, and conversational context. It does not own a single
implicit theorem.

### Formalization

A stable target representing a theorem, lemma, definition, counterexample,
disproof, or other named Lean work item. A formalization may exist before a
checked Lean artifact exists.

### Artifact

A checked Lean declaration produced by a formalization. The existing artifact
index remains evidence about which declaration currently lives at which path;
it is not, by itself, sufficient to represent planned, running, or failed
formalizations.

### Focus

The formalization, if any, that the composer, run, and canvas are currently
working on. A project-level discussion has no single focus.

## Goals

1. Let a user formalize several related statements in one continuous session.
2. Preserve an independent status and history for every formalization.
3. Make the current composer/run focus visible and correctable.
4. Let the user switch the canvas between formalizations without losing chat
   context.
5. Let project navigation expose formalizations independently of conversations.
6. Preserve project-level discussions that refer to multiple formalizations.
7. Reuse the existing adapter, in-process prover, project repository, timeline,
   and artifact ledger.
8. Maintain compatibility for clients that do not send formalization scope.

## Non-Goals

- Do not introduce another prover service or HTTP boundary.
- Do not create a separate chat transcript per formalization.
- Do not require one session per theorem.
- Do not remove the recommended one-primary-declaration-per-file convention.
- Do not store compile or verification status as mutable authoritative state;
  status remains derived from run and ledger evidence.
- Do not change the existing canonical storage responsibilities for working
  files, timeline snapshots, project history, or usage.
- Do not require automatic splitting of every historical multi-declaration Lean
  file in the first release.
- Do not make project discussion require a formalization focus.
- Do not add simultaneous runs within one session; work in one session remains
  sequential.

## Current Behavior

The existing implementation already permits several runs and several file paths
inside one session:

- A follow-up message creates another run with the selected `session_id`.
- The adapter replays the prior transcript and supplies the current project
  context.
- Every code write becomes a session timeline step with a `run_id` and path.
- The canvas can switch between the distinct paths touched by the session.
- Successfully checked declarations are indexed project-wide as artifacts.

However, the product still treats a session as one theorem/definition cluster:

- The project composer creates a new session for every new proof.
- The session title comes from its initial request.
- A session has one derived status, taken from its latest non-scratch code step.
- The canvas calls the most recently touched non-scratch file the main file.
- The project overview lists sessions, not formalizations.
- SafeVerify and completion presentation are primarily session/run-oriented.
- The artifact index only records successful checked outputs and extracts the
  first named declaration from a file.

Consequently, if theorem A is proved and a later theorem B fails in the same
session, the project session row appears failed even though theorem A remains a
valid project artifact.

## Proposed User Experience

### Session Formalization Rail

The Chat + Canvas view should include a compact formalization rail or selector:

```text
Formalizations
✓ continuous_mul       Proved
● compact_image        In progress
✕ uniform_limit        Failing
+ New formalization
```

The rail must:

- list every formalization associated with the session;
- show kind, declaration/title, and derived status;
- distinguish the focused formalization;
- permit keyboard and pointer selection;
- provide a `Project discussion` or `All work` selection;
- place unassociated scratch and support files under `All files`, not invent
  formalization identities for them.

Selecting a formalization changes the canvas scope, not the canonical chat
transcript.

### Current Version Across Conversations

A session timeline is immutable conversation history; it is not the canonical
current copy of a formalization. When a formalization is selected, the canvas
defaults to its latest project-wide file revisions across every project
session.

If the open conversation last touched an older revision, the canvas must:

- display the current project version by default;
- identify the conversation that last updated it;
- offer `View this conversation's version`;
- offer `Open updating conversation`; and
- label historical snapshots so they cannot be mistaken for current source.

Canonical snapshots and historical code steps remain separate frontend data
sources. The current revision is derived from formalization-file membership and
the latest project timeline blobs; no mutable current-content pointer is stored.

Manual edits to a current formalization carry its revision token. A changed
token produces `409 revision_conflict` before the shared file is written.
Checks and SafeVerify evidence are current only when they reference the
canonical file revision.

### Automatic Composer Attribution

The composer should not require a persistent scope dropdown. Lea infers the
next run's focus from, in order:

1. formalizations explicitly named in the message;
2. an explicit one-shot user override;
3. the formalization currently viewed in the canvas, as an ambiguity hint;
4. new-formalization language such as “prove another theorem”; or
5. project discussion when no single target is evident.

The normal composer displays a passive `Scope: automatic` chip. Clicking it
opens an escape-hatch override for project discussion or a linked
formalization. An override applies to the next run and is then cleared.

`+ New formalization` remains an explicit action in the formalization rail. It
creates no database row until the user submits the message.

Actual declaration attribution from streamed code changes is stronger evidence
than the pre-run inference. If Lea edits a different formalization, the UI
follows that formalization and the persisted code/check evidence remains
attached to what was actually changed.

### Creating a Formalization

When `+ New formalization` is selected:

1. The user submits a natural-language request.
2. The adapter creates a draft formalization associated with the current
   project and session.
3. The run is focused on that draft formalization.
4. In the interactive workflow, Lea may first propose the exact statement and
   declaration name as it does today.
5. Once confirmed, the draft identity is updated with the agreed kind,
   declaration name, and statement.
6. Code steps and checked artifacts produced by the run are linked to the
   formalization.

A draft must be visible in the rail before it has a Lean file. A failed first
attempt must remain visible and resumable.

If the user starts from the project page rather than an existing session, the
default `New formalization` action creates both:

- a new conversation session; and
- a draft formalization focused by that session's first run.

The user may later choose an existing conversation as an advanced option.

### Continuing an Existing Formalization

When an existing formalization is focused:

- the run receives its identity, statement, current artifact paths, status, and
  relevant project context;
- code writes should default to its known primary file or file set;
- the canvas opens on its primary file;
- check and SafeVerify actions apply to the selected file within that
  formalization;
- a successful re-formalization updates its current artifact evidence without
  creating a duplicate formalization identity.

### Cross-Formalization Discussion

The user may ask:

> How does `continuous_mul` help prove `compact_image`?

This should remain one conversational turn. The run may focus
`compact_image` and reference `continuous_mul`, or may have no single focus when
the request is purely explanatory.

The UI should render referenced formalizations as links or chips in the
timeline. The user must not need to merge their identities or open parallel
chats.

## Project Information Architecture

The project window should expose two independent collections:

```text
Project
├── Formalizations
│   ├── continuous_mul
│   ├── compact_image
│   └── uniform_limit
└── Conversations
    └── Exploring compactness
        ├── continuous_mul
        └── compact_image
```

### Formalizations View

The project Formalizations view should support:

- status and kind filters;
- search by display title, declaration name, module, or path;
- sorting by last activity, name, status, or dependency order;
- status summary counts;
- opening the current code and evidence;
- opening the most recent associated conversation;
- starting a new conversation focused on the formalization;
- displaying dependencies and dependents when known.

### Conversations View

The Conversations view should list:

- session title;
- last activity;
- active-run state;
- associated formalization chips;
- aggregate formalization summary, such as `2 proved · 1 failing`;
- project-discussion-only sessions with no artificial proof status.

The existing single session status dot must not imply that every
formalization in the conversation shares one verdict.

The session title remains conversation-level. It should be user-editable and
must not automatically change whenever the user focuses a different
formalization. A generated title may summarize the opening request, while the
formalization chips explain the work the conversation later accumulated.

### Deep Links

A formalization selection must be addressable independently of the session:

```text
session=<session-id>&formalization=<formalization-id>
```

Opening the link loads the canonical session transcript and focuses the
specified formalization in the canvas. If the formalization is no longer linked
to that session, the UI should open the formalization project view and explain
the mismatch rather than silently selecting another file.

## Canvas Behavior

### Focused View

When a formalization is selected, the canvas should show:

- its declaration/title and status;
- its primary file;
- any additional associated files;
- its code-step history;
- per-file compile verdicts;
- its latest SafeVerify result;
- its latest responsible run;
- links to dependencies and associated sessions.

The stepper should be filtered to code steps associated with the selected
formalization. Switching formalizations should restore each formalization's most
recently selected file and step for the current browser session.

### All Files View

`All files` preserves the current low-level session file workflow:

- every path touched by the session;
- scratch/probe files;
- unclassified support files;
- the full cross-file step history.

Unclassified files should be clearly labeled. They must not inherit the focused
formalization's verdict merely because they were written during the same run.

### Multi-File Formalizations

A formalization may have one primary file and zero or more supporting files.
Status must identify which evidence is primary and whether a failing supporting
file blocks the formalization's usable result.

Version 1 may optimize the UI for one primary `.lean` file, but the persistence
and API model must not require exactly one file.

## Timeline Behavior

The chat timeline remains globally ordered and unfiltered by default.

Runs should be grouped into visible work episodes:

```text
Started formalization compact_image
Run 3 · 4 code changes · Proved
```

Each run episode should display:

- focus formalization, if any;
- referenced formalizations, when available;
- run lifecycle and result;
- code-change count;
- final mathematical result;
- usage and approval details where already supported.

The user may optionally filter code events to the selected formalization, but
ordinary chat messages must remain available so the conversation does not
fragment into hidden subthreads.

## Status Model

### Formalization Validity

Formalization validity is derived, not stored. The derivation should combine:

1. current file existence;
2. the newest relevant check verdict;
3. sorry/admit detection;
4. SafeVerify evidence where applicable;
5. the latest completed result kind, including `disproved`;
6. source freshness for externally sourced targets such as Overleaf.

The validity vocabulary should support at least:

- `draft`
- `planned`
- `unchecked`
- `failing`
- `proved`
- `defined`
- `disproved`
- `needs_review`
- `stale`
- `missing`

The exact API vocabulary may normalize these states, but proof, definition, and
disproof outcomes must remain distinct.

### Formalization Activity

Current activity is a separate, derived overlay:

- `idle`
- `queued`
- `running`
- `waiting_for_approval`

An active retry must not erase the last known file truth. For example, a
previously proved formalization being revised should render as:

```text
compact_image   Running · previously proved
```

This preserves the existing architectural distinction between run lifecycle and
working-copy validity.

### Session Summary

A session summary is an aggregate over its linked formalizations, for example:

```json
{
  "formalization_count": 3,
  "proved": 1,
  "defined": 1,
  "failing": 1,
  "active_run_count": 0
}
```

A session with no linked formalizations is a valid discussion session and should
display a conversational state rather than `empty` or `unchecked`.

For API compatibility, the existing `session.status` field may remain during a
transition, but the standalone UI must use the formalization summary and active
run state once the new fields are available.

## Data Model

### `formalizations`

Add a first-class table for targets that can exist before an artifact:

```sql
formalizations (
  id                    text primary key,
  project_id            text references projects(id),
  loose_session_id      text references sessions(id),
  display_title         text not null,
  declaration_name      text,
  kind                  text not null,
  statement             text,
  origin                text not null,
  origin_key            text,
  source_hash           text,
  created_at            text not null,
  updated_at            text not null
)
```

Invariants:

- Exactly one durable scope is present: `project_id` for project work or
  `loose_session_id` for loose-session work.
- A project formalization is not owned by one conversation.
- `declaration_name` may be null while the formalization is a draft.
- A non-null declaration name is unique within its durable scope.
- `kind` describes intent (`theorem`, `lemma`, `definition`,
  `counterexample`, or another supported target kind), not compile status.
- `origin_key` may hold a stable external identity such as an Overleaf target
  key.
- `source_hash` is the newest known external-source fingerprint. It is nullable
  for UI-originated work and when the source system has not synchronized it.
- The primary path is derived from the `formalization_files` relationship and
  current artifact evidence; it is not duplicated on this row.

No mutable status column should be added.

### `session_formalizations`

Add the many-to-many conversation association:

```sql
session_formalizations (
  session_id        text not null references sessions(id),
  formalization_id  text not null references formalizations(id),
  created_at        text not null,
  primary key (session_id, formalization_id)
)
```

This is a durable user/workflow association, not derived proof status. First
run, latest run, and last activity are derived from `runs` and `timeline`; they
must not be copied onto this relationship where they could drift.

### Run Focus

Add a nullable focus to `runs`:

```sql
runs.focus_formalization_id text references formalizations(id)
runs.focus_source_hash text
```

Null means project discussion, general assistance, or a legacy/unclassified
run. A run may reference additional formalizations in structured run metadata,
but it has at most one primary focus. `focus_source_hash` records the external
source revision the run actually received, so a later artifact can retain its
generation provenance.

### Timeline Attribution

Add an optional formalization association to code timeline rows:

```sql
timeline.formalization_id text references formalizations(id)
```

This is required for:

- manual edits with no `run_id`;
- multiple files touched by one run;
- stable history after a formalization's primary path changes;
- avoiding path-only inference when files contain several declarations.

Message rows may leave this field null and derive their visible run focus from
`run_id`. Explicitly formalization-scoped system markers may set it.

### Formalization Files

Represent the file set explicitly:

```sql
formalization_files (
  formalization_id  text not null references formalizations(id),
  path              text not null,
  role              text not null,
  created_at        text not null,
  updated_at        text not null,
  primary key (formalization_id, path)
)
```

Initial roles:

- `primary`
- `support`
- `generated`

Scratch/probe files should normally remain unassociated.

### Verification Evidence

SafeVerify evidence must be tied to the exact code snapshot it verified rather
than to the session's latest run:

```sql
verification_events (
  id                text primary key,
  formalization_id  text references formalizations(id),
  session_id        text not null references sessions(id),
  run_id            text references runs(id),
  code_step_id      integer references timeline(id),
  path              text not null,
  status            text not null,
  detail            text,
  created_at        text not null
)
```

The table is append-only evidence. A verification is current only when its
`code_step_id` is still the newest code step for the verified path. Editing that
path therefore makes prior verification outdated without deleting its history.

### Artifact Link

Add:

```sql
artifacts.formalization_id text references formalizations(id)
artifacts.source_hash text
```

The artifact index continues to answer which checked declaration lives at which
path. Re-formalizing the same target updates the current artifact evidence while
the formalization identity and its timeline attribution remain stable. The
artifact source hash comes from the focused run and is compared with the
formalization's newest known source hash to derive external staleness; missing
hashes mean freshness is unknown, not stale.

## API Surface

### Project Formalizations

```http
GET  /api/projects/{project_id}/formalizations
POST /api/projects/{project_id}/formalizations
GET  /api/formalizations/{formalization_id}
PATCH /api/formalizations/{formalization_id}
```

Recommended create shape:

```json
{
  "display_title": "Compact image theorem",
  "declaration_name": "compact_image",
  "kind": "theorem",
  "statement": "The continuous image of a compact set is compact.",
  "origin": "ui"
}
```

The declaration and statement may be omitted for an initial draft.

### Session Formalizations

```http
GET /api/sessions/{session_id}/formalizations
```

`GET /api/sessions/{session_id}` should also include:

```json
{
  "formalizations": [],
  "formalization_summary": {},
  "latest_focus_formalization_id": null
}
```

The server must return enough information to render the rail without one request
per formalization. `latest_focus_formalization_id` is derived from the newest
focused run and is only a restoration hint; an explicit deep link or the user's
current local selection takes precedence.

### Run Creation

Extend `POST /api/runs` with optional scope:

```json
{
  "session_id": "session-id",
  "message": "Now prove the compact image theorem.",
  "focus_formalization_id": "formalization-id"
}
```

For explicit creation in an existing session:

```json
{
  "session_id": "session-id",
  "message": "Formalize the compact image theorem.",
  "new_formalization": {
    "display_title": "Compact image theorem",
    "kind": "theorem"
  }
}
```

The adapter should create the draft formalization and run in one transactionally
coherent operation. It must not return a run focused on a formalization that was
not durably created and associated with the session.

Existing clients that omit both fields retain legacy behavior and produce an
unfocused run.

### Formalization Detail

Recommended response:

```json
{
  "id": "formalization-id",
  "project_id": "project-id",
  "display_title": "Compact image theorem",
  "declaration_name": "compact_image",
  "kind": "theorem",
  "statement": "...",
  "validity_status": "proved",
  "activity": {
    "status": "idle",
    "run_id": null
  },
  "primary_path": "Topology/compact_image.lean",
  "files": [],
  "artifact": {},
  "latest_check": {},
  "safe_verify": {},
  "sessions": [],
  "latest_run": {},
  "dependencies": [],
  "dependents": []
}
```

Derived evidence may be null when the formalization is still a draft.

## Prover and Prompt Contract

The adapter should prepend focused formalization context separately from the
project context:

```text
Current formalization focus
ID: <formalization-id>
Kind: <kind>
Display title: <title>
Lean declaration: <declaration or not chosen>
Agreed statement: <statement or not chosen>
Primary file: <path or not created>
Other files: <paths>
Current derived status: <status>
```

For a new draft, the prompt should make clear that Lea is creating a new
formalization and must not overwrite a sibling declaration merely because the
session previously worked on it.

For an existing focus, Lea should inspect the focused formalization's current
files before editing. The full session transcript remains available so prior
mathematical discussion is not lost.

A project-discussion run receives no current-focus block. The assistant may read
project files and discuss several formalizations without being forced to write
code.

## Concurrency and Safety

- At most one top-level run remains active per session.
- Different sessions in one project may continue to run concurrently.
- Formalization attribution does not relax existing workspace confinement.
- A focused run should write its formalization's known files and new,
  attributable support files.
- Writes to another active formalization's primary path must be rejected or
  require an explicit reassignment operation.
- Changing a formalization's primary path must preserve its identity and history.
- Deleting or retiring a formalization must not silently delete project files;
  file deletion remains a separate, explicit action.
- Project deletion cascades through formalizations and their association rows
  under the existing project deletion contract.

## Search and Discovery

Global search should return both conversations and formalizations, visibly
distinguished:

```text
Formalizations
  compact_image — Topology — Proved

Conversations
  Exploring compactness — touched compact_image
```

Searching by project title should continue to find its conversations. Searching
by declaration, module, or path should find the formalization directly.

## Migration and Backfill

The migration must be additive and preserve all existing sessions, runs,
timeline rows, artifacts, and project files.

### Backfill Rules

1. Create one formalization for each existing artifact index row.
2. Use the artifact's project scope when present; otherwise scope it to its
   recorded session.
3. Copy declaration name, kind, path, origin, and timestamps where available.
4. Link the artifact to the new formalization.
5. Associate the artifact's recorded session and run with the formalization.
6. Attribute matching historical code rows by exact normalized path when the
   match is unambiguous.
7. Leave ambiguous or unmatched rows unassociated rather than guessing.
8. Keep sessions with no artifacts as valid conversation-only sessions.

### Multi-Declaration Files

The current first-declaration extractor cannot reconstruct all historical
formalizations from a file containing several declarations.

Backfill may scan all named top-level declarations, but it must:

- avoid creating duplicates for existing artifact rows;
- mark ambiguous file attribution for review;
- never rewrite the Lean source;
- never infer that helper declarations are independent user targets without
  stronger evidence.

### Compatibility

- Existing session and run endpoints remain available.
- Existing artifact endpoints remain available.
- Existing Overleaf target/session associations continue to resolve.
- Old clients may continue to treat a session as one formalization during the
  transition.
- New UI code must prefer formalization validity plus activity over the legacy
  latest-session verdict.

## Accessibility Requirements

- Formalization status must not be communicated by color alone.
- The rail and scope selector must be fully keyboard accessible.
- Status icons need accessible names such as `compact_image, proved`.
- Focus changes must announce the selected formalization without moving keyboard
  focus unexpectedly.
- Canvas filtering must provide a visible way back to `All files`.
- Aggregate summaries must be readable text, not icon-only counters.

## Acceptance Criteria

1. A user can create theorem A and theorem B in the same project session.
2. The session transcript remains one continuous, correctly ordered
   conversation.
3. A and B appear as separate formalizations in the session rail and project
   Formalizations view.
4. A and B retain independent compile and verification status.
5. If A is proved and B fails, A remains `proved`; the session summary reports
   both outcomes rather than displaying one misleading verdict.
6. Selecting A opens A's primary file and filtered history; selecting B opens
   B's.
7. `All files` still exposes scratch, support, and unclassified session files.
8. The composer visibly indicates project discussion, existing focus, or new
   formalization before a run starts.
9. A project-discussion run can mention several formalizations without creating
   or reassigning one.
10. A new formalization can exist as a draft before any Lean file or successful
    artifact exists.
11. Re-formalizing the same declaration updates its evidence without creating a
    duplicate formalization.
12. A formalization may be associated with more than one session.
13. A session with no formalizations remains a valid searchable conversation.
14. Existing clients can create unfocused runs without sending new fields.
15. Existing artifact and Overleaf associations survive migration.
16. Formalization validity and activity are derived from run and ledger
    evidence; no authoritative mutable status column is introduced.
17. Deep-linking to a session and formalization restores both the conversation
    and canvas focus.
18. The UI never labels a disproved statement as proved.
19. Automated tests cover migration, status aggregation, run focus, manual edit
    attribution, multi-session association, and legacy API compatibility.

## Suggested Delivery Slices

### Slice 1 — Read-Only Formalization Presentation

- Backfill formalizations from existing artifacts.
- Add project/session formalization read APIs.
- Add the project Formalizations list.
- Add the session rail and per-formalization canvas filtering.
- Replace the session status dot with aggregate summary copy.

### Slice 2 — Explicit Run Focus

- Add composer scope.
- Add `runs.focus_formalization_id`.
- Attribute run code steps and artifacts to the focus.
- Add focused prompt context.
- Add formalization-aware SafeVerify presentation.

### Slice 3 — New Formalizations Inside Existing Sessions

- Add transactional draft creation through `POST /api/runs`.
- Support statement/declaration confirmation and draft updates.
- Add `+ New formalization` to the session rail.
- Add work-episode markers to the timeline.

### Slice 4 — Multi-Session and Multi-File Workflows

- Add formalization session history and `Continue in conversation`.
- Add explicit support-file association.
- Add dependency/dependent presentation.
- Add formalization search and deep links.

## Open Product Questions

1. Should selecting a formalization filter only code events or optionally filter
   explanatory chat messages as well?
2. Should a new-formalization scope suggestion be produced locally, by the
   intent classifier, or by the first assistant response?
3. When the assistant proposes a different declaration name, should the draft
   update automatically or require user confirmation?
4. Should helper lemmas become independent formalizations, child
   formalizations, or remain support declarations?
5. When one file contains several user-facing declarations, should the canvas
   offer declaration-level navigation inside the file?
6. Which verification evidence is required before a formalization reads
   `proved`: successful `lean_check`, SafeVerify, or a policy-configurable
   combination?
7. Should the default project page emphasize Formalizations or Conversations?
8. How should archived or retired formalizations appear in search and session
   history?
