# Plan — Multi-Formalization Sessions

> **Status:** Implemented — 2026-07-28
>
> Implements
> [`FEATURE-multi-formalization-sessions.md`](FEATURE-multi-formalization-sessions.md).

## Outcome

Ship a standalone Lea workflow in which one conversation can create and work on
several formalizations while every formalization retains independent files,
history, validity, verification evidence, and project navigation.

The finished model is:

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

The critical path is:

```text
schema and attribution
  → derived read model
  → read-only UI
  → scoped run creation
  → edit/check/verify correctness
  → search, Overleaf, and rollout
```

## Implementation Record

The plan was completed on 2026-07-28 as an additive migration and compatible API
extension. The delivered implementation includes:

- first-class formalizations, session membership, file attribution, run focus,
  and snapshot-scoped verification evidence;
- independent derived validity and run activity without a mutable status
  column;
- atomic focused/new-formalization run creation and focused prover context;
- a session scope rail, scoped canvas, project Formalizations view, search,
  deep links, and conversation rename support;
- shared identity and source-freshness propagation through the Overleaf
  companion; and
- migration, service, route, frontend helper, and companion contract coverage.

The cross-conversation consistency revision adds a derived canonical snapshot
for each formalization. The canvas displays that project-wide snapshot by
default while preserving session code steps as historical evidence. Revision
tokens protect manual edits from overwriting work performed in another
conversation.

The standalone scope state was integrated into the existing proof-session store
instead of introducing another state container. This preserves one source of
truth for the selected session, run, and formalization.

## Scope Decisions (Locked)

1. **Session remains the transcript.** There is one ordered session timeline;
   the feature does not create hidden per-formalization chat transcripts.
2. **Formalization is not an artifact alias.** A formalization can be draft,
   running, or failing before a checked artifact exists. The existing
   `artifacts` table remains checked-output evidence.
3. **Validity and activity are separate.** File truth is derived from ledger
   evidence; queued/running/approval state is a run overlay. A retry can display
   `Running · previously proved`.
4. **No stored status.** Neither `formalizations` nor association tables gain a
   mutable status column.
5. **One primary focus per run.** A run may mention several formalizations but
   has at most one `focus_formalization_id`. Null means project/general
   discussion or legacy unclassified work.
6. **Attribution is explicit where writes occur.** New code timeline rows carry
   `formalization_id`; path inference is a compatibility fallback, not the
   long-term source of truth.
7. **One top-level run per session remains.** This feature does not change run
   admission or supersede semantics.
8. **The current project repo remains shared.** No new worktree, prover server,
   or storage boundary is introduced.
9. **Old clients remain valid.** A client may omit all new fields and create an
   unfocused run through the existing API.
10. **List reads are batch reads.** Project/session formalization lists must not
    issue one status query per row.

## Two Model Completions Required Before Coding

The feature specification identifies the right product model but two pieces of
evidence need explicit persistence to implement it correctly.

### Snapshot-Scoped SafeVerify Evidence

Today, standalone SafeVerify writes one verdict onto the session's latest run.
That cannot distinguish theorem A from theorem B and can survive an unrelated
file edit.

Add an append-only `verification_events` table:

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

A SafeVerify result is current only when its `code_step_id` is still the newest
code step for the verified path. Existing `runs.safe_verify_*` columns remain a
temporary compatibility mirror until every UI reader uses verification events.

### External-Source Freshness Provenance

An Overleaf formalization can only be called stale if the system knows both the
current source hash and the source hash used to produce the checked artifact.

Add nullable provenance:

```sql
formalizations.source_hash
runs.focus_source_hash
artifacts.source_hash
```

The companion supplies the current target hash when it creates or focuses a
formalization. The artifact finalizer copies the focused run's hash onto the
artifact. `stale` is derived when both values exist and differ. A missing hash
means freshness is unknown, not stale.

These additions should be reflected in the feature specification in the same PR
as the migration so the plan and specification do not diverge.

## Grounding in the Current Tree

### Adapter

- `adapter/app/store.py` owns session, run, timeline, artifact, and aggregate
  reads/writes.
- `adapter/app/routes/runs.py:create_run` currently performs session creation,
  run creation, and user-message creation as separate writes before enqueueing.
- `adapter/app/bridge.py:run_lea` resolves the session's project repo, replays
  the prior transcript, composes current project context, records
  `FileChanged`/`CheckResult`, and finalizes artifacts.
- `adapter/app/routes/sessions.py` owns session detail plus manual
  write/check/rebuild/SafeVerify operations.
- `adapter/app/routes/projects.py` owns project detail, artifact/ledger routes,
  and project tabs' data.
