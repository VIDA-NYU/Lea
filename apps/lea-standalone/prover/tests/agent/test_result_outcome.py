"""The selected checked result finishes without a correspondence classifier."""
import pytest
from lea import agent
from lea.check_evidence import digest, environment_hash
from lea.check_runtime import CheckText
from lea.config import LeaConfig
from lea.events import Finished
from lea.providers import TextDelta, ToolCall, Done, Usage


@pytest.mark.parametrize('outcome', ['proved', 'defined', 'disproved'])
def test_checked_selection_finishes_without_review(tmp_path, monkeypatch, outcome):
    path = tmp_path / 'Result.lean'
    code = 'def result : Nat := 0\n' if outcome == 'defined' else 'theorem result : True := by trivial\n'
    calls = []

    def provider(model, system, messages, *args, **kwargs):
        assert 'classify the mathematical outcome' not in system
        calls.append(system)
        if len(calls) == 1:
            yield ToolCall('write_file', {'path': str(path), 'content': code})
            yield ToolCall('lean_check', {'path': str(path)})
        else:
            yield TextDelta('Finished. See Lea Status for correspondence findings.')
        yield Done(Usage(2, 1), 0)

    def checked(_):
        environment = {'configuration': {}, 'files': {}}
        execution = dict(evidence_version=1, source_identity=str(path.resolve()),
            content_sha256=digest(path.read_bytes()), environment=environment,
            environment_sha256=environment_hash(environment))
        result = CheckText('OK — no errors, no warnings.', execution, path.read_text(), str(path))
        result.selected_result = {'user_name': 'result', 'content_sha256': execution['content_sha256']}
        return result

    monkeypatch.setattr(agent, '_provider_stream', provider)
    monkeypatch.setattr(agent._tools, 'lean_check', checked)
    monkeypatch.setattr(agent, 'load_system_prompt', lambda *args, **kwargs: 'Test')
    cfg = LeaConfig(model='test', max_turns=3, mcp_servers={}, narrate_tool_steps=False,
        selected_result={'path': str(path), 'declaration': 'result', 'kind': outcome})
    events = list(agent.run_events(cfg, [{'role': 'user', 'content': 'Formalize the target.'}], working_dir=tmp_path))
    final = next(event for event in reversed(events) if isinstance(event, Finished))
    assert final.reason == 'completed'
    assert final.result_kind == outcome
    assert final.result_detail is None
    assert len(calls) == 2
