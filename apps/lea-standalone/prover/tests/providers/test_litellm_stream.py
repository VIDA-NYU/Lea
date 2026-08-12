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


def fake_cost_per_token(model, prompt_tokens, completion_tokens):
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

    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        sys.exit(1)
    print("All providers tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
