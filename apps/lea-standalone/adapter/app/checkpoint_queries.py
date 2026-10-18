"""Shared SQL/admission functions. No workspace or executor dependency."""
from __future__ import annotations
import json
import math
from . import db
SCHEMA_VERSION = 1

def _valid_usage_details(details) -> bool:
    if details is None:
        return True
    if not isinstance(details, dict):
        return False
    for key in ('cached_input_tokens', 'cache_write_tokens', 'reasoning_tokens', 'unpriced_calls', 'details_missing_calls'):
        value = details.get(key)
        if value is not None and (type(value) is not int or value < 0):
            return False
    counts = details.get('basis_counts', {})
    return isinstance(counts, dict) and all((isinstance(key, str) and type(value) is int and (value >= 0) for key, value in counts.items()))

def continuation_checkpoint(run_id: str, checkpoint_id: int | None=None) -> tuple[dict, dict, int]:
    """Ordinary pauses resume only a complete, durably journaled turn."""
    with db.connect() as conn:
        row = conn.execute('select * from run_checkpoints where run_id = ?\n                              and (? is null or id = ?) order by id desc limit 1', (run_id, checkpoint_id, checkpoint_id)).fetchone()
        if not row or row['schema_version'] != SCHEMA_VERSION:
            raise ValueError('Saved agent state is unavailable. Use Re-formalize to start a new attempt.')
        try:
            state = json.loads(row['payload'])
            saved_manifest = json.loads(row['manifest'])
        except (ValueError, TypeError) as exc:
            raise ValueError('Saved agent state is unreadable. Use Re-formalize to start a new attempt.') from exc
        if not isinstance(state, dict) or not isinstance(saved_manifest, dict) or (not isinstance(saved_manifest.get('files', {}), dict)) or (not isinstance(state.get('status_context', {}), dict)) or (not isinstance(state.get('proof_state', {}), dict)):
            raise ValueError('Saved checkpoint format is incompatible. Use Re-formalize to start a new attempt.')
        detector = state.get('stall_detector', {})
        if not isinstance(detector, dict) or type(detector.get('failures', 0)) is not int or detector.get('failures', 0) < 0 or (type(detector.get('evidence_revision', 0)) is not int) or (detector.get('evidence_revision', 0) < 0) or (detector.get('fingerprint') is not None and (not isinstance(detector['fingerprint'], str) or len(detector['fingerprint']) != 64)) or (not isinstance(detector.get('seen_evidence', []), list)) or any((not isinstance(x, str) or len(x) != 64 for x in detector.get('seen_evidence', []))) or (not _valid_usage_details(state.get('usage_details'))) or (not isinstance(state.get('dependency_evidence', {}), dict)) or any((type(state.get(k, 0)) is not int or state.get(k, 0) < 0 for k in ('input_tokens', 'output_tokens'))) or (type(state.get('cost_usd', 0)) not in (int, float)) or (not math.isfinite(state.get('cost_usd', 0))) or (state.get('cost_usd', 0) < 0):
            raise ValueError('Saved checkpoint accounting or stall state is corrupt. Review interrupted work before continuing.')
        if state.get('stage') != 'turn_boundary' or not isinstance(state.get('messages'), list) or type(state.get('turn')) is not int or (state['turn'] < 0) or any((not isinstance(m, dict) for m in state['messages'])):
            raise ValueError('The saved execution point is not a complete turn. Review interrupted work before continuing.')
        uncertain = conn.execute("select id from run_invocations where run_id = ?\n            and (completed_at is null or (completed_at > ? and not\n                 (kind='model_auxiliary' and name='manual_compaction'))) limit 1", (run_id, row['created_at'])).fetchone()
        if uncertain:
            raise ValueError('An action may have completed outside the saved state. Review interrupted work before continuing.')
    return (state, saved_manifest, row['id'])

def load_checkpoint(run_id: str) -> tuple[dict, dict] | None:
    with db.connect() as conn:
        row = conn.execute('select payload, manifest from run_checkpoints where run_id = ? order by id desc limit 1', (run_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row['payload'])
        if payload.get('stage') == 'model_complete':
            items = conn.execute('select call_index, result_json from run_invocations where run_id = ?\n                                   and completed_at is not null and call_index is not null\n                                   and started_at >= (select created_at from run_checkpoints\n                                   where run_id = ? order by id desc limit 1)', (run_id, run_id)).fetchall()
            payload['completed_tools'] = {**payload.get('completed_tools', {}), **{str(item['call_index']): json.loads(item['result_json']) for item in items}}
    return (payload, json.loads(row['manifest']))

def load_safe_conversation(run_id: str) -> list | None:
    """A manual successor starts a new turn after the last complete turn."""
    with db.connect() as conn:
        rows = conn.execute('select payload from run_checkpoints where run_id = ? order by id desc', (run_id,)).fetchall()
    for row in rows:
        payload = json.loads(row['payload'])
        if payload.get('stage') == 'turn_boundary':
            return payload.get('messages')
    return None
