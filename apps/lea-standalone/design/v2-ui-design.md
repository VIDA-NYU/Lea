# LeaUI v2 — UI Design

> Status: **draft / living doc.** Companion to the interactive mockup at
> [`design/v2-mockup.html`](./v2-mockup.html) (open it in a browser; the chat
> steps, canvas stepper, project window, and ⌘K search are all clickable).
>
> This documents the v2 UI rebuild. The backend (adapter at `adapter/`, vendored
> prover at `prover/`) is staying; only the React/TS frontend is rebuilt from
> scratch. See `CLAUDE.md` for backend architecture.

---

## 1. Vision

A **Claude-web-app-style** interface for the Lea theorem prover. Three regions:

```
┌──────────┬───────────────────────┬──────────────────────┐
│ SESSIONS │        CHAT           │       CANVAS         │
│  (left)  │   (middle)            │      (right)         │
│          │   NL chat + agent     │   evolving Lean      │
│ Chats /  │   narration + tool    │   code snapshots,    │
│ Projects │   activity as steps   │   stepper, verdict   │
└──────────┴───────────────────────┴──────────────────────┘
```

The middle is the conversation (natural language + agent activity); the right is
the **artifact** (the Lean proof), exactly like Claude's artifacts/canvas. A
"step" in the chat is paired 1:1 with a code snapshot in the canvas.

Projects are **first-class workspaces** (like Claude Projects), not just a filter.

---

## 1a. Foundational principles

These shape every decision below.

### P1 — The agent is a *collaborator*, not an autoformalizer
Lea should **think out loud in natural language**: explain the mathematical idea,
the strategy, and the choice it's facing — *then* act. The Lean code and tool calls
are the supporting artifact of that conversation, not the main event.

Concretely, in the chat each step **leads with mathematical reasoning** (the plan /
explanation) and **demotes tool calls** (`search_mathlib`, `lean_check`, …) to a
secondary "worked via" line. The agent surfaces choices ("use Mathlib's result, or
formalize the elementary argument line by line?") and invites the user to steer.

> **Prover-side implication:** this needs Lea's prompt modified to produce genuine
> mathematical narration — beyond the existing terse `narrate_tool_steps`. That's an
> intended change; we own `prover/` now. The narration still flows through the
> existing `assistant_delta` / `message` SSE events, so the adapter doesn't change.

### P2 — The canvas is *writeable* — the human is a co-editor
The Lean canvas is **not read-only**. The user can **edit the proof directly** and
**run `lean_check` (and SafeVerify) themselves**. If the user tweaks the code, Lea
continues from whatever they leave in the file. Proving is two-way: the agent and
the human write into the same file. (Mockup: ✎ Edit + ▶ Run lean_check in the
canvas header.)

> **Implication:** the canvas needs an edit mode + a manual check action wired to
> the adapter, and the runner must treat user edits as the new working state. See
> §7.2.

---

## 2. Information architecture

- **Sidebar has two groups:**
  - **Projects** — list of projects; clicking one opens its *own window* (§3.4).
  - **Chats** — **loose (non-project) sessions only**, grouped by date.
- **Project sessions never appear in the Chats list** — they live inside the
  project window. This keeps the left list from getting crowded. *(decided)*
- **Search is global.** One ⌘K overlay spans **everything** — loose chats *and*
  project sessions (each tagged with its project). So project work is hidden from
  the sidebar but always findable. *(decided)*

Mental model: a session belongs to **at most one** project, or none ("loose").

---

## 3. Layout regions & decisions

### 3.1 Sidebar (left, ~264px)
- Brand, `+ New proof`, global search button (⌘K), Projects group, Chats group,
  account/settings footer.
- Session rows show a **status dot**: 🟢 proved · 🟡 running · 🔴 failed.
- Project rows show an icon + proof count.

### 3.2 Chat (middle)
- Header: session title, live status chip, model chip, canvas toggle.
- **Agent tool activity is grouped into collapsible "step" cards**, each = one
  unit of narration + its tool calls (`search_mathlib`, `write_file`,
  `lean_check ✓/✗`). *(decided, but see open Q-1)*
- **The agent checks in conversationally** — no approval gate, no buttons. When it
  wants confirmation (e.g. after translating the statement), it presents the Lean
  skeleton + its plan as a normal narration step and **ends its turn**; you reply in
  the chat however you like ("go ahead" / "rename it to…" / "no, I meant…").
  *(architecture D18 — no permission tiers, no Accept/Decline.)*
- Run ends with a **terminal result card**: green (proved) / red (failed /
  max-turns), with metrics (steps, tool calls, tokens, cost, time).
- Composer at the bottom with a **run-mode switch** (formalize vs assistant/chat
  run) and a model switch. *(mode switch placement is open — Q-4)*

### 3.3 Canvas (right, ~46%, collapsible)
- Header: file path + close.
- **Stepper**: `‹ Step N of M ›` + a per-snapshot **verdict** chip (compiles /
  error / not-checked).
- Code with line numbers + **diff highlighting** (green added / red error line).
- Footer: `lean_check` result + **SafeVerify** badge.
- **Writeable (P2):** ✎ Edit mode makes the file editable; ▶ Run `lean_check`
  verifies the user's edits on demand. User edits become the new working state.
- Collapsible like Claude artifacts (toggle from chat header or ✕).

### 3.4 Project window (replaces middle+canvas when a project is open)
Two columns:
- **Main:** hero (name + `namespace Lea.<Project>` + description) → "new proof in
  this project" composer → **list of the project's sessions** (with
  fully-qualified lemma names).
