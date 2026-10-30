# PLAN — Blueprint as a system: tools, drafts, reconciliation, unlocks

Audience: Lea engineers and the agents implementing the work packages below.
Companion to `docs/PLAN-ui-redesign.md` (the UI shell this plugs into).

## 0. Why

A run proved `Lea.RealAnalysis.sumChoosePow` (passing check recorded in the DB)
and the blueprint still showed the node as *planned*. The agent had written
`- lean: \`Lea.RealAnalysis.sumChoosePow\` — file \`...\`` (the parser kept the
whole phrase), `- status: PROVED` (ignored), `- uses: none` on six leaves (a
dependency on a node called "none", so those leaves could never become ready)
and a `## Goal` prose heading (a phantom node). Root cause: the agent is told
two incompatible blueprint formats (`prover/workspace/lea.md` vs the project
context message) and nothing checks what it writes.

Decisions taken (do not reopen):

- **The agent never writes blueprint markdown.** It calls three host-mediated
  tools (`blueprint_read`, `blueprint_upsert`, `blueprint_remove`). The system
  validates, assigns declaration names, renders `.lea/blueprint.md`, commits.
- **Humans keep "Edit markdown".** The parser becomes tolerant and the save
  path normalizes to the canonical rendering. The parser never sits between
  the agent and the graph.
- **The key is the declaration's short name.** `assigned lean = <namespace>.<key>`,
  `assigned file = <PascalCase(key)>.lean` unless the node already names them.
- **Status is never in the file.** It is derived (`graph.py`) and the system
  binds nodes to declarations after every passing check (reconciliation).
- **A draft is a state.** A drafting run (`purpose = blueprint_draft`) writes
  `.lea/blueprint.draft.md`; the review panel shows exactly what the system
  accepted; Accept merges selected nodes into the live blueprint, Discard
  deletes the draft.
- **Unlocks are events.** After reconciliation the bridge diffs the graph and
  streams `blueprint_node_changed` and `blueprint_unlocked`.
- **Leaves are ready.** A not-proved node whose every dependency is proved is
  `ready`, vacuously for leaves. The adapter is the only place that computes it.

## 1. How the work is split

Four packages run in parallel, each in its own git worktree on a branch off
the keystone commit on `desktop-app`. A package edits only the files it owns;
anything else goes through the contracts in §2 or into its final report.

| Package | Branch | Owns (create or edit) | Must not touch |
|---|---|---|---|
| **P1 store** | `bp/store` | `adapter/app/blueprint.py`, new `adapter/app/blueprint_store.py`, `adapter/app/graph.py`, `adapter/app/blueprint_seed.py`, blueprint routes in `adapter/app/routes/projects.py` + `adapter/app/api_routes/projects.py`, `EDITABLE_DOCS`/`read_doc`/`write_doc`/`_seed_docs` in `adapter/app/projects.py`, `EDIT_ACTIONS`/`DOC_PATHS` in `adapter/app/workspace_commands.py`, `SNAPSHOT_ACTIONS` in `adapter/app/workspace_effects.py`, tests `adapter/tests/test_blueprint*.py`, `adapter/tests/test_graph.py` | `bridge.py`, anything under `prover/`, `src/` |
| **P2 host** | `bp/host` | new `prover/lea/blueprint_tools.py`, `prover/lea/agent.py` (one dispatch branch), `prover/lea/config.py` (two fields), `prover/lea/tools.py` (protected-path refusal in `write_file`/`edit_file`), new `adapter/app/blueprint_host.py`, `adapter/app/bridge.py` (config builder + one `isinstance` branch next to `ProjectLibraryRequested`), purpose whitelist in `adapter/app/routes/runs.py`, tests `prover/tests/agent/test_blueprint_tools.py`, `adapter/tests/test_blueprint_host.py` | `blueprint.py`, `blueprint_store.py` (import it; stub it in tests), `graph.py`, `projects.py`, `src/` |
| **P3 reconcile** | `bp/reconcile` | new `adapter/app/blueprint_reconcile.py`, `adapter/app/bridge.py` (the `CheckResult` branch and run start only), `compose_context_message` in `adapter/app/projects.py`, `store.create_session` + `SessionCreate` (both routes files, the session create handler only), `prover/workspace/lea.md`, tests `adapter/tests/test_blueprint_reconcile.py` | `blueprint.py`, `blueprint_store.py` (import; stub in tests), `graph.py` internals, `src/` |
| **P4 ui** | `bp/ui` | `src/app/lib/blueprintModel.js`, `src/app/lib/blueprintActions.ts`, `src/app/lib/blueprintApi.ts`, `src/app/lib/api.ts` + `src/app/lib/types.ts` (additive), `src/app/components/blueprint/*`, `src/app/hooks/useProofStream.ts` (new `listen` calls only), `src/app/stores/workbench.ts` (additive), `src/app/lib/slashCommands.js`, `src/app/lib/slashCommandRunner.ts`, `src/app/components/shell/ProjectHome.tsx` (Up next only), `src/app/lib/shellModel.js` (`upNext` only), `src/styles/blueprint.css`, tests `src/app/blueprint*.test.mjs` | `adapter/`, `prover/`, `App.tsx`, `Canvas.tsx`, `ChatThread.tsx`, `Sidebar.tsx` |

