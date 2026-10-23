"""Provider layer — a thin streaming wrapper over LiteLLM.

One `stream()` drives every provider through `litellm.completion`, yielding a
unified event stream (`TextDelta | ToolCall | _ToolMeta | Done`). Messages use
Lea's neutral format and are converted to OpenAI shape here; LiteLLM translates
from there to whatever provider the model name selects (`gemini/…`,
`anthropic/…`, `openai/…`, `openrouter/…`, …). Cost comes from LiteLLM.
Models behind a Portkey AI gateway (`portkey/…`) are pointed at the gateway:
Anthropic-family models through its native Messages route, everything else
through the `openai/` path — see the Portkey section below.

Anthropic-family models (any route) get prompt-cache breakpoints — see
`_with_cache_breakpoints`.
"""

import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

import litellm

from .usage import merge_details, legacy_details


@dataclass
class Usage:
    # Inclusive prompt tokens remain separate from the optional cache details.
    input_tokens: int = 0
    output_tokens: int = 0
    usage_details: dict | None = field(default=None, compare=False)
    raw_usages: list[dict] = field(default_factory=list, compare=False, repr=False)

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        details = other.usage_details
        if details is None and (other.input_tokens or other.output_tokens):
            details = legacy_details()
        self.usage_details = merge_details(self.usage_details, details)
        self.raw_usages.extend(other.raw_usages)

    @property
    def cache_read_tokens(self) -> int:
        return int((self.usage_details or {}).get("cached_input_tokens") or 0)

    @property
    def cache_write_tokens(self) -> int:
        return int((self.usage_details or {}).get("cache_write_tokens") or 0)


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCall:
    name: str
    args: dict
    raw_part: object = None  # kept for message-replay compatibility; unused with LiteLLM


@dataclass
class _ToolMeta:
    """Internal: carries the provider tool-call id so the agent can build tool_result messages."""
    tool_use_id: str


@dataclass
class Done:
    usage: Usage
    cost: float = 0.0
    # Responses reasoning items must be replayed with the assistant tool call on
    # the next turn.  They stay internal to Lea's transcript and are never exposed
    # as assistant text.
    reasoning_items: list[dict[str, Any]] = field(default_factory=list)
    usage_details: dict | None = None
    raw_usage: dict | None = None

    def __post_init__(self):
        if self.raw_usage is not None:
            self.usage.raw_usages.append(self.raw_usage)
        if self.usage_details is not None:
            self.usage.usage_details = self.usage_details
        else:
            self.usage_details = self.usage.usage_details


_WARNED_MODELS: set[str] = set()
_CATALOG_RESPONSES_MODELS: frozenset[str] = frozenset()


def set_catalog_models(models: list[dict]) -> None:
    """Atomically replace discovery hints, without touching LiteLLM's globals.

    Already built requests retain their route and parameters. These hints let
    new, non-GPT-named OpenAI models use their declared Responses protocol.
    """
    global _CATALOG_RESPONSES_MODELS
    _CATALOG_RESPONSES_MODELS = frozenset(
        m["value"].removeprefix("openai/").removeprefix("responses/")
        for m in models if m.get("provider") == "openai" and m.get("mode") == "responses"
        and m.get("compatibility") != "unsupported"
    )


