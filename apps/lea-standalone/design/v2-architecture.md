# LeaUI v2 — Backend Architecture & Decisions

> Status: **living doc.** This is the source of truth for the **backend + data**
> design. Scope is **v2 core**; the **projects** feature (Instructions, Memory,
> Files) is deferred to **v2.1**. UI design lives separately in
> [`v2-ui-design.md`](./v2-ui-design.md) / [`v2-mockup.html`](./v2-mockup.html)
> and is **not** being worked yet — backend + data come first.
>
> **How to read this:** §0 is the canonical current design (read this first). §1–§7
> are the numbered decisions D1–D20 with rationale and rejected alternatives — the
> *why*. §9 is the concrete file layout. Build status is tracked live in
> [`v2-backend-progress.html`](./v2-backend-progress.html).

---

## 0. Current architecture at a glance

The canonical backend, as it stands now. (Decision references in parentheses.)

- **One process; the prover is a pure library** (D1, D20). The adapter imports
  `prover/lea` in-process and drives `run_events()`. There is **no CLI and no HTTP
  server** — both were deleted; the prover is `run_events()` + the typed events in
  `lea/events.py`, nothing else. (The sibling external `lea-prover` checkout remains
  the CLI reference.)
- **Three capabilities** the adapter calls (D2): `run` (stream an agent activation),
  `check(path)` (LSP `lean_check`, no model), `verify(path)` (SafeVerify kernel
  replay). `check`/`verify` are callable on a user-edited file with no run.
- **Stateless `run`** (D16): one activation = one pure call; the prover holds nothing
  between activations. The adapter (DB + git + on-disk file) is the single source of
  truth. *(In progress: `run_events` still has internal `_save_session`/`resume`;
  going fully stateless/messages-in is A9.)*
- **Typed event stream** (D17): meaning-level events the adapter *acts* on
  (`FileChanged`, `CheckResult`, `VerifyResult`, `Finished`, `Error`) + display
  events (`TextDelta`, `ToolCall/Result`, …) + one two-way control event
  (`ToolApprovalRequested`, D19). This replaces `runner.py`'s tool-name guessing.
- **Source of truth for code is the filesystem** (D3): the live `.lean` on disk is
  what the agent, the user, `lean_check`, and SafeVerify all touch (keeps the LSP
  fast path). **Git** owns content history — one repo per session under
  `prover/workspace/proofs/<session-id>/` (D7), committing on every write (D8).
  **SQLite** is a queryable *index* over sessions/runs/messages + a curated
  `code_steps` timeline pointing at git commits (D4–D6); it never stores proof code.
- **The session is the spine** (D14): one linear thread of `user message → agent
  activation → user edit → …`. A "run" is just an operational activation record, not
  a UI grouping; there are **no "attempts."**
- **The canvas is writeable** (P2): user edits are first-class run-less steps (D9),
  optionally narrated (D10–D11), and fed to the next activation as a `git diff`
  (D12).
- **Supervision is two layers + a kill switch** (D18, D19): (1) a *conversational
  check-in* — the agent ends its turn to ask, you reply in chat, no buttons; (2) a
  *per-tool gate* — `bash`/`write_file`/`edit_file` prompt allow/deny/always-session,
  read-only tools + `lean_check` auto-allowed. Plus **interrupt/Stop** as the only
  hard control. No permission *tiers*.

**Build status (see the tracker for detail):** A1–A4 done — the typed meaning-level
events exist and are emitted (A1, A2), the permission-tier machinery is gone (A3),
and the prover is a pure library (A4, CLI + HTTP deleted). Next: `check`/`verify`/
the `interface.py` facade (A5–A7), the git store (B), the SQLite index changes (C),
and the adapter bridge + routes (D).

---

## 1. Process architecture

### D1 — One process; prover imported as a library
The adapter imports `prover/lea` as a Python library and drives `run_events()`
**in-process**; it consumes the agent event stream directly. There is no separate
prover process. *(Originally framed as "collapse the two HTTP services" — the prover
ran an HTTP API on `:8000` and the adapter on `:8001`. D20 went further and **deleted
the prover's HTTP server entirely**, so there is no HTTP to collapse; the prover is a
pure library. See D20.)*
- **Why:** deletes `runner.py`'s defensive SSE-normalization layer (the most fragile
  file in the repo); lets the backend share the prover's workspace **and the warm
  LSP daemon** directly; one venv; simpler everything.