Shared, written by the integrator in the keystone commit (read, do not edit):

- `docs/PLAN-blueprint-tools.md` (this file).
- `apps/lea-standalone/fixtures/blueprint/*.md` + `*.expected.json` — the parser
  oracle for both languages (see `fixtures/blueprint/README.md`).
- `apps/lea-standalone/adapter/tests/test_blueprint_fixtures.py` — red until P1.
- `apps/lea-standalone/src/app/blueprintFixtures.test.mjs` — red until P4.
- `apps/lea-standalone/adapter/migrations/versions/0052_blueprint_tools.py` —
  adds `sessions.blueprint_node` and the `blueprint_invocations` table.

Bridge contention: P2 and P3 both edit `adapter/app/bridge.py`. P2 touches the
config builders (near `_project_library_config`) and adds one `isinstance`
branch beside the `ProjectLibraryRequested` one. P3 touches the `CheckResult`
branch and the run-start block where the context message is composed. Keep all
logic in your new module; the bridge diff should be a handful of lines.

## 2. Contracts

### 2.1 Blueprint format

Canonical rendering (what the system writes; `render()` output):

```
# Blueprint — <title>

<preamble prose, verbatim, optional>

## <key>
- kind: theorem | lemma | definition
- lean: `Lea.Ns.key`          (omitted when unknown)
- file: `Path/In/Repo.lean`   (omitted when unknown)
- uses: a, b                  (omitted when empty)

<statement prose or LaTeX, verbatim>
```

Rules:

- One blank line between blocks; file ends with exactly one `\n`.
- Key: `^[A-Za-z_][A-Za-z0-9_']*$`, at most 64 chars. Warning `invalid_key`
  otherwise (the node is still parsed).
- A `## ` section is a **node** iff its header block contains at least one
  recognised header line. The header block is the run of consecutive
  `- word: value` lines directly after the heading (blank lines before the
  first one are skipped). Recognised keys: `kind`, `lean`, `file`, `uses`.
  Unknown keys inside the header block are dropped with warning
  `unknown_header`. `- word: value` lines after the header block are prose.
- A `## ` section with no recognised header line is a **prose section**
  (`{"heading", "body"}`), preserved verbatim and in order by `render()`.
- Text before the first `## ` heading is the preamble (title included).
- Lines inside a ``` fence are inert (never headings, never headers).
- `lean:` value: the first backtick span if any; else the first whitespace
  token with trailing `,;:.)` stripped. Empty → `null`.
- `file:` value: same extraction rule. Empty → `null`.
- `uses:` value: split on commas and whitespace, strip backticks; the sentinels
  `none`, `-`, `—`, `–`, `n/a`, `nothing` (case-insensitive) mean empty **when
  the whole value is a single sentinel token and no node has that key**.
- `kind:` lower-cased; not in {definition, lemma, theorem} → `unknown_kind`;
  absent → `missing_kind`.
- Document-level warnings: `duplicate_key`, `dangling_dependency` (a `uses`
  target that is not a node), `self_dependency`, `cycle` (every node on a
  cycle gets one). Warning shape: `{"code", "node", "message"}`.
- Statuses never appear in the file.

Python API (`adapter/app/blueprint.py`, P1), kept dict-based for callers:

```python
parse(text) -> {"preamble": str, "blocks": [Node | Prose], "nodes": [Node], "edges": [{"from","to"}], "warnings": [Warning]}
# Node  = {"type": "node", "key", "kind", "lean", "file", "uses": [str], "statement"}
# Prose = {"type": "prose", "heading", "body"}
render(parsed) -> str          # canonical; render(parse(render(parse(t)))) == render(parse(t))
validate(text) -> [Warning]    # == parse(text)["warnings"]
```

JS API (`src/app/lib/blueprintModel.js`, P4): `parseBlueprintMarkdown(text)`
returns `{ nodes, preamble, blocks }` with the same node fields;
`serializeBlueprint(nodes, { preamble, prose? })` renders canonically (file
always emitted when present). Nodes are what the fixtures check in JS.

### 2.2 Store (`adapter/app/blueprint_store.py`, P1)

The only module that writes `.lea/blueprint.md` or `.lea/blueprint.draft.md`.
Every write: project mutation lock → read → mutate → `render` → write →
`GitStore.commit_all(repo, message, paths=[rel])` → record a project-scoped
domain change so SQL views invalidate (see 2.9). Never partial.

```python
LIVE = ".lea/blueprint.md"; DRAFT = ".lea/blueprint.draft.md"

