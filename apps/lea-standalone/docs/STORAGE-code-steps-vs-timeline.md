# Code Steps vs Generic Timeline Events

## Decision

Keep `code_steps` as a first-class table for proof evolution instead of replacing it
with a fully generic `timeline_events(type, data_json)` table.

## Context

OpenCode stores agent activity as flexible message parts and session timeline events.
OpenHands stores conversation state as `base_state.json` plus per-event JSON files.
Both are good fits for general-purpose coding agents: new UI concepts can be added as
new event types without schema work. Lea is narrower. Its durable user-facing artifact
is Lean proof content, and most product queries revolve around proof files, check
verdicts, artifact kind, and the latest canvas state.

## Why Keep `code_steps`

`code_steps` makes Lea's core queries simple and explicit:

- derive session status from the latest non-scratch proof verdict;
- load the latest canvas file for a session/path;
- show proof history in order with check status and artifact kind;
- back-fill `lean_check` results after a write;
- export or replay proof evolution without parsing generic JSON events.

The table should evolve away from Git pointers, but not disappear. A future version can
store proof bytes through content-addressed blobs:

```text
code_steps
  id, session_id, run_id, seq, turn, author, path
  before_blob_id, after_blob_id
  patch, additions, deletions
  check_status, check_detail, artifact_kind
  summary, created_at
```

`artifact_blobs` then owns exact Lean file contents:

```text
artifact_blobs
  id, sha256, content, created_at
```

## Tradeoff

This is intentionally less extensible than a generic timeline table. If Lea later adds
a new timeline concept, it may need a small purpose-built table or a JSON side channel
instead of simply inserting a new event type.

That tradeoff is acceptable because Lea is not a general-purpose coding harness. It is a
Lean formalization agent, and optimizing the schema around proof artifacts keeps
concurrency, status derivation, and UI hydration easier to reason about.

## Related Rule

Subagents may write temporary candidate files, but only parent/coordinator runs promote
clean canonical output into `code_steps`. Temp candidate attempts are not normal
`code_steps` unless promoted or explicitly retained for debugging.

Subagent transcripts can still be retained separately for audit, resume, and debugging:
messages, tool calls, Lean diagnostics, summaries, and final candidate results. A
promoted `code_step` may link to the winning transcript, but transcript events do not
replace the proof-history table.
