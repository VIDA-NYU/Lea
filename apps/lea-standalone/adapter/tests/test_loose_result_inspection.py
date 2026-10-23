"""Real kernel inspection of chat files outside registered Lake library roots."""
from pathlib import Path
import shutil

import pytest

from lea.result_selection import inspect_selected


@pytest.fixture(params=['session-with-hyphen','00000000-0000-4000-8000-000000000001','registered'])
def loose(tmp_path, request):
    if not shutil.which('lake'):
        pytest.skip('Lean toolchain required')
    (tmp_path/'lakefile.toml').write_text('name = "loose_inspection"\n[[lean_lib]]\nname = "Lea"\nsrcDir = "proofs"\n')
    pin = Path(__file__).resolve().parents[2]/'prover/workspace/lean-toolchain'
    shutil.copyfile(pin,tmp_path/'lean-toolchain')
    base = tmp_path/'proofs'
    if request.param != 'registered':
        base /= request.param
    path = base/'Lea/Misc/Main.lean'
    path.parent.mkdir(parents=True)
    return path


def test_loose_file_has_real_selected_declaration_and_axiom_evidence(loose):
    loose.write_text('namespace Lea.Misc\ntheorem target : True := by trivial\nend Lea.Misc\n')
    selected = inspect_selected(loose,'Lea.Misc.target','theorem')
    assert selected['user_name'] == 'Lea.Misc.target'
    assert selected['kind'] == 'theorem' and not selected['axioms']


def test_loose_inspection_rejects_unsupported_axioms(loose):
    loose.write_text('axiom invented : False\ntheorem target : False := invented\n')
    with pytest.raises(ValueError,match='axioms'):
        inspect_selected(loose,'target','theorem')


def test_inspection_does_not_add_import_lean_to_the_original_source(loose):
    loose.write_text('#check Lean.Elab.Command.CommandElabM\ntheorem target : True := by trivial\n')
    with pytest.raises(ValueError,match='compilation failed|did not build'):
        inspect_selected(loose,'target','theorem')