def load(project, proofs_root, *, draft=False) -> dict   # parse() of the file; "" when missing
def exists_draft(project, proofs_root) -> bool
def assign_names(namespace: str, key: str) -> tuple[str, str]   # ("Lea.Ns.key", "PascalKey.lean")

def upsert(project, proofs_root, nodes: list[dict], *, draft=False, actor="agent") -> dict
# nodes[i] = {"key" (required), "kind"?, "statement"?, "uses"?, "lean"?, "file"?}
# Merge onto an existing node of the same key (absent fields keep their value;
# "uses" replaces). New nodes append at the end. For every node without lean/file,
# assign_names() fills them. Validation per node, with the whole batch visible
# (a node may use a key defined in the same batch):
#   invalid_key, unknown_kind, missing_kind (new node only), dangling_dependency,
#   self_dependency, cycle, statement_too_long (> 4000 chars)
# Rejected nodes are not written; the rest are. Returns:
# {"applied": [{"key","kind","lean","file","uses","status"}], "rejected": [{"key","code","message"}],
#  "ready": [key], "counts": {"total","proved","ready","stated","failed","planned"}, "draft": bool, "commit_sha": str|None}
# "status"/"ready"/"counts" come from graph.build_graph on the written file (draft: planned/ready only).

def remove(project, proofs_root, keys: list[str], *, cascade=False, draft=False, actor="agent") -> dict
# Refuses a key other nodes use unless cascade (then dependents drop the edge).
# {"removed": [key], "rejected": [{"key","code","message"}], "commit_sha"}   codes: unknown_key, in_use

def accept_draft(project, proofs_root, keys: list[str] | None = None) -> dict
# upsert(selected draft nodes) into LIVE (names already assigned), delete DRAFT, commit once.
# {"applied": [...], "rejected": [...], "commit_sha"}

def discard_draft(project, proofs_root) -> dict   # delete DRAFT, commit. {"discarded": bool}

def normalize(text: str) -> tuple[str, list[dict]]   # (render(parse(text)), warnings) for the human PUT path
```

### 2.3 Tool schemas and replies (`prover/lea/blueprint_tools.py`, P2)

Opt-in, host-mediated, mirroring `project_library_tools.py`: the prover defines
schemas and the events; the loop yields `BlueprintRequested(tool, arguments,
invocation_key)` and expects `BlueprintAck(result=dict | None, error=str | None)`;
the tool result the model sees is `json.dumps(result)` or `"Error: <error>"`.

```
blueprint_read      {"keys"?: [string]}
  → {"nodes": [{"key","kind","statement","uses","lean","file","status","verified"}],
     "ready": [key], "blocked": [{"key","waits_on":[key]}],
     "counts": {...}, "draft": bool}
blueprint_upsert    {"nodes": [{"key": string (required), "kind"?: "theorem"|"lemma"|"definition",
                               "statement"?: string, "uses"?: [string], "lean"?: string}], minItems 1, maxItems 100}
  → store.upsert() result (2.2)
blueprint_remove    {"keys": [string] (minItems 1), "cascade"?: bool}
  → store.remove() result
