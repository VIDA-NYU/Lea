# Plan — Overleaf Lean Pane Chat Mirror

Implementation plan for `docs/FEATURE-overleaf-lean-pane-chat-mirror.md`
(the file is still titled `FEATURE-overleaf-theorem-chat.md` on disk; the
feature was renamed to "Lean Pane Chat Mirror" in the latest edit).

Branch context: builds on `lean_pane_overleaf` (the project-wide Lean pane and
its `Chat`-adjacent actions). No adapter changes are required — the mirror is a
thin companion + extension surface over the **existing** adapter run/session
APIs.

## Scope decisions (locked)

- **v1 transcript = text bubbles + `Open in Lea`.** Render user/assistant text
  with light Markdown/inline-code, hide verbose tool narration, and defer all
  generated-Lean inspection to the `Open in Lea` escape hatch. Collapsed
  code-step cards are deferred to a later version.
- **Minimal continuation prompts.** The full theorem-context preamble is sent
  only on the first message that *creates* the session. Later messages send the
  user's text plus a one-line stale note when the source changed.
- **Polling, not companion SSE, for v1** (matches the spec's "Streaming"
  section). The companion is the single SSE driver of the adapter run; the
  extension only polls the companion.
- **No new persistence model.** The adapter remains source of truth for
  sessions/runs/messages/code-steps/usage. The companion adds only a small
  `targetKey → leaSessionId` association map for chats that exist before any
  formalization job.

## Architecture seam (grounding)

```
Lean pane item (content.js)
  → companion :31245  /lean-pane/chat/*        (new)
     → adapter :8001  POST /api/runs            (existing, startApiRun)
                       GET  /api/sessions/{id}   (existing, fetchApiSessionDetail)
                       POST /api/runs/{id}/interrupt (existing, interruptApiRun)
```

Everything the mirror needs already exists on the adapter:

- `startApiRun(...)` (`companion/leaApiClient.mjs`) already accepts
  `sessionId`, `autonomous`, `projectSlug`, `projectTitle`, `origin`,
  `originUrl` — the exact provenance fields formalization runs use.
- `runApiProofJob(...)` starts a run, exposes `onRunStarted(runId, sessionId,
  body)`, drives the SSE stream to terminal `done`, and reads usage back. We
  reuse it verbatim for chat runs.
- `GET /api/sessions/{id}` returns `{ status, messages[], code_steps[], runs[],
  active_run, usage, project, origin, ... }`. `messages` rows are
  `{ id, session_id, run_id, role, content, kind, seq, created_at }`
  (`db.py`), which is exactly what the mirror renders.
- `interruptApiRun(...)` → `POST /api/runs/{id}/interrupt` for Stop.

## Target → session resolution

The association key reuses the companion's existing job key shape
(`buildLeaTarget` in `server.mjs`):

```
targetKey = `${slugProjectId(overleafProjectId)}:${targetKind}:${targetLabel}`
```

Resolution order (newest-wins), implemented as one helper
`resolveChatSession({ state, target })`:

1. **Active job** for `jobKey` (`findActiveJob`) — if present, the chat attaches
   to that job's session once it exists and renders the `running` state.
2. **Newest finished job with a session** (`findLatestJobWithLeaSession`,
   already in `server.mjs`) — gives `leaSessionId` for any target that was ever
   formalized/stubbed.
3. **Companion chat association** (new `chatSessions.json`, below) — covers
   chat-only targets that never ran a formalization job.
4. **None** → `no-session` state; the first sent message creates the session.

This means an item that has *ever* been formalized needs **no** new companion
storage — its session is recovered from the job store. The association map only
backstops the pure-chat-first case.

### Companion association store

New file `apps/overleaf-extension/.overleaf-lean-stub/chatSessions.json`
(gitignored, alongside `jobs.json`/`settings.json`), loaded into `state` in
`createServer`:

```json
{
  "chatSessions": {
    "<slug>:<targetKind>:<targetLabel>": {
      "leaSessionId": "…",
      "createdAt": "…",
      "updatedAt": "…",
      "sourceHash": "…"
    }
  }
}
```

