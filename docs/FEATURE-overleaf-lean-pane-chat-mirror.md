# Feature: Overleaf Lean Pane Chat Mirror

> **Status (v1 implemented 2026-06-30).** Ships behind the Lean pane's per-item
> `Chat` action. Scope locked to **text bubbles + `Open in Lea`** and **minimal
> continuation prompts** (full context preamble only on the session-creating
> message). Companion endpoints `/lean-pane/chat/{session,message,interrupt}` +
> `GET /lean-pane/chat/session/:sessionId` wrap the existing adapter run/session
> APIs; the companion is the single SSE driver and the extension polls; no adapter
> changes. Implementation plan: `docs/PLAN-overleaf-lean-pane-chat-mirror.md`.

## Summary

Add access to the existing Lea chat from the Overleaf Lean pane. For each marked
theorem, lemma, proposition, corollary, or definition, the Lean pane should let
the user open a compact mirrored view of that item's corresponding full Lea UI
chat session.

This is not a separate chat system. It is a Lean-pane entry point into the same
adapter-backed session that the full Lea UI already uses. The full UI remains
the canonical chat; the Overleaf surface mirrors that conversation in context so
the user can ask quick follow-up questions, request fixes, and continue a
formalization thread without leaving Overleaf.

## Goal

Let a user stay in Overleaf for short theorem-specific interactions with Lea:

- Ask why a proof failed.
- Ask Lea to continue or repair an existing formalization.
- Ask for an explanation of the generated Lean.
- Ask for a smaller lemma, missing dependency, or statement adjustment.
- See the latest assistant response and run status without opening the full UI.
- Open the corresponding full Lea session when the interaction needs more room.

Chat access should be directly associated with one mathematical item in the Lean
pane. The transcript shown in Overleaf should be understood as a mirror of the
corresponding chat in the full standalone UI.

## Non-Goals

- Do not create a separate prover API, chat server, or chat database.
- Do not make the extension a full replacement for the standalone UI.
- Do not support project-wide free-floating chats in version 1.
- Do not support editing arbitrary Lean files from the Overleaf chat composer in
  version 1.
- Do not persist a second Overleaf-owned transcript that can diverge from the
  full UI.
- Do not introduce a new persistence model for proof content. Proof bytes still
  belong to git; chat/run metadata still belongs to the adapter SQLite store.

## Current Behavior

The Overleaf extension can start autonomous formalization runs from detected Lean
pane items. The companion stores job metadata and links those jobs to adapter
sessions through `leaSessionId`. The Lean pane can already expose a link to the
full Lea UI session when one exists.

The missing behavior is that the Lean pane only links out to that chat. To ask a
follow-up, the user must open the standalone UI, find or follow the associated
session, and use the full chat interface there. The extension has no in-pane
mirror of the same conversation.

## Proposed Behavior

Each Lean pane item should expose a `Chat` action. Opening it shows a compact
drawer or inline panel from the Lean pane that mirrors the item's full Lea UI
chat session.

The panel should show:

- Item identity: kind, Lean declaration name, LaTeX label, source file.
- Current item status: missing stub, in progress, valid, invalid, stale, error,
  disproved, defined, or unknown.
- The associated Lea session messages, mirrored from the adapter session.
- A composer for a short follow-up message.
- Live progress while Lea is responding.
- A persistent `Open in Lea` action for the canonical full session.

If no associated Lea session exists yet, the Lean pane mirror should show an
empty-state thread and create the canonical session on first user message. The
first message must carry enough theorem context for Lea to know which item it is
discussing.

## User Stories

1. As a user, I can open the Lean pane, choose a theorem, and click `Chat` without
   leaving Overleaf.
2. As a user, I can see the messages from the formalization run that created or
   attempted the proof for that theorem.
3. As a user, I can ask "why did this fail?" on an invalid theorem and receive a
   response in the Lean-pane mirror.
4. As a user, I can ask Lea to continue from a generated stub and have that run
   append to the same theorem-associated session.
5. As a user, I can open the full Lea UI for the same mirrored session when I
   want the code canvas, approvals, settings, or a larger transcript.

## Access Model

The Lean pane is the access surface for theorem chat in Overleaf. Chat controls
should appear in the context of a pane item, not as a global extension popover,
floating document-level chat, or independent project chat.

Opening chat from a pane item selects that item's adapter session and renders a
mirrored transcript. Closing the panel should not close, archive, or otherwise
mutate the canonical full UI session. Reopening the same pane item should reload
the same session from the adapter.

The `Open in Lea` action is an escape hatch from the mirror to the canonical full
UI. It should navigate to the same `leaSessionId` currently shown in the
Lean-pane mirror.