```

Descriptions (model-facing, write them in full in the schema):

- read: "Read the project blueprint: every node with its derived status, which
  nodes are ready to prove, and what blocks the rest. Call this instead of
  reading .lea/blueprint.md."
- upsert: "Create or update blueprint nodes. The key is the declaration's short
  name (snake_case for lemmas and theorems, camelCase for definitions). The
  system assigns the Lean name `<namespace>.<key>` and a file; declare exactly
  that name. Give every node a one or two sentence statement and list the keys
  it depends on in `uses` (omit for leaves). Never write status. Send all
  nodes of a plan in one call."
- remove: "Remove blueprint nodes by key. Refused when other nodes depend on
  them unless cascade is true."

Config (`prover/lea/config.py`): `blueprint_tools: bool = False`,
`protected_paths: list[str] = []` (repo-relative; `write_file`/`edit_file` on a
protected path return `"Error: <path> is maintained by the system. Use
blueprint_upsert / blueprint_remove instead."` without writing).

Host handler (`adapter/app/blueprint_host.py`):
`tool_request(run_id, invocation_key, tool, arguments) -> dict`, idempotent per
`(run_id, invocation_key)` through the `blueprint_invocations` table exactly as
`project_dependencies.tool_request` does. Routing: `draft = run["purpose"] ==
"blueprint_draft"`. After a write it emits `blueprint_updated` (2.4) through
the bridge (return the payload; the bridge emits).

Bridge wiring: `_blueprint_tools_config(cfg, project, run)` for every project
run: `blueprint_tools=True`, `extra_tools += TOOL_NAMES`, `protected_paths =
[LIVE, DRAFT]`; when `run["purpose"] == "blueprint_draft"` also
`tools = ["read_file", "search_mathlib", *TOOL_NAMES]` (no writes, no checks).
Run purpose whitelist (`routes/runs.py`) gains `"blueprint_draft"`.

### 2.4 Stream events (bridge → SSE; P2 emits `blueprint_updated`, P3 the others)

```
blueprint_updated       {"project_id", "draft": bool, "applied": [key], "removed": [key], "run_id", "session_id"}
blueprint_node_changed  {"project_id", "key", "from": status, "to": status, "lean", "file", "run_id", "session_id"}
blueprint_unlocked      {"project_id", "keys": [key], "cause": key | null, "run_id", "session_id"}
```

`from`/`to` ∈ planned | stated | ready | proved | failed.

### 2.5 HTTP API (P1 unless noted)

- `GET /api/projects/{id}/blueprint` → adds `"nodes"` (parsed live nodes, canonical
  fields), `"draft": {"content", "nodes", "warnings"} | null`. Keeps `content`,
  `warnings`, `file_revision`.
- `PUT /api/projects/{id}/blueprint` → normalizes (`store.normalize`), writes the
  canonical text, returns it as `content` plus `warnings`.
- `PUT /api/projects/{id}/blueprint/draft` → same for the draft (action
  `project.blueprint_draft`, `DOC_PATHS` entry `.lea/blueprint.draft.md`).
- `POST /api/projects/{id}/blueprint/nodes` `{"nodes": [...], "draft"?: bool}` →
  `store.upsert(actor="user")` (action `project.blueprint_upsert`).
- `DELETE`-style `POST /api/projects/{id}/blueprint/nodes/remove` `{"keys", "cascade"?}`
  → `store.remove` (action `project.blueprint_remove`).
- `POST /api/projects/{id}/blueprint/draft/accept` `{"keys"?: [..]}` →
  `store.accept_draft` (action `project.blueprint_accept_draft`).
- `POST /api/projects/{id}/blueprint/draft/discard` → `store.discard_draft`
  (action `project.blueprint_discard_draft`).
- `GET /api/projects/{id}/graph` → node gains `"declared": bool` (lean resolved to
  a declaration in the repo) and `"planned_file"` (the `file:` line, even when
  the file does not exist); `file` stays the resolved file. Graph-level
  `warnings` gains `{"code": "unresolved_lean", "node"}` entries.
- `POST /api/projects/{id}/sessions` (P3) accepts `"blueprint_node": str | null`;
  session rows carry `blueprint_node`.
- `POST /api/runs` (P2) accepts `purpose: "blueprint_draft"`.

All new writes follow the existing pattern: `@workspace_commands.register(action)`
in `routes/projects.py`, `@workspace_commands.intake(action)` in
`api_routes/projects.py`, `_guard_project(write=True)`.

