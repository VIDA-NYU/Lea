"""Timing setup/teardown must not change leases, errors or trust decisions."""
from contextlib import contextmanager
import hashlib

import pytest

from lea import check_runtime, result_selection, tools
from lea.check_runtime import CheckBudget, CheckText
from lea.config import LeaConfig


def test_context_timings_exclude_the_leased_body_and_preserve_exception(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(check_runtime.time, 'monotonic', lambda:clock[0])
    budget = CheckBudget(timeout=1000)
    @contextmanager
    def lease():
        clock[0] += 2
        try:
            yield 'owned'
        finally:
            clock[0] += 3
    with pytest.raises(ValueError, match='failure'):
        with budget.context('preparation', lease()) as value:
            assert value == 'owned'
            clock[0] += 100
            raise ValueError('failure')
    timing = budget.finish()['timings_ms']
    assert timing['preparation'] == 2000
    assert timing['cleanup'] == 3000
    assert timing['total'] == 105000


@pytest.mark.parametrize('reject', [False, True])
def test_selected_result_and_inspection_timing_are_part_of_durable_execution(tmp_path, monkeypatch, reject):
    path = tmp_path / 'Target.lean'
    code = 'theorem target : True := by trivial\n'
    path.write_text(code)
    digest = hashlib.sha256(code.encode()).hexdigest()
    execution = {'content_sha256':digest,'timings_ms':{'total':1}}
    monkeypatch.setattr(tools, 'lean_check', lambda *_:CheckText('OK',execution,code,str(path)))
    monkeypatch.setattr(tools, 'current_config', lambda:LeaConfig(model='test', max_turns=1,
        selected_result={'declaration':'target','kind':'theorem'}))
    def inspect(*args):
        if reject:
            raise ValueError('Unsupported axiom sorryAx')
        return {'user_name':'target','full_name':'target','content_sha256':digest,'axioms':[],'kind':'theorem'}
    monkeypatch.setattr(result_selection, 'inspect_selected', inspect)
    result = tools._generated_lean_check(str(path))
    assert 'result_inspection' in result.execution['timings_ms']
    assert ('Error:' in result) == reject
    assert (result.execution['selected_result'] is None) == reject
    assert check_runtime.serialize_result(result)['execution'] == result.execution


def test_slow_sql_stop_poll_does_not_repeat_for_each_dependency_file(monkeypatch):
    from pathlib import Path
    from lea import check_evidence as evidence
    clock=[0.0]
    calls=[]
    monkeypatch.setattr(check_runtime.time,'monotonic',lambda:clock[0])
    def stop():
        calls.append(1)
        clock[0]+=.075  # A contended metadata read exceeds the 50 ms poll interval.
        return False
    def observe(path):
        clock[0]+=.001
        return 'hash',[1,1,1,1,1]
    monkeypatch.setattr(evidence,'file_observation',observe)
    monkeypatch.setattr(evidence,'_resolve',lambda *a:[(Path('/toolchain/src/lean/Init.lean'),Path('/toolchain/lib/lean/Init.olean'))])
    monkeypatch.setattr(evidence,'_artifact_family',lambda path,dirs:[str(path)+'.'+str(i) for i in range(1000)])
    budget=CheckBudget(timeout=10,should_stop=stop)
    result=evidence.manifest(Path('/workspace/Main.lean'),'example : True := trivial',None,budget)
    assert len(result['files'])>=1000
    assert len(calls)<40 and clock[0]<5  # Previously every sidecar polled and the budget expired.
    assert budget.execution['cancellation_polls']==len(calls)
    assert budget.execution['timings_ms']['cancellation']>0
