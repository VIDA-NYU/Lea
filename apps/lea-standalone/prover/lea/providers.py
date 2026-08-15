"""Provider layer — a thin streaming wrapper over LiteLLM.

One `stream()` drives every provider through `litellm.completion`, yielding a
unified event stream (`TextDelta | ToolCall | _ToolMeta | Done`). Messages use
Lea's neutral format and are converted to OpenAI shape here; LiteLLM translates
from there to whatever provider the model name selects (`gemini/…`,
`anthropic/…`, `openai/…`, `openrouter/…`, …). Cost comes from LiteLLM.
"""

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import litellm


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


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


_WARNED_MODELS: set[str] = set()


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
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        out.append({
                            "role": "tool",
                            "tool_call_id": item.get("tool_call_id") or item.get("tool_use_id"),
                            "content": item["content"],
                        })
        elif msg["role"] == "assistant":
            oai = {"role": "assistant", "content": None}
            text_parts, tool_calls, reasoning_items = [], [], []
            content = msg["content"]
            if isinstance(content, str):
                # Transcripts persisted before assistant turns were normalized to
                # parts (e.g. a max-turns summary) may still carry a bare string.
                # Replay it as a single text part instead of iterating its characters.
                content = [{"type": "text", "text": content}]
            elif not isinstance(content, list):
                content = []
            for item in content:
                if not isinstance(item, dict):
                    continue
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


def _litellm_model(model: str) -> str:
    """Make GPT-5.6 provider resolution independent of LiteLLM's remote model map."""
    if _is_openai_gpt_5_6(model) and "/" not in model:
        return f"openai/{model}"
    return model


def _gpt_5_6_kwargs(tools: list, model_kwargs: dict) -> dict:
    """Return the smallest Responses-compatible request policy for GPT-5.6.

    Lea's flagship formalization route historically used GPT-5.5's effective
    ``medium`` reasoning.  Making that explicit both preserves the behavior and
    tells LiteLLM to bridge a tool-bearing GPT-5.6 call to Responses.  Because Lea
    owns and persists its transcript (rather than a ``previous_response_id``), the
    encrypted reasoning item is requested for exact manual replay.
    """
    out = dict(model_kwargs)
    effort = out.setdefault("reasoning_effort", "medium")
    if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
        raise ValueError(
            "reasoning_effort for GPT-5.6 must be one of "
            "none, low, medium, high, xhigh, or max"
        )
    if tools:
        # LiteLLM's completion() signature is Chat-shaped.  Responses-only
        # parameters passed at the top level are filtered before its bridge in
        # 1.88.x; extra_body is the documented bridge escape hatch and the
        # transformation promotes these supported keys into the Responses request.
        raw_extra_body = out.get("extra_body") or {}
        if not isinstance(raw_extra_body, dict):
            raise ValueError("extra_body must be an object when using GPT-5.6 tools")
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
            raise ValueError("include must be a list when using GPT-5.6 tools")
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


def _compute_cost(model: str, usage: Usage) -> float:
    """Cost via LiteLLM; falls back to 0.0 (with a one-time warning) for unmapped models."""
    try:
        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model,
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
        )
        return (prompt_cost or 0.0) + (completion_cost or 0.0)
    except Exception as e:
        if model not in _WARNED_MODELS:
            _WARNED_MODELS.add(model)
            print(f"[lea] cost unavailable for model '{model}' ({e}); reporting $0.00", file=sys.stderr)
        return 0.0


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
    is_gpt_5_6 = _is_openai_gpt_5_6(model)
    if is_gpt_5_6:
        model_kwargs = _gpt_5_6_kwargs(tools, model_kwargs)
    # Merge so an explicit model_kwargs api_key wins over the env-derived one,
    # instead of colliding (both supplying api_key raises "got multiple values").
    call = dict(
        model=_litellm_model(model),
        messages=_to_openai_messages(system, messages, include_reasoning=is_gpt_5_6),
        tools=_to_openai_tools(tools) or None,
        **{**_api_key_kwargs(model), **model_kwargs},
    )
    if streaming:
        yield from _stream_streaming(model, call)
    else:
        yield from _stream_blocking(model, call)


def _stream_streaming(model: str, call: dict):
    """Streaming path: parse chunk deltas into events as they arrive."""
    usage = Usage()
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
        if getattr(chunk, "usage", None):
            usage.input_tokens = chunk.usage.prompt_tokens or 0
            usage.output_tokens = chunk.usage.completion_tokens or 0

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
    yield Done(usage, _compute_cost(model, usage), list(reasoning_items.values()))


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

    u = getattr(response, "usage", None)
    usage = Usage(getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0) if u else Usage()
    yield Done(usage, _compute_cost(model, usage), list(reasoning_items.values()))