### 2.6 Reconciliation (`adapter/app/blueprint_reconcile.py`, P3)

```python
def reconcile(project, proofs_root, *, changed_path: str | None = None) -> dict
# 1. graph_before = graph.build_graph(...)
# 2. scan declarations (graph._scan_lean_decls). For each node whose lean does not
#    resolve: bind when a declaration's full name == "<namespace>.<key>", or its
#    short name == key and that short name is unique in the repo. Binding = store.upsert
#    with {"key", "lean", "file"} (actor="system", one commit "blueprint: bind <key> → <lean>").
# 3. graph_after = build_graph(...)
# 4. return {"bound": [{"key","lean","file"}], "changed": [{"key","from","to","lean","file"}],
#            "unlocked": [key]}   # unlocked = status became "ready"
def diff_graph(before_nodes, after_nodes) -> {"changed": [...], "unlocked": [key]}   # pure
```

Bridge: in the `CheckResult` branch, when `project and ev.status == "ok" and
accepted`: commit the checked `.lean` file to the project repo
(`gs.commit_all(repo, f"agent lean_check ok: {rel}", paths=[rel])`), call
`reconcile(changed_path=rel)`, emit one `blueprint_node_changed` per change and
one `blueprint_unlocked` when `unlocked` is non-empty (`cause` = the key whose
status became proved, else null). At run start, when the session has
`blueprint_node` and that node has no `lean`, bind names through
`store.upsert` before composing the context message.

### 2.7 Context message (P3, `projects.compose_context_message`)

Replace the blueprint dump and the format tutorial with:

```
## Project Blueprint
<counts line>: "19 nodes · 1 proved · 2 ready (T_three, dvd_of_prime_pow_dvd) · 16 planned"
<if the session is bound to a node>
This session proves `<key>` (<kind>): <statement>
Declare it as `<lean>` in `<file>`. Dependencies: a — proved (A.lean), b — stated with sorry (B.lean): use as a hypothesis, c — planned: state what you need as a hypothesis.
<always>
Use blueprint_read for the full plan and blueprint_upsert to add or change nodes (one call for many nodes). The system renders .lea/blueprint.md; do not edit it.
```

Delete the "Maintain `blueprint.md`" section and its template from
`prover/workspace/lea.md`; keep the coordinator/worker roles text.

### 2.8 Graph (P1)

`ready` overlay: `status in (planned, stated)` and every `uses` target is
`proved` — leaves included. Node fields added: `declared`, `planned_file`.

### 2.9 View invalidation (P1)

SQL views (`peripheral_views`) refresh when `domain_changes` has a newer
project-scoped row. A disk write during a run creates none. After every store
write, insert one: `insert into domain_changes (batch_id, entity_type,
entity_id, action, project_id) values (lea_change_batch(), 'blueprint',
<project_id>, 'update', <project_id>)` via a helper `store.note_blueprint_change(project_id)`.
Acceptance: `peripheral_views.input_revision(conn, project_id)` increases.

### 2.10 Session binding

`sessions.blueprint_node` (migration 0052). `store.create_session(...,
blueprint_node=None)`. The UI reads it from `SessionSummary.blueprint_node`
and falls back to the localStorage bindings for older sessions.

## 3. Briefs and acceptance tests

Each package must make its listed tests pass and leave the previously green
suites green. Write the tests first, in the named files.

### P1 store

Tests: `adapter/tests/test_blueprint_fixtures.py` (keystone, make it pass),
`adapter/tests/test_blueprint_store.py` (new), `adapter/tests/test_graph.py` (extend).

1. Fixtures pass: every case in `fixtures/blueprint` (nodes, prose sections,
   warnings as a set, render idempotence, `canonical` cases byte-identical).
2. `upsert` on an empty blueprint with three nodes where `main` uses `helper`
   and `helper` uses nothing: both assigned `Lea.Demo.<key>` and
   `<Pascal>.lean`; `ready == ["helper"]` (leaf), `main` planned; file
   committed; a second identical call changes nothing (no new commit).
3. `upsert` rejects per node: `uses` a missing key (`dangling_dependency`),
   a cycle introduced by the batch (`cycle` on each node of the cycle),
   `kind: proposition` (`unknown_kind`), key `Bad Key` (`invalid_key`);
   the valid nodes in the same batch are written.
