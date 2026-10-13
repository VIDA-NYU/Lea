# Lea API — Design Document

**Status:** Draft / proposal for the next development phase
**Audience:** Lea maintainers and anyone building services on top of the agent core
**Scope:** An HTTP + streaming API that exposes the `config-driven, event-out` agent
as a service.

---

## 1. Background and goals

The recent refactor turned Lea from a code-driven CLI into a config-driven agent
**library** with one stable interface:

- **Config-in:** `run_events(config, task)` takes a validated `LeaConfig` and a task string.
- **Event-out:** it yields a typed event stream (`TurnStarted`, `AssistantTextDelta`,
  `ToolCalled`, `ToolResulted`, `UsageUpdated`, `Finished`) and never prints.
- **Pure validation:** `validate_config(raw)` checks a config payload with no disk
  access and no agent run, raising a typed `ConfigError` on the first problem.

The decision log frames this explicitly: *the agent is the product; the CLI, eval,
and the UI are consumers of it.* The API is simply the **third consumer** — it
serializes the same event contract over the network. This document specifies the
endpoints, payloads, event wire format, and cross-cutting concerns for that API.

### Design principles

1. **The event stream is the API contract.** The wire format mirrors `events.py`.
   Treat it as a versioned public schema.
2. **A run is an async job, not a blocking request.** Proof runs are long-lived and
   emit a continuous stream, so start-and-subscribe beats request/response.
3. **Validate before you spend.** Config validation is a cheap, side-effect-free
   endpoint so a UI can check input before paying for a run.
4. **Decouple verification from generation.** Checking a Lean proof is independently
   useful (best-of-n, eval, external callers) and gets its own endpoint.
5. **Surface the extension points.** Tools, skills, and MCP servers are first-class,
   discoverable resources.

---

## 2. Conventions

### Base URL and versioning

```
https://<host>/v1
```

The major version is in the path. The event schema carries its own `schema_version`
so streaming consumers can detect changes independently of the URL version.

### Authentication

