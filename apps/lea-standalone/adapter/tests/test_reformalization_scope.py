from copy import deepcopy

import pytest

from app.reformalization_scope import PreservationConflict, validate_file
from lea.agent import ProofVerificationState
from lea.check_runtime import CheckText, serialize_result, restore_result
from lea.result_selection import select


def decl(name, line, axioms=(), dependencies=(), typ="True"):
    return dict(user_name=name, full_name=name, kind="theorem", type_expr=typ,
                body_expr=None, visibility="public", axioms=list(axioms), dependencies=list(dependencies),
                type=typ, range=dict(start_line=line, start_column=0, end_line=line, end_column=100))


def report(code, items):
    for d in items:
        d["range"]["end_column"] = len(code.splitlines()[d["range"]["end_line"] - 1])
    return dict(declarations=items, content_sha256="hash", complete=True)


def test_complete_target_beside_unrelated_sorry_and_preserve_source():
    old = "theorem a : True := by trivial\ntheorem b : True := by sorry\ntheorem c : True := by sorry\n"
    new = old.replace("theorem b : True := by sorry", "theorem b : True := by trivial")
    baseline = report(old, [decl("a", 1), decl("b", 2, ["sorryAx"]), decl("c", 3, ["sorryAx"])])
    candidate = report(new, [decl("a", 1), decl("b", 2), decl("c", 3, ["sorryAx"])])
    assert select(candidate, "b")["signature"] == "b : True"
    assert validate_file(old, new, baseline, candidate, "b")["protected"] == ["a", "c"]
    with pytest.raises(ValueError, match="sorryAx"):
        select(candidate, "c")
    damaged = new.replace("by trivial", "by exact True.intro", 1)
    with pytest.raises(PreservationConflict, match="Protected proof"):
        validate_file(old, damaged, baseline, report(damaged, deepcopy(candidate["declarations"])), "b")


def test_helper_completion_requires_fixed_contract_and_source_update_is_explicit():
    old = "theorem h : True := by sorry\ntheorem b : True := by have := h; sorry\n"
    new = "theorem h : True := by trivial\ntheorem b : True := by exact h\n"
    before = report(old, [decl("h", 1, ["sorryAx"]), decl("b", 2, ["sorryAx"], ["h"])])
    after = report(new, [decl("h", 1), decl("b", 2, (), ["h"])])
    assert "h" in validate_file(old, new, before, after, "b")["editable"]
    after["declarations"][1]["type_expr"] = "False"
    with pytest.raises(PreservationConflict, match="interface"):
        validate_file(old, new, before, after, "b")


def test_prover_state_uses_selected_evidence_and_checkpoint_roundtrips():
    from pathlib import Path
    state = ProofVerificationState({"declaration": "b"})
    result = CheckText("warning: declaration c uses 'sorry'", {"content_sha256": "same"}, "source")
    result.selected_result = {"user_name": "b", "content_sha256": "same", "signature": "b : True"}
    restored = restore_result(serialize_result(result))
    state.note_tool_result("lean_check", {"path": "/tmp/b.lean"}, restored)
    assert state.latest_check_passed
    assert state.selected_evidence["user_name"] == "b"
    old_checkpoint = ProofVerificationState({"declaration": "b"})
    old_checkpoint.latest_check_content = "source"
    assert old_checkpoint.needs_final_check() is False  # no selected file yet
    old_checkpoint.latest_proof_path = "/tmp/b.lean"
    assert old_checkpoint.needs_final_check()


def test_selected_result_rejects_ambiguous_name_and_transitive_sorry():
    metadata = dict(declarations=[decl("A.b", 1), decl("B.b", 2)], content_sha256="x")
    with pytest.raises(ValueError, match="ambiguous"):
        select(metadata, "b")
    metadata["declarations"][0]["axioms"] = ["sorryAx"]
    with pytest.raises(ValueError, match="sorryAx"):
        select(metadata, "A.b")


