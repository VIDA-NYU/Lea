from lea.interface import Finished, UsageUpdated
from lea.providers import Usage

from app import bridge, store
from test_bridge import _context, _drain


def test_cancelled_model_retains_observed_usage_without_claiming_final_bill(tmp_path, monkeypatch):
    ctx, queue = _context(tmp_path, monkeypatch)

    def events(*args, **kwargs):
        yield UsageUpdated(10, 5, 0.02)
        bridge.request_stop(ctx.run_id)
        yield Finished("interrupted", "Run stopped.", 1, ctx.session_id, "gemini/test",
                       Usage(10, 5), 0.02, {"messages": []}, usage_status="partial")

    monkeypatch.setattr(bridge, "run_events", events)
    bridge.run_lea(ctx)
    row = store.get_run(ctx.run_id)
    assert row["status"] == "cancelled"
    assert row["stop_reason"] == "user_stop"
    assert row["usage_status"] == "partial"
    assert row["cost_usd"] == 0.02
    assert _drain(queue)[-1]["payload"]["usage_status"] == "partial"