- **Rejected:** keeping a separate process (clean black-box boundary, remote-prover
  option) — not worth the normalization tax and the duplicated workspace/LSP access
  the writeable canvas needs.
- **Future implication:** lifecycles are coupled (a prover crash takes the UI
  backend down); we lose easy swap-in of a *remote* prover. Acceptable because we
  **own** `prover/` now and can design the interface cleanly.
- **Status:** the prover side is done (A4); the *adapter* side — deleting
  `runner.py`/`lea_api_client.py` and the `dev.mjs` three-process orchestration —
  is the pending **D-group** work (the bridge). Until then `npm run dev` doesn't run
  end-to-end.

### D2 — The prover exposes three first-class capabilities
The agent loop, LSP-backed `lean_check`, and SafeVerify are **independent services**,
not just agent-internal tools. The interface (shape TBD — see Open Questions):
- `run(...)` — stream an agent loop (events: narration, tool calls, code steps, …).
- `check(path) -> verdict` — LSP `lean_check` on a file, **without** the agent loop.
- `verify(path) -> verdict` — SafeVerify (kernel replay + axiom whitelist).
- **Why:** the writeable canvas needs `check`/`verify` callable on a user-edited file
  with no model run. LSP was added because cold `lean_check` was ~20s; SafeVerify
  because the agent tends to "cheat." Both are reusable building blocks.

---

## 2. Source of truth for proof code

### D3 — Filesystem-canonical; DB is a history log *(not DB-canonical)*
The on-disk `.lean` is the **live working copy** everything operates on; the DB
records history (pointers, below). Restore an old state = write a snapshot back to
the file.
- **Why:** the LSP daemon's ~0.2s edits depend on a **stable file edited in place**.
  Making the DB canonical (re-materializing the file per check) reintroduces exactly
  the cold-compile cost the LSP daemon was added to kill. Lean/LSP/SafeVerify all
  want a real file on disk.
- **Rejected:** DB-canonical (clean transactions, trivial restore) — breaks the LSP
  fast path; fights how Lean works.
- **Cost:** filesystem is mutable shared state → needs a per-session lock during a
  run (mitigated by the existing one-run-at-a-time constraint).

---

## 3. Database

### D4 — Keep SQLite, as an *index*, not the code store
SQLite stays for what files are bad at: cross-session queries (sidebar lists,
search, stats), safe concurrent partial writes during a streaming run, and the
future proof-reuse graph (v2.1). It **never stores proof code**.
- **Why:** sidebar/search/stats are one-line `SELECT`s vs. loading every JSON file;
  SQLite handles incremental mid-run writes + concurrent UI reads for free.
- **Rejected:** filesystem-only (simpler, git-friendly) — you hand-roll querying and
  it slows as history grows. DB-canonical — see D3.
- **Stance:** treat the DB as **disposable, index-grade** data; the `.lean` files +
  git are what truly matter, so the DB stays rebuildable.

### D5 — Schema deltas from the current `db.py`
Existing tables (`sessions`, `projects`, `runs`, `messages`, `code_steps`,
`status_events`, `run_usage_breakdown`) stay. v2 changes:

**`code_steps` — slimmed to a pointer + presentation metadata (D7, D8):**
```
id            text primary key
session_id    text not null
run_id        text                 -- NULL for user edits made outside a run (D9)
step_number   integer not null     -- ordering within the SESSION (continuous timeline, D14)
turn          integer              -- agent turn; NULL for user edits
author        text not null        -- 'agent' | 'user'
path          text not null        -- the file this step shows (relative to session dir)
commit_sha    text not null        -- pointer into the session's git repo  ← the content
summary       text                 -- short label / narration for the step
check_status  text                 -- 'ok' | 'error' | 'unchecked'   (verdict, D6)
check_detail  text                 -- first error line (nullable)
created_at    text not null
```
- **Dropped:** `code` (now in git) — migration keeps the legacy `code` column
  **nullable** for old rows; new rows use `commit_sha`.
