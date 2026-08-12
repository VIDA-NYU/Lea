"""Run creation, queue observation, reconnect, and interrupt behavior."""

import asyncio

import pytest

from app import db, runbroker, runregistry, store
from app.config import LeaConfig
from app.runregistry import RunRegistry
from app.routes import runs as runs_route
from app.routes.runs import NewFormalizationRequest, RunRequest


class _Req:
    def __init__(self, since=None, last_event_id=None):
        self.query_params = {} if since is None else {"since": since}
        self.headers = {} if last_event_id is None else {"last-event-id": last_event_id}


def _fresh_registry(monkeypatch, capacity=1):
    runbroker._brokers.clear()
    reg = RunRegistry(max_concurrent=capacity)
    monkeypatch.setattr(runregistry, "registry", reg)
    return reg


def _patch_config(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runs_route,
        "load_config",
        lambda: LeaConfig(model="m", max_turns=3, lea_root=tmp_path, max_spend_usd=None),
    )
    # Creation tests inspect persisted rows. Keep the real background dispatcher
    # from invoking the prover; observer tests install brokers explicitly.
    monkeypatch.setattr(runs_route.bridge, "enqueue_run", lambda run_id: None)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    _patch_config(monkeypatch, tmp_path)
    return _fresh_registry(monkeypatch)