def _to_openai_tools(tools: list) -> list:
    """Convert Lea's tool schema ({name, description, input_schema}) to OpenAI function tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


def _to_openai_messages(system: str, messages: list, *, include_reasoning: bool = False) -> list:
    """Convert Lea's neutral message format to OpenAI chat messages."""
    out = [{"role": "system", "content": system}]
    for msg in messages:
        if msg["role"] == "user":
            if isinstance(msg["content"], str):
                out.append({"role": "user", "content": msg["content"]})
            elif isinstance(msg["content"], list):
                for item in msg["content"]:
                    if item.get("type") == "tool_result":
                        out.append({
                            "role": "tool",
                            "tool_call_id": item.get("tool_call_id") or item.get("tool_use_id"),
                            "content": item["content"],
                        })
        elif msg["role"] == "assistant":
            oai = {"role": "assistant", "content": None}
            text_parts, tool_calls, reasoning_items = [], [], []
            for item in msg["content"]:
                if item.get("type") == "text":
                    text_parts.append(item["text"])
                elif include_reasoning and item.get("type") == "reasoning":
                    reasoning_items.extend(item.get("items") or [])
                elif item.get("type") == "tool_call":
                    tool_calls.append({
                        "id": item["id"],
                        "type": "function",
                        "function": {"name": item["name"], "arguments": json.dumps(item["args"])},
                    })
            if text_parts:
                oai["content"] = "\n".join(text_parts)
            if tool_calls:
                oai["tool_calls"] = tool_calls
            if reasoning_items:
                # LiteLLM's Chat→Responses bridge recognizes this extension and
                # restores each encrypted reasoning item before the function call.
                oai["reasoning_items"] = reasoning_items
            out.append(oai)
    return out


def _is_openai_gpt_5_6(model: str) -> bool:
    """Whether ``model`` is a GPT-5.6 family ID routed directly to OpenAI."""
    normalized = model.lower()
    if normalized.startswith("openai/"):
        normalized = normalized.removeprefix("openai/")
    if normalized.startswith("responses/"):
        normalized = normalized.removeprefix("responses/")
    return normalized == "gpt-5.6" or normalized.startswith("gpt-5.6-")


def _uses_openai_responses(model: str) -> bool:
    """Direct OpenAI Responses routes, including future IDs absent from LiteLLM.

    No global LiteLLM metadata is changed by catalog refreshes. The route is
    resolved once for each call, and reasoning items stay in Lea's transcript.
    """
    bare = model.removeprefix("openai/")
    if "/" in bare and not bare.startswith("responses/"):
        return False
    if bare.startswith("responses/"):
        return True
    if bare in _CATALOG_RESPONSES_MODELS:
        return True
    spec = getattr(litellm, "model_cost", {}).get(bare, {})
    if spec.get("mode") == "responses" and spec.get("litellm_provider") == "openai":
        return True
    match = re.match(r"^gpt-(\d+)(?:\.(\d+))?(?:-|$)", bare)
    return bool(match and (int(match[1]), int(match[2] or 0)) >= (5, 6))


# --- Portkey AI gateway -------------------------------------------------------
# A Portkey gateway (hosted, or self-hosted like NYU's) fronts many providers behind
# ONE OpenAI-compatible endpoint. LiteLLM has no native Portkey provider, so Lea
# routes these through LiteLLM's `openai/` path with the gateway as `api_base`.
# Anthropic-family models are the exception: Portkey also serves the Anthropic
# Messages API natively (`{root}/v1/messages`), and Lea takes that route via
# LiteLLM's `anthropic/` provider — see `_portkey_anthropic_kwargs` for why.
#
# Model IDs: `portkey/<catalog-name>`, where the catalog name is whatever the
# gateway expects — typically Portkey's model-catalog syntax `@provider-slug/model`
# (e.g. `portkey/@vertexai-jdoe/anthropic.claude-opus-4-8`). Only the leading
# `portkey/` is stripped; the rest reaches the gateway verbatim. A bare `@slug/model`
# (already in gateway syntax) is recognised as Portkey too.
#
# Auth/endpoint come from the environment, like every other provider's key:
#   PORTKEY_API_KEY      required — sent as `x-portkey-api-key` (and as the Bearer
#                        token, which Portkey's OpenAI-compat mode also accepts)
#   PORTKEY_BASE_URL     the gateway's `/v1` root; unset → Portkey's hosted service
#   PORTKEY_VIRTUAL_KEY / PORTKEY_CONFIG / PORTKEY_PROVIDER
#                        optional routing headers for gateways that need them
PORTKEY_PREFIX = "portkey/"
PORTKEY_DEFAULT_BASE_URL = "https://api.portkey.ai/v1"
_PORTKEY_OPTIONAL_HEADERS = {
    "PORTKEY_VIRTUAL_KEY": "x-portkey-virtual-key",
    "PORTKEY_CONFIG": "x-portkey-config",
    "PORTKEY_PROVIDER": "x-portkey-provider",
}