## Identity Model

The primary association is:

```text
overleafProjectId + targetKind + targetLabel -> Lea session
```

Where:

- `overleafProjectId` is the Overleaf project namespace already used by the
  companion.
- `targetKind` is `theorem` or `definition` in the existing formalize payload
  shape.
- `targetLabel` is the Lean declaration name from the Lea marker.

The companion job store already records this association for formalization jobs
through `jobKey` and `leaSessionId`. Version 1 should reuse that mapping.

If multiple historical jobs exist for the same target, the chat should attach to
the newest job with a `leaSessionId`. If an active job exists, the chat should
attach to that active job's session once available and show an in-progress state
until the adapter session exists.

## Session Creation Rules

When a user sends a chat message for an item with an existing `leaSessionId`:

1. The companion calls the adapter `POST /api/runs`.
2. It passes `session_id = leaSessionId`.
3. It passes `project_slug`, `project_title`, `origin = "overleaf"`, and
   `origin_url` just like formalization runs.
4. The run should be interactive by default from the user's point of view, but
   still usable from the extension. Version 1 should use `autonomous: true`
   unless and until the extension supports tool approval UI.

When a user sends the first chat message for an item with no session:

1. The companion creates a new adapter run with no `session_id`.
2. The run message is a context-wrapped prompt that includes the target kind,
   label, natural-language statement, source file, source hash, target uses, and
   any known Lean artifact path.
3. The companion stores the returned `session_id` as the target's chat session
   association.
4. The Lean pane item begins returning `leaSessionId` and `leaSessionUrl`.

## Prompt Contract

For theorem-scoped chat, the user-visible message should remain concise, but the
adapter run message should include a context preamble.

Required context:

```text
You are helping with this Overleaf item.

Project: <overleafProjectId/projectSlug>
Kind: <theorem|definition>
Label: <targetLabel>
LaTeX label: <latexLabel if present>
Source file: <sourceFile>:<sourceStartLine>-<sourceEndLine>
Source hash: <sourceHash>
Natural-language statement:
<naturalLanguageLatex>

Known Lean declaration: <leanDeclarationName>
Known Lean artifact: <recordedProofPath if present>
Known status: <pane status>
User request:
<composer text>
```

If the item is stale, the prompt should explicitly say that the Overleaf source
changed after the known Lean artifact was generated.

## Companion API

Add companion endpoints for the Lean-pane chat mirror:

```http
POST /lean-pane/chat/session
POST /lean-pane/chat/message
GET  /lean-pane/chat/session/:sessionId
POST /lean-pane/chat/interrupt
```

Recommended request/response shapes:

```ts
type LeanPaneChatTarget = {
  overleafProjectId: string;
  targetKind: "theorem" | "definition";
  targetLabel: string;
  sourceHash?: string;
  sourceFile?: string;
  sourceStartLine?: number;
  sourceEndLine?: number;
  naturalLanguageLatex?: string;
  leanDeclarationName?: string;
  recordedProofPath?: string;
};

type ChatSessionResponse = {
  ok: true;
  targetKey: string;
  leaSessionId: string | null;
  leaSessionUrl: string | null;
  status: string;
  messages: ChatMessage[];
  runs: RunSummary[];
  activeRun: RunSummary | null;
};

type ChatMessageRequest = {
  target: LeanPaneChatTarget;
  message: string;
};

type ChatMessageResponse = {
  ok: true;
  targetKey: string;
  leaSessionId: string;
  leaSessionUrl: string;
  runId: string;
  userMessage: ChatMessage;
};
```

The companion should delegate session detail to adapter
`GET /api/sessions/{session_id}` and run creation to `POST /api/runs`.

## Streaming

Version 1 can use polling to keep the implementation simple:

- After `POST /lean-pane/chat/message`, the extension polls
  `/lean-pane/chat/session/:sessionId`.
- Polling continues while `activeRun` is pending/running.
- The companion opens the adapter SSE stream to drive the run, following the
  existing `runApiProofJob` pattern.

A later version may add companion-side SSE for lower latency:

```http
GET /lean-pane/chat/runs/:runId/events
```

The extension should never open two active drivers for the same adapter run. If
the companion starts and drives a run, the Lean-pane mirror should observe
through the companion rather than independently driving adapter SSE.

## Implementation Phases

Version 1 should be the smallest useful Lean-pane mirror of theorem chat:

1. Resolve target-to-session associations from existing jobs and optional
   companion chat metadata.
2. Add companion chat endpoints that wrap existing adapter session/run APIs.
3. Add the Lean pane `Chat` action and mirrored chat panel.
4. Support sending one follow-up message, polling until completion, stopping the
   run, and opening the canonical full Lea session.
