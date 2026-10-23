"""Statement-only completion retains sorry without weakening ordinary proof gates."""
import pytest

from lea import agent
from lea.check_evidence import digest, environment_hash
from lea.check_runtime import CheckText
from lea.config import LeaConfig
from lea.events import Finished
from lea.providers import TextDelta, ToolCall, Done, Usage


@pytest.mark.parametrize('stub', [True, False])
def test_checked_sorry_finishes_only_in_explicit_stub_mode(tmp_path, monkeypatch, stub):
    path = tmp_path/'Target.lean'
    code = 'theorem target : True := by\n  sorry\n'
    calls = []
    def provider(*args, **kwargs):
        calls.append(args[2])
        if len(calls) == 1:
            yield ToolCall('write_file', {'path': str(path), 'content': code})
            yield ToolCall('lean_check', {'path': str(path)})
        else:
            yield TextDelta('The requested statement stub compiles.')
        yield Done(Usage(2, 1), 0)
    def check(_):
        environment = {'configuration': {}, 'files': {}}
        execution = dict(evidence_version=1, source_identity=str(path.resolve()),
            content_sha256=digest(path.read_bytes()), environment=environment,
            environment_sha256=environment_hash(environment))
        return CheckText("warning: declaration uses 'sorry'", execution, path.read_text(), str(path))
    monkeypatch.setattr(agent, '_provider_stream', provider)
    monkeypatch.setattr(agent._tools, 'lean_check', check)
    config = LeaConfig(model='test', max_turns=3, mcp_servers={}, stub_generation=stub,
        prompt_variant='overleaf_stub' if stub else 'default')
    events = list(agent.run_events(config, [{'role':'user','content':'Create a checked sorry stub and stop.'}], working_dir=tmp_path))
    final = next(event for event in reversed(events) if isinstance(event, Finished))
    assert path.read_text() == code
    if stub:
        assert final.reason == 'stubbed' and final.result_kind is None
        assert len(calls) == 2
    else:
        assert final.reason in {'max_turns', 'stalled'}
        assert any('without errors or sorry' in str(call) for call in calls)
