from types import SimpleNamespace as NS

import pytest
from lea import providers


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "openai/gpt-6-astra", "gpt-9-future", "responses/gpt-future"])
@pytest.mark.parametrize("streaming", [True, False])
def test_responses_tool_calls_preserve_reasoning_and_replay(model, streaming, monkeypatch):
    captured = []
    reasoning = {"id": "reason-1", "type": "reasoning", "encrypted_content": "encrypted", "summary": []}
    tool = NS(id="call-1", index=0, function=NS(name="lean_check", arguments='{"path":"Proof.lean"}'))
    def completion(**kwargs):
        captured.append(kwargs)
        message = NS(content=None, tool_calls=[tool], reasoning_items=[reasoning])
        response = NS(choices=[NS(message=message, delta=message, finish_reason="tool_calls")], usage=NS(prompt_tokens=10, completion_tokens=5))
        return [response] if kwargs.get("stream") else response
    monkeypatch.setattr(providers.litellm, "completion", completion)
    monkeypatch.setattr(providers.litellm, "completion_cost", lambda **kw: 0)
    tools = [{"name": "lean_check", "description": "check", "input_schema": {"type": "object"}}]
    events = list(providers.stream(model, "system", [{"role": "user", "content": "prove"}], tools, streaming=streaming))
    assert captured[0]["model"].startswith("openai/responses/")
    assert captured[0]["extra_body"]["include"] == ["reasoning.encrypted_content"]
    assert captured[0]["extra_body"]["store"] is False
    assert any(isinstance(e, providers.ToolCall) and e.args == {"path": "Proof.lean"} for e in events)
    assert events[-1].reasoning_items == [reasoning]
    messages = [{"role": "assistant", "content": [
        {"type": "reasoning", "items": events[-1].reasoning_items},
        {"type": "tool_call", "name": "lean_check", "args": {}, "id": "call-1"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_call_id": "call-1", "content": "ok"}]}]
    list(providers.stream(model, "system", messages, tools, streaming=streaming))
    assert captured[-1]["messages"][1]["reasoning_items"] == [reasoning]
    assert captured[-1]["messages"][2]["tool_call_id"] == "call-1"
    # Exercise LiteLLM's actual bridge selection, including unknown future IDs.
    from litellm.main import responses_api_bridge_check
    spec, _ = responses_api_bridge_check(captured[0]["model"].removeprefix("openai/"), "openai")
    assert spec["mode"] == "responses"


def test_gateway_and_legacy_routes_remain_distinct():
    assert not providers._uses_openai_responses("portkey/@provider/gpt-6.1-sol")
    assert not providers._uses_openai_responses("openrouter/openai/gpt-6.1-sol")
    assert providers._litellm_model("claude-future") == "anthropic/claude-future"
    assert providers._litellm_model("gpt-4o") == "openai/gpt-4o"


def test_installed_litellm_transforms_future_model_request_without_network(monkeypatch):
    """Exercise the real completion bridge up to its Responses transport boundary."""
    captured = []
    def responses(**kwargs):
        captured.append(kwargs)
        raise RuntimeError("catalog-test-transport-boundary")
    monkeypatch.setattr(providers.litellm, "responses", responses)
    with pytest.raises(Exception, match="catalog-test-transport-boundary"):
        list(providers.stream("gpt-9-future", "system", [{"role": "user", "content": "prove"}],
            [{"name": "lean_check", "description": "check", "input_schema": {"type": "object"}}],
            {"api_key": "test-placeholder", "num_retries": 0}, streaming=False))
    assert captured
    request = captured[0]
    assert request["model"] == "gpt-9-future"
    assert request["tools"][0]["name"] == "lean_check"
    assert request["store"] is False
    assert "reasoning.encrypted_content" in request["include"]