def is_portkey_model(model: str) -> bool:
    """Whether ``model`` is served through a Portkey gateway."""
    return model.startswith(PORTKEY_PREFIX) or model.startswith("@")


def portkey_model_name(model: str) -> str:
    """The model name the gateway expects: the `portkey/` prefix (if any) stripped,
    everything else untouched — `portkey/@vertexai-x/anthropic.claude-opus-4-8`
    becomes `@vertexai-x/anthropic.claude-opus-4-8`."""
    return model[len(PORTKEY_PREFIX):] if model.startswith(PORTKEY_PREFIX) else model


def portkey_base_url(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get("PORTKEY_BASE_URL") or "").strip().rstrip("/") or PORTKEY_DEFAULT_BASE_URL


def _portkey_gateway_root(base_url: str) -> str:
    """The gateway's root, for LiteLLM providers that append their own path.

    `PORTKEY_BASE_URL` conventionally includes the `/v1` (it is the OpenAI-compat
    root, and the `openai/` path posts to `{base}/chat/completions`), but LiteLLM's
    `anthropic/` provider appends `/v1/messages` itself — handing it the `/v1` form
    would post to `/v1/v1/messages`.
    """
    root = base_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def _portkey_split_catalog(name: str) -> tuple[str | None, str]:
    """Split a Portkey model-catalog name `@provider-slug/model` into the slug and
    the bare model name. A non-catalog name passes through with no slug."""
    if name.startswith("@") and "/" in name:
        slug, bare = name.split("/", 1)
        return slug, bare
    return None, name


def _portkey_api_key() -> str:
    api_key = os.environ.get("PORTKEY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "Portkey models need the PORTKEY_API_KEY environment variable "
            "(add the Portkey key in Settings → API keys)."
        )
    return api_key


def _portkey_headers(api_key: str, model_kwargs: dict, *defaults: tuple[str, str]) -> dict:
    """Portkey routing headers: the key, `defaults` (lowest precedence), the optional
    env-supplied headers, then the caller's `extra_headers`, which win so a run can
    still override a routing header explicitly."""
    headers = {"x-portkey-api-key": api_key}
    headers.update(dict(defaults))
    for env_name, header in _PORTKEY_OPTIONAL_HEADERS.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            headers[header] = value
    headers.update(model_kwargs.get("extra_headers") or {})
    return headers


def _portkey_kwargs(model_kwargs: dict) -> dict:
    """LiteLLM kwargs that point an `openai/` call at the Portkey gateway.

    `strict-open-ai-compliance` asks the gateway to normalise provider-native
    finish reasons (Anthropic's `tool_use`) to the OpenAI vocabulary; the stream
    parser tolerates the raw label anyway, but ask for the dialect we parse against.
    """
    out = dict(model_kwargs)
    api_key = _portkey_api_key()
    out["extra_headers"] = _portkey_headers(
        api_key, out, ("x-portkey-strict-open-ai-compliance", "true"),
    )
    out.setdefault("api_key", api_key)
    out.setdefault("api_base", portkey_base_url())
    return out


