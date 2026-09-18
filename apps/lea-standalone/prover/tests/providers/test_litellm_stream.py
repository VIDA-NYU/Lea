"""Unit test for the LiteLLM streaming wrapper (providers.stream).

Mocks litellm.completion (canned chunks) and litellm.cost_per_token so the
event mapping, tool-call assembly, and cost are verified without any network.

Run:  uv run python -m tests.providers.test_litellm_stream
Exits 0 if every check passes, 1 otherwise.
"""

import sys
import types

import lea.providers as providers
from lea.providers import TextDelta, ToolCall, Done, _ToolMeta, Usage

_FAILURES: list[str] = []
_CAPTURED: dict = {}


def check(name: str, cond: bool) -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}")
        _FAILURES.append(name)


def ns(**kw):
    return types.SimpleNamespace(**kw)


def _choice(content=None, tool_calls=None, reasoning_items=None, finish_reason=None):
    return ns(
        delta=ns(content=content, tool_calls=tool_calls, reasoning_items=reasoning_items),
        finish_reason=finish_reason,
    )


def _chunk(choices, usage=None):
    return ns(choices=choices, usage=usage)


def fake_completion(**kwargs):
    _CAPTURED.update(kwargs)
    return [
        _chunk([_choice(content="Hello ")]),
        _chunk([_choice(content="world")]),
        _chunk([_choice(tool_calls=[ns(index=0, id="call_1",
                                       function=ns(name="lean_check", arguments='{"path":'))])]),
        _chunk([_choice(tool_calls=[ns(index=0, id=None,
                                       function=ns(name=None, arguments=' "/x.lean"}'))])]),
        _chunk([_choice(finish_reason="tool_calls")]),
        _chunk([], usage=ns(prompt_tokens=100, completion_tokens=50)),
    ]


def fake_cost_per_token(model, prompt_tokens, completion_tokens, **cache):
    return (0.001, 0.002)


def fake_completion_blocking(**kwargs):
    _CAPTURED.update(kwargs)
    message = ns(
        content="Hello world",
        tool_calls=[ns(id="call_1", function=ns(name="lean_check", arguments='{"path": "/x.lean"}'))],
    )
    return ns(choices=[ns(message=message)], usage=ns(prompt_tokens=100, completion_tokens=50))


REASONING_ITEM = {
    "id": "rs_1",
    "type": "reasoning",
    "encrypted_content": "encrypted-turn-state",
    "summary": [],
}


def fake_gpt_5_6_completion(**kwargs):
    _CAPTURED.clear()
    _CAPTURED.update(kwargs)
    return [
        _chunk([_choice(
            reasoning_items=[REASONING_ITEM],
            tool_calls=[ns(
                index=0,
                id="call_56",
                function=ns(name="lean_check", arguments='{"path": "/x.lean"}'),
            )],
            finish_reason="tool_calls",
        )]),
        _chunk([], usage=ns(prompt_tokens=80, completion_tokens=20)),
    ]


TOOLS = [{
    "name": "lean_check",
    "description": "check a Lean file",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
}]
MESSAGES = [{"role": "user", "content": "prove it"}]


def test_blocking_mode():
    providers.litellm.completion = fake_completion_blocking
    providers.litellm.cost_per_token = fake_cost_per_token
    events = list(providers.stream("gemini/test-model", "SYS", MESSAGES, TOOLS, {"max_tokens": 100}, streaming=False))
    check("blocking: TextDelta whole content", events[0] == TextDelta("Hello world"))
    check("blocking: ToolCall assembled", events[1] == ToolCall("lean_check", {"path": "/x.lean"}))
    check("blocking: _ToolMeta id", events[2] == _ToolMeta("call_1"))
    check("blocking: Done usage", isinstance(events[-1], Done) and events[-1].usage == Usage(100, 50))
    check("blocking: Done cost", abs(events[-1].cost - 0.003) < 1e-9)