def test_using_an_unrelated_stub_does_not_grant_permission_to_edit_it():
    from app.reformalization_scope import validate_project
    old = 'theorem c : True := by sorry\ntheorem b : True := by sorry\n'
    new = 'theorem c : True := by trivial\ntheorem b : True := by exact c\n'
    before = report(old, [decl('c', 1, ['sorryAx']), decl('b', 2, ['sorryAx'])])
    after = report(new, [decl('c', 1), decl('b', 2, dependencies=['c'])])
    with pytest.raises(PreservationConflict, match='Protected source'):
        validate_project({'Shared.lean': old}, {'Shared.lean': new}, {'Shared.lean': before},
            {'Shared.lean': after}, 'b', after['declarations'][1])
    receipt = validate_project({'Shared.lean': old}, {'Shared.lean': new}, {'Shared.lean': before},
        {'Shared.lean': after}, 'b', after['declarations'][1], pinned_helpers=['c'])
    assert 'c' in receipt['editable']


def test_capability_requires_executor_and_honors_admission_rollback(monkeypatch):
    from app import reformalizations as ops
    from lea import execution
    monkeypatch.delenv('LEA_REFORMALIZATION_ENABLED', raising=False)
    monkeypatch.setattr(execution, 'capability', lambda: {'available': True})
    assert ops.capability()['automatic_shared_files']
    monkeypatch.setenv('LEA_REFORMALIZATION_ENABLED', '0')
    assert not ops.capability()['admission_enabled']
    monkeypatch.setenv('LEA_REFORMALIZATION_ENABLED', '1')
    monkeypatch.setattr(execution, 'capability', lambda: {'available': False})
    assert not ops.capability()['automatic_shared_files']


def generated_constant_fixture():
    energy = 'def energy (x : Nat) := x + 1\n'
    target = 'theorem target : True := by trivial\n'
    aux = {**decl('energy._proof_1', 1), 'range': None}
    definition = {**decl('energy', 1), 'kind': 'definition', 'body_expr': 'x + 1'}
    definition_report = report(energy, [definition])
    definition_report['declarations'].append(aux)
    sources = {'Energy.lean': energy, 'Target.lean': target}
    reports = {'Energy.lean': definition_report, 'Target.lean': report(target, [decl('target', 1)])}
    return sources, reports


def test_project_retains_rangeless_compiler_constants_in_unchanged_modules():
    from app.reformalization_scope import validate_project
    before, baseline = generated_constant_fixture()
    after, candidate = deepcopy(before), deepcopy(baseline)
    after['Target.lean'] = 'theorem target : True := by exact True.intro\n'
    candidate['Target.lean'] = report(after['Target.lean'], [decl('target', 1)])
    result = validate_project(before, after, baseline, candidate, 'target', candidate['Target.lean']['declarations'][0])
    assert result['protected'] == ['energy', 'energy._proof_1']
    assert result['editable'] == ['target']


@pytest.mark.parametrize('change', ['source', 'relocation', 'type', 'axioms', 'removed'])
def test_rangeless_constants_never_grant_source_or_semantic_edit_permission(change):
    from app.reformalization_scope import validate_project
    before, baseline = generated_constant_fixture()
    after, candidate = deepcopy(before), deepcopy(baseline)
    if change == 'source':
        after['Energy.lean'] += '-- changed context\n'
    elif change == 'relocation':
        after['Moved.lean'] = after.pop('Energy.lean')
        candidate['Moved.lean'] = candidate.pop('Energy.lean')
    elif change == 'type':
        candidate['Energy.lean']['declarations'][1]['type_expr'] = 'False'
    elif change == 'axioms':
        candidate['Energy.lean']['declarations'][1]['axioms'] = ['sorryAx']
    else:
        candidate['Energy.lean']['declarations'].pop()
    with pytest.raises(PreservationConflict):
        validate_project(before, after, baseline, candidate, 'target', candidate['Target.lean']['declarations'][0])


def test_file_with_rangeless_constant_requires_exact_retention():
    code = 'theorem target : True := by trivial\n'
    baseline = report(code, [decl('target', 1)])
    baseline['declarations'].append({**decl('generated', 1), 'range': None})
    assert validate_file(code, code, baseline, deepcopy(baseline), 'target')['protected'] == ['generated']
    changed = code.replace('trivial', 'exact True.intro')
    candidate = report(changed, [decl('target', 1)])
    candidate['declarations'].append({**decl('generated', 1), 'range': None})
    with pytest.raises(PreservationConflict, match='protected boundaries'):
        validate_file(code, changed, baseline, candidate, 'target')
