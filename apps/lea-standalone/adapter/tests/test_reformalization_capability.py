"""Unsupported execution is distinct from a worker that can become ready."""
import pytest
from fastapi import HTTPException
from app import reformalization_intake


@pytest.mark.parametrize('ready,available,code', [
    (True, False, 'reformalization_executor_unavailable'),
    (False, False, 'reformalization_unavailable'),
    (True, True, 'reformalization_unavailable'),
])
def test_reformalization_admission_reports_the_capability_failure(monkeypatch, ready, available, code):
    health = {'ready': ready, 'capabilities': {
        'execution': {'available': available, 'reason': 'No supported filesystem executor.'},
        'reformalization': {'admission_enabled': False, 'staging': available},
    }}
    monkeypatch.setattr(reformalization_intake.execution, 'health', lambda: health)
    with pytest.raises(HTTPException) as error:
        reformalization_intake.enqueue({'idempotency_key': 'unsupported-repair'}, None)
    assert error.value.status_code == 503
    assert error.value.detail['error'] == code
