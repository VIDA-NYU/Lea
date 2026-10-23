"""Pinned-version source discovery should resolve symbols before compilation."""
from pathlib import Path

import pytest

from lea import tools


@pytest.fixture
def lake(tmp_path):
    (tmp_path / 'lakefile.toml').write_text('name = "search_fixture"\n')
    root = tmp_path / '.lake/packages/mathlib/Mathlib'
    for name, text in {
        'Tactic/Noise.lean':'def Eventually := True\ndef EvenFunction := True\n',
        'Algebra/Group/Even.lean':'-- Parity\n@[to_additive /-- Even a means a = r + r -/]\ndef IsSquare := True\n',
        'NumberTheory/UsesEven.lean':'theorem example_even (h : Even a) : Even a := h\n',
    }.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


@pytest.mark.parametrize('query', ['Even', 'def Even'])
def test_exact_symbol_excludes_prefix_noise_and_finds_generated_module(lake, query):
    result = tools.search_mathlib(query, path=str(lake))
    assert 'Eventually' not in result and 'EvenFunction' not in result
    assert 'import Mathlib.Algebra.Group.Even' in result
    assert result.index('Algebra/Group/Even.lean') < result.index('UsesEven.lean')
    assert 'to_additive' in result


def test_module_lookup_confirms_existence_without_a_compiler(lake, monkeypatch):
    monkeypatch.setattr(tools.command_executor, 'run', lambda *a, **k: pytest.fail('Module lookup ran a subprocess'))
    assert 'Module found' in tools.search_mathlib('Mathlib.Algebra.Group.Even', path=str(lake))
    assert 'No Mathlib module' in tools.search_mathlib('Mathlib.Algebra.Parity', path=str(lake))


def test_queries_are_bounded_and_bad_regex_is_reported(lake):
    assert 'nonempty' in tools.search_mathlib('', path=str(lake))
    assert '256' in tools.search_mathlib('x' * 257, path=str(lake))
    assert 'Error:' in tools.search_mathlib('[', path=str(lake))
    assert tools.search_mathlib('Even', max_results=1, path=str(lake)).count('import Mathlib.') == 1


def test_search_cannot_select_an_unreadable_lake_project(lake, monkeypatch):
    monkeypatch.setattr(tools, '_readable_roots', lambda:[lake / 'proofs'])
    assert 'outside the allowed read roots' in tools.search_mathlib('Even', path=str(lake))


def test_relative_paths_use_the_active_project_instead_of_process_cwd(lake):
    from lea.config import LeaConfig
    from lea.runctx import run_context
    proof = lake / 'proofs/Main.lean'
    proof.parent.mkdir()
    proof.write_text('')
    with run_context(config=LeaConfig(model='test',max_turns=1), working_dir=str(lake)):
        result = tools.search_mathlib('Even', path='proofs/Main.lean')
    assert str(lake) in result and 'import Mathlib.Algebra.Group.Even' in result