- **Right rail** with three cards (the core of the project concept):
  - 📋 **Instructions** — user-authored, project-scoped guidance injected into the
    agent (style rules, naming, which tactics/APIs to prefer).
  - 🧠 **Memory** — accumulated facts/learnings the agent reuses (which lemmas are
    proved & reusable, what failed and why, user preferences).
  - 📁 **Files** — project knowledge: the proved `.lean` files, a blueprint, and
    (possibly) uploaded reference docs.
- Breadcrumb back to the sessions view.

### 3.5 Global search overlay (⌘K)
- Sectioned results: "Loose chats" + "Inside projects" (project-tagged).
- Note line reminds: project sessions stay out of the left list but are searchable.

---

## 4. Core interaction: step ↔ snapshot alignment

The single most important behavior. **Each chat step card is linked to one canvas
snapshot.** Clicking a step jumps the canvas to that snapshot; the canvas stepper
walks the same sequence. This is how a user replays "how the proof evolved."

This already exists in v1 conceptually (`stepTimeline.mjs` →
`buildStepTimeline`/`timelineStepCount`, which pairs an assistant narration
message with a code snapshot by index). **Carry this model forward** — it's the
backbone of the chat↔canvas sync. The terminal result message is NOT a step.

---

## 5. Visual language

Carried from the mockup (keep as design tokens in CSS vars / Tailwind theme):

- Warm neutral base `#f5f4ef`, white panels, terracotta accent `#c96442`.
- Semantic colors: green `#4f8a5b` (proved), red `#c0564a` (failed/error),
  amber `#b8842a` (running / awaiting your reply / not-yet-checked).
- System sans for UI, `ui-monospace` for code & Lean identifiers.
- Lean syntax token colors defined as CSS vars (`--kw --fn --str --num --com
  --type`) — reuse for the real highlighter.

---

## 6. Open design questions (decide before/while building)

1. **Tool activity = grouped step cards vs. flat transcript.** We chose grouped
   collapsible cards. Some users want every tool call inline. Revisit after we
   feel it with real runs.
2. **Files semantics in a project:** just the proved `.lean` files + blueprint, or
   also arbitrary uploaded reference docs (PDFs/papers) as agent context? (Affects
   whether we need an upload + retrieval path.)
3. **Memory vs Instructions:** keep separate (memory = auto/agent-managed,
   instructions = user-written) or merge into one "project knowledge" block like
   Claude? Leaning separate because for a prover the distinction is meaningful.
4. **Run-mode switch placement:** in the composer (current mockup) vs. a
   per-session setting vs. inferred from intent. Ties to the existing
   "two run types" (formalization-run vs assistant-run).
5. **Namespace surfacing:** show `Lea.<Project>` prominently (current) or hide the
   Lean-implementation detail from non-expert users?
6. **Canvas for multi-file proofs / projects:** one file at a time (current) vs. a
   file tree when a proof spans modules.

---

## 7. Wiring notes for React + TypeScript