def test_gpt_5_6_responses_compatibility():
    providers.litellm.completion = fake_gpt_5_6_completion
    providers.litellm.cost_per_token = fake_cost_per_token
    caller_kwargs = {"max_tokens": 100, "include": []}
    events = list(providers.stream(
        "gpt-5.6-sol",
        "SYS",
        MESSAGES,
        TOOLS,
        caller_kwargs,
    ))

    check("gpt-5.6: explicit provider prefix", _CAPTURED.get("model") == "openai/gpt-5.6-sol")
    check("gpt-5.6: medium reasoning preserved", _CAPTURED.get("reasoning_effort") == "medium")
    extra_body = _CAPTURED.get("extra_body") or {}
    check("gpt-5.6: stateless Responses request", extra_body.get("store") is False)
    check(
        "gpt-5.6: encrypted reasoning requested",
        extra_body.get("include") == ["reasoning.encrypted_content"],
    )
    check("gpt-5.6: caller kwargs not mutated", caller_kwargs == {"max_tokens": 100, "include": []})
    check(
        "gpt-5.6: reasoning captured for replay",
        isinstance(events[-1], Done) and events[-1].reasoning_items == [REASONING_ITEM],
    )

    replay = [
        {"role": "user", "content": "prove it"},
        {"role": "assistant", "content": [
            {"type": "reasoning", "items": [REASONING_ITEM]},
            {"type": "tool_call", "name": "lean_check", "args": {"path": "/x.lean"}, "id": "call_56"},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_name": "lean_check", "content": "OK", "tool_call_id": "call_56"},
        ]},
    ]
    list(providers.stream("gpt-5.6-sol", "SYS", replay, TOOLS))
    sent = _CAPTURED["messages"]
    check("gpt-5.6: reasoning replayed on assistant turn", sent[2]["reasoning_items"] == [REASONING_ITEM])
    check("gpt-5.6: function call id preserved", sent[2]["tool_calls"][0]["id"] == "call_56")
    check("gpt-5.6: tool result call id preserved", sent[3]["tool_call_id"] == "call_56")

    providers.litellm.completion = fake_completion
    _CAPTURED.clear()
    list(providers.stream("gemini/test-model", "SYS", replay, TOOLS))
    legacy_sent = _CAPTURED["messages"]
    check("legacy: model routing unchanged", _CAPTURED.get("model") == "gemini/test-model")
    check("legacy: no GPT-5.6 request fields", not any(
        key in _CAPTURED for key in ("reasoning_effort", "store", "include", "extra_body")
    ))
    check("legacy: provider-specific reasoning omitted", "reasoning_items" not in legacy_sent[2])


def test_portkey_gateway_routing():
    """`portkey/<catalog-name>` rides LiteLLM's openai/ path, pointed at the gateway."""
    import os

    saved_env = {k: os.environ.get(k) for k in (
        "PORTKEY_API_KEY", "PORTKEY_BASE_URL", "PORTKEY_VIRTUAL_KEY", "PORTKEY_CONFIG", "PORTKEY_PROVIDER",
    )}
    priced: list[str] = []

    def fake_cost(model, prompt_tokens, completion_tokens, **cache):
        priced.append(model)
        if model == "gpt-4o":
            return (0.005, 0.025)
        raise Exception(f"no price for {model}")

    try:
        for k in saved_env:
            os.environ.pop(k, None)
        os.environ["PORTKEY_API_KEY"] = "pk-test"
        os.environ["PORTKEY_BASE_URL"] = "https://gateway.example/v1/"
        providers.litellm.completion = fake_completion
        providers.litellm.cost_per_token = fake_cost
        _CAPTURED.clear()
        caller_kwargs = {"max_tokens": 100, "extra_headers": {"x-portkey-trace-id": "t1"}}
        catalog = "@openai-jdoe/gpt-4o"
        events = list(providers.stream(f"portkey/{catalog}", "SYS", MESSAGES, TOOLS, caller_kwargs))

        check("portkey: openai-compatible route", _CAPTURED.get("model") == f"openai/{catalog}")
        check("portkey: gateway is api_base (trailing slash trimmed)",
              _CAPTURED.get("api_base") == "https://gateway.example/v1")
        check("portkey: key as bearer", _CAPTURED.get("api_key") == "pk-test")
        headers = _CAPTURED.get("extra_headers") or {}
        check("portkey: x-portkey-api-key header", headers.get("x-portkey-api-key") == "pk-test")
        check("portkey: strict OpenAI compliance requested",
              headers.get("x-portkey-strict-open-ai-compliance") == "true")
        check("portkey: caller headers preserved", headers.get("x-portkey-trace-id") == "t1")
        check("portkey: no routing header without env", "x-portkey-provider" not in headers)
        check("portkey: caller kwargs not mutated",
              caller_kwargs == {"max_tokens": 100, "extra_headers": {"x-portkey-trace-id": "t1"}})
        check("portkey: tool call still assembled", ToolCall("lean_check", {"path": "/x.lean"}) in events)
        check("portkey: cost from the bare upstream model",
              isinstance(events[-1], Done) and abs(events[-1].cost - 0.03) < 1e-9)
        check("portkey: price lookup by the bare catalog model", priced == ["gpt-4o"])
        check("portkey: plain system message on the chat-completions route",
              _CAPTURED["messages"][0] == {"role": "system", "content": "SYS"})

        # A bare catalog name (already in gateway syntax) is Portkey too, and the
        # hosted service is the default gateway.
        os.environ.pop("PORTKEY_BASE_URL", None)
        os.environ["PORTKEY_PROVIDER"] = "@openai-jdoe"
        _CAPTURED.clear()
        list(providers.stream(catalog, "SYS", MESSAGES, TOOLS))
        check("portkey: bare @slug/model recognised", _CAPTURED.get("model") == f"openai/{catalog}")
        check("portkey: hosted default base url", _CAPTURED.get("api_base") == providers.PORTKEY_DEFAULT_BASE_URL)
        check("portkey: optional routing header from env",
              (_CAPTURED.get("extra_headers") or {}).get("x-portkey-provider") == "@openai-jdoe")

        os.environ.pop("PORTKEY_API_KEY", None)
        try:
            list(providers.stream(f"portkey/{catalog}", "SYS", MESSAGES, TOOLS))
            check("portkey: missing key raises", False)
        except RuntimeError as e:
            check("portkey: missing key raises", "PORTKEY_API_KEY" in str(e))

        providers.litellm.completion = fake_completion
        _CAPTURED.clear()
        list(providers.stream("gemini/test-model", "SYS", MESSAGES, TOOLS))
        check("portkey: other providers untouched",
              _CAPTURED.get("model") == "gemini/test-model" and "api_base" not in _CAPTURED)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# A tool-using conversation: the final message is a tool result, the shape the
