"""The one config object the agent loop reads — `LeaConfig`.

This is the *single* config dataclass for the whole system (LeaUI architecture
D1·cfg). The prover is a pure in-process library: `run_events(config, …)` is
typed to this class, and the **adapter** owns the loader (`adapter/app/config.py`
reads `config/lea.local.toml` and builds one of these) plus the config file.
There is no second adapter-side config and no mapper between them — one class,
one loader, one file.

The prover must never import the adapter (it stays independently importable), so
the dataclass lives here, in the lower layer. The agent-internal fields carry
sensible defaults so the UI can omit them; the two deployment fields at the
bottom (`lea_root`, `max_spend_usd`) are read by the adapter and ignored by the
loop. Provider API keys are deliberately **not** here — the adapter exports them
to `os.environ` (litellm reads them there), so this object holds no secrets and
is safe to log or serialize.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LeaConfig:
    """Everything the core loop needs, plus two adapter-read deployment fields."""

    # --- required: the only two values a caller must supply ---
    model: str                  # LiteLLM "provider/model", e.g. "gemini/gemini-3.1-pro-preview"
    max_turns: int | None       # None → run until the proof is done

    # --- agent loop knobs (defaulted; the UI omits them) ---
    narrate_tool_steps: bool = False  # True → ask the model to summarize intent before tool calls
    prompt_variant: str = "interactive"  # the chat variant (formalization vs assistant routing)
    model_kwargs: dict = field(default_factory=dict)  # passthrough to litellm.completion
    stream: bool = True         # True → stream tokens live; False → one blocking call
    tools: list[str] | None = None  # tool allowlist (order = call order); None → all registered
    # Opt-in tools to ADD to the default set when `tools is None` (v2.5). Lets a caller
    # request "the default toolset plus spawn_subagent" without naming the defaults —
    # naming them means snapshotting them, and a snapshot taken before the run starts
    # cannot contain the MCP or HTTP tools that register once it does.
    extra_tools: list[str] = field(default_factory=list)
    tool_modules: list[str] = field(default_factory=list)  # modules to import so custom tools register
    skills: list[str] = field(default_factory=list)  # skill markdown injected into the system prompt
    # Extra directories holding sub-agent role YAML (v2.5 B2). User-authored roles are
    # adapter-owned rows materialized to a temp dir; the prover is handed directories, not
    # a database, exactly as `skills` hands it files rather than rows.
    agent_dirs: list[str] = field(default_factory=list)
    # The directory holding materialized multi-file skills (v2.5 H7). Added to the run's
    # READABLE roots so the agent can open a skill's references on demand — writes stay
    # confined by `_sandboxed_write_path`, which this does not touch.
    skills_root: str | None = None
    # Declarative HTTP tools (v2.5 F1): {name, description, method, url, input_schema,
    # auth_key_name, ...} each registered as an ordinary tool for the run. No code — and
    # `auth_key_name` NAMES a key rather than carrying one, so this stays serializable.
    http_tools: list[dict] = field(default_factory=list)
    mcp_servers: dict = field(default_factory=dict)  # name → server spec (stdio or remote)
    # A subagent role's prompt head (item 19): appended AFTER the shared Lean core so
    # a role composes onto — never replaces — the hard rules. None for a top-level run.
    system_prompt_head: str | None = None
    # A per-run COST cap (D6): the loop ends cleanly (summarize-on-cap) once accumulated
    # spend crosses this. None → uncapped. Distinct from `max_spend_usd` (the cumulative
    # app-wide billing guard the adapter enforces): this is a single-run ceiling the loop
    # itself honours — used to give a sub-agent role its own `max_cost`.
    max_cost_usd: float | None = None
    # Per-role sub-agent overrides (D6): {profile_name: {model?, max_turns?, max_cost?,
    # system_prompt?, tools?}}. The coordinator carries the user's edits from the
    # Sub-agents page; `_child_config` merges the matching entry over a role's YAML
    # defaults at spawn — so a role is retuned WITHOUT mutating the vendored profile.
    subagent_overrides: dict = field(default_factory=dict)

    # --- context compaction (G1) ---
    # The condenser bounds the model-facing context on a long run. `context_token_limit`
    # is the model's usable window; compaction triggers once a turn's real input tokens
    # cross `compaction_threshold` of it (200_000 × 0.75 = 150k by default — high enough
    # that normal runs never hit it). Set `context_token_limit` to 0 to disable entirely.
    # The condenser prunes superseded tool outputs first (cheap, no LLM) and only
    # summarizes the older middle if that isn't enough — always keeping the leading goal
    # plus `compaction_keep_recent_turns` most-recent turns verbatim, and the last
    # `compaction_keep_recent_results` tool results unmasked.
    context_token_limit: int = 200_000
    compaction_threshold: float = 0.75
    compaction_keep_recent_turns: int = 6
    compaction_keep_recent_results: int = 4

    # --- deployment / UI (the adapter reads these; the loop ignores them) ---
    lea_root: Path | None = None      # where proof files + per-session git repos live
    max_spend_usd: float | None = None  # UI billing guard checked by the adapter
