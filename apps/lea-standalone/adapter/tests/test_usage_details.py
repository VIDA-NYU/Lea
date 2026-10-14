"""Additive accounting must not change old totals or charge deltas twice."""
import json

import pytest

from app import db, store, bridge, recovery
from lea.usage import merge_details


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "usage.sqlite3")
    db.init_db()
    session = store.create_session("Usage")
    return session


def test_mixed_historical_cache_aware_and_unpriced_usage(ledger):
    old = store.create_run(ledger["id"], "old", None, 5)
    store.update_run(old["id"], "completed", input_tokens=100, output_tokens=10, cost_usd=.42, usage_status="final")
    rich = {"cost_basis": "cache_aware_estimate", "cached_input_tokens": 800,
            "cache_write_tokens": 50, "reasoning_tokens": 20}
    new = store.create_run(ledger["id"], "new", None, 5)
    assert store.claim_pending_run(new["id"])
    assert store.checkpoint_run_usage(new["id"], 1000, 100, .01, 1, rich)
    assert not store.checkpoint_run_usage(new["id"], 9999, 9999, 999, 1, rich)
    store.update_run(new["id"], "completed", usage_status="final", usage_details=rich)
    unknown = store.create_run(ledger["id"], "unmapped", None, 5)
    store.update_run(unknown["id"], "completed", input_tokens=50, output_tokens=5, cost_usd=0,
                     usage_status="final", usage_details={"cost_basis": "unavailable", "unpriced_calls": 1})
    global_usage = store.global_usage()
    assert global_usage["cost_usd"] == pytest.approx(.43)
    assert global_usage["input_tokens"] == 1150  # cache counts are subsets
    assert global_usage["usage_details"]["cached_input_tokens"] == 800
    assert global_usage["usage_details"]["unpriced_calls"] == 1
    assert global_usage["usage_details"]["cost_basis"] == "mixed"
    assert store.total_spend_usd() == pytest.approx(.43)
    assert store.get_run(old["id"])["usage_details"]["cost_basis"] == "legacy_estimate"
    assert store.get_run(old["id"])["cost_usd"] == .42
    assert store.get_run_status(new["id"])["usage_details"] == rich
    detail = store.session_detail(ledger["id"])
    assert detail["usage_details"]["unpriced_calls"] == 1
    assert detail["runs"][1]["usage_details"] == rich


def test_turn_accumulation_and_raw_invocation_journal(ledger):
    run = store.create_run(ledger["id"], "test", None, 5)
    assert store.claim_pending_run(run["id"])
    attempt = recovery.start_attempt(run["id"])
    details = {"cost_basis": "cache_aware_estimate", "cached_input_tokens": 90, "reasoning_tokens": 5}
    turns = bridge._UsageByTurn()
    turns.add(1, 100, 10, .01, details)
    turns.add(1, 20, 5, .005, {"cost_basis": "provider_reported", "cached_input_tokens": 10})
    store.replace_run_usage_breakdown(run["id"], turns.rows())
    row = store.usage_breakdown_for_session(ledger["id"])[0]
    assert row["input_tokens"] == 120
    assert row["cost_usd"] == pytest.approx(.015)
    assert row["usage_details"]["cached_input_tokens"] == 100
    assert row["usage_details"]["cost_basis"] == "mixed"
    recorder = recovery.Recorder(run["id"], attempt, {})
    recorder("invocation_start", {"id": "paid", "kind": "model", "name": "stream", "input": {}})
    result = {"raw_usage": {"prompt_tokens_details": {"cached_tokens": 90}}, "usage_details": details}
    recorder("invocation_result", {"id": "paid", "result": result})
    with db.connect() as conn:
        assert json.loads(conn.execute("select result_json from run_invocations where id='paid'").fetchone()[0]) == result
        assert conn.execute("select usage_details_json from runs where id=?", (run["id"],)).fetchone()[0] is None


def test_legacy_breakdown_keeps_dollar_amount(ledger):
    run = store.create_run(ledger["id"], "old", None, 5)
    store.replace_run_usage_breakdown(run["id"], [{"input_tokens": 100, "output_tokens": 20, "cost_usd": .33}])
    row = store.usage_breakdown_for_session(ledger["id"])[0]
    assert row["cost_usd"] == .33
    assert row["usage_details"]["cost_basis"] == "legacy_estimate"


def test_manual_compaction_is_journaled_and_charged_once(ledger):
    from lea.providers import Usage
    run = store.create_run(ledger["id"], "test", None, 5)
    store.update_run(run["id"], "completed", input_tokens=100, output_tokens=10, cost_usd=.42, usage_status="final")
    action = store.begin_auxiliary_invocation(run["id"], "manual_compaction", "test")
    usage = Usage(200, 20, {"cost_basis": "cache_aware_estimate", "cached_input_tokens": 180})
    usage.raw_usages = [{"prompt_tokens": 200, "prompt_tokens_details": {"cached_tokens": 180}}]
    store.finish_auxiliary_invocation(action, usage, .01)
    store.finish_auxiliary_invocation(action, usage, .01)
    assert store.get_run(run["id"])["cost_usd"] == pytest.approx(.43)
    assert store.get_run(run["id"])["input_tokens"] == 300
    assert store.get_run(run["id"])["usage_details"]["cached_input_tokens"] == 180
    assert store.usage_breakdown_for_session(ledger["id"])[0]["phase"] == "compaction"
    with db.connect() as conn:
        raw = json.loads(conn.execute("select result_json from run_invocations where id=?", (action,)).fetchone()[0])
        assert raw["raw_usage"] == usage.raw_usages