- `adapter/app/routes/search.py` currently returns only session hits.
- Alembic's current head is
  `0007_merge_artifact_and_compaction`; the next migration should use revision
  `0008_multi_formalization_sessions`.

### Standalone UI

- `App.tsx` creates a new project session from the project composer and creates
  follow-up runs with only `selectedSessionId`.
- `stores/proofSession.ts` owns the open session's timeline, canvas, run,
  approval, and SafeVerify state.
- `hooks/useProofStream.ts` loads session detail and reconciles streamed
  messages/code steps.
- `Canvas.tsx` and `lib/canvasFiles.mjs` treat paths as the current selectable
  unit.
- `ProjectWindow.tsx` lists sessions and owns the project's overview,
  blueprint, and filesystem tabs.
- `SearchOverlay.tsx` only knows how to open session results.
- Frontend unit tests use Node's built-in runner and favor pure `.mjs` helpers;
  new selection, aggregation, and filtering logic should follow that pattern.

### Overleaf

- The companion already knows stable target kind/label/source hash and adapter
  session id.
- It starts adapter runs through `leaApiClient.mjs`, supplying project and
  origin fields but no formalization identity.
- Existing target status is a job/activity overlay plus adapter ledger file
  truth. This must remain valid while formalization ids are introduced.

## Target Schema

Migration `0008_multi_formalization_sessions.py` adds:

### New Tables

- `formalizations`
- `session_formalizations`
- `formalization_files`
- `verification_events`

### New Columns

- `runs.focus_formalization_id`
- `runs.focus_source_hash`
- `timeline.formalization_id`
- `artifacts.formalization_id`
- `artifacts.source_hash`

### Required Constraints and Indexes

- Exactly one formalization scope:
  `project_id XOR loose_session_id`.
- Unique non-null declaration name per project scope.
- Unique non-null declaration name per loose-session scope.
- Unique non-null `(project_id, origin, origin_key)` for stable external
  targets.
- Unique `(session_id, formalization_id)` association.
- Unique `(formalization_id, path)` file association.
- At most one `role='primary'` file per formalization through a partial unique
  index.
- Check constraints for supported file roles and verification status values.
- Indexes for:
  - project formalization lists;
  - session formalization lists;
  - active/latest focused runs;
  - formalization timeline history;
  - latest verification by formalization/path;
  - artifact lookup by formalization.

SQLite `ALTER TABLE ... ADD COLUMN ... REFERENCES ...` is sufficient for the
nullable foreign-key columns; use explicit table creation and partial indexes
for the new entities. Migration tests must inspect constraints and indexes, not
only column presence.

## Backfill Contract

The migration backfill is additive and conservative:

1. Create one formalization for every existing artifact row.
2. Use the artifact id as input to a deterministic formalization id generator
   so rerunning a test migration produces stable results.
3. Scope project artifacts to `project_id`; scope loose artifacts to their
   `session_id`.
4. Copy declaration name, kind, path, and timestamps.
5. Add the artifact path as `role='primary'`.
6. Set `artifacts.formalization_id`.
7. If the artifact has a session, add `session_formalizations`.
8. Set an unfocused historical run's focus only when exactly one
   formalization is the unambiguous artifact candidate for that run.
9. Attribute historical code rows by normalized path only when one
   formalization is the unique candidate in that session/project.
10. Leave ambiguous multi-declaration and shared-path rows null.
11. Do not create formalizations from scratch files, project documents, or
    arbitrary unmatched `.lean` files.
12. Preserve conversation-only sessions.

Backfill must be idempotent in tests even though Alembic executes it once in
production.

## Sequencing at a Glance

| Phase | Deliverable | Depends on | Primary risk |
|---|---|---|---|
| 0 | Schema, backfill, low-level store primitives | — | Incorrect historical attribution |
| 1 | Derived formalization read model and APIs | 0 | N+1 queries / wrong validity |
| 2 | Read-only project/session UX | 1 | Selection state fighting live stream |
| 3 | Scoped run creation and bridge attribution | 0–2 | Partial run/formalization creation |
| 4 | Manual edit, check, SafeVerify, and timeline correctness | 3 | Verdict attached to wrong snapshot |
| 5 | Search, deep links, session naming, multi-session workflows | 2–4 | Navigation ambiguity |
| 6 | Overleaf adoption and legacy reconciliation | 3–5 | Duplicate external identities |
| 7 | Rollout, performance audit, cleanup | 0–6 | Compatibility regressions |

Phases 0 and 1 are backend-only and independently shippable. Phase 2 may ship as
read-only presentation for backfilled artifacts. Phase 3 is the first point at
which the primary new-session workflow is complete.

---