def test_create_run_tags_session_and_run_with_project_slug(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = runs_route.create_run(
        RunRequest(
            message="prove thm_one",
            autonomous=True,
            project_slug="doc-a",
            project_title="Doc A",
        )
    )

    session = store.get_session(result["session_id"])
    project = store.get_project_by_slug("doc-a")
    assert project is not None
    assert session["project_id"] == project["id"]
    assert result["project_id"] == project["id"]
    assert result["project_slug"] == "doc-a"
    assert result["project_namespace"] == project["namespace"]

    runs_route.create_run(
        RunRequest(message="prove thm_two", autonomous=True, project_slug="doc-a")
    )
    assert len(store.list_projects()) == 1
    doc_sessions = [
        s for s in store.usage_stats()["sessions"] if s["project_slug"] == "doc-a"
    ]
    assert len(doc_sessions) == 2


def test_create_run_records_overleaf_origin(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    url = "https://www.overleaf.com/project/doc-a"
    result = runs_route.create_run(
        RunRequest(
            message="prove thm_one",
            autonomous=True,
            project_slug="doc-a",
            project_title="Doc A",
            origin="overleaf",
            origin_url=url,
        )
    )
    session = store.get_session(result["session_id"])
    assert session["origin"] == "overleaf"
    assert session["origin_url"] == url


def test_create_run_without_origin_defaults_to_ui(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = runs_route.create_run(RunRequest(message="interactive run"))
    session = store.get_session(result["session_id"])
    assert session["origin"] == "ui"
    assert session["origin_url"] is None


def test_create_run_without_slug_stays_project_less(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = runs_route.create_run(RunRequest(message="interactive run"))
    session = store.get_session(result["session_id"])
    assert session["project_id"] is None
    assert result["project_id"] is None
    assert result["project_slug"] is None
    assert result["project_namespace"] is None
    assert store.list_projects() == []


def test_new_formalization_and_run_are_created_atomically(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = runs_route.create_run(
        RunRequest(
            message="prove compact_image",
            project_slug="topology",
            new_formalization=NewFormalizationRequest(
                display_title="Compact image",
                declaration_name="compact_image",
            ),
        )
    )

    assert result["formalization"]["id"] == result["focus_formalization_id"]
    assert result["formalization"]["activity"] == {
        "status": "queued",
        "run_id": result["run_id"],
    }
    assert result["formalization"]["validity_status"] == "planned"
    assert result["formalization"]["files"] == []
    assert result["formalization"]["sessions"][0]["id"] == result["session_id"]
    detail = store.session_detail(result["session_id"])
    assert detail["runs"][0]["focus_formalization_id"] == result["formalization"]["id"]
    assert detail["messages"][0]["formalization_id"] == result["formalization"]["id"]
    assert [
        item["id"]
        for item in store.list_raw_session_formalizations(result["session_id"])
    ] == [result["formalization"]["id"]]


def test_external_origin_key_reuses_formalization_across_sessions(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    request = dict(
        message="formalize source target",
        project_slug="paper",
        origin="overleaf",
        new_formalization=NewFormalizationRequest(
            display_title="source_target",
            declaration_name="source_target",
            origin="overleaf",
            origin_key="paper:theorem:source_target",
            source_hash="v1",
        ),
    )
    first = runs_route.create_run(RunRequest(**request))
    second = runs_route.create_run(RunRequest(**request))

    assert first["session_id"] != second["session_id"]
    assert first["formalization"]["id"] == second["formalization"]["id"]


def test_invalid_focus_rolls_back_the_whole_bundle(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    before = len(store.list_sessions())

    with pytest.raises(Exception) as caught:
        runs_route.create_run(
            RunRequest(
                message="should not persist",
                project_slug="analysis",
                focus_formalization_id="missing",
            )
        )

    assert getattr(caught.value, "status_code", None) == 404
    assert len(store.list_sessions()) == before


def test_create_run_snapshots_explicit_model_over_config_default(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(
        runs_route.settings_service,
        "validate_configured_model",
        lambda model: seen.append(model) or model.strip(),
    )

    result = runs_route.create_run(
        RunRequest(message="use the picker", model="picker/model")
    )

    run = store.get_run(result["run_id"])
    assert seen == ["picker/model"]
    assert result["model"] == "picker/model"
    assert run["model"] == "picker/model"


def test_create_run_without_explicit_model_uses_config_default(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    result = runs_route.create_run(RunRequest(message="use the default"))

    assert result["model"] == "m"
    assert store.get_run(result["run_id"])["model"] == "m"


def test_create_run_rejects_explicit_model_without_provider_key(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    def reject_model(model):
        raise runs_route.settings_service.SettingsValidationError(
            "A provider key is required.",
            "api_keys.EXAMPLE_API_KEY",
        )

    monkeypatch.setattr(
        runs_route.settings_service,
        "validate_configured_model",
        reject_model,
    )

    try:
        runs_route.create_run(
            RunRequest(message="missing credentials", model="example/model")
        )
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 422
        assert getattr(exc, "detail", None) == {
            "message": "A provider key is required.",
            "field": "api_keys.EXAMPLE_API_KEY",
        }
    else:
        raise AssertionError("Expected HTTPException")


def test_create_run_ignores_invalid_slug(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = runs_route.create_run(
        RunRequest(message="bad slug run", project_slug="not a valid slug!")
    )
    assert store.get_session(result["session_id"])["project_id"] is None
    assert store.list_projects() == []


def test_get_run_row_returns_only_cheap_outcome_columns(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    started = runs_route.create_run(RunRequest(message="prove it", autonomous=True))
    run_id = started["run_id"]
    assert set(runs_route.get_run_row(run_id)) == {
        "id", "status", "result_kind", "result_detail"
    }
    store.update_run(run_id, "proved", result_kind="proved", result_detail="qed")
    row = runs_route.get_run_row(run_id)
    assert row["status"] == "proved"
    assert row["result_kind"] == "proved"
    assert row["result_detail"] == "qed"


def test_get_run_row_404s_on_unknown_run(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        runs_route.get_run_row("no-such-run")
        assert False, "expected a 404"
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 404


def _collect_through_done(response):
    async def collect():
        frames = []
        async for chunk in response.body_iterator:
            frames.append(chunk if isinstance(chunk, str) else chunk.decode())
            if "event: done" in frames[-1]:
                break
        return "".join(frames)

    return asyncio.run(collect())


def test_run_events_replays_buffered_history_then_tails_live(tmp_path, monkeypatch):
    reg = _setup(tmp_path, monkeypatch)
    started = runs_route.create_run(RunRequest(message="prove it", autonomous=True))
    run_id = started["run_id"]
    store.update_run(run_id, "running")
    reg.try_admit(run_id, started["session_id"])
    broker = runbroker.create(run_id)
    broker.put({"type": "status", "payload": {"status": "tool_call", "turn": 1}})

    async def run_case():
        response = await runs_route.run_events(run_id, _Req())

        async def finish():
            await asyncio.sleep(0.1)
            store.update_run(run_id, "proved")
            broker.put({"type": "done", "payload": {"status": "proved"}})

        asyncio.create_task(finish())
        frames = []
        async for chunk in response.body_iterator:
            frames.append(chunk if isinstance(chunk, str) else chunk.decode())
            if "event: done" in frames[-1]:
                break
        return "".join(frames)

    out = asyncio.run(run_case())
    assert "event: status" in out
    assert "event: done" in out
    assert "id: 1" in out


def test_reattach_cursor_skips_seen_events(tmp_path, monkeypatch):
    reg = _setup(tmp_path, monkeypatch)
    started = runs_route.create_run(RunRequest(message="prove it", autonomous=True))
    run_id = started["run_id"]
    store.update_run(run_id, "running")
    reg.try_admit(run_id, started["session_id"])
    broker = runbroker.create(run_id)
    broker.put({"type": "message", "payload": {"id": "m1"}})
    broker.put({"type": "code_step", "payload": {"id": "c1"}})
    broker.put({"type": "done", "payload": {"status": "proved"}})

    response = asyncio.run(runs_route.run_events(run_id, _Req(last_event_id="1")))
    out = _collect_through_done(response)
    assert "event: message" not in out
    assert "event: code_step" in out
    assert "event: done" in out


def test_terminal_run_without_broker_synthesizes_done(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    started = runs_route.create_run(RunRequest(message="prove it", autonomous=True))
    run_id = started["run_id"]
    store.update_run(
        run_id, "disproved", result_kind="disproved", result_detail="DISPROVED"
    )
    response = asyncio.run(runs_route.run_events(run_id, _Req()))
    out = _collect_through_done(response)
    assert "event: done" in out
    assert '"status": "disproved"' in out
    assert '"result_kind": "disproved"' in out


def test_queued_run_announces_position_before_broker_events(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    first = runs_route.create_run(RunRequest(message="first", autonomous=True))
    second = runs_route.create_run(RunRequest(message="second", autonomous=True))
    assert first["queue_position"] == 0
    assert second["queue_position"] == 1
    broker = runbroker.create(second["run_id"])
    broker.put({"type": "done", "payload": {"status": "proved"}})
    response = asyncio.run(runs_route.run_events(second["run_id"], _Req()))
    out = _collect_through_done(response)
    assert out.index("event: queued") < out.index("event: done")
    assert '"position": 1' in out


def test_interrupt_pending_unadmitted_run_finalizes_it(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    started = runs_route.create_run(RunRequest(message="prove it", autonomous=True))
    broker = runbroker.create(started["run_id"])
    assert runs_route.interrupt_run(started["run_id"]) == {"status": "interrupted"}
    run = store.get_run(started["run_id"])
    assert run["status"] == "failed"
    assert broker.closed


def test_interrupt_cancels_an_admitted_run_that_has_not_started(tmp_path, monkeypatch):
    """A run whose slot is claimed but whose driver has not begun is cancelled
    OUTRIGHT, not left for the cooperative path (AUDIT-2026-07-24 C7).

    This asserts the opposite of what it used to. The old shape checked the registry
    and returned 'interrupting' with the row still pending, on the theory that the
    driver owned it — but the driver had not started, so the run went on to claim the
    row, make a model call, and only then notice the stop flag. Now the row is
    finalized atomically and `run_lea` refuses to start, so an interrupt at this
    moment costs nothing. `store.claim_pending_run` is the same UPDATE from the
    driver's side, so exactly one of them wins.
    """
    reg = _setup(tmp_path, monkeypatch)
    started = runs_route.create_run(RunRequest(message="prove it", autonomous=True))
    reg.try_admit(started["run_id"], started["session_id"])

    assert runs_route.interrupt_run(started["run_id"]) == {"status": "interrupted"}
    assert store.get_run(started["run_id"])["status"] == "failed"
    # ...and the driver, arriving late, must decline to run it.
    assert store.claim_pending_run(started["run_id"]) is False


def test_interrupt_of_an_already_running_run_stays_cooperative(tmp_path, monkeypatch):
    """Once the driver holds the row, the endpoint must NOT rewrite it — the run stops
    at its next turn boundary and finalizes itself."""
    _setup(tmp_path, monkeypatch)
    started = runs_route.create_run(RunRequest(message="prove it", autonomous=True))
    assert store.claim_pending_run(started["run_id"]) is True  # the driver got there first

    assert runs_route.interrupt_run(started["run_id"]) == {"status": "interrupting"}
    assert store.get_run(started["run_id"])["status"] == "running"