Bearer API keys: `Authorization: Bearer <key>`. Keys carry per-key rate limits and
spend caps. (Model-provider credentials — `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, etc.
— remain server-side environment configuration and are never accepted over the API.)

### Content types

- Requests/responses: `application/json`.
- Event stream: `text/event-stream` (SSE) by default; WebSocket offered as an
  alternative transport for bidirectional clients.

### Idempotency

Mutating `POST` endpoints that incur cost (`/runs`, `/eval/runs`) accept an
`Idempotency-Key` header so client retries don't double-spend.

### Errors

Errors map the `LeaError` hierarchy to HTTP status codes and a structured body:

```json
{
  "error": {
    "type": "InvalidConfigValueError",
    "message": "'model.stream' must be a boolean, got str.",
    "field": "model.stream"
  }
}
```

| Exception                              | HTTP | Meaning                                  |
| -------------------------------------- | ---- | ---------------------------------------- |
| `ConfigFormatError`                    | 400  | Body is not a valid config mapping       |
| `UnknownConfigKeyError`                | 422  | Unrecognized key in a section            |
| `MissingConfigKeyError`                | 422  | Required key absent after merge          |
| `InvalidConfigValueError`              | 422  | Key present, wrong type/value            |
| `ToolError`                            | 422 / 502 | Unknown tool selected / tool failed |
| `SkillError`                           | 422  | Skill file missing or unreadable         |
| `McpError`                             | 424  | MCP session unavailable / call failed    |
| (run not found)                        | 404  | Unknown `run_id` / `session_id`          |
| (provider/model error)                 | 502  | Upstream model call failed               |

---

## 3. The event stream (core contract)

Every event is a JSON object with a `type` discriminator and a monotonically
increasing `seq` (for replay/resumption). Fields below mirror the dataclasses in
`lea/events.py`.

```jsonc
// SessionResumed — emitted once at startup if the run resumed a session
{ "type": "session_resumed", "seq": 0, "session_id": "…", "message_count": 12 }

// TurnStarted — start of a loop iteration (1-based)
{ "type": "turn_started", "seq": 1, "turn": 1 }

// AssistantTextDelta — a streaming chunk of assistant text (not the whole message)
{ "type": "assistant_text_delta", "seq": 2, "text": "We proceed by induction" }

// ToolCalled — the model asked to run a tool
{ "type": "tool_called", "seq": 7, "name": "lean_check", "args": { "path": "Proof.lean" } }

// UsageUpdated — per-turn token usage + cost delta
{ "type": "usage_updated", "seq": 8, "input_tokens": 5120, "output_tokens": 384, "cost": 0.0091 }

// ToolResulted — a tool finished. `content` is full; `preview` is the truncation shown to users
{ "type": "tool_resulted", "seq": 9, "name": "lean_check", "content": "…", "preview": "no goals" }

// Finished — terminal event. reason is "completed" or "max_turns"
{
  "type": "finished", "seq": 42, "reason": "completed",
  "text": "Proof complete.", "turns": 6,
  "session_id": "…", "model": "gemini/gemini-3.1-pro-preview",
  "usage": { "input_tokens": 30100, "output_tokens": 2150 },
  "cost": 0.061,
  "transcript_url": "/v1/runs/{id}/transcript"
}
```

Notes:
- The stream always ends with exactly one `finished` event (or an `error` frame).
- `transcript` is large; the stream links to it rather than inlining it.
- SSE frames use `id: <seq>` so a client can reconnect with `Last-Event-ID` and
  resume from where it dropped.

---

## 4. Endpoints

### 4.1 Runs

#### `POST /v1/runs` — start a run

Request:

```json
{
  "task": "Prove that the sum of two even numbers is even.",
  "config": {
    "model": { "name": "gemini/gemini-3.1-pro-preview", "stream": true,
               "model_kwargs": { "max_tokens": 16384 } },
    "agent": { "prompt_variant": "default", "max_turns": null,
               "tools": null, "tool_modules": [], "skills": [] },
    "mcp": { "servers": {} }
  },
  "resume": false
}
```

- `config` may be a full object, a partial overlay (merged over `default.yaml`), or
  omitted entirely (pure defaults). Alternatively pass `"config_ref": "<name>"` to use
  a stored config (see 4.3).
- `resume` accepts `false`, `true`, or a `session_id` string.

Response `202 Accepted`:

```json
{ "run_id": "run_8f2a…", "status": "queued", "events_url": "/v1/runs/run_8f2a…/events" }
```

The config is validated synchronously before acceptance; a bad config returns `422`
with the typed error and does not create a run.

#### `GET /v1/runs/{id}/events` — subscribe to the event stream

SSE (default) or WebSocket. Supports `Last-Event-ID` / `?from_seq=` for replay.
This is the primary way to observe a run.

#### `GET /v1/runs/{id}` — run status + result

```json
{
  "run_id": "run_8f2a…",
  "status": "running | completed | failed | cancelled",
  "model": "gemini/gemini-3.1-pro-preview",
  "result": { "reason": "completed", "text": "…", "turns": 6,
              "usage": { "input_tokens": 30100, "output_tokens": 2150 }, "cost": 0.061 },
  "created_at": "…", "finished_at": "…"
}
```

`result` is null until the run finishes.

#### `GET /v1/runs/{id}/transcript` — clean transcript

Returns the transcript dict the agent already assembles (`session_id`, `model`,
`turns`, `usage`, `messages` with `raw_part` stripped).

#### `GET /v1/runs/{id}/proof` — generated artifact

The resulting Lean file(s) produced in the workspace for this run, if any.

#### `POST /v1/runs/{id}/cancel`

Cancels a running job. The agent generator is closed; its `finally` block tears down
any MCP servers cleanly. Returns the run in `cancelled` state.

#### `POST /v1/runs/{id}/resume`

Body `{ "task": "<follow-up>" }`. Starts a new run seeded from this run's session.

#### `GET /v1/runs`

List/filter runs by `status`, `model`, time range; paginated.

### 4.2 Sessions

Lea persists sessions and can resume them.

| Method & path                 | Purpose                                      |
| ----------------------------- | -------------------------------------------- |
| `GET /v1/sessions`            | List saved sessions (id, message_count, model) |
| `GET /v1/sessions/{id}`       | Full message history for a session           |
| `DELETE /v1/sessions/{id}`    | Delete a session                             |

### 4.3 Config

| Method & path                  | Purpose                                                         |
| ------------------------------ | --------------------------------------------------------------- |
| `POST /v1/config/validate`     | Validate a config payload; return resolved `LeaConfig` or typed error. No run, no disk. |
| `GET /v1/config/default`       | The shipped `default.yaml` as the base template.                |
| `GET /v1/configs`              | List stored named configs (optional feature).                   |
| `POST /v1/configs`             | Create a stored named config; referenced later as `config_ref`. |
| `GET/PUT/DELETE /v1/configs/{name}` | Read / update / delete a stored config.                    |

`POST /v1/config/validate` response on success:

```json
{
  "valid": true,
  "config": {
    "model_name": "gemini/gemini-3.1-pro-preview",
    "model_kwargs": { "max_tokens": 16384 },
    "stream": true,
    "prompt_variant": "default",
    "max_turns": null,
    "tools": null,
    "tool_modules": [],
    "skills": [],
    "narrate_tool_steps": false,
    "mcp_servers": {}
  }
}
```

### 4.4 Tools

Backed by the tool registry; reflects built-ins, `tool_modules`, and live MCP tools.

| Method & path                     | Purpose                                                |
| --------------------------------- | ------------------------------------------------------ |
| `GET /v1/tools`                   | List registered tools with model-facing schemas.       |
| `GET /v1/tools/{name}`            | One tool's schema.                                      |
| `POST /v1/tools/{name}/invoke`    | Execute a single tool directly (debugging / non-agent flows). |

`POST /v1/tools/{name}/invoke` — body is the tool's `args`; response `{ "content": "…" }`.
Useful for running `lean_check` or `search_mathlib` standalone without a full loop.

### 4.5 Verification (standalone service)

Decoupled from generation; backed by SafeVerify + the LSP fast path.

#### `POST /v1/verify`

```json
{ "proof": "theorem foo : 2 + 2 = 4 := by rfl", "imports": ["Mathlib"], "target": "foo" }
```

Response:

```json
{
  "verified": true,
  "diagnostics": [],
  "axioms": ["propext", "Classical.choice", "Quot.sound"],
  "elapsed_ms": 38
}
```

For long checks, return `202` + `verify_id` and expose `GET /v1/verify/{id}`.

### 4.6 Skills

| Method & path                | Purpose                                |
| ---------------------------- | -------------------------------------- |
| `GET /v1/skills`             | List available skill files.            |
| `GET /v1/skills/{name}`      | Skill content (markdown).              |
| `POST/PUT/DELETE /v1/skills/{name}` | Author/manage skills (optional). |

### 4.7 MCP servers

| Method & path                          | Purpose                                                     |
| -------------------------------------- | ----------------------------------------------------------- |
| `GET /v1/mcp/servers`                  | Configured servers, connection status, and contributed tools. |
| `POST /v1/mcp/servers/{name}/probe`    | Test connectivity and list tools without starting a run.     |

The `probe` response surfaces the warned-and-skipped behavior (a server that fails to
start is reported, not fatal) and any name-collision prefixing.

### 4.8 Models & cost

| Method & path                 | Purpose                                                         |
| ----------------------------- | --------------------------------------------------------------- |
| `GET /v1/models`              | Selectable `provider/model` strings; whether cost mapping exists. |
| `POST /v1/cost/estimate`      | `{ model, input_tokens, output_tokens } → { cost }` via LiteLLM. |

### 4.9 Eval / batch

Eval is a consumer of the agent core, exposed as one.

| Method & path                 | Purpose                                                       |
| ----------------------------- | ------------------------------------------------------------- |
| `POST /v1/eval/runs`          | Batch over a benchmark (`minif2f`, `putnam`, `fqb`), best-of-n. |
| `GET /v1/eval/runs/{id}`      | Progress + aggregate metrics; links to individual `/v1/runs/{id}`. |

### 4.10 Meta

| Method & path           | Purpose                                  |
| ----------------------- | ---------------------------------------- |
| `GET /v1/healthz`       | Liveness/readiness.                      |
| `GET /v1/version`       | Build / agent version.                   |
| `GET /v1/capabilities`  | Feature discovery (transports, limits).  |

---

## 5. Cross-cutting concerns

**Webhooks.** As an alternative to holding an SSE connection, clients can register a
webhook for terminal events (`run.finished`, `run.failed`, `run.cancelled`,
`eval.finished`). Payloads are signed.

**Rate limits & spend caps.** Enforced per API key. `429` carries `Retry-After`;
spend-cap breaches return `402` with the cap context.

**Event schema versioning.** The event JSON is the public wire format. Additive
changes only within a `schema_version`; breaking changes bump it. Consumers should
ignore unknown event types and fields.

**Observability.** Each run carries a `run_id` that threads through logs, the event
stream, the transcript, and any eval aggregation, so a single proof is traceable
end to end.

**Concurrency & resources.** Runs are queued and scheduled against a worker pool;
MCP server processes are owned per-run and torn down on completion or cancel (the
existing lifecycle in `run_events` already guarantees this).

---

## 6. Phasing

### v1 (minimal but complete)

Enough to build a streaming UI, validate before spending, and offer verification as a
service:

- `POST /v1/runs`
- `GET /v1/runs/{id}/events`
- `GET /v1/runs/{id}`
- `POST /v1/runs/{id}/cancel`
- `POST /v1/config/validate`
- `GET /v1/tools`
- `POST /v1/verify`

### v2

Sessions, transcripts, resume, stored configs (`config_ref`), models/cost, and tool
invoke.

### v3

Eval/batch, skills management, MCP server management, webhooks.

---

## 7. Mapping to the existing codebase

| API surface                  | Backed by                                              |
| ---------------------------- | ------------------------------------------------------ |
| Run + event stream           | `agent.run_events()` + `events.py` dataclasses         |
| Run result / transcript      | `Finished` event + the transcript dict in `agent.py`   |
| Cancel (clean MCP teardown)  | generator `close()` + `run_events` `finally` block     |
| Config validate / default    | `validation.validate_config`, `config.load_config`, `configs/default.yaml` |
| Typed API errors             | `errors.LeaError` hierarchy                            |
| Tools list / invoke          | `registry.build_toolset`, `registry.REGISTRY`          |
| Skills                       | `skills.load_skills`, `skills/*.md`                    |
| MCP servers / probe          | `mcp.MCPManager`                                       |
| Models / cost                | `providers.stream`, `litellm.cost_per_token`           |
| Verify                       | SafeVerify + `lean_check` / LSP daemon                 |
| Eval batch                   | `eval/run_*.py` as consumers of `run_events`           |
```