## Phase 0 — Schema, Backfill, and Store Foundation

### 0.1 Add Alembic Revision

Create:

```text
apps/lea-standalone/adapter/migrations/versions/
  0008_multi_formalization_sessions.py
```

The migration should:

- create the four new tables;
- add the five nullable columns;
- create constraints/indexes;
- backfill from `artifacts`;
- leave a non-destructive downgrade with an explicit
  `NotImplementedError`, matching migrations that cannot safely discard
  artifact history.

### 0.2 Add Persistence Primitives

Add cohesive formalization functions to `store.py`, grouped in one section:

- `create_formalization(...)`
- `get_formalization(id)`
- `find_formalization_by_declaration(scope, name)`
- `find_formalization_by_origin(project_id, origin, origin_key)`
- `update_formalization_identity(...)`
- `link_session_formalization(...)`
- `list_session_formalizations(session_id)`
- `list_project_formalizations(project_id)`
- `link_formalization_file(...)`
- `set_primary_formalization_file(...)`
- `record_verification_event(...)`
- `latest_current_verification(...)`

All identity operations that read then create must use `write()` and rely on
unique indexes as the final concurrency guard.

Do not add `latest_run_id`, status, counts, or other derived values to
association rows.

### 0.3 Add Atomic Run Bundle

Refactor `routes/runs.py:create_run` onto one store transaction:

```text
create_run_bundle(
  session input,
  optional focus id,
  optional new-formalization input,
  run input,
  user message
) -> session + formalization + run + message
```

The transaction must:

1. resolve or create the session;
2. validate that focus and session share the same project/scope;
3. create or idempotently resolve a requested new formalization;
4. link session ↔ formalization;
5. insert the focused run;
6. insert the user timeline message;
7. update the session activity timestamp;
8. commit;
9. enqueue only after commit.

An enqueue failure leaves an ordinary pending run recoverable by the existing
startup recovery path. A validation failure writes nothing.

Legacy calls use the same bundle with no focus.

### Phase 0 Tests

Add or extend:

- `adapter/tests/test_migrations.py`
- `adapter/tests/test_store.py`
- new `adapter/tests/test_formalizations.py`
- `adapter/tests/test_routes_runs.py`

Cover:

- all constraints and partial indexes;
- project and loose scopes;
- duplicate declaration/origin races;
- deterministic backfill;
- ambiguous path left unattributed;
- transaction rollback on invalid focus;
- legacy unfocused run creation;
- project mismatch rejection;
- project deletion cleanup.

---

## Phase 1 — Derived Read Model and APIs

### 1.1 Add Formalization Service

Create:

```text
apps/lea-standalone/adapter/app/formalizations.py
```

This module composes raw store rows into the API read model. It should own:

- validity derivation;
- activity overlay derivation;
- session aggregate summaries;
- primary/support file presentation;
- current SafeVerify evidence;
- artifact/source-freshness presentation;
- dependency/dependent projection when already available from the project
  graph.

Keep HTTP concerns in routes and raw SQL in `store.py`.

### 1.2 Define Deterministic Validity

Implement and unit-test a pure derivation function. Recommended precedence:

1. `draft` — no agreed declaration and no files.
2. `missing` — a primary/artifact path is recorded but absent.
3. `planned` — identity/statement exists but no primary output yet.
4. `unchecked` — current primary snapshot has no check verdict.
5. `failing` — current primary snapshot has an error verdict or sorry/admit.
6. `stale` — the output is otherwise usable, but the current external source
   hash differs from the artifact source hash.
7. `disproved` — current checked output belongs to a completed disproof result.
8. `defined` — current checked output is a definition.
9. `needs_review` — checked output requires review.
10. `proved` — checked proof output.

The function receives normalized evidence and performs no database access.

Activity is derived separately from the newest pending/running focused run:
`queued`, `running`, `waiting_for_approval`, or `idle`.

### 1.3 Add Batch Queries

Project/session list functions should use CTEs/window functions or bounded
grouped queries to fetch:

- current artifact;
- primary file;
- latest formalization code step;
- current verification event;
- active/latest focused run;
- associated sessions/counts.

The list endpoint must remain a fixed small number of SQL queries regardless of
formalization count. Add a query-count regression test or instrumented
connection fixture.

### 1.4 Add Routes

Create:

```text
apps/lea-standalone/adapter/app/routes/formalizations.py
```

Register it in `main.py` and implement:

```http
GET   /api/projects/{project_id}/formalizations
POST  /api/projects/{project_id}/formalizations
GET   /api/formalizations/{formalization_id}
PATCH /api/formalizations/{formalization_id}
GET   /api/sessions/{session_id}/formalizations
```