def _portkey_anthropic_kwargs(model: str, model_kwargs: dict) -> dict:
    """LiteLLM kwargs that point an `anthropic/` call at the Portkey gateway's
    native Messages route.

    Why not the OpenAI-compatible route like everything else: the gateway's
    chat-completions translation drops `cache_control` from tool results, so the
    moving prompt-cache breakpoint (`_with_cache_breakpoints`) — the one that makes
    a long tool-using conversation cheap — cannot survive it. The native route
    carries the breakpoints and the cache usage counters verbatim, and needs no
    finish-reason translation. It is how Claude Code runs through Portkey.

    Auth follows Portkey's documented Anthropic-SDK shape: the Portkey key in
    `x-portkey-api-key`, the provider slug from the catalog name in
    `x-portkey-provider`, and the bare model name in the request body.
    """
    out = dict(model_kwargs)
    api_key = _portkey_api_key()
    slug, _bare = _portkey_split_catalog(portkey_model_name(model))
    defaults = (("x-portkey-provider", slug),) if slug else ()
    out["extra_headers"] = _portkey_headers(api_key, out, *defaults)
    out.setdefault("api_key", api_key)
    out.setdefault("api_base", _portkey_gateway_root(portkey_base_url()))
    return out


def _portkey_route_missing(exc: Exception) -> bool:
    """Whether a gateway error means the native Messages route itself is absent
    (an older gateway build) rather than the request being bad — only then is
    falling back to the chat-completions dialect the right move."""
    return getattr(exc, "status_code", None) in (404, 405, 501)


def _portkey_cost_candidates(model: str) -> list[str]:
    """Model names to try in LiteLLM's price map for a gateway model.

    The gateway name carries a provider slug and often a vendor prefix
    (`@vertexai-x/anthropic.claude-opus-4-8`); the price map knows the model as
    `claude-opus-4-8`. Try the bare name, then the name after its first `.`.
    """
    name = portkey_model_name(model)
    if name.startswith("@") and "/" in name:
        name = name.split("/", 1)[1]
    candidates = [name]
    if "." in name:
        candidates.append(name.split(".", 1)[1])
    return [c for c in candidates if c]


def _is_anthropic_family(model: str) -> bool:
    """Whether the model is a Claude, however it is reached: `anthropic/…`, a bare
    `claude-…`, Bedrock's `anthropic.claude-…`, Vertex, or a gateway catalog name
    with the vendor prefix inside it."""
    return "claude" in model.lower()


def _litellm_model(model: str, *, portkey_native: bool = False) -> str:
    """Make GPT-5.6 provider resolution independent of LiteLLM's remote model map,
    and send Portkey models down LiteLLM's OpenAI-compatible path — or, for a
    Claude taking the gateway's native Messages route, the `anthropic/` path with
    the bare model name (the provider slug travels as a header)."""
    if is_portkey_model(model):
        if portkey_native:
            return f"anthropic/{_portkey_split_catalog(portkey_model_name(model))[1]}"
        return f"openai/{portkey_model_name(model)}"
    if _is_openai_gpt_5_6(model) and "/" not in model:
        return f"openai/{model}"
    if _uses_openai_responses(model) and not _is_openai_gpt_5_6(model):
        return "openai/responses/" + model.removeprefix("openai/").removeprefix("responses/")
    if model.startswith("gpt-") or re.match(r"^o\d", model):
        return f"openai/{model}"
    if model.startswith("claude-"):
        return f"anthropic/{model}"
    return model


_CACHE_BREAKPOINT = {"type": "ephemeral"}