4. `upsert` merge: an existing node with a statement receives `{"key","uses"}`
   only → statement kept, uses replaced; `lean` given explicitly is kept, not
   reassigned.
5. `remove` refuses an in-use key (`in_use`) and removes it with `cascade`,
   dropping the edge from its dependents.
6. Draft: `upsert(draft=True)` writes only `blueprint.draft.md`; `accept_draft(keys=[a])`
   moves `a` into the live file and deletes the draft; `discard_draft` deletes.
7. `normalize` on `02-realanalysis-blend.md` yields a canonical text with 18
   nodes and no `Goal` node, and `sumChoosePow.lean == "Lea.RealAnalysis.sumChoosePow"`.
8. Graph: a leaf planned node is `ready`; a node with `lean` that resolves to
   no declaration has `declared == False` and the graph carries an
   `unresolved_lean` warning; with a declaration in the repo and a current ok
   check it is `proved` (existing test_graph coverage stays green).
9. View invalidation: after `upsert`, `peripheral_views.input_revision(conn,
   project_id)` is greater than before.
10. Routes: `put_blueprint.worker(...)` returns normalized content; the GET
    payload has `nodes` and `draft` (null when absent); accept/discard/upsert
    routes exist and are registered as actions.

### P2 host

Tests: `prover/tests/agent/test_blueprint_tools.py` (new, pattern:
`tests/agent/test_project_library.py`), `adapter/tests/test_blueprint_host.py`
(new, pattern: `tests/test_bridge.py::_fake_run_events` and
`tests/test_project_library.py`).

1. Prover: with `blueprint_tools=True` a `blueprint_upsert` tool call yields
   `BlueprintRequested` with the arguments and a stable invocation key; the
   ack's result reaches the model as JSON; an ack `error` becomes
   `"Error: ..."`; with the flag off the tool is not in the toolset.
2. Prover: `write_file` on a protected path returns the refusal text and
   writes nothing; a non-protected path writes as before.
3. Adapter: `blueprint_host.tool_request` dispatches read/upsert/remove to a
   stubbed `blueprint_store` and is idempotent (same key twice → one store
   call, same reply; same key with different arguments → ValueError).
4. Adapter: a run with `purpose="blueprint_draft"` routes writes to the draft
   (`draft=True` reaches the store) and its config has no `write_file`.
5. Bridge: a fake run that yields `BlueprintRequested("blueprint_upsert", ...)`
   gets a `BlueprintAck` and the SSE stream carries `blueprint_updated` with
   the applied keys; a project run's config has `blueprint_tools=True` and
   `protected_paths == [LIVE, DRAFT]`; a loose (project-less) run has neither.
6. Routes: `POST /api/runs` accepts `purpose: "blueprint_draft"` and still
   rejects an unknown purpose.

### P3 reconcile

Tests: `adapter/tests/test_blueprint_reconcile.py` (new; pattern:
`tests/test_graph.py::_setup_demo` and `tests/test_bridge.py`).

1. `diff_graph`: planned→proved on `a` with `b` depending only on `a` →
   `changed == [a]`, `unlocked == [b]`; no change → both empty.
2. `reconcile`: a node `helper` with no `lean` and a repo declaring
   `Lea.Demo.helper` → bound (`lean`, `file`), one commit, returned in `bound`.
   A short-name match that is ambiguous (two files declare `helper`) is not bound.
3. Bridge: fake run with `FileChanged` + `CheckResult(ok)` on `Helper.lean` in a
   project whose blueprint has `helper` (leaf) and `main uses helper`: the
   `.lean` file is committed in the project repo; the stream carries
   `blueprint_node_changed {key: helper, to: proved}` and
   `blueprint_unlocked {keys: [main], cause: helper}`.
4. Session binding: `create_session(..., blueprint_node="main")` round-trips
   through `list_project_sessions`; `POST /sessions` accepts it; a run in that
   session binds names for `main` before the context message is composed
   (the blueprint file gains `lean`/`file` for `main`).
5. Context message: contains the counts line, the bound node block with
   "Declare it as `Lea.Demo.main` in `Main.lean`", the dependency lines, the
   tool instruction, and no longer contains "## continuous_sq" or the
   format tutorial; `prover/workspace/lea.md` has no "blueprint.md" section
   (assert on the file text).

### P4 ui