# moving cache breakpoint has to land on almost every turn of a run.
TOOL_CONVERSATION = [
    {"role": "user", "content": "prove it"},
    {"role": "assistant", "content": [
        {"type": "text", "text": "checking"},
        {"type": "tool_call", "name": "lean_check", "args": {"path": "/x.lean"}, "id": "call_9"},
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_name": "lean_check", "content": "OK", "tool_call_id": "call_9"},
    ]},
]
BP = {"type": "ephemeral"}


def fake_completion_cached(**kwargs):
    """A Claude reply whose usage carries Anthropic's cache counters (LiteLLM folds
    them into prompt_tokens, so 5350 = 100 uncached + 5000 read + 250 written)."""
    _CAPTURED.clear()
    _CAPTURED.update(kwargs)
    return [
        _chunk([_choice(content="done")]),
        _chunk([_choice(finish_reason="stop")]),
        _chunk([], usage=ns(prompt_tokens=5350, completion_tokens=42,
                            cache_read_input_tokens=5000, cache_creation_input_tokens=250)),
    ]


def test_anthropic_cache_breakpoints():
    """Claude models get two prompt-cache breakpoints: system prompt + last block."""
    providers.litellm.completion = fake_completion
    providers.litellm.cost_per_token = fake_cost_per_token

    _CAPTURED.clear()
    list(providers.stream("anthropic/claude-opus-4-8", "SYS", TOOL_CONVERSATION, TOOLS))
    sent = _CAPTURED["messages"]
    check("cache: system prompt is a block with a breakpoint",
          sent[0] == {"role": "system", "content": [{"type": "text", "text": "SYS", "cache_control": BP}]})
    check("cache: moving breakpoint on the final tool result",
          sent[-1]["role"] == "tool" and sent[-1].get("cache_control") == BP)
    check("cache: earlier messages unmarked",
          all("cache_control" not in m for m in sent[1:-1]) and sent[1] == {"role": "user", "content": "prove it"})
    check("cache: exactly two breakpoints",
          sum(1 for m in sent for blk in ([m] + (m["content"] if isinstance(m["content"], list) else []))
              if isinstance(blk, dict) and blk.get("cache_control") == BP) == 2)

    _CAPTURED.clear()
    list(providers.stream("claude-sonnet-4-6", "SYS", MESSAGES, TOOLS))
    sent = _CAPTURED["messages"]
    check("cache: bare claude name recognised; user text becomes a marked block",
          sent[-1] == {"role": "user", "content": [{"type": "text", "text": "prove it", "cache_control": BP}]})

    _CAPTURED.clear()
    list(providers.stream("gemini/test-model", "SYS", TOOL_CONVERSATION, TOOLS))
    sent = _CAPTURED["messages"]
    check("cache: other providers untouched",
          sent[0] == {"role": "system", "content": "SYS"} and "cache_control" not in sent[-1])
    check("cache: caller's conversation not mutated",
          "cache_control" not in TOOL_CONVERSATION[-1]["content"][-1])

    # Cache counters are read from usage and handed to the price lookup.
    priced: list = []

    def fake_cost(model, prompt_tokens, completion_tokens, **cache):
        priced.append((model, prompt_tokens, cache))
        return (0.004, 0.001)

    providers.litellm.completion = fake_completion_cached
    providers.litellm.cost_per_token = fake_cost
    events = list(providers.stream("anthropic/claude-opus-4-8", "SYS", MESSAGES, TOOLS))
    done = events[-1]
    check("cache: usage keeps the whole prompt as input_tokens", done.usage.input_tokens == 5350)
    check("cache: usage splits out read/write",
          (done.usage.cache_read_tokens, done.usage.cache_write_tokens) == (5000, 250))
    check("cache: counters reach the price lookup",
          priced == [("anthropic/claude-opus-4-8", 5350,
                      {"cache_read_input_tokens": 5000, "cache_creation_input_tokens": 250})])
    check("cache: Done cost", abs(done.cost - 0.005) < 1e-9)

    # A usage without counters (every other provider) stays a plain two-count call.
    providers.litellm.completion = fake_completion
    priced.clear()
    list(providers.stream("gemini/test-model", "SYS", MESSAGES, TOOLS))
    check("cache: no counters → plain price lookup", priced == [("gemini/test-model", 100, {})])


