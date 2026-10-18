"""Import advice is bounded, read-only, and cannot replace proof evidence."""
from pathlib import Path
import pytest
from lea import tools
from lea.check_runtime import CheckText


@pytest.fixture
def proof(tmp_path):
    (tmp_path/'lakefile.toml').write_text('name="advice"\n')
    path=tmp_path/'Proof.lean'
    path.write_text('import Mathlib.Data.Real.Basic\ntheorem target (x : ℝ) : x = x := rfl\n')
    return path


def test_advice_timeout_keeps_proof_and_cannot_emit_proof_evidence(proof, monkeypatch):
    original=proof.read_bytes()
    observed={}
    def check(path, *, budget, allow_cold, use_lsp):
        observed.update(path=Path(path), budget=budget, allow_cold=allow_cold, use_lsp=use_lsp)
        source=Path(path).read_text()
        assert 'import Mathlib\n' not in source
        assert 'import Mathlib.Tactic.Linter.MinImports\n' in source
        return CheckText('Error: deadline exceeded', {'failure':{'kind':'timeout'}})
    monkeypatch.setattr(tools,'lean_check',check)
    result=tools.suggest_imports(str(proof))
    assert result.startswith('Import analysis skipped:') and 'without retrying' in result
    assert not isinstance(result,CheckText)  # Scratch compilation is not target proof evidence.
    assert observed['budget'].deadline-observed['budget'].started == 10
    assert observed['allow_cold'] is False
    assert observed['use_lsp'] is False
    assert proof.read_bytes()==original and not observed['path'].exists()


def test_advice_never_rechecks_a_broad_umbrella(proof,monkeypatch):
    proof.write_text('import Mathlib\nexample : True := trivial\n')
    monkeypatch.setattr(tools,'lean_check',lambda *a,**k:pytest.fail('Broad analysis compiled'))
    assert tools.suggest_imports(str(proof)).startswith('Import analysis skipped:')


def test_advice_removes_its_scaffolding_and_rejects_changed_source(proof,monkeypatch):
    monkeypatch.setattr(tools,'lean_check',lambda *a,**k:'warning: unneeded import \'Mathlib.Tactic.Linter.MinImports\'\n')
    result=tools.suggest_imports(str(proof))
    assert 'import Mathlib.Data.Real.Basic' in result and 'Linter.MinImports' not in result
    def racing(*a,**k):
        proof.write_text('example : False := by sorry\n')
        return 'OK'
    monkeypatch.setattr(tools,'lean_check',racing)
    assert 'proof changed' in tools.suggest_imports(str(proof))


def test_agent_completes_after_advice_timeout_and_remembers_attempt_on_resume(tmp_path,monkeypatch):
    from dataclasses import replace
    from lea import agent,registry
    from lea.config import LeaConfig
    from lea.events import Finished,ToolResulted
    from lea.providers import ToolCall,TextDelta,Done,Usage
    from lea.check_evidence import digest,environment_hash
    path=tmp_path/'Target.lean'
    source='theorem target : True := by trivial\n'
    turns=[0];advice=[];records=[]
    def stream(*a,**k):
        turns[0]+=1
        if turns[0]==1:
            yield ToolCall('write_file',{'path':str(path),'content':source})
            yield ToolCall('lean_check',{'path':str(path)})
            yield ToolCall('suggest_imports',{'path':str(path)})
            yield ToolCall('suggest_imports',{'path':str(path)})
        else:yield TextDelta('The theorem is proved.')
        yield Done(Usage(1,1),0)
    def checked(args):
        environment={'configuration':{},'files':{}}
        execution={'evidence_version':1,'source_identity':str(path.resolve()),'content_sha256':digest(path.read_bytes()),
            'environment':environment,'environment_sha256':environment_hash(environment)}
        return CheckText('OK',execution,path.read_text())
    def analyze(args):
        advice.append(args)
        return 'Import analysis skipped: budget exhausted. Complete using the passing check.'
    monkeypatch.setattr(agent,'stream',stream)
    monkeypatch.setattr(agent,'load_system_prompt',lambda *a,**k:'Test')
    monkeypatch.setattr(agent,'_classify_final_result',lambda *a,**k:('proved',None,Usage(),0))
    monkeypatch.setitem(registry.REGISTRY,'lean_check',replace(registry.REGISTRY['lean_check'],handler=checked))
    monkeypatch.setitem(registry.REGISTRY,'suggest_imports',replace(registry.REGISTRY['suggest_imports'],handler=analyze))
    cfg=LeaConfig(model='offline',max_turns=4,mcp_servers={},narrate_tool_steps=False)
    events=list(agent.run_events(cfg,[{'role':'user','content':'Prove True.'}],working_dir=str(tmp_path),
        checkpoint_callback=lambda kind,value:records.append((kind,value))))
    finished=next(e for e in events if isinstance(e,Finished))
    assert finished.reason=='completed' and finished.result_kind=='proved'
    assert len(advice)==1
    checkpoint=next(value for kind,value in records if kind=='checkpoint' and value['stage']=='turn_boundary'
        and value.get('import_advice_attempted') and not value.get('final_result'))
    resumed=[0]
    def resumed_stream(*a,**k):
        resumed[0]+=1
        if resumed[0]==1:yield ToolCall('suggest_imports',{'path':str(path)})
        else:yield TextDelta('The theorem remains proved.')
        yield Done(Usage(1,1),0)
    monkeypatch.setattr(agent,'stream',resumed_stream)
    events=list(agent.run_events(cfg,[],working_dir=str(tmp_path),resume_state=checkpoint))
    assert len(advice)==1
    assert any('already attempted' in e.content for e in events if isinstance(e,ToolResulted))
    assert next(e for e in events if isinstance(e,Finished)).reason=='completed'