- **Deferred:** `used_project_formalizations` (v2.1 projects).

**`messages` — link an edit explanation to its edit (D11):**
- add `commit_sha text` (nullable) — ties an `edit_note` message to the user's commit.
- new `kind` value: `'edit_note'`.

**`sessions`** — optionally cache the canonical primary file path (`lean_path`) and/or
the last `author=agent` commit (for D12's diff-on-divergence); both are derivable, so
caching is an optimization, not required.

### D6 — Verdict lives in the DB, not the commit message
A write is committed *before* `lean_check` returns, so putting the verdict in the
commit message would force a `git amend`. The commit message holds write metadata;
`check_status`/`check_detail` on the `code_steps` row hold the verdict.

---

## 3A. Session & run lifecycle

### D14 — The session is the spine; "runs" are operational, not "attempts"
The collaborative model is **one continuous linear thread** — `user message → agent
activation → user edit(+note) → agent activation → …`, like a chat — **not** a set of
grouped retry "attempts." "Attempts" (retry / best-of-n) is an **autoformalization**
concept; it stays in the separate Lea+eval repo (D15) and never enters the UI.
- A **run** demotes to an *operational* record: one agent-activation episode
  (start→stop), used only for streaming lifecycle (status: `running` / `done` /
  `failed` / `cancelled`; interrupt) and
  per-activation cost/token accounting (the result-card metrics). It is **not** a
  UI-organizing unit.
- **Why:** collaboration is a conversation, not independent shots at a goal; grouping
  by "attempt" fights the linear thread.
- **Consequences:**
  - **Drop v1's `runAttempts` grouping** — do not port it.
  - **`step_number` is session-level continuous** (one timeline across runs + user
    edits) — revises the earlier per-run note in D5/D8.
  - **`session.status` = current working-copy verdict** (the latest `check_status`,
    agent- or user-produced) — *not* "last run outcome," because a user edit can change
    the proof's state after a run completes (P2).
  - `run_id` on `code_steps`/`messages` stays for provenance/accounting only (which
    activation produced a row; `NULL` for user edits) — never for grouping.

### D15 — The vendored prover is UI-only; eval lives elsewhere
Deleted `eval/`, `blueprints/`, `fqb-reports/`, `EVALS.md`, and `tests/cheats/` from
`prover/`. Best-of-n, the miniF2F/Putnam/FQB harnesses, and the cheat-regression suite
live in the **separate Lea + eval repo**. The vendored copy exists only to serve the
collaborative UI, so it stays lean.
- **Why:** evals are maintained in their own repo (and the upstream Lea checkouts);
  duplicating them here is dead weight that would only drift.

### D20 — The vendored prover is a *pure library* — no CLI, no HTTP server
LeaUI uses exactly one of the prover's three historical consumers: the **library**
(`run_events()` + the typed events). The other two — the **CLI** (`lea/cli.py` →
stdout via `lea/render.py`, plus the `run()` wrapper and `~/.lea/sessions` listing)
and the **bundled HTTP server** (`lea_api/`) — are deleted. The adapter imports the
prover as a library (D1) and never shells out or talks HTTP, so both are dead weight.
- **Deleted (A4):** `lea/cli.py`, `lea/render.py`, `agent.run()` stdout wrapper,
  `list_sessions`, the `lea`/`lea-api` entry-point scripts, `lea_api/` (the whole
  FastAPI server), and their tests (`tests/render/`, `tests/api/`).
- **Why now:** we vendored the prover precisely so we could carve it down to what
  LeaUI needs without babysitting the CLI/HTTP surfaces. Keeping them green was real
  wasted effort (A1–A3 spent it). A pure library is less to reason about and lets us
  integrate cleanly.
- **Supersedes the diff-ability stance.** `CLAUDE.md` previously kept the prover's
  layout "unchanged from upstream, diff-able against a reference checkout." That goal
  is now served by the **sibling external `lea-prover` checkout** (the CLI reference
  point), so the vendored copy is free to diverge. `CLAUDE.md` updated to match.
- **Deferred to D1 (still pending):** the *LeaUI-side* orchestration still references
  the deleted server — `scripts/dev.mjs` (starts `python -m lea_api` on :8000),
  `doctor.mjs`, `Dockerfile`, `package.json`, and the adapter's `lea_api_base_url`
  HTTP client. That whole three-process → one-process rewire is D1; until it lands,
  `npm run dev`/Docker won't run end-to-end (the v1 stack is down during the refactor,
  as accepted). The prover's `pyproject` `[api]` extra (fastapi/uvicorn) is kept for
  now so `npm run setup` still resolves; it is removed together with that rewire.