In version 1, `PATCH` may edit display title and statement, and may complete a
draft's declaration identity before it has a checked artifact. Renaming a
declaration that already has an artifact must return `409` until an explicit
source-aware rename workflow is specified.

Also extend session detail with:

- `formalizations`
- `formalization_summary`
- `latest_focus_formalization_id`

`latest_focus_formalization_id` is a derived restoration hint, not session
state.

### 1.5 Legacy Artifact Reconciliation

Change `_record_run_artifacts` so a successful artifact from an unfocused
legacy run:

1. resolves or creates a formalization by scope + declaration;
2. links its session;
3. links the artifact and primary path;
4. leaves the already-finished run unfocused unless attribution is
   unambiguous.

This ensures old clients and older Overleaf companions continue populating the
new project Formalizations view after migration.

### Phase 1 Tests

Add:

- `adapter/tests/test_formalization_status.py`
- `adapter/tests/test_routes_formalizations.py`

Extend:

- `test_routes_sessions.py`
- `test_bridge.py`
- `test_db_sessions.py`

Cover every validity/activity state, the active-retry/previous-valid case,
project aggregates, conversation-only sessions, unfocused artifact
reconciliation, and bounded list-query count.

---

## Phase 2 — Read-Only Standalone UI

### 2.1 Add API Types and Client Functions

Extend `src/app/lib/types.ts` and `lib/api.ts` with:

- `Formalization`
- `FormalizationSummary`
- `FormalizationValidity`
- `FormalizationActivity`
- `FormalizationFile`
- project/session list/detail functions.

Add nullable `formalization_id` to `CodeStep` and focused formalization fields to
run summaries.

### 2.2 Add Formalization Store

Create:

```text
src/app/stores/formalizations.ts
```

Own:

- open session's formalization list;
- project formalization list;
- selected formalization id;
- one-shot composer override (`project`, `existing`, `new`, or automatic);
- per-formalization selected file/step in ephemeral browser state.

Do not duplicate code steps or chat messages into this store. Those remain in
`proofSession`; selectors filter them by formalization id.

Reset selection when the session changes. Restore in this order:

1. explicit deep-link id;
2. current in-memory selection if still linked;
3. API `latest_focus_formalization_id`;
4. most recently active formalization;
5. `All files`.

### 2.3 Add Pure Selection Helpers

Create `src/app/lib/formalizations.mjs` with pure functions:

- `stepsForFormalization`
- `filesForFormalization`
- `sessionFormalizationSummary`
- `restoreFormalizationSelection`
- `formalizationStatusLabel`
- `formalizationStatusClass`
- `inferComposerFormalizationScope`

Node tests should pin fallback and aggregation behavior before React wiring.

### 2.4 Add Session Rail

Create `components/FormalizationRail.tsx` and place it at the canvas boundary.

Desktop:

- compact vertical rail or header-attached panel;
- validity text/icon plus activity overlay;
- `Project discussion`;
- `All files`;
- `+ New formalization` placeholder disabled until Phase 3.

Narrow layouts:

- replace the rail with an accessible selector;
- preserve the same keyboard order and labels.

Selecting a formalization filters the canvas steps/files. It does not filter the
chat transcript.

### 2.5 Make Canvas Scope-Aware

Update:

- `Canvas.tsx`
- `canvasFiles.mjs`
- `useProofStream.ts`
- `proofSession.ts`

Rules:

- focused view receives filtered steps;
- `All files` preserves current behavior, including scratch files;
- streamed focused code steps keep the selected formalization live-following;
- a code step attributed to another formalization updates the canvas to the
  formalization Lea is actually editing;
- SafeVerify shown in Phase 2 is read-only formalization evidence from the API.

### 2.6 Add Project Formalizations Tab

Update `ProjectWindow.tsx` with a `Formalizations` tab and add
`ProjectFormalizations.tsx`.

Initial controls:

- search;
- validity filter;
- activity indicator;
- sort by recent/name/status;
- aggregate counts;
- open formalization in its latest associated session;
- open formalization detail when no session exists.

Keep Conversations as the existing session list but replace the single verdict
dot with aggregate summary and active-run indication.

### Phase 2 Tests

Add:

- `src/app/formalizations.test.mjs`
- component tests only if the existing test environment supports them without
  adding a second frontend runner.

Extend:

- `canvasFiles.test.mjs`
- `timeline.test.mjs`
- `sessionDeepLink.test.mjs`

Manual browser checks:

- A proved + B failing in one session;
- selection survives reload;
- streamed B step does not move a user viewing A;
- keyboard-only rail operation;
- narrow layout selector;
- project with 100+ formalizations remains responsive.

---