Tests: `src/app/blueprintFixtures.test.mjs` (keystone, make it pass),
`src/app/blueprintModel.test.mjs` (extend), new `src/app/blueprintEvents.test.mjs`.
Verification: `npm run typecheck -w apps/lea-standalone`, `npm run test:frontend
-w apps/lea-standalone`, `npm run build -w apps/lea-standalone`.

1. Fixtures pass for `parseBlueprintMarkdown` (nodes, prose not nodes,
   `file`, tolerant `lean`, `uses` sentinels, fences) and `serializeBlueprint`
   round-trips nodes.
2. `isReady`/`readyNodes` use `node.status === 'ready'` only.
3. Pure reducer `applyBlueprintEvent(state, event)` in `blueprintModel.js`:
   `blueprint_unlocked` → notice text "Proved helper · unlocked main, other"
   and `nextKeys == [main, other]`; `blueprint_node_changed` → `refresh`
   bumps; `blueprint_updated` with `draft: true` while a proposal is drafting
   → proposal phase stays `drafting` and refresh bumps.
4. `useProofStream` listens for the three events: bumps `blueprintRefresh`,
   sets `blueprintNotice`, and calls `window.leaDesktop?.notify({title, body,
   sessionId})` only when `document.hidden` or the window lacks focus (guard
   the global; browser builds have none).
5. `/blueprint` drafting: `openOrDraftBlueprint` starts the run with
   `purpose: 'blueprint_draft'`, `scope: 'project'` and the new prompt
   (`draftingPrompt` becomes: "Draft the proof blueprint for this project.
   Decompose the goal into nodes and call blueprint_upsert once with every
   node: key, kind, a one or two sentence statement, and uses. Put the main
   theorem first and its lemmas below it. Do not prove anything and do not
   write files. Then stop and summarise the nodes."). The proposal shows
   `draft.nodes` from `GET /blueprint` with assigned lean names and files;
   Accept → `acceptBlueprintDraft(keys)`; Discard → `discardBlueprintDraft`;
   Edit markdown edits the draft (PUT draft). `generateProjectBlueprint`
   (artifact seeding) stays as the first step.
6. `startNodeSession` passes `node.key` as `blueprint_node`; `bindingPrompt`
   shrinks to "Prove the blueprint node `<key>`." (the server supplies the
   node context). `nodeSessions`/`runningNodeKeys` read
   `session.blueprint_node` first, localStorage bindings second.
7. Notice bar in `BlueprintView`: the unlock notice with a "Prove next (n)"
   button calling `startReadySessions`. `/blueprint auto`: start sessions for
   every ready node now and for each `blueprint_unlocked` afterwards; cap 20
   sessions per invocation; `/blueprint stop` ends it; a notice reports each
   step. `/blueprint next` unchanged.
8. `AddNodeForm` posts through `upsertBlueprintNodes` (no markdown append).
   `NodeDetailPanel` shows the assigned name and planned file for a node with
   `declared === false` ("to declare as … in …").
9. Up next (`upNext`): unchanged semantics; add an `unlocked` badge for keys
   in the latest `blueprint_unlocked` within the session.

## 4. Verification

```
# adapter (from apps/lea-standalone/adapter)
uv run pytest tests/test_blueprint_fixtures.py tests/test_blueprint_store.py tests/test_graph.py -q   # P1
uv run pytest tests/test_blueprint_host.py tests/test_bridge.py -q                                  # P2
uv run pytest tests/test_blueprint_reconcile.py tests/test_bridge.py tests/test_projects.py -q      # P3
# prover (from apps/lea-standalone/prover)
uv run pytest tests/agent/test_blueprint_tools.py tests/agent/test_project_library.py -q            # P2
# ui (from the monorepo root)
npm run typecheck -w apps/lea-standalone && npm run test:frontend -w apps/lea-standalone && npm run build -w apps/lea-standalone   # P4
```

Before reporting done, each package also runs the whole adapter suite once
(`uv run pytest -q`, about 3 minutes) and lists any failure it did not cause.

## 5. Integration order

P1 → P2 → P3 → P4, fast-forward merges into `desktop-app`, full suites after
each. P2 and P3 stub `blueprint_store` in their tests; the integrator replaces
the stubs with the real module by re-running their tests after P1 lands and
fixes signature drift there. Final manual check: `/blueprint prove T_three`
on the RealAnalysis project, watch the node flip and the unlock notice.