def test_portkey_native_anthropic_route():
    """A Claude behind Portkey takes the gateway's native Messages route via
    LiteLLM's anthropic/ provider, with breakpoints; a gateway without that route
    falls back to chat completions."""
    import os

    saved_env = {k: os.environ.get(k) for k in (
        "PORTKEY_API_KEY", "PORTKEY_BASE_URL", "PORTKEY_VIRTUAL_KEY", "PORTKEY_CONFIG", "PORTKEY_PROVIDER",
    )}
    catalog = "@vertexai-jdoe/anthropic.claude-opus-4-8"
    priced: list = []

    def fake_cost(model, prompt_tokens, completion_tokens, **cache):
        priced.append(model)
        if model == "claude-opus-4-8":
            return (0.004, 0.001)
        raise Exception(f"no price for {model}")

    try:
        for k in saved_env:
            os.environ.pop(k, None)
        os.environ["PORTKEY_API_KEY"] = "pk-test"
        os.environ["PORTKEY_BASE_URL"] = "https://gateway.example/v1/"
        providers.litellm.completion = fake_completion_cached
        providers.litellm.cost_per_token = fake_cost
        caller_kwargs = {"max_tokens": 100, "extra_headers": {"x-portkey-trace-id": "t1"}}
        events = list(providers.stream(f"portkey/{catalog}", "SYS", TOOL_CONVERSATION, TOOLS, caller_kwargs))

        check("native: anthropic/ route with the bare model",
              _CAPTURED.get("model") == "anthropic/anthropic.claude-opus-4-8")
        check("native: gateway ROOT as api_base (LiteLLM appends /v1/messages)",
              _CAPTURED.get("api_base") == "https://gateway.example")
        check("native: key as api_key", _CAPTURED.get("api_key") == "pk-test")
        headers = _CAPTURED.get("extra_headers") or {}
        check("native: x-portkey-api-key header", headers.get("x-portkey-api-key") == "pk-test")
        check("native: provider slug from the catalog name",
              headers.get("x-portkey-provider") == "@vertexai-jdoe")
        check("native: no strict-compliance header", "x-portkey-strict-open-ai-compliance" not in headers)
        check("native: caller headers preserved", headers.get("x-portkey-trace-id") == "t1")
        sent = _CAPTURED["messages"]
        check("native: system breakpoint",
              sent[0]["content"] == [{"type": "text", "text": "SYS", "cache_control": BP}])
        check("native: moving breakpoint on the tool result", sent[-1].get("cache_control") == BP)
        check("native: caller kwargs not mutated",
              caller_kwargs == {"max_tokens": 100, "extra_headers": {"x-portkey-trace-id": "t1"}})
        done = events[-1]
        check("native: cache usage parsed",
              (done.usage.input_tokens, done.usage.cache_read_tokens, done.usage.cache_write_tokens)
              == (5350, 5000, 250))
        check("native: priced from the bare upstream model, cache-aware",
              priced == ["anthropic.claude-opus-4-8", "claude-opus-4-8"] and abs(done.cost - 0.005) < 1e-9)

        # PORTKEY_PROVIDER in the environment overrides the slug from the name.
        os.environ["PORTKEY_PROVIDER"] = "@other"
        list(providers.stream(f"portkey/{catalog}", "SYS", MESSAGES, TOOLS))
        check("native: env routing header wins over the catalog slug",
              (_CAPTURED.get("extra_headers") or {}).get("x-portkey-provider") == "@other")
        os.environ.pop("PORTKEY_PROVIDER")

        # Fallback: a gateway with no native route (404) → chat-completions dialect.
        calls: list = []

        class RouteMissing(Exception):
            status_code = 404

        def flaky_completion(**kwargs):
            calls.append(kwargs)
            if kwargs["model"].startswith("anthropic/"):
                raise RouteMissing("no route")
            return fake_completion(**kwargs)

        providers.litellm.completion = flaky_completion
        providers.litellm.cost_per_token = fake_cost_per_token
        _CAPTURED.clear()
        events = list(providers.stream(f"portkey/{catalog}", "SYS", MESSAGES, TOOLS))
        check("fallback: native tried first, then chat completions",
              [c["model"] for c in calls] == ["anthropic/anthropic.claude-opus-4-8", f"openai/{catalog}"])
        check("fallback: chat-completions route at the /v1 base",
              calls[-1]["api_base"] == "https://gateway.example/v1"
              and calls[-1]["extra_headers"].get("x-portkey-strict-open-ai-compliance") == "true")
        check("fallback: no breakpoints on the translated route",
              calls[-1]["messages"][0] == {"role": "system", "content": "SYS"})
        check("fallback: events still flow", ToolCall("lean_check", {"path": "/x.lean"}) in events)

        # Any other gateway error propagates — it would only recur on the fallback.
        class Forbidden(Exception):
            status_code = 403

        def forbidden_completion(**kwargs):
            raise Forbidden("bad key")

        providers.litellm.completion = forbidden_completion
        try:
            list(providers.stream(f"portkey/{catalog}", "SYS", MESSAGES, TOOLS))
            check("fallback: other errors propagate", False)
        except Forbidden:
            check("fallback: other errors propagate", True)

        # Once events have been yielded, a route-missing error mid-stream propagates
        # too: a re-run would duplicate them.
        def half_then_404(**kwargs):
            yield _chunk([_choice(content="partial")])
            raise RouteMissing("dropped")

        providers.litellm.completion = half_then_404
        try:
            list(providers.stream(f"portkey/{catalog}", "SYS", MESSAGES, TOOLS))
            check("fallback: never after events were yielded", False)
        except RouteMissing:
            check("fallback: never after events were yielded", True)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main():
    print("providers (LiteLLM stream) tests:")
    providers.litellm.completion = fake_completion
    providers.litellm.cost_per_token = fake_cost_per_token

    tools = TOOLS
    messages = MESSAGES
    events = list(providers.stream("gemini/test-model", "SYS", messages, tools, {"max_tokens": 100}))

    # Event sequence
    check("event[0] TextDelta 'Hello '", events[0] == TextDelta("Hello "))
    check("event[1] TextDelta 'world'", events[1] == TextDelta("world"))
    check("event[2] ToolCall assembled args", events[2] == ToolCall("lean_check", {"path": "/x.lean"}))
    check("event[3] _ToolMeta id", events[3] == _ToolMeta("call_1"))
    check("last event is Done", isinstance(events[-1], Done))
    check("no duplicate tool calls", sum(isinstance(e, ToolCall) for e in events) == 1)

    done = events[-1]
    check("Done.usage", done.usage == Usage(100, 50))
    check("Done.cost == 0.003", abs(done.cost - 0.003) < 1e-9)

    # Converters fed LiteLLM the right thing
    check("model passed through", _CAPTURED.get("model") == "gemini/test-model")
    check("stream=True", _CAPTURED.get("stream") is True)
    check("max_tokens from model_kwargs", _CAPTURED.get("max_tokens") == 100)
    msgs = _CAPTURED.get("messages", [])
    check("system message first", msgs and msgs[0] == {"role": "system", "content": "SYS"})
    sent_tools = _CAPTURED.get("tools") or []
    check("openai function-tool shape",
          bool(sent_tools) and sent_tools[0]["type"] == "function"
          and sent_tools[0]["function"]["name"] == "lean_check")

    test_blocking_mode()
    test_gpt_5_6_responses_compatibility()
    test_portkey_gateway_routing()
    test_anthropic_cache_breakpoints()
    test_portkey_native_anthropic_route()

    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        sys.exit(1)
    print("All providers tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