5. Refresh the Lean pane manifest after chat completion.

Later versions can add companion SSE, approval handling, unread indicators,
better code-step rendering, and richer transcript filtering.

## UI Specification

The chat mirror entry point appears on each actionable Lean pane item:

- Primary label: `Chat`
- Secondary affordance: chat/message icon if the pane has icon-only controls
- Badge: unread/new response count is optional for later

The mirrored chat panel should include:

- Header with declaration name and status chip.
- Short source line: `main.tex:42-51`.
- Transcript area with compact user/assistant bubbles from the adapter session.
- Lightweight rendering for Markdown and inline code.
- Collapsed code-step cards for generated Lean artifacts.
- Composer with placeholder `Ask Lea about this item...`.
- Send and Stop buttons.
- `Open in Lea` link to the canonical full UI session.

The panel should support these states:

- `no-session`: no prior Lea session; first message starts one.
- `loading-session`: fetching adapter session detail.
- `ready`: messages loaded, composer enabled.
- `running`: active run in progress; composer disabled except Stop.
- `error`: session or message failed; retry action shown.
- `adapter-unavailable`: companion reachable but adapter unavailable.

## Behavior Details

- Chat access is scoped through one Lean pane item at a time. Opening chat on
  another item switches the mirrored session context.
- Sending a message should flush the latest Overleaf `.tex` mirror first, using
  the existing mirror flow, so Lea sees current source.
- If the item source hash differs from the latest associated job hash, the chat
  should mark the item stale and include that fact in the prompt.
- The mirror should preserve the exact adapter session transcript. It may hide
  verbose tool narration by default, but it must not rewrite persisted messages.
- If a run produces a new proof artifact, the Lean pane manifest should refresh
  and the item's status should update.
- If the run hits max spend, missing key, timeout, or adapter conflict, the
  mirrored panel should show the same failure class as the rest of the extension.

## Tool Approval

Version 1 should avoid introducing tool approval UI inside Overleaf. Chat runs
started from the extension should therefore use the same autonomous behavior as
formalization runs.

Future work may support approvals in the Lean-pane mirror. If added, approvals
should mirror the full UI's allow/deny/always-for-session contract and call the
adapter approval endpoint through the companion.

## Data and Persistence

The adapter remains the source of truth for:

- Sessions.
- Runs.
- Messages.
- Code steps.
- Usage and cost.

The companion may persist only target-to-session association metadata needed to
find a chat before any formalization job exists. Suggested storage:

```json
{
  "chatSessions": {
    "<overleafProjectSlug>:<targetKind>:<targetLabel>": {
      "leaSessionId": "...",
      "createdAt": "...",
      "updatedAt": "...",
      "sourceHash": "..."
    }
  }
}
```

If the association can be recovered from an existing job, the companion should
prefer the job/session data rather than duplicating it.

## Acceptance Criteria

- A Lean pane item with an existing formalization session shows a `Chat` action.
- Opening chat through the Lean pane loads the existing adapter messages for
  that item.
- Sending a message appends a new adapter run to the same session.
- A Lean pane item with no prior session can start a new theorem-scoped chat.
- The full Lea UI link opens the exact same session shown in the Lean-pane
  mirror.
- The mirrored panel shows running, stopped, failed, and completed states.
- A completed chat run refreshes the pane manifest and item status.
- Tests cover target-to-session resolution, first-message session creation,
  existing-session continuation, stale-source prompt context, and adapter failure.

## Test Plan

Companion tests:

- Resolve newest `leaSessionId` for a target from existing jobs.
- Create a new chat association when no job/session exists.
- Continue an existing session by passing `session_id` to `POST /api/runs`.
- Include theorem context and stale-source metadata in the prompt wrapper.
- Surface adapter errors without corrupting chat association state.

Extension tests:

- Render `Chat` actions for pane items.
- Open the mirrored chat panel and request session detail.
- Disable composer while a run is active.
- Show `Open in Lea` when a session URL exists.
- Refresh the Lean pane after a chat run completes.

Adapter tests should not be necessary for version 1 unless a new adapter endpoint
is added. The preferred implementation uses existing adapter run/session APIs.

## Open Questions

- Should theorem chat runs be labeled differently from formalization runs in the
  full UI timeline?
- Should a no-session first message create a regular adapter session immediately,
  or should there be a companion-only draft until the first run starts?
- Should the Lean-pane mirror expose generated code steps inline in version 1,
  or only show text messages plus `Open in Lea`?
- How much of the standalone `ChatThread` behavior should be ported versus
  simplified for extension constraints?