## Phase 3 — Scoped Runs and New Formalizations

### 3.1 Extend Run API Contract

Add Pydantic models in `routes/runs.py`:

```python
class NewFormalizationRequest(BaseModel):
    display_title: str
    kind: str
    declaration_name: str | None = None
    statement: str | None = None
    origin: str = "ui"
    origin_key: str | None = None
    source_hash: str | None = None

class RunRequest(BaseModel):
    ...
    focus_formalization_id: str | None = None
    focus_source_hash: str | None = None
    new_formalization: NewFormalizationRequest | None = None
```

Reject requests that send both an existing focus id and a new-formalization
object.

The response adds:

- `focus_formalization_id`
- `formalization` for newly created/resolved drafts.

### 3.2 Idempotent External Creation

For `origin_key != null`, `new_formalization` is create-or-resolve within the
project:

- same origin key and compatible identity → reuse;
- same origin key but conflicting declaration/kind → `409`;
- same declaration but a different external origin → return the existing
  formalization only when explicitly allowed by the caller; otherwise `409`.

This prevents retries and companion restarts from duplicating targets.

### 3.3 Add Focused Prompt Context

In `bridge.py`, resolve the run's focus before composing messages. Add one
current formalization context message after project context and before the
replayed/user turns.

The block includes:

- id, kind, display title;
- agreed statement/declaration;
- primary/support files;
- current validity/activity evidence;
- current source hash;
- instruction not to overwrite sibling formalizations.

Strip stale copies from transcript replay just as project context is stripped
today. Mark the message with a stable sentinel so compaction/replay can identify
it.

Project-discussion runs receive no block.

### 3.4 Attribute Bridge Writes

For focused runs:

- pass `formalization_id` into `add_code_step`;
- do not attribute scratch/candidate paths;
- do not attribute `.lea` project documents;
- link non-scratch `.lean` paths initially as `generated`;
- on successful artifact extraction, promote the file containing the focused
  declaration to `primary`;
- keep other attributable `.lean` paths as `support` or `generated`.

If the focused declaration name is known, artifact extraction must search for
that exact declaration rather than blindly using the first declaration in the
file. Legacy unfocused runs retain the existing first-declaration fallback.

### 3.5 Add Automatic Composer Attribution

`ChatThread.tsx` shows a passive `Scope: automatic` chip rather than a
persistent dropdown. Its menu provides a one-shot manual override.

`App.handleSubmit` loads project candidates when available, resolves explicit
declaration names before using the viewed formalization as a hint, detects new
formalization intent, and passes the resolved focus into `createRun`.

Behavior:

- an explicit theorem/definition name beats the currently viewed item;
- messages naming several formalizations remain project-wide;
- the selected canvas item is only an ambiguity hint;
- an explicit override beats inference and clears after a successful submit;
- actual streamed code attribution updates the visible formalization;
- server validation errors retain the prompt and manual override.

### 3.6 Enable `+ New Formalization`

The rail action:

1. selects composer scope `new`;
2. focuses the composer;
3. leaves the existing chat visible;
4. creates no DB row until submit;
5. immediately renders the server-returned draft before attaching the stream.

The project `New formalization` action creates a new session and draft through
the same atomic run endpoint, rather than first calling
`createSessionInProject`.

### 3.7 Timeline Episodes

Add formalization focus to run summaries and render run episode labels:

```text
Started compact_image
Run 3 · 4 code changes · Proved
```

Do not persist synthetic duplicate chat messages solely for this label; derive
the episode header from run focus and timeline rows.

### Phase 3 Tests

Adapter:

- focused existing run;
- new draft + run atomicity;
- idempotent origin key;
- focus/scope mismatch;
- focused prompt ordering and stale-context removal;
- scratch/project-doc exclusion;
- exact target declaration not first in file;
- primary/support promotion;
- legacy unfocused behavior unchanged.

Frontend:

- explicit-name inference and ambiguity fallback;
- one-shot override serialization;
- new scope replaced by returned id;
- project discussion sends null focus;
- scope survives API error;
- project New formalization creates one session, one draft, one run;
- run episode grouping.

---

## Phase 4 — Manual Edit, Check, and Verification Correctness

### 4.1 Extend Manual Operation Requests

Add optional `formalization_id` to:

- `FileWriteRequest`
- `PathRequest`
- frontend `writeSessionFile`
- `leanCheckSession`
- `verifySession`
- rebuild calls where attribution is recorded.

Validation:

- formalization must be linked to the session or share its project;
- path must be a member of the formalization, unless the operation explicitly
  adds it as a support file;
- the path remains confined by existing filesystem checks;
- `All files` operations may omit formalization id and remain legacy behavior.