- **Still pending (A9):** `_save_session`/`_load_session`/`SESSIONS_DIR`/`resume` stay
  for the moment — they're `run_events`'s *internal* multi-turn mechanism, not CLI-only.
  They die when `run_events` goes stateless/messages-in (the real D16 signature change).

---

## 3B. The `run()` interface

### D16 — `run()` is stateless (adapter-authoritative)
One agent activation = one pure call; the prover holds **nothing** between activations.
```
run(messages, working_file, model_cfg, max_turns, diff_context, hooks)
    -> event stream          # hooks = { interrupt-flag, gate-policy }  (D18/D19)
                             #   gate-policy = adapter-supplied gated-tool set + session
                             #   allowlist; tool-approval decisions return via .send() (D19)
```
- The adapter (DB + git + on-disk file) is the **single source of truth**; the prover
  is pure compute over it.
- `working_file` is passed **by path, not contents** (FS-canonical, D3) — the agent and
  a manual edit always see the same bytes.
- **Why:** a crash mid-session loses nothing (the next call rebuilds context from disk);
  no in-memory drift; cleanest library boundary; eases the eventual server collapse
  (no session state to migrate).
- **Rejected:** a stateful, prover-held session — in-memory state drifts from the DB,
  resume-after-restart is hard, and it re-introduces hidden state (part of what makes
  today's `api_session_id` bridge fiddly).
- **On "re-passing the transcript":** the adapter→prover hand-off is in-process and
  **free** (D1, same Python memory). The *model* still receives the full history every
  turn — but that's inherent to LLM conversations (the API is itself stateless) and
  **identical under stateful or stateless**, so it's not a cost of this choice.
  Long-session context-window growth is a separate, later concern (summarization).
- **The library `run()` is persistence-free — no `_save_session`.** The prover's own
  session memory (`agent.py`'s `_save_session` → `~/.lea/sessions/<id>.json`, plus the
  `--sessions` listing / `--resume`) is a **CLI-only** concern. The adapter owns the
  transcript (SQLite `messages` + git), so prover-side JSON would be a *second copy that
  drifts* — exactly the hidden state this decision removes (same fiddliness as today's
  `api_session_id` bridge and `lea_api`'s job store, both of which the adapter seam also
  bypasses). **Action when we modify `prover/`:** move the `_save_session` calls *out* of
  the core loop and *up* into `cli.py`'s wrapper, so `run()` just yields events and the
  caller decides whether to persist. Don't delete `_save_session` outright — the
  standalone CLI still uses it (keeps `prover/` diff-able against upstream); the adapter's
  import seam simply never goes near it.

### D17 — Typed events for side-effects; generic tool events for display
`run()` yields a stream of typed events. **"Semantic" = meaning-level** ("a file
changed") rather than mechanical ("a `write_file` tool ran"). Split by purpose:

**Meaning-level events** — for everything the adapter must *act* on, so it reacts
directly and never decodes tool names / argument shapes (this is what lets us delete
`runner.py`'s guessing):
- `FileChanged(path)` → adapter commits to git + inserts a `code_step`
- `CheckResult(path, status, detail)` → verdict on that step
- `VerifyResult(status, detail)` → SafeVerify badge
- `Finished(status, final_text)` → result card (status includes "ended turn / awaiting
  human" — the agent choosing to check in is just a normal turn-end, D18)
- `Error(message)` → failure

**Streaming / display events:**
- `TextDelta(text)` → narration → `assistant_delta`
- `TurnStarted` / `TurnEnded` → message-flush + step boundaries
- `ToolCall(name, args)` / `ToolResult(…)` → generic "worked via" chip (**display
  only**; new tools render without a schema change)
- `UsageUpdated(input, output, cost)` → running metrics

**Control event (two-way)** — the one event the adapter answers, not just renders:
- `ToolApprovalRequested(tool_name, args)` → the per-tool gate (D19). The adapter
  auto-allows (read-only / allowlisted) or prompts the human, then sends
  `allow | deny | always_session` back into the run via `.send()`.

- **Why hybrid:** the prover owns the tools, so it's the natural place to emit
  meaning-level events; the adapter then needs to know *nothing* about tool
  conventions. Generic tool events stay only for display, keeping new tools cheap.
- The current UI SSE names (`assistant_delta`, `message`, `code_step`, `status`,
  `error`, `done`) become the adapter's *re-emission* of these to the browser — same
  browser contract, now fed by typed events instead of normalized JSON. The
  `approval_*` SSE events return — but now for the **per-tool gate** (D19), not the
  deleted permission-tier flow.

### D18 — Supervision is two layers: a conversational check-in (this) + a per-tool gate (D19)
Supervision has **two orthogonal layers**. They answer different questions:
- **Layer 1 — "should we go this way?" (this decision):** the agent's *judgment*. It ends
  its turn when the *math/plan* needs the human. Semantic, agent-initiated, lives in the
  prompt. This is the original "approval = conversation."
- **Layer 2 — "is it OK to run this exact command?" (D19):** a mechanical safety gate that
  pauses before each impactful tool call. Adapter-enforced, deterministic.

There is **no permission-*tier* system** — no `theorem_translation` / `stepwise` tiers,
no candidate/best-of-n approval framing (that was autoformalization machinery; deleted in
A3). The two layers above replace it.

**Layer 1 details:**
- The agent **ends its turn when it wants input**, exactly like a chat assistant ending a
  message. That's a normal activation end (`Finished`), not a special pause; the human's
  next chat message is the next activation.
- **Accept / decline = the next user message**, free-form. **No Accept/Decline buttons:**
  a button would only inject a canned `user_message`, which the chat already does — and
  the human can add things a button can't foresee ("go ahead, but name it `foo`", "put
  it in `bar.lean`", "yes, but skip X"), which the agent handles naturally.
- **When to check in lives in the prompt** (prover-side, P1): *"after translating the
  statement, present it with your plan and stop for the human; during routine
  search/edit, keep going and narrate — don't ask per step."* Routine multi-step work
  (e.g. a run of searches) proceeds without asking, since the goal was already stated.
- **Trade-off:** the check-in is the agent's judgment, not a hard gate, so there's no
  *guarantee* it stops before proving. Mitigated by live narration, the D19 gate on
  impactful tools, and always-available interrupt.

**Interrupt / Stop** still exists, independent of both layers: a stop flag the adapter
flips; the agent checks it between steps and stops **cleanly** (file committed, canvas
accurate) rather than a hard kill. `max_turns` is just a number; the one-run lock just
greys out a second concurrent run.

### D19 — Per-tool approval gate, Claude-Code-style (the mechanical safety layer)
Before an **impactful** tool call runs, the agent pauses and asks the human
**allow / deny / always-allow-this-session**. This is layer 2 of D18.

- **Gated tools:** `bash`, `write_file`, `edit_file` — the ones that run shell or change
  the proof. **Auto-allowed (never gated):** `read_file`, `search_mathlib`, and
  `lean_check` (read-only or effect-free; `lean_check` runs constantly via the warm LSP, so
  gating it would be torture). The human still *sees* every auto-allowed call via live
  narration + the step timeline — they just don't have to approve the harmless ones.
- **"Always allow this session"** adds the tool to a **per-session, in-memory allowlist**;
  subsequent calls of that tool skip the gate for the rest of the session. Resets each
  session (re-consent is a safe default). **Granularity = per-tool** for v2 ("allow all
  `bash`"); per-command patterns (`bash(lake build)`) are a later refinement.
- **Mechanism — reuse, don't rebuild.** The agent loop already has a two-way generator
  hook: `decision = yield ApprovalRequested(...)`. D19 **repurposes** it from the deleted
  tier-gate to a general tool gate: rename to a tool-shaped event (e.g.
  `ToolApprovalRequested(tool_name, args)`), yield it *before executing a gated, not-yet-
  allowed tool*, and receive `allow | deny | always_session` back via `.send()`. Deny
  returns a tool error to the model ("the human declined; choose another step") so the run
  continues rather than dying.
- **D16-compatible (not hidden state).** The pause is *within* one activation — the
  adapter drives the generator and owns all state; the decision comes back in-process via
  `.send()`. If the process dies mid-pause the activation just restarts (same as any
  crash). The prover still holds **nothing between activations**, so "stateless between
  activations" (D16) survives; we only add a human-in-the-loop suspension point *inside*
  an activation.
- **Where each piece lives:** the gate-or-not policy + allowlist are the **adapter's**
  (it owns supervision); the prover just yields the request and honors the decision. The
  browser shows an allow/deny/always prompt and POSTs the choice to a small approval
  endpoint (re-added — see §6), which `.send()`s it into the run.
- **Trade-off:** first `bash`/`write` of a session prompts; "always allow" then makes the
  steady state frictionless — the deliberate Claude-Code shape (default-ask, fast
  allowlist) rather than a heavy always-on gate.

---

## 4. Filesystem & git layout

### D7 — Git owns proof content history; one repo per session
- Each session = `prover/workspace/proofs/<session-id>/`, **its own git repo**.
- The `.lean` files live there (so Lake/Lean can compile them; `.git` dirs are
  invisible to Lake). This dir tree is already gitignored from the LeaUI repo —
  it's runtime data, not a submodule.
- **Why per-session repo (vs one shared repo):** the dir *is* the session — `git log`
  there is its full history, deleting a session is `rm -rf`, no contention, portable.
- **Future drawbacks (v2.1, logged):**
  1. Promoting a proof into a project moves its file into `projects/<slug>/` — across
     *separate* repos, so git history doesn't follow (would need grafting). Mitigation:
     promotion cares about the *final* proof; the DB index + final file persist; the
     old per-session repo can be archived.
  2. Project-scope "git as memory" spans many repos, not one `git log` — derive it
     from the DB index or a project-level repo later.
  3. `<session-id>` makes Lean module names throwaway until project assignment re-homes
     the file with a real namespace — fine for standalone `lean_check`.

### D8 — Commit every write; the DB index curates which commits are "steps"
**Commit granularity ≠ step granularity.**
- **Git:** commit on **every write** (`write_file`/`edit_file`), *including failed,
  non-compiling states*. Writes are model-latency-gated (seconds apart) and roughly
  one per editing-turn, so it's not noisy; failed states are valuable as memory.
  Commit message stamps `turn`, `author`, tool.
- **DB:** a `code_steps` row is inserted only at **step boundaries** (per turn / per
  meaningful change), pointing at the relevant commit SHA. Git keeps everything; the
  canvas stepper shows the curated subset.
- **Canvas read path:** snapshot content = `git show <sha>:path`; step diff =
  `git diff <prev_sha> <sha>`.

---

## 5. The writeable canvas — human as co-author (UI principle P2)

### D9 — User edits are first-class, run-less steps
A manual canvas edit + manual `lean_check` produces a `code_steps` row with
`run_id = NULL`, `author = 'user'`, and a git commit (`author=user`). No model turn
required — human iteration is free.
- **Why:** the canvas timeline naturally interleaves agent and human contributions;
  zero model cost for human edits; `author` future-proofs attribution and feeds
  git-as-memory.
- **Cost:** queries/stats must tolerate `run_id IS NULL`; user steps have no narration
  message unless the user explains the edit (D10).

### D10 — Edits can be narrated; explanation triggers a run by default
After an edit, the composer shows a badge: `✎ Edited <file>` + a *"Explain your
edits…"* box (optional). Two actions:
- **"Send & continue"** (default) — records the explanation **and triggers an agent
  run**.
- **"Save note"** — records only, no run (the release valve so editing can stay free).
- **Why default-trigger:** keeps the loop conversational/interactive. Honest trade:
  most explained edits then cost a turn; "Save note" is the opt-out.

### D11 — The explanation is a linked message (no special plumbing)
The explanation → a `messages` row (`role='user'`, `kind='edit_note'`, `commit_sha`
set to the edit's commit). Because it's a normal transcript message, it rides the
**existing** path that feeds context to the prover — no bespoke channel. The chat
renders it inline as a small edit card; the canvas step can surface it.

### D12 — Agent context on human edits = diff-on-divergence
Track the last `author=agent` commit SHA the agent "knew." When a run starts and
`HEAD ≠ that SHA`, inject `git diff <last-agent-sha>..HEAD` (+ any pending
`edit_note`) into the run context.
- **Why a diff, not the whole file:** points the agent at exactly what the human
  changed. One uniform rule covers both: explained edits → *diff + note*; unexplained
  edits → *diff alone* (agent infers, nothing fancy).

### D13 — Prover-side responsibilities (logged for when we modify `prover/`)
- Accept a "human edited the file — here's the diff (+ optional note)" context block.
- **Acknowledge** the edit in narration before continuing ("I see you swapped the
  import line" / "Your note: '…' — taking that into account") — the receipt that
  tells the human their change was considered.
- Handle explained **and** silent edits.
- (Separately, P1:) produce genuine mathematical narration — collaborator, not
  autoformalizer.

---

## 6. API surface implied (adapter)

New/changed endpoints the above implies (shapes TBD):
- `POST /api/sessions/{id}/file` — write the working copy (user edit) + commit.
- `POST /api/sessions/{id}/lean-check` — standalone `check` on the working copy
  (LSP), outside a run; returns verdict.
- `POST /api/sessions/{id}/verify` — standalone SafeVerify.
- Run start consumes diff-on-divergence context (D12).
- `edit_note` flows through existing message creation; `commit_sha` linkage added.
- `POST /api/runs/{id}/approvals/{approval_id}` — the human's `allow | deny |
  always_session` decision for the per-tool gate (D19); the adapter `.send()`s it into
  the paused run. (Repurposed from the old approval endpoint, not net-new.)

Existing live-run SSE (`assistant_delta`, `message`, `code_step`, `status`, `error`,
`done`) is **reused**; `code_step` now carries a `commit_sha`. The `approval_*` SSE
events + endpoint return — but for the **per-tool gate** (D19), not the deleted
permission-tier flow. Layer-1 accept/decline (D18) is still just the next user message.

---

## 7. Deferred / future (not v2 core)

- **Projects** (v2.1): project window, Instructions (→ per-project `lea.md` prompt
  inject), Memory (new storage), Files/uploads, proof-reuse graph. See
  `v2-ui-design.md` §3.4 / §7.2.
- **Git as agent memory** (future): feed `git log`/diffs of past attempts into the
  prompt — durable episodic memory. Prover-side change + token cost. Dovetails with
  project Memory.
- **Global search** (later): SQLite FTS5 over `sessions.title` + `messages.content`.

---

## 8. Open questions (next topics)

1. ~~`run()` control hooks~~ — **resolved (D18/D19):** supervision is two layers —
   layer 1 conversational check-in (D18), layer 2 per-tool gate on bash/write/edit (D19,
   reusing the two-way `yield`/`.send()` hook); plus interrupt/Stop. `max_turns` is a
   number; the one-run lock greys out a second concurrent run. The `run()` interface is
   now fully specified — D16 (shape) · D17 (events) · D18+D19 (control).
2. Exact step-boundary policy in D8 (per turn vs per state-change) — affects which
   commits become steps.
3. Whether `sessions.lean_path` / `last_agent_commit` are cached columns or always
   derived (D5).
4. Concurrency: what the UI allows if a user edits while a run is active (lock the
   editor, or queue the edit as the next working state).

---

## 9. File layout

Where the design lands on disk. ✅ = exists now · 🔜 = planned (todo in parens).

### Prover — the library (`prover/lea/`)
```
prover/lea/
  agent.py        ✅ run_events() generator + the meaning-level event emission (A1/A2)
  events.py       ✅ typed events: TextDelta, FileChanged, CheckResult, VerifyResult,
                  ✅   Error, ToolCall/Result, Finished, TurnStarted, UsageUpdated
                  🔜   + ToolApprovalRequested for the per-tool gate (A8)
  tools.py        ✅ the tools (write/edit/read/lean_check/search/bash)
  lsp_daemon.py   ✅ warm LSP — the check() fast path
  prompt.py / providers.py / validation.py / config.py / registry.py / … ✅ unchanged
  interface.py    🔜 facade re-exporting run / check / verify + event classes (A7)
                  🔜   — the ONE import surface the adapter uses
  (check/verify)  🔜 standalone check(path) (A5) + verify(path) via SafeVerify (A6)
prover/third_party/SafeVerify/  ✅ kernel-replay grader (verify build block)
prover/workspace/proofs/<session-id>/  🔜 per-session git repos (B1, runtime data)
```
Deleted (A4, D20): `lea/cli.py`, `lea/render.py`, `lea_api/`. There is no CLI or HTTP
server — `interface.py` + `events.py` are the whole public surface.

### Adapter — the backend (`adapter/app/`)
Current files are the **v1** shape (HTTP client to the old prover API). The v2 target:
```
adapter/app/
  main.py         🔜 FastAPI app construction + wiring only (thin)
  routes/         🔜 endpoints split by resource (D2 of the D-group)
    sessions.py   🔜   create/list/get; POST file (user edit), lean-check, verify
    runs.py       🔜   run start, SSE stream, interrupt, the approval decision (D19)
    messages.py   🔜   message create incl. edit_note
    settings.py   🔜   settings + model catalog
  bridge.py       🔜 THE prover seam: import lea.interface, drive run_events(),
                  🔜   map typed events → SSE. REPLACES runner.py (D1/D17)
  git_store.py    🔜 per-session repo: init, commit-every-write, show(sha), diff (B)
  db.py           ✅ schema + migrations  (C adds the slimmed code_steps / edit_note)
  store.py        ✅ query layer over db
  config.py / settings.py / models_catalog.py  ✅ keep
```
Deleted by the D-group (pending): `runner.py`, `lea_api_client.py` (D1 — no HTTP).
Deferred to v2.1: `project_assignment.py` / `project_unassignment.py` / `project_usage.py`.

### LeaUI orchestration (pending D-group rewrite)
`scripts/dev.mjs` / `doctor.mjs` / `Dockerfile` / `package.json` still start & health-check
the deleted `lea_api` HTTP server. They collapse from three processes to one when the
bridge lands; until then `npm run dev` / Docker don't run end-to-end.

---

## Decision index

D1 one process · D2 run/check/verify capabilities · D3 filesystem-canonical ·
D4 keep SQLite as index · D5 schema deltas · D6 verdict in DB · D7 per-session git
repo · D8 commit-every-write, DB curates steps · D9 user edits are run-less steps ·
D10 explain-edit triggers run, with opt-out · D11 explanation = linked message ·
D12 diff-on-divergence context · D13 prover-side edit handling + acknowledgment ·
D14 session-as-spine, runs operational (no "attempts") · D15 vendored prover is
UI-only (eval deleted) · D16 stateless adapter-authoritative run() · D17 typed events for side-effects,
generic tool events for display · D18 supervision = layer 1 conversational check-in
(no tiers/buttons; the agent ends its turn to ask) · D19 supervision layer 2 = per-tool
approval gate on bash/write/edit (Claude-Code-style allow/deny/always-session; reuses the
two-way yield/.send() hook; auto-allows read-only + lean_check) ·
D20 vendored prover is a pure library — CLI (`cli.py`/`render.py`/`run()` wrapper) and
HTTP server (`lea_api/`) deleted; only `run_events()` + typed events remain.
