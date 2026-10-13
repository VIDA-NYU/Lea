# Architecture decisions

A running log of the significant decisions behind Lea's config-driven redesign,
with the reasoning so future changes don't relitigate settled ground. Newest
context at the bottom of each section.

> Status: in progress on branch `config-driven-foundation`. The agent is the
> product; benchmarking and the UI are consumers of it.

---

## 1. Config-driven, but minimal

**Decision.** Move Lea from code-driven (behavior baked into Python + CLI flags)
to config-driven: model, params, prompt variant, and loop limits come from YAML.
Keep the core loop a dumb `while`; push variability into config.

**Why.** mini-swe-agent (the ~100-line SWE agent that scores >74% on SWE-bench)
shows you can be deeply configurable *and* minimal — config doesn't bloat the
loop, it moves variability outside it. Lea is single-purpose (NL math → Lean
proof), but the same separation lets users change behavior without editing code,
which is what enables the UI and community extension later.

## 2. Agent = product, eval = consumer (module boundary, not a branch)

**Decision.** The agent core is a reusable library with one stable interface;
`eval/` and the UI are separate consumers that drive it. Keep this as a module
boundary, not a long-lived git branch.

**Why.** Branches diverge and rot; this is a *dependency*, not a *divergence*.
Precedent: OpenHands' `evaluation/` directory drives the same agent core.

## 3. Config-in / event-out contract

**Decision.** `run_events(config, task)` yields a typed event stream
(`TurnStarted`, `AssistantTextDelta`, `ToolCalled`, `ToolResulted`,
`UsageUpdated`, `Finished`, …). The CLI renderer, the UI, and eval all consume
the same events. A backward-compatible `run()` wrapper drains the events to
stdout and returns `(text, transcript)` so existing callers (eval) don't change.

**Why.** The UI needs structured, live output — not stdout scraping. One contract
serves all three consumers; the event stream *is* the transparency layer.

## 4. No hardcoded defaults

**Decision.** `configs/default.yaml` is the single source of truth for defaults,
always loaded as the base; `--config FILE` overlays on top. `LeaConfig` has no
field defaults.

**Why.** Changing a default should mean editing YAML, not Python. Keeps config
honest and in one place.

## 5. Validation is separate and I/O-free; errors are typed

**Decision.** `validation.py` holds the `LeaConfig` schema + `validate_config(raw)`
(pure, no disk, raise-on-first). `config.py` is the file-I/O wrapper. Typed
exceptions live in `errors.py` (`ConfigError` → Format / UnknownKey / MissingKey
/ InvalidValue).

**Why.** The UI/API can validate a config payload before use without touching
disk or running the agent. Typed errors pinpoint exactly what failed.

## 6. Engine = LiteLLM, streaming, contract preserved

**Decision.** Drive the model through `litellm.completion(stream=True)` so any
provider/model works via config. Rewrite only `providers.stream()` internals;
keep the `TextDelta | ToolCall | _ToolMeta | Done` event types and the existing
neutral message format (convert to OpenAI shape inside `stream()`).

**Why.** LiteLLM is how mini-swe-agent stays provider-agnostic. Keeping the
contract makes the swap engine-only: `agent.py`, sessions, transcripts, and eval
are untouched.

**Streaming is config-driven (`model.stream: bool`).** The UI wants live token
output, so streaming is the default; but a consumer can set `stream: false` for a
single blocking call (mini-swe-agent's mode). Both paths in `providers.stream()`
yield the *same* event types (`TextDelta`/`ToolCall`/`Done`), so the agent loop
and renderer are identical either way — blocking just emits one whole `TextDelta`.
This turns the earlier streaming-vs-blocking divergence from mini into a setting.

**Notes.**
- Model names use LiteLLM's `provider/model` convention (`gemini/…`,
  `anthropic/…`); provider is in the prefix, so `detect_provider` is dropped.
- LiteLLM's `gemini/` provider reads `GEMINI_API_KEY` (the old code used
  `GOOGLE_API_KEY`) — accept either.
- We deliberately did **not** add a `_normalize_model` shim that guesses a
  prefix for bare names — that's just `detect_provider` in disguise, and bare
  names are ambiguous (`gpt-4o` could be openai/azure/openrouter). Requiring an
  explicit prefix is the unambiguous, LiteLLM-recommended design.