### 4.2 Persist Manual Code Attribution

Pass formalization id through:

- `upsert_user_code_step`
- cascade/environment code-step creation;
- `set_code_step_check` validation;
- SSE/reconcile payloads.

Coalescing may only merge user edits when session, path, and formalization id all
match.

### 4.3 Record SafeVerify Events

On `POST /api/sessions/{id}/verify`:

1. resolve the current code step for path/formalization;
2. run SafeVerify;
3. append `verification_events`;
4. return `verification_event_id` and `code_step_id`;
5. mirror to the legacy latest-run columns during compatibility;
6. refresh formalization detail/summary.

An edit creates a newer code step, automatically making the prior verification
non-current without deleting history.

### 4.4 Formalization-Aware Divergence

Replace the session-wide “latest agent file” divergence lookup for focused runs
with:

```text
latest agent code step for (session, formalization)
```

This ensures a manual edit to theorem A is surfaced when the user returns to A
even if theorem B was the session's most recently edited file.

Project-discussion and legacy runs retain the current fallback.

### 4.5 Verification UI

Move `safeVerify` display state from one session scalar to:

- persisted per-formalization read model;
- transient in-flight result keyed by formalization id and code step id.

The canvas should show:

- `Verified` when evidence matches the current snapshot;
- `Verification outdated` when history has a verdict for an older snapshot;
- no verification badge when none exists.

### Phase 4 Tests

Cover:

- editing A does not invalidate B's verification;
- editing A makes A's prior verification outdated;
- verify B after A was the session's latest run;
- coalescing never crosses formalization identity;
- divergence finds an older formalization's edit;
- cascade check attribution;
- legacy path-only manual operation still works.

---

## Phase 5 — Navigation, Search, and Multi-Session Workflow

### 5.1 Formalization Deep Links

Extend the existing session deep-link helper to parse and serialize:

```text
?session=<session-id>&formalization=<formalization-id>
```

Validation behavior:

- linked formalization → load session and focus it;
- same-project but unlinked → open formalization detail and show
  `Not discussed in this conversation`;
- missing/deleted id → retain session and show a non-blocking notice;
- formalization-only link → open project formalization detail.

### 5.2 Global Search Union

Keep `GET /api/search` backward compatible:

```json
{
  "results": [legacy session rows],
  "sessions": [session rows],
  "formalizations": [formalization rows]
}
```

Old clients continue reading `results`. New clients section:

- Formalizations
- Loose chats
- Project conversations

Formalization search matches display title, declaration, statement, project
title, module, and path with the existing escaped-LIKE conventions and query
limits.

### 5.3 Continue in Conversation

Formalization detail lists associated sessions newest-first and offers:

- open latest conversation;
- choose another prior conversation;
- start new conversation focused on this formalization.

Starting a new conversation uses `focus_formalization_id` with no duplicate
formalization creation.

### 5.4 Session Rename

Add:

```http
PATCH /api/sessions/{session_id}
```

with user-editable title only. Session titles remain conversation labels and do
not change automatically when focus changes.

### 5.5 Dependency Presentation

Reuse the blueprint/project graph where declaration mapping is unambiguous.
Return dependencies/dependents in formalization detail without creating a
second stored dependency graph.

### Phase 5 Tests

- search response compatibility;
- declaration/path/project matches;
- keyboard navigation across mixed result types;
- deep-link restoration and mismatch handling;
- one formalization in two sessions;
- session rename does not mutate formalization title;
- dependency links resolve to formalization ids.

---

## Phase 6 — Overleaf Adoption and Legacy Reconciliation

### 6.1 Extend Adapter Client Payload

Update `apps/overleaf-extension/companion/leaApiClient.mjs` so formalization and
chat runs may send:

- `focusFormalizationId`;
- or `newFormalization` with kind, label/title, stable `originKey`, and
  `sourceHash`.

Keep these optional for compatibility with older adapters.

### 6.2 Resolve Stable Overleaf Identity

Use the companion's existing target key:

```text
<project-slug>:<target-kind>:<target-label>
```

as `origin_key`.

Resolution order:

1. formalization id already saved on the job/chat association;
2. adapter formalization lookup by project + origin key;
3. idempotent `new_formalization` in the run request;
4. legacy unfocused run when talking to an older adapter.

Persist the returned formalization id alongside `leaSessionId`.

### 6.3 Preserve Existing Target Status

Do not replace the companion's existing activity + ledger status engine in the
same PR. Instead:

- use formalization identity for navigation and attribution;
- continue using target-status ledger evidence for pane status;
- add contract tests that both surfaces agree on proved/defined/disproved/error
  cases;
