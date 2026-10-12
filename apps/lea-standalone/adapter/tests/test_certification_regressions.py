"""Audit reproductions use disposable data and scripted providers, with real Lean."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[4]


def reproduce(name):
    result = subprocess.run([sys.executable, str(ROOT / 'docs/audit-2026-10-10' / name)],
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('text', ['NOT PROVED', 'NEEDS_REVIEW: this is not PROVED',
                                 '```PROVED```', 'PROVED DISPROVED', 'unknown', ''])
def test_classifier_fails_closed(text):
    from lea.agent import _parse_result_kind
    assert _parse_result_kind(text) == 'needs_review'


@pytest.mark.parametrize('text,expected', [('PROVED', 'proved'), (' disproved\n', 'disproved'),
                                         ('NEEDS_REVIEW', 'needs_review')])
def test_classifier_exact_tokens(text, expected):
    from lea.agent import _parse_result_kind
    assert _parse_result_kind(text) == expected


def test_real_agent_shell_edit_cannot_certify_bad_source():
    evidence = reproduce('repro_agent_revision.py')['real_agent_revision']
    assert evidence['actual_final_check'] == 'error'
    assert evidence['run_status'] != 'proved'
    assert evidence['formalization_status'] != 'proved'


def test_real_dependency_edit_invalidates_dependent():
    evidence = reproduce('repro_dependency_revision.py')['dependency_edit']
    assert evidence['actual_downstream_check'] == 'error'
    assert evidence['downstream_status_after_save'] != 'proved'
    assert evidence['downstream_status_after_rebuild'] != 'proved'
    assert evidence['downstream_check_current'] is False