- **Consequence (deferred):** the eval harnesses still pass *bare* model names
  (e.g. `gemini-3.1-pro-preview`); they will need the `provider/` prefix when
  eval adopts the config path. Not done yet — eval adoption is a later step.

## 7. `model_kwargs` open dict instead of fixed param fields

**Decision.** Config carries `model_kwargs: dict` splatted into
`litellm.completion(**model_kwargs)`. No fixed `max_tokens`/`temperature` fields.

**Why.** mini-swe-agent's pattern: any LiteLLM arg (temperature, max_tokens,
reasoning_effort, …) becomes config with zero code change.

## 8. Cost from LiteLLM + per-turn cost transparency

**Decision.** Compute cost via `litellm.completion_cost` (delete the hand-kept
`MODEL_PRICING` table), with a graceful fallback to `$0.00` + a one-time warning
when a model isn't in LiteLLM's price map. Surface **per-turn cost** in the event
stream (`UsageUpdated.cost`) and the CLI, with cumulative cost on `Finished`.

**Why.** Cost-tracking for any model with nothing to maintain per model. Gemini
*preview* models may be absent from the price map, hence the fallback. Per-turn
cost gives the UI/users live spend transparency.

## 9. Tools live in a registry; config selects them

**Decision.** Replace the two hand-synced globals (`TOOLS_SCHEMA` list +
`TOOL_HANDLERS` dict) with a registry of `Tool` records (`name` + model-facing
`schema` + `dict[args] -> str` `handler`). The loop never imports tools directly;
it calls `build_toolset(config.tools)`. The built-ins register at import (so
they stay the readable source) and custom tools register through a public API —
`@tool(...)`/`register(...)` — from Python modules named in `agent.tool_modules`.