How the design maps onto the existing backend. **Bold = NEW backend work the
design implies** (doesn't exist yet); the rest is reuse/rewire of v1.

### 7.1 Data the UI already gets (reuse)
- **Sessions / runs / messages / code_steps / status_events** — persisted in
  SQLite via `adapter/app/store.py` (`session_detail`, `usage_stats`). The
  sidebar, chat thread, canvas snapshots, and result metrics all come from here.
- **Live run via SSE** — `GET /api/runs/{id}/events` emits `assistant_delta`,
  `message`, `code_step`, `status`, `error`, `done`. The chat (steps) and canvas
  (snapshots) are driven by `message` + `code_step`. *(v2 replaces `runner.py`'s
  normalization with the prover's typed events — architecture D1/D17; no `approval_*` — D18.)*
- **Step↔snapshot pairing** — port `stepTimeline` logic (v1 `src/app/`).
- **Approval = conversation** — no `permission_tier`, no approval endpoint, no
  Accept/Decline. The agent ends its turn to ask; the human's next chat message is the
  answer (architecture D18). Drop v1's approval flow entirely.
- **Settings** — model / max_turns / provider key via
  `GET`/`PUT /api/settings` (validated in `adapter/app/settings.py`,
  `MODEL_OPTIONS` + model catalog). The composer's model switch reads this.
- **Projects (partial)** — `project_assignment.py` / `project_unassignment.py`,
  namespace rewrite, `workspace/projects/<slug>.md` index, lean_check re-verify.
  Routes: `GET`/`POST /api/projects`, `PUT /api/projects/{id}`, the assign/unassign
  check+commit pairs. The project window's **sessions list + namespace** come from
  here.

### 7.2 NEW backend work the v2 design implies
- **Sidebar Chats/Projects split + "loose only" filter** — need a store query for
  *sessions without a project* vs *grouped by project*. (Project association
  exists via `projectAssociation`; the filtered listing endpoints may be new.)
- **Global search endpoint** — search across session titles / messages / lemma
  names, returning loose + project-tagged results. Likely **new** (`GET
  /api/search?q=`).
- **Project Instructions** — user-authored, project-scoped text **injected into
  the agent's prompt**. The prover already appends a `lea.md` from cwd/workspace
  root (`prompt.py load_system_prompt`); project instructions could write a
  per-project `lea.md` (or a new config field). **New** storage + plumbing through
  the adapter → Lea API run request.
- **Project Memory** — accumulated, agent-/system-managed facts per project.
  **New** storage + a policy for what populates it (proved-lemma registry is the
  obvious seed; could be derived from the project's `code_steps`/namespace index
  rather than free-form at first).
- **Project Files / uploads** — if we go beyond "the proved `.lean` files" (which
  we already have) to **uploaded reference docs**, that's a **new** upload +
  storage + (optional) retrieval path. Defer until Q-2 is decided.
- **Writeable canvas — manual edit + check (P2)** — need an adapter route to (a)
  write user-edited file content into the file under `lea_root`, and (b) run
  `lean_check` (and SafeVerify) on demand *outside* an agent run, returning the
  verdict. The prover already exposes `lean_check` as a tool; this likely means a
  direct adapter endpoint (e.g. `POST /api/files/check` or a per-run working-copy
  write+check) rather than going through a full run. The runner must then treat the
  user-written file as the starting state for any subsequent agent turn. **New.**
- **Collaborator narration (P1)** — *prover-side* prompt change, not adapter work;
  richer narration rides the existing `assistant_delta` / `message` events. Flagged
  here so it's not forgotten when we modify `prover/`.

### 7.3 Frontend reuse vs rebuild
- **Rebuild from scratch:** `App.tsx`, all top-level `components/*.tsx`, layout.
  Drop the unused shadcn/Radix scaffolding under `src/app/components/ui/` unless a
  specific component is wanted.
- **Port (logic, not UI):** `stepTimeline.mjs`, `codeDiff.ts`, `markdownParser` +
  `mathRenderer` (KaTeX), `runAttempts.ts` (groups repeated attempts on one
  theorem), `projectAssociation.mjs`, the `api.ts` client + SSE `EventSource`
  lifecycle.
- **Keep the contract:** the adapter↔frontend event names and `/api/*` routes are
  the integration surface; rebuilding the UI shouldn't require touching the
  adapter unless we add the NEW endpoints in §7.2.

### 7.4 Suggested build order
1. Shell: three-pane layout + sidebar (Chats only) + a static chat thread.
2. Wire a live run: SSE → chat steps + canvas snapshots + stepper (the core loop).
3. Conversational check-ins (agent ends turn → you reply in the composer; no approval UI).
4. Sidebar Projects group + project window (read-only first: sessions + files
   from existing data).
5. Project Instructions + Memory (needs §7.2 backend).
6. Global search (needs §7.2 endpoint).
7. Settings page.

---

## 8. Things to remember

- Config is one gitignored TOML (`config/lea.local.toml`), edited via Settings;
  keep `config.py`, `settings.py`, and `dev.mjs` in sync when adding fields.
- One Lea run at a time (`active_run_lock` in the runner).
- `lea_root` defaults to `prover/`; the runner reads `.lean` files from there to
  snapshot code — the canvas depends on this.
- SafeVerify badge reflects real kernel-replay verification (not just a sorry
  grep) — surface it honestly (verified / failed / unavailable).
- Path-drift warning (Lea writes one file but checks another) is a real signal the
  UI should keep showing.