def _with_cache_breakpoints(oai_messages: list) -> list:
    """A copy of the OpenAI-shaped messages with Anthropic prompt-cache breakpoints.

    Anthropic caching is an explicit prefix match (unlike Gemini/OpenAI, which
    cache server-side with no opt-in): content up to a `cache_control` breakpoint
    is cached for ~5 minutes and re-served at ~0.1x the input rate, for a one-time
    ~1.25x write premium. Two breakpoints (the API allows four):

      1. The system prompt. The request renders tools -> system -> messages, so
         this one breakpoint caches the tool schemas AND the system prompt, both
         byte-identical across every turn of a run.
      2. The last block of the final message — the "moving" breakpoint. Each turn
         re-sends the whole conversation, and last turn's breakpoint is a prefix
         of this turn's request, so the history is a cache read and only the new
         suffix is written.

    LiteLLM carries `cache_control` from a system content block, a user content
    block, and a tool message (message-level) through to the Messages API, and
    Bedrock/Vertex's equivalents. Prefixes under the model's minimum (~4k tokens
    on Opus) silently don't cache, so this is safe for tiny conversations too.
    Messages are rebuilt, never marked in place: `_to_openai_messages` makes fresh
    dicts each turn, so a marker can't linger on a block that is no longer last.
    """
    if not oai_messages:
        return oai_messages
    out = [dict(m) for m in oai_messages]
    system = out[0]
    if system.get("role") == "system" and isinstance(system.get("content"), str):
        system["content"] = [
            {"type": "text", "text": system["content"], "cache_control": _CACHE_BREAKPOINT},
        ]
    if len(out) < 2:
        return out
    last = out[-1]
    content = last.get("content")
    if last.get("role") == "user" and isinstance(content, str):
        last["content"] = [{"type": "text", "text": content, "cache_control": _CACHE_BREAKPOINT}]
    elif last.get("role") == "user" and isinstance(content, list) and content:
        last["content"] = [*content[:-1], {**content[-1], "cache_control": _CACHE_BREAKPOINT}]
    else:
        # A tool result (LiteLLM reads its cache_control at message level) or an
        # assistant turn.
        last["cache_control"] = _CACHE_BREAKPOINT
    return out


def _responses_kwargs(tools: list, model_kwargs: dict, *, default_effort: str | None = None) -> dict:
    """Stateless Responses policy; retain GPT-5.6's historical medium effort.

    Other models use their provider default unless the caller selects an effort.
    New IDs need an explicit per-request allowance with older LiteLLM versions.
    """
    out = dict(model_kwargs)
    if default_effort:
        out.setdefault("reasoning_effort", default_effort)
    effort = out.get("reasoning_effort")
    if effort is not None:
        if effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("Unsupported reasoning_effort for this Responses request")
        allowed = list(out.get("allowed_openai_params") or [])
        if "reasoning_effort" not in allowed:
            allowed.append("reasoning_effort")
        out["allowed_openai_params"] = allowed
    if tools:
        # LiteLLM's completion() signature is Chat-shaped.  Responses-only
        # parameters passed at the top level are filtered before its bridge in
        # 1.88.x; extra_body is the documented bridge escape hatch and the
        # transformation promotes these supported keys into the Responses request.
        raw_extra_body = out.get("extra_body") or {}
        if not isinstance(raw_extra_body, dict):
            raise ValueError("extra_body must be an object when using Responses tools")
        extra_body = dict(raw_extra_body)
        if "store" in out:
            extra_body["store"] = out.pop("store")
        else:
            extra_body.setdefault("store", False)
        if "include" in out:
            include = out.pop("include")
        else:
            include = extra_body.get("include", [])
        if not isinstance(include, list):
            raise ValueError("include must be a list when using Responses tools")
        if "reasoning.encrypted_content" not in include:
            # Copy a caller-owned list before extending it.
            include = [*include, "reasoning.encrypted_content"]
        extra_body["include"] = include
        out["extra_body"] = extra_body
    return out


def _plain_reasoning_item(item: Any) -> dict[str, Any] | None:
    """Normalize LiteLLM's pydantic/dict reasoning item for transcript storage."""
    if isinstance(item, dict):
        raw = dict(item)
    elif hasattr(item, "model_dump"):
        raw = item.model_dump(exclude_none=True)
    else:
        raw = {
            key: getattr(item, key)
            for key in ("id", "type", "encrypted_content", "summary")
            if getattr(item, key, None) is not None
        }
    if not raw.get("id"):
        return None
    return {
        "id": str(raw["id"]),
        "type": "reasoning",
        "encrypted_content": raw.get("encrypted_content"),
        "summary": raw.get("summary") or [],
    }