Config selection is an **optional allowlist**: `agent.tools: null` → every
registered tool in registration order (today's behavior, exactly); a list →
*exactly* those tools, in that order (the list both filters and orders, so
removing `bash` is "leave it out"). Unknown name / duplicate registration / bad
`tool_modules` import → typed `ToolError`.

**Why.** This is the first of the three extension points the whole redesign is
for (tools / MCP / skills). Making tools data — not hardcoded globals — is what
lets users add, drop, or reorder tools from config without touching the loop, and
gives MCP a place to land (MCP tools will register here too). The allowlist over
an enabled/disabled pair keeps one knob with no conflicting state; ordering falls
out for free. Built-ins keep using the readable `TOOLS_SCHEMA`/`TOOL_HANDLERS`
tables as their source and register in bulk, so nothing about today's built-in tools
changed — only how the loop reaches them. The event contract, sessions, and
transcripts are untouched.

## 10. Skills = config-listed markdown injected into the system prompt

**Decision.** A *skill* is a markdown fragment of procedural knowledge (a tactic
recipe, a naming convention, project house rules). `agent.skills` is an optional,
ordered list of file paths (default `[]`); `load_skills` reads each and appends it
under a `## Skill: <stem>` header, and `load_system_prompt(variant, skills)`
injects them **always-on** after the base variant. A new `lea/skills.py` owns the
I/O; a missing/unreadable file raises typed `SkillError`. The implicit `lea.md`
auto-append is **kept** for back-compat; skills are the explicit path and are
injected after it.

Chosen over the alternatives: explicit file list (not directory auto-discovery —
same magic-path we left behind with `lea.md`); plain markdown (not frontmatter —
zero ceremony; frontmatter/menus can come if we ever add triggered loading); and
always-on injection (not model-triggered progressive disclosure — that's a much
bigger feature than "knowledge the agent should always have").

**Why.** Second of the three extension points (tools / MCP / **skills**). It makes
procedural knowledge a config artifact users can add/remove/reorder without
touching code or the prompt source, mirroring the tools allowlist (decision 9):
explicit, ordered, validated, no magic. No registry — skills are just text, so a
file list is the natural unit (unlike tools, which need handlers).

## 11. MCP servers register tools into the same registry

**Decision.** Add an `mcp.servers` config section (Claude-Desktop `mcpServers`
style). At run start, `MCPManager` connects to each server, lists its tools, and
registers each into the shared registry — after which they are ordinary tools to
the loop, governed by the same `agent.tools` allowlist. `agent.run_events` owns
the lifecycle: it starts the manager (so MCP tools are registered *before*
`build_toolset`) and stops it in a `finally`.

**Naming: bare, prefix only on collision.** MCP tools register under their *real*
name (`lean_run_code`), matching Claude Desktop/Cursor and how models are trained
to call MCP tools. Only when a name is already taken (a built-in or another
server) do we prefix the clashing one with `<server>__`; the handler still calls
the real tool name on the server. This was changed after a live test against
`lean-lsp-mcp`: with always-`<server>__<tool>` naming, the model wasted 5 turns
calling the bare `lean_run_code` (getting "unknown tool") before using the
prefixed name — clear evidence that always-prefixing fights the model.

Sub-decisions (user-chosen): **both transports** — stdio (subprocess `command`)
and remote (`url`, streamable HTTP default or `transport: sse`); `mcp` is a
**core dependency** (not an optional extra); a server that fails to start is
**warned-and-skipped**, not fatal (robust for the agent-as-product use; the run
continues with the remaining tools).

Implementation note (mechanism): MCP's SDK is asyncio + anyio, whose client
contexts must be entered and exited in the *same* task. So the manager runs one
long-lived `_serve` coroutine on a private event loop in a background thread: it
opens every session, signals ready, then parks on a stop event holding the
contexts open; tool calls are dispatched onto that loop via
`run_coroutine_threadsafe`, presenting the loop with plain `dict -> str` handlers.
This mirrors the warm-persistent-process pattern of `lsp_daemon.py` (avoids a
per-call server respawn) and keeps the sync agent loop untouched. A new
`unregister()` on the registry lets the manager tear its tools down on stop so a
later run re-registers cleanly.

**Why.** Third and last of the extension points (tools / skills / **MCP**). The
registry (decision 9) is exactly what makes this small: MCP is just another tool
source feeding the same `REGISTRY`, so nothing downstream — `build_toolset`, the
loop, events, sessions — changes. It lets users add whole tool suites (filesystem,
git, search, a Lean MCP server) from config with no code.

---

## mini-swe-agent alignment

Every decision mapped against mini-swe-agent (the config-driven agent we modeled
on), marked where Lea follows it vs. diverges and why.

**Followed (same as mini):**
- Config-driven design with a minimal core loop.
- Agent = product; benchmarks/UI = consumers (module boundary, not branches).
- Engine = LiteLLM.
- `provider/model` naming; no provider-guessing.
- Open `model_kwargs` dict splatted into `litellm.completion`.
- Cost from LiteLLM (`completion_cost`/`cost_per_token`) + graceful fallback.

**Diverged (with reason):**
- **Typed event stream.** mini is blocking and has no event stream; Lea yields a
  typed event stream so the UI can render live. (Streaming itself is now a config
  toggle — see decision 6 — so mini's *blocking* mode is available too; the typed
  *contract* is the genuine, deliberate addition.)
- **No hardcoded defaults.** mini uses pydantic field defaults; Lea makes
  `default.yaml` the sole source (`LeaConfig` has no defaults).
- **Validation.** mini uses pydantic + "don't catch exceptions"; Lea hand-rolls an
  I/O-free `validate_config` + typed `ConfigError` hierarchy so the UI/API can
  validate a payload with precise, pinpointing errors.
- **Per-turn cost surfaced live.** mini records cost in message metadata; Lea emits
  per-turn cost as an event + CLI line for transparency.

**Pre-existing Lea ≠ mini (predate this work):**
- **Tools.** mini's default is text-based (bash in fenced code blocks, no tool API);
  Lea uses the native tool-calling API with a built-in toolset.
- **Message format.** mini stores OpenAI-format messages natively; Lea keeps a
  neutral format and converts to OpenAI shape inside `stream()` (to preserve the
  contract while streaming).

**A mini feature we deferred:**
- Pluggable `model_class` registry (`litellm`/`litellm_textbased`/`portkey`/…).
  Not needed while LiteLLM is the only backend.

---

## Deferred (not yet decided / built)

Swappable verifier (behind one tool interface); benchmark config + eval-harness
adoption (eval still passes bare model names → need `provider/` prefix);
pluggable `model_class` registry for non-LiteLLM backends; MCP resources/prompts
(only tools are wired today); remote-MCP auth flows beyond static headers.