Written atomically via the existing `writeJson` helper. We **prefer job/session
data when available** and only fall back to this map (spec: "If the association
can be recovered from an existing job, the companion should prefer the
job/session data rather than duplicating it").

## Companion endpoints

Add four routes in `routeRequest` (`server.mjs`) and four exported handlers
(mirroring the `handleFormalize` / `handleLeanPaneManifest` style so they're
unit-testable without the HTTP layer):

| Route | Handler | Purpose |
|-------|---------|---------|
| `POST /lean-pane/chat/session` | `handleChatSession` | Resolve target → session; return current transcript + status, or `no-session`. |
| `POST /lean-pane/chat/message` | `handleChatMessage` | Resolve-or-create session, start a run, return `runId` + `userMessage` immediately. |
| `GET /lean-pane/chat/session/:sessionId` | `handleChatPoll` | Proxy adapter session detail, reshaped to `ChatSessionResponse` (polling). |
| `POST /lean-pane/chat/interrupt` | `handleChatInterrupt` | Stop the active run for a session/run. |

### `handleChatSession(payload, state)` — load

1. Validate `target` (reuse `normalizeTargetKind` + `isValidLeanIdentifier`;
   `overleafProjectId` required).
2. `resolveChatSession(...)`. If no `leaSessionId`, return
   `{ ok, targetKey, leaSessionId: null, status: "no-session", messages: [],
   runs: [], activeRun: null }`.
3. If a `leaSessionId` exists, call `fetchApiSessionDetail(...)` and return it
   reshaped to `ChatSessionResponse` (see "Reshape" below). Adapter unreachable
   → `{ ok: false, error: "adapter_unavailable" }` so the UI shows that state.

### `handleChatMessage(payload, state)` — send

1. Validate `target` + non-empty `message`.
2. `syncSharedSettingsFromAdapter(state)`, then `validateLeaRuntime(state,
   { requireApiKey: true })` and `spendLimitReached(state)` — identical preflight
   to `handleFormalize`, returning the same error codes (`max_spend_reached`
   402, `missing_<family>_key`, etc.) so the panel reuses existing failure
   classes.
3. `resolveChatSession(...)` → `{ leaSessionId | null, latestJobHash }`.
4. Compute `stale = latestJobHash && latestJobHash !== target.sourceHash`.
5. Build the run message:
   - **First message (no session):** `buildChatPrompt(target, { stale,
     firstMessage: true, userText })` — the full context preamble (Prompt
     Contract below).
   - **Continuation (session exists):** `buildChatPrompt(target, { stale,
     firstMessage: false, userText })` — just `userText`, prefixed with a single
     stale line when `stale`.
6. Start the run in the **background** with `runApiProofJob({ ...,
   sessionId: leaSessionId, autonomous: true, projectSlug:
   slugProjectId(overleafProjectId), projectTitle, origin: "overleaf",
   originUrl: buildOverleafDocumentUrl(overleafProjectId), onRunStarted })`.
   Capture `runId` + `sessionId` from `onRunStarted` via a resolved promise, so
   the HTTP handler can **return as soon as the run is created** (not when it
   finishes). The `.catch()`/post-completion work (persist association, refresh)
   runs in the background, exactly like `handleFormalize`'s
   `runLeaJob(...).catch(...)`.
7. On `onRunStarted`, persist the association
   (`chatSessions[targetKey] = { leaSessionId, sourceHash, updatedAt }`) and
   write `chatSessions.json`.
8. Return `{ ok, targetKey, leaSessionId, leaSessionUrl:
   buildLeaSessionUrl(...), runId, userMessage }`.

> The "never two drivers" rule (spec, Streaming) is satisfied: the companion
> opens the adapter SSE stream (driving the run); the extension never touches
> adapter SSE and only polls the companion `GET`.

### `handleChatPoll(sessionId, state)` — poll

`fetchApiSessionDetail` → reshape → `ChatSessionResponse`. The extension polls
this while `activeRun` is pending/running, then stops (mirrors the manifest
polling driven by `hasInProgressItems`).

### `handleChatInterrupt(payload, state)` — stop

Resolve the active run (`payload.runId`, else adapter `active_run.id` from a
session fetch) and call `interruptApiRun(...)`.

### Reshape: adapter session detail → `ChatSessionResponse`

Pure function `toChatSessionResponse(detail, { targetKey, leaSessionUrl })` so
it can be unit-tested without a live adapter:

- `messages`: filter to `role ∈ {user, assistant}` and drop tool-narration
  kinds; keep `{ id, role, content, kind, createdAt }`, ordered by `seq`.
- `runs`: map to `RunSummary` (`{ id, status, createdAt }`).
- `activeRun`: `detail.active_run` (or null).
- `status`: `detail.status` (derived session status from the adapter).

## Prompt contract

`buildChatPrompt(target, { stale, firstMessage, userText })` in a **pure,
DOM-free** module (so both the companion and tests use it). First-message form
follows the spec's "Prompt Contract" verbatim:

```
You are helping with this Overleaf item.

Project: <projectSlug>
Kind: <theorem|definition>
Label: <targetLabel>
LaTeX label: <latexLabel?>
Source file: <sourceFile>:<sourceStartLine>-<sourceEndLine>
Source hash: <sourceHash>
Natural-language statement:
<naturalLanguageLatex>

Known Lean declaration: <leanDeclarationName?>
Known Lean artifact: <recordedProofPath?>
Known status: <pane status>
[If stale] Note: the Overleaf source changed after the known Lean artifact was generated.
User request:
<userText>
```

Continuation form: `userText`, with a leading
`Note: the Overleaf source changed since the last run.\n\n` only when `stale`.

## Extension UI

All DOM/fetch/polling stays in the `content.js` IIFE; all **pure** logic goes
into a testable module (extend `leanPaneView.mjs` or add `leanPaneChat.mjs`
loaded with the same `import(chrome.runtime.getURL(...))` pattern).

### Entry point

In `renderLeanPaneItemDetail` (the action row at `content.js` ~L515–527, where
`Go to source` / `Formalize` / `Copy` buttons are appended), add
`renderChatButton(item)`:

- Label `Chat`; shown for every actionable item (any item that has or can have a
  session). Gated by a pure `canChatPaneItem(item)` helper in the view module.
- On click: open the mirror panel scoped to this item and call
  `POST /lean-pane/chat/session` to load.

### Chat panel

A single reusable panel owned by the pane (one at a time — "Opening chat on
another item switches the mirrored session context"). Structure per the spec's
UI section:

- Header: declaration name + status chip (reuse `formatPaneStatus`).
- Source line: `main.tex:42-51`.
- Transcript: compact user/assistant bubbles; light Markdown + inline code via a
  small pure renderer (reuse the pane's existing lite-render helpers where
  possible).
- Composer: placeholder `Ask Lea about this item...`, Send + Stop.
- `Open in Lea` link → `leaSessionUrl` (reuse `buildLeaSessionUrl`; same target
  the in-document badge already uses).

### State machine (pure `nextChatState(...)`)

`no-session → loading-session → ready → running → (ready|error)`, plus
`adapter-unavailable`. Composer is enabled only in `ready`/`no-session`;
disabled with Stop active in `running`. Send flow:

1. Flush the latest `.tex` mirror first (reuse the existing force-sync used
   before `formalize`) so Lea sees current source.
2. `POST /lean-pane/chat/message` → optimistic-append the user bubble, enter
   `running`.
3. Poll `GET /lean-pane/chat/session/:sessionId` until `activeRun` clears.
4. On completion: render new assistant messages, return to `ready`, and call
   `refreshLeanPaneNow({ background: true })` so the item status/manifest
   updates (a new proof artifact flips the chip).

Escape closes the panel; focus moves into the composer on open (consistent with
the pane's a11y items).

## File-by-file change list

**Companion**
- `companion/server.mjs`: 4 routes in `routeRequest`; `handleChatSession`,
  `handleChatMessage`, `handleChatPoll`, `handleChatInterrupt`;
  `resolveChatSession`; `chatSessions` load in `createServer` + atomic writes.
- `companion/chatPrompt.mjs` (new, pure): `buildChatPrompt`,
  `toChatSessionResponse`, `chatTargetKey`. Imported by `server.mjs` and tests.
- `companion/leaApiClient.mjs`: no change (reuse `startApiRun`/`runApiProofJob`/
  `fetchApiSessionDetail`/`interruptApiRun`). Add a thin export only if a
  message-only run helper proves cleaner than `runApiProofJob` + `onRunStarted`.

**Extension**
- `extension/content.js`: `renderChatButton`, the chat-panel build/teardown,
  send/poll/stop wiring, manifest refresh on completion.
- `extension/leanPaneView.mjs` (or new `leanPaneChat.mjs`): `canChatPaneItem`,
  `nextChatState`, `chatMessagesToBubbles`, target-payload shaper.
- `extension/content.css`: chat panel, bubbles, composer, status chip styles
  (reuse `--ol-*` warm-paper tokens).
- `extension/manifest.json`: add the new `.mjs` to `web_accessible_resources`
  if a separate `leanPaneChat.mjs` is introduced.

**Docs**
- Rename `FEATURE-overleaf-theorem-chat.md` →
  `FEATURE-overleaf-lean-pane-chat-mirror.md` (filename now lags the title), and
  mark v1 scope decisions as resolved.

## Failure handling matrix

| Condition | Companion result | Panel state |
|-----------|------------------|-------------|
| Adapter down | `{ ok:false, error:"adapter_unavailable" }` | `adapter-unavailable` |
| Missing provider key | `missing_<family>_key` (preflight) | `error` + settings hint |
| Max spend reached | 402 `max_spend_reached` | `error` (spend message) |
| Run ends failed/timeout | terminal `done` status surfaced via poll | `ready` + failure note |
| Active run already running | `running` from resolve | composer disabled, Stop active |
| Adapter conflict on concurrent run | adapter 409 surfaced | `error` + retry |

## Test plan

**Companion** (`tests/companion.test.mjs` style, no live adapter — inject
`fetchImpl`):
- `resolveChatSession` returns newest `leaSessionId` from jobs.
- `no-session` when no job/association exists; association created on first send.
- Continuation passes `session_id` to `POST /api/runs`; first message omits it.
- `buildChatPrompt` includes full context on first message, stale note when
  stale, minimal body on continuation.
- Adapter error surfaces without corrupting `chatSessions.json`.
- `toChatSessionResponse` filters tool narration and orders by `seq`.

**Extension** (`tests/leanPaneChat.test.mjs` + `tests/contentActions.test.mjs`):
- `canChatPaneItem` / `renderChatButton` appears on pane items.
- Opening the panel requests session detail.
- `nextChatState` disables the composer while a run is active.
- `Open in Lea` rendered when a session URL exists.
- Manifest refresh fires after a chat run completes.

**Adapter**: none required (reuses existing run/session APIs), per the spec.

## Sequencing

1. `chatPrompt.mjs` (pure: prompt + reshape + key) and its companion tests.
2. `resolveChatSession` + `chatSessions.json` load/write + tests.
3. The four companion endpoints (load/poll/interrupt first; message last) +
   tests, injecting `fetchImpl`.
4. Background run orchestration via `runApiProofJob` + `onRunStarted`.
5. Extension pure helpers + tests.
6. `renderChatButton` + panel + send/poll/stop + CSS + manifest refresh.
7. Doc rename + scope-resolution note; run the overleaf suite green.

## Risks / watch-items

- **Returning `runId` before the run finishes.** `runApiProofJob` resolves only
  at terminal `done`; the handler must resolve on `onRunStarted` instead of
  awaiting the whole job, or the `POST message` call blocks for the full run.
- **Concurrent run on a session.** If a formalization run is already active for
  the target, sending a chat message may hit an adapter conflict. v1 should
  detect the active run in `resolveChatSession` and render `running` (block the
  send) rather than racing.
- **Stale-hash source.** Reuse the *same* `targetTextHash` comparison
  `enrichLeanPaneItem` already uses (item 1 of the pane-improvements plan), so
  the chat's stale signal can't diverge from the pane chip.
- **Tool-narration filtering.** Hiding kinds must not drop real assistant text;
  pin the kept set (`role ∈ {user, assistant}` minus known tool kinds) with a
  test against a representative session detail.

## Open questions to confirm before coding

- Should chat runs be visually distinguished from formalization runs in the full
  UI timeline? (No adapter run "kind" field exists today — would be additive.)
- Is reusing the formalization project namespace (`project_slug`) for chat usage
  acceptable, or should chat spend be attributed separately in Stats?