def _merge_reasoning_items(target: dict[str, dict[str, Any]], items: Any) -> None:
    """Merge streamed reasoning snapshots by stable Responses item ID."""
    for item in items or []:
        normalized = _plain_reasoning_item(item)
        if normalized is not None:
            target[normalized["id"]] = normalized


def _plain(value):
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if hasattr(value, "__dict__"):
        return {k: _plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    return value


def _normalized_usage(raw: dict) -> Usage:
    # LiteLLM prompt_tokens is already inclusive (including Anthropic cache
    # reads/writes). Native Anthropic input_tokens excludes those cache tokens.
    prompt = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
    output = raw.get("completion_tokens_details") or raw.get("output_tokens_details") or {}
    cached = prompt.get("cached_tokens", raw.get("cache_read_input_tokens"))
    written = prompt.get("cache_creation_tokens", prompt.get("cache_write_tokens",
                         raw.get("cache_creation_input_tokens")))
    creation = prompt.get("cache_creation_token_details") or raw.get("cache_creation")
    if written is None and creation:
        written = sum(v or 0 for v in creation.values())
    input_tokens = raw.get("prompt_tokens")
    if input_tokens is None:
        input_tokens = raw.get("input_tokens", 0) or 0
        if any(key in raw for key in ("cache_read_input_tokens", "cache_creation_input_tokens", "cache_creation")):
            input_tokens += (cached or 0) + (written or 0)
    output_tokens = raw.get("completion_tokens", raw.get("output_tokens", 0)) or 0
    reasoning = output.get("reasoning_tokens", raw.get("reasoning_tokens"))
    details = {"version": 1, "cached_input_tokens": cached,
               "cache_write_tokens": written, "reasoning_tokens": reasoning,
               "details_missing_calls": int(not prompt and cached is None and written is None)}
    return Usage(int(input_tokens or 0), int(output_tokens), details)


def _terminal_done(model: str, response: dict, reasoning_items: list) -> Done:
    raw = response.get("usage") or {}
    usage = _normalized_usage(raw)
    details = usage.usage_details
    response_model = response.get("model") or _litellm_model(model)
    details.update(response_model=response_model, service_tier=response.get("service_tier"))
    cost = None
    # A provider/proxy's explicit charge takes precedence over estimated pricing.
    hidden = response.get("_hidden_params") or {}
    headers = hidden.get("additional_headers") or {}
    reported = headers.get("llm_provider-x-litellm-response-cost")
    if reported is None:
        reported = raw.get("cost", response.get("usage_cost"))
    try:
        if reported is not None:
            cost = float(reported)
            if not math.isfinite(cost) or cost < 0:
                raise ValueError("negative reported cost")
            basis = "provider_reported"
        elif raw:
            # Keep the full response: cache TTLs, response model, regional and
            # service-tier metadata all affect provider-specific calculations.
            normalized = dict(raw, prompt_tokens=usage.input_tokens, completion_tokens=usage.output_tokens)
            # Remove native aliases after normalization; otherwise LiteLLM
            # identifies this as Responses usage and normalizes it a second time.
            normalized.pop("input_tokens", None)
            normalized.pop("output_tokens", None)
            normalized.pop("input_tokens_details", None)
            normalized.pop("output_tokens_details", None)
            prompt_details = dict(raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {})
            if raw.get("cache_creation") and not prompt_details.get("cache_creation_token_details"):
                prompt_details["cache_creation_token_details"] = raw["cache_creation"]
            if details["cached_input_tokens"] is not None:
                prompt_details["cached_tokens"] = details["cached_input_tokens"]
            if details["cache_write_tokens"] is not None:
                prompt_details["cache_creation_tokens"] = details["cache_write_tokens"]
            if prompt_details:
                normalized["prompt_tokens_details"] = prompt_details
            output_details = raw.get("completion_tokens_details") or raw.get("output_tokens_details")
            if output_details:
                normalized["completion_tokens_details"] = output_details
            candidates = ([response.get("model"), *_portkey_cost_candidates(model)]
                          if is_portkey_model(model) else [response_model])
            last_error = None
            for pricing_model in dict.fromkeys(candidate for candidate in candidates if candidate):
                try:
                    cost_response = dict(response, model=pricing_model, usage=normalized)
                    full_response = litellm.ModelResponse(**cost_response)
                    full_response._hidden_params.update(hidden)
                    cost = litellm.completion_cost(completion_response=full_response,
                                                  model=pricing_model)
                    if cost is None or not math.isfinite(cost) or cost < 0:
                        raise ValueError("no valid pricing returned")
                    details["pricing_model"] = pricing_model
                    break
                except Exception as error:
                    last_error = error
            else:
                raise last_error or ValueError("no known upstream pricing model")
            basis = "cache_aware_estimate"
        else:
            basis = "unavailable"
    except Exception as e:
        basis = "unavailable"
        cost = None
        if model not in _WARNED_MODELS:
            _WARNED_MODELS.add(model)
            print(f"[lea] pricing unavailable for '{model}': {e}", file=sys.stderr)
    details.update(cost_basis=basis, basis_counts={basis: 1},
                   unpriced_calls=int(basis == "unavailable"))
    # Float totals retain their historical contract. Metadata explicitly marks
    # unavailable charges; consumers must not present this placeholder as free.
    return Done(usage, float(cost or 0), reasoning_items, details, raw)


def _response_metadata(response) -> dict:
    raw = _plain(response) or {}
    hidden = getattr(response, "_hidden_params", None)
    if hidden:
        raw["_hidden_params"] = _plain(hidden)
    return raw


def _api_key_kwargs(model: str) -> dict:
    """Accept GOOGLE_API_KEY for gemini/* models (LiteLLM expects GEMINI_API_KEY)."""
    if model.startswith("gemini/") and not os.environ.get("GEMINI_API_KEY") and os.environ.get("GOOGLE_API_KEY"):
        return {"api_key": os.environ["GOOGLE_API_KEY"]}
    return {}


def stream(model: str, system: str, messages: list, tools: list,
           model_kwargs: dict | None = None, streaming: bool = True):
    """Yield TextDelta, ToolCall, _ToolMeta, and Done events from the model via LiteLLM.

    messages: Lea's neutral format ({"role", "content": str | list of blocks}).
    tools: Lea tool schema dicts (name, description, input_schema); [] for none.
    model_kwargs: passthrough to litellm.completion (temperature, max_tokens, ...).
    streaming: True → stream tokens live; False → one blocking call. Both modes
        yield the same event types, so the agent loop is identical either way.
    """
    model_kwargs = dict(model_kwargs or {})
    portkey = is_portkey_model(model)
    anthropic = _is_anthropic_family(model)

    if portkey and anthropic:
        # The gateway's native Messages route (prompt caching survives it). If the
        # gateway predates the route, fall back to its chat-completions dialect —
        # but only when nothing has been yielded yet: once events are out, a
        # re-run would duplicate them, and any other error would just recur.
        native = _build_call(
            model, _litellm_model(model, portkey_native=True), system, messages, tools,
            _portkey_anthropic_kwargs(model, model_kwargs), cache_breakpoints=True,
        )
        started = False
        try:
            for event in _run(model, native, streaming):
                started = True
                yield event
            return
        except Exception as exc:  # noqa: BLE001 — classified below
            if started or not _portkey_route_missing(exc):
                raise
            print(
                f"[lea] Portkey gateway has no native Anthropic route ({exc}); falling "
                "back to its chat-completions dialect (prompt caching unavailable)",
                file=sys.stderr,
            )

    responses = _uses_openai_responses(model)
    if responses:
        model_kwargs = _responses_kwargs(tools, model_kwargs, default_effort="medium" if _is_openai_gpt_5_6(model) else None)
    if portkey:
        model_kwargs = _portkey_kwargs(model_kwargs)
    call = _build_call(
        model, _litellm_model(model), system, messages, tools, model_kwargs,
        include_reasoning=responses,
        # Through the gateway's chat-completions translation the breakpoints are
        # dropped from tool results (see _portkey_anthropic_kwargs) — not worth
        # the list-shaped system message it would cost.
        cache_breakpoints=anthropic and not portkey,
    )
    yield from _run(model, call, streaming)


def _build_call(model: str, litellm_model: str, system: str, messages: list, tools: list,
                model_kwargs: dict, *, include_reasoning: bool = False,
                cache_breakpoints: bool = False) -> dict:
    """The `litellm.completion` kwargs for one turn."""
    oai_messages = _to_openai_messages(system, messages, include_reasoning=include_reasoning)
    if cache_breakpoints:
        oai_messages = _with_cache_breakpoints(oai_messages)
    # Merge so an explicit model_kwargs api_key wins over the env-derived one,
    # instead of colliding (both supplying api_key raises "got multiple values").
    return dict(
        model=litellm_model,
        messages=oai_messages,
        tools=_to_openai_tools(tools) or None,
        **{**_api_key_kwargs(model), **model_kwargs},
    )


def _run(model: str, call: dict, streaming: bool):
    if streaming:
        yield from _stream_streaming(model, call)
    else:
        yield from _stream_blocking(model, call)


def _stream_streaming(model: str, call: dict):
    """Streaming path: parse chunk deltas into events as they arrive."""
    terminal = {}
    tool_calls_acc: dict[int, dict] = {}  # index -> {id, name, args_json}
    reasoning_items: dict[str, dict[str, Any]] = {}

    def flush_tool_calls():
        for idx in sorted(tool_calls_acc.keys()):
            tc = tool_calls_acc[idx]
            args = json.loads(tc["args_json"]) if tc["args_json"] else {}
            yield ToolCall(tc["name"], args)
            yield _ToolMeta(tc["id"])
        tool_calls_acc.clear()

    response = litellm.completion(stream=True, stream_options={"include_usage": True}, **call)
    for chunk in response:
        metadata = _response_metadata(chunk)
        for key in ("model", "service_tier", "_hidden_params", "usage_cost"):
            if metadata.get(key) is not None:
                terminal[key] = metadata[key]
        if getattr(chunk, "usage", None):
            terminal["usage"] = metadata["usage"]

        if not chunk.choices:
            continue

        choice = chunk.choices[0]
        delta = choice.delta

        _merge_reasoning_items(reasoning_items, getattr(delta, "reasoning_items", None))

        if getattr(delta, "content", None):
            yield TextDelta(delta.content)

        if getattr(delta, "tool_calls", None):
            for tc in delta.tool_calls:
                acc = tool_calls_acc.setdefault(tc.index, {"id": "", "name": "", "args_json": ""})
                if tc.id:
                    acc["id"] = tc.id
                if tc.function and tc.function.name:
                    acc["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    acc["args_json"] += tc.function.arguments

        if choice.finish_reason == "tool_calls":
            yield from flush_tool_calls()

    # Flush any tool calls a provider left without a "tool_calls" finish_reason.
    yield from flush_tool_calls()
    yield _terminal_done(model, terminal, list(reasoning_items.values()))


def _stream_blocking(model: str, call: dict):
    """Blocking path: one completion call, emitted as the same event types."""
    response = litellm.completion(**call)
    message = response.choices[0].message
    reasoning_items: dict[str, dict[str, Any]] = {}
    _merge_reasoning_items(reasoning_items, getattr(message, "reasoning_items", None))

    if getattr(message, "content", None):
        yield TextDelta(message.content)

    for tc in (getattr(message, "tool_calls", None) or []):
        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
        yield ToolCall(tc.function.name, args)
        yield _ToolMeta(tc.id)

    yield _terminal_done(model, _response_metadata(response), list(reasoning_items.values()))