- consolidate status readers only in a later dedicated plan if duplication
  remains.

### 6.4 Source Freshness

Send the current target hash on every focused run. The checked artifact receives
the run hash at finalization. Before any new run, the Overleaf pane may continue
computing stale from its current source and ledger/job evidence; once hashes are
available on both sides, adapter formalization detail can report the same stale
state.

### 6.5 Legacy Reconciliation Command

Add an idempotent admin/startup reconciliation helper, not an automatic
destructive rewrite:

- finds artifact rows with null `formalization_id`;
- applies the Phase 0 backfill rules;
- reports ambiguous rows;
- supports dry-run output;
- never rewrites project files.

This handles databases imported from backups that bypassed or partially
predated the migration backfill.

### Phase 6 Tests

Companion:

- payload shaping against new and old adapter contracts;
- idempotent target origin key;
- retry reuses formalization;
- source hash copied through;
- chat and formalize paths converge on one identity.

Integration harness:

- Overleaf theorem creates project + session + formalization + artifact;
- re-formalize reuses identity;
- standalone UI opens the same formalization/session deep link;
- old companion payload still creates a recoverable formalization after success.

---

## Phase 7 — Rollout, Performance, and Cleanup

### 7.1 Rollout Gates

Enable in this order:

1. schema/backfill;
2. read APIs;
3. project Formalizations tab;
4. session rail and aggregate summaries;
5. scoped composer/new-formalization creation;
6. manual edit/check/verify attribution;
7. search/deep links;
8. Overleaf focused payloads.

Each gate should be removable independently until the next one has completed a
full regression cycle. Prefer a single adapter capability flag in the settings
payload over scattered frontend build flags:

```json
{
  "capabilities": {
    "multi_formalization_sessions": true
  }
}
```

The frontend only enables write controls when the capability is present; the
read-only fallback remains usable.

### 7.2 Performance Audit

Measure before/after:

- `GET /api/sessions/{id}` for 10, 100, and 500 code steps;
- project formalization list for 10, 100, and 1,000 formalizations;
- global search at 1,000 sessions + 1,000 formalizations;
- streamed code-step reconciliation;
- SQLite query count per list request;
- React render count when a code step arrives for an unfocused formalization.

Targets:

- fixed query count for list endpoints;
- no per-formalization HTTP fan-out;
- no canvas re-highlight for an unfocused formalization;
- no regression to run-event stream latency.

### 7.3 Compatibility Cleanup

Only after all readers have migrated:

- stop writing new SafeVerify results solely to latest-run compatibility
  columns;
- remove frontend reads of scalar session SafeVerify;
- mark `session.status` as legacy in API types and docs;
- retain old fields for at least one release or until the Overleaf companion
  version floor is raised;
- update architecture/design docs that still say one theorem cluster per
  session.

Do not drop historical columns in this feature's initial migration.

## Failure and Edge-Case Matrix

| Case | Required behavior | Owning phase |
|---|---|---|
| A proved, B fails | A stays proved; session summary shows both | 1–2 |
| Retry of proved A is running | `Running · previously proved` | 1–3 |
| New draft run validation fails | No session/formalization/run/message partial rows | 0 |
| Focus belongs to another project | 422, no writes | 0/3 |
| Focused run writes scratch file | Timeline may show in All files; no formalization file link | 3 |
| Focused run writes project memory | Project context updates; no proof attribution | 3 |
| Target declaration is second in file | Exact target is indexed, not first helper | 3 |
| Same file contains two targets | Both may reference file; ambiguous old steps remain null | 0/3 |
| User edits A after working on B | Edit and next divergence remain attributed to A | 4 |
| SafeVerify A then edit A | Old verification preserved but not current | 4 |
| SafeVerify A then edit B | A verification remains current | 4 |
| Formalization opened from unrelated session link | Explain mismatch; do not silently switch | 5 |
| Same Overleaf target retried | Reuse origin-key identity | 6 |
| Old client sends no focus | Run works; checked artifact reconciles after finish | 1/6 |
| Project deleted | Associations/evidence removed under project cascade contract | 0 |
| Formalization retired | No implicit file deletion | 5 or later |

## File-by-File Change List

### Adapter

New:

- `migrations/versions/0008_multi_formalization_sessions.py`
- `app/formalizations.py`
- `app/routes/formalizations.py`
- `tests/test_formalizations.py`
- `tests/test_formalization_status.py`
- `tests/test_routes_formalizations.py`

Modify:

- `app/store.py`
- `app/routes/runs.py`
- `app/routes/sessions.py`
- `app/routes/projects.py`
- `app/routes/search.py`
- `app/bridge.py`
- `app/main.py`
- `app/artifacts.py`
- migration, route, bridge, session, search, backup, and deletion tests.

Backup/export code must include the new tables automatically or be updated
explicitly if it enumerates tables.

### Standalone Frontend

New:

- `src/app/stores/formalizations.ts`
- `src/app/lib/formalizations.mjs`
- `src/app/formalizations.test.mjs`
- `src/app/components/FormalizationRail.tsx`
- `src/app/components/ComposerScope.tsx`
- `src/app/components/ProjectFormalizations.tsx`
- optional `src/app/components/FormalizationDetail.tsx`

Modify:

- `src/app/lib/types.ts`
- `src/app/lib/api.ts`
- `src/app/App.tsx`
- `src/app/hooks/useProofStream.ts`
- `src/app/stores/proofSession.ts`
- `src/app/components/Canvas.tsx`
- `src/app/components/ChatThread.tsx`
- `src/app/components/ProjectWindow.tsx`
- `src/app/components/SearchOverlay.tsx`
- `src/app/lib/canvasFiles.mjs`
- relevant CSS and pure-helper tests.

### Overleaf

Modify:

- `companion/leaApiClient.mjs`
- formalization/chat dispatch in `companion/server.mjs`
- job/chat persistence shapes;
- companion contract and integration tests.

The extension pane itself need not add a new formalization rail in this feature;
it continues to navigate by its existing target items.

## Verification Commands

Run at every phase boundary:

```bash
cd apps/lea-standalone/adapter
./.venv/bin/python -m pytest

cd ../../..
npm run test:frontend -w apps/lea-standalone
npm test -w apps/overleaf-extension
npm run typecheck -w apps/lea-standalone
npm run test:integration
```

The integration harness becomes mandatory once Phase 3 changes run creation and
at every later phase. Earlier backend-only phases may run it as an additional
regression check.

Migration verification must include:

- fresh database to head;
- existing pre-0008 database to head;
- backup/restore containing formalizations;
- migration with ambiguous multi-declaration artifacts;
- repeated startup after successful migration.

## PR Strategy

Prefer seven reviewable PRs:

1. migration + backfill + store primitives;
2. formalization service + read APIs;
3. read-only standalone UI;
4. scoped run creation + bridge attribution + composer;
5. edit/check/SafeVerify attribution;
6. search/deep links/multi-session UX;
7. Overleaf adoption + integration + compatibility cleanup gates.

Every PR must:

- keep old API callers working;
- include its migration/contract tests;
- update this plan's status table when implementation begins;
- avoid mixing unrelated visual restyling or store refactors.

## Definition of Done

The feature is complete when:

1. One project session can create and discuss at least two formalizations.
2. Each formalization has independent validity, activity, files, history, and
   current SafeVerify evidence.
3. The chat transcript remains continuous and globally ordered.
4. The project exposes Formalizations independently of Conversations.
5. The composer automatically resolves project, existing-focus, or
   new-formalization scope and offers a visible one-shot override.
6. A failing second theorem cannot make a proved first theorem appear failed.
7. Manual edits, checks, divergence, and SafeVerify remain attached to the
   correct formalization snapshot.
8. One formalization can be continued from a second session without
   duplication.
9. Search and deep links open the correct session/formalization pair.
10. Overleaf and standalone runs converge on the same project formalization
    identity.
11. Legacy clients and historical databases remain usable.
12. All adapter, frontend, Overleaf, typecheck, migration, and integration tests
    pass.
13. List/query performance meets Phase 7 targets.
14. No authoritative mutable formalization status is stored.

## Open Risks

1. **Historical ambiguity.** Several declarations may share a file, and old
   timeline rows do not identify which declaration motivated a write. The plan
   deliberately leaves ambiguous rows unassigned.
2. **Artifact/formalization identity drift.** Declaration renames require an
   explicit identity update rather than creating a second target. Rename UX may
   need a follow-up specification.
3. **Support-file ownership.** Two formalizations may legitimately share a
   support file. Version 1 allows many-to-many file membership but must avoid
   claiming that one formalization exclusively owns it.
4. **Status query cost.** Per-formalization evidence is naturally join-heavy;
   batch queries and query-count tests are mandatory.
5. **Context growth.** A long session discussing many formalizations may enlarge
   transcript and project context. Existing compaction remains the mitigation;
   focused context should include only the selected formalization's detailed
   evidence.
6. **Cross-surface freshness.** The adapter cannot know the latest Overleaf
   source hash until the companion supplies it. Unknown freshness must not be
   displayed as current.
7. **Compatibility duration.** Scalar session status and session-level
   SafeVerify cannot be removed until all standalone and companion readers have
   migrated.
