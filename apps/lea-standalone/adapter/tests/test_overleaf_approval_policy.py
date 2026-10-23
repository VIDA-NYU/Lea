"""Approval defaults survive Overleaf → LeaChat without changing execution mode."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

import pytest
from fastapi import BackgroundTasks, HTTPException
from lea.interface import FileChanged, Finished, ToolApprovalRequested, ToolCalled, ToolResulted, TurnStarted
from lea.providers import Usage

from app import bridge, db, runbroker, runregistry, runtime, store
from app.config import LeaConfig
from app.routes import projects as projects_route, runs as runs_route
from app.routes.projects import (
    FormalizationTargetSyncRequest, GithubImportConfirmRequest, GithubImportPreviewRequest,
    MirrorFile, MirrorRequest, ProjectIdentityUpdate, SessionCreate,
)
from app.routes.runs import NewFormalizationRequest, RunRequest
from app.runregistry import RunRegistry


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "test.sqlite3")
    monkeypatch.setattr(store, "RAW_EVENT_LOG_DIR", tmp_path / "events")
    monkeypatch.setattr(runtime, "_state", "ready")
    monkeypatch.setattr(runregistry, "registry", RunRegistry(max_concurrent=1))
    monkeypatch.setattr(runbroker, "_brokers", {})
    monkeypatch.setattr(bridge, "_session_allowlists", {})
    monkeypatch.setattr(bridge, "_pending_approvals", {})
    monkeypatch.setattr(bridge, "_stop_events", {})
    db.init_db()
    config = LeaConfig(model="m", max_turns=3, lea_root=tmp_path,
                       prompt_variant="interactive", max_spend_usd=None)
    for module in (runs_route, projects_route, bridge):
        monkeypatch.setattr(module, "load_config", lambda: config)
    monkeypatch.setattr(runs_route, "permission_tier", lambda: "stepwise")
    monkeypatch.setattr(bridge, "enqueue_run", lambda run_id: None)
    return config


def _overleaf_run(**kwargs):
    return runs_route.create_run(RunRequest(
        message="Formalize paper theorem", project_slug="paper", origin="overleaf",
        autonomous=True, **kwargs,
    ))


def _drive(result, config, monkeypatch):
    seen = {}

    def fake(cfg, messages, *, working_dir=None, session_id=None, gate=None, **kwargs):
        seen.update(prompt=cfg.prompt_variant, tools=cfg.tools, extra_tools=cfg.extra_tools,
                    gate=gate, executed=[])
        yield TurnStarted(1)
        proof = Path(working_dir) / "Proof.lean"
        for name in ("bash", "write_file", "edit_file"):
            args = {"command": "pwd"} if name == "bash" else {"path": str(proof)}
            if gate and gate(name, args):
                decision = yield ToolApprovalRequested(name, args)
                assert decision in {"allow", "always_session"}
            yield ToolCalled(name, args)
            seen["executed"].append(name)
            if name != "bash":
                proof.write_text("import Mathlib\n\ntheorem example_proof : True := by trivial\n")
                yield FileChanged(str(proof))
            yield ToolResulted(name, "ok", "ok")
        yield Finished("completed", "Updated proof", 1, session_id, "m",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {"messages": []})

    class ApprovingQueue(Queue):
        def put(self, event):
            super().put(event)
            if event["type"] == "approval_requested":
                from app import run_commands,commands,command_execution
                run_commands.approve(result['run_id'],event['payload']['approval_id'],'allow')
                control=commands.claim('policy-fixture',epoch,'control')
                commands.complete(control,command_execution.control(control))

    monkeypatch.setattr(bridge, "run_events", fake)
    events = ApprovingQueue()
    from app import commands,run_commands
    run_commands.recover_at_startup()
    epoch=commands.start_worker('policy-fixture')
    token=commands.claim('policy-fixture',epoch,'compute',kinds={'run.execute'})
    row = store.get_run(result["run_id"])
    bridge.run_lea(bridge.RunnerContext(
        session_id=row["session_id"], run_id=row["id"], task="Update proof", config=config,
        events=events, autonomous=bool(row["autonomous"]), approval_mode=row["approval_mode"],command_token=token,
    ))
    output = list(events.queue)
    assert output[-1]["type"] == "done"
    assert store.get_run(row["id"])["status"] != "failed"
    return seen, output


def test_overleaf_followup_is_conversational_without_any_tool_approval(env, monkeypatch):
    first = _overleaf_run()
    store.update_run(first['run_id'],'answered',result_kind='answered')
    followup = runs_route.create_run(RunRequest(message="Update the proof", session_id=first["session_id"]))
    assert first["autonomous"] is True and first["approval_mode"] == "none"
    assert followup["autonomous"] is False and followup["approval_mode"] == "none"
    assert store.get_session(followup["session_id"])["origin"] == "overleaf"
    seen, events = _drive(followup, env, monkeypatch)
    assert seen["prompt"] == "interactive"
    assert "spawn_subagent" in seen["extra_tools"]
    assert seen["gate"] is None
    assert not any(event["type"].startswith("approval_") for event in events)
    assert seen["executed"] == ["bash", "write_file", "edit_file"]
    assert store.session_detail(followup["session_id"])["code_steps"]
    assert store.get_run(followup["run_id"])["pending_approval"] is None
    detail = store.session_detail(followup["session_id"])
    assert detail["project"]["overleaf_linked"] is True
    assert all(run["approval_mode"] == "none" for run in detail["runs"])
    assert detail["runs"][-1]["autonomous"] is False
    assert runs_route.get_run_row(followup["run_id"])["autonomous"] is False


@pytest.mark.parametrize("entry", ["new_chat", "precreated_session", "formalization"])
def test_every_leachat_entry_inherits_linked_project(env, entry):
    first = _overleaf_run(new_formalization=NewFormalizationRequest(display_title="Target"))
    scope = {"project_slug": "paper"}
    if entry == "precreated_session":
        session = projects_route.create_session_in_project(first["project_id"], SessionCreate(title="New chat"))
        scope = {"session_id": session["id"]}
    elif entry == "formalization":
        scope["focus_formalization_id"] = first["focus_formalization_id"]
    result = runs_route.create_run(RunRequest(message="Continue", autonomous=False, **scope))
    assert result["approval_mode"] == "none" and result["autonomous"] is False
    assert store.get_session(result["session_id"])["origin"] == "ui"


def test_loose_overleaf_session_inherits_no_gate(env):
    session = store.create_session("Legacy loose chat", origin="overleaf")
    result = runs_route.create_run(RunRequest(message="Continue", session_id=session["id"]))
    assert result["approval_mode"] == "none" and result["autonomous"] is False


@pytest.mark.parametrize("project_slug", [None, "ordinary"])
def test_ordinary_chats_still_approve_each_mutating_tool(env, monkeypatch, project_slug):
    result = runs_route.create_run(RunRequest(message="Update", project_slug=project_slug))
    assert result["approval_mode"] == "stepwise" and result["autonomous"] is False
    seen, events = _drive(result, env, monkeypatch)
    assert seen["prompt"] == "interactive"
    assert callable(seen["gate"]) and not seen["gate"]("read_file", {})
    assert [event["payload"]["tool_name"] for event in events if event["type"] == "approval_requested"] == [
        "bash", "write_file", "edit_file",
    ]


@pytest.mark.parametrize("global_none", [False, True])
def test_unattended_execution_is_preserved_and_does_not_link_project(env, monkeypatch, global_none):
    monkeypatch.setattr(runs_route, "permission_tier", lambda: "none" if global_none else "stepwise")
    result = runs_route.create_run(RunRequest(message="Prove", autonomous=not global_none, project_slug="ordinary"))
    assert result["autonomous"] is True and result["approval_mode"] == "none"
    assert store.get_project(result["project_id"])["overleaf_linked"] is False
    seen, _ = _drive(result, env, monkeypatch)
    assert seen["prompt"] == "default" and seen["gate"] is None
    assert seen["tools"] is None or "spawn_subagent" not in seen["tools"]
    assert "spawn_subagent" not in seen["extra_tools"]
    monkeypatch.setattr(runs_route, "permission_tier", lambda: "stepwise")
    followup = runs_route.create_run(RunRequest(message="Discuss", session_id=result["session_id"]))
    assert followup["autonomous"] is False and followup["approval_mode"] == "stepwise"


@pytest.mark.parametrize("origin_source", ["session", "formalization", "request"])
def test_run_admission_establishes_linkage_from_origin(env, origin_source):
    project = store.create_project("paper")
    session = store.create_session("Chat", project_id=project["id"],
                                   origin="overleaf" if origin_source == "session" else "ui")
    args = {"session_id": session["id"]}
    if origin_source == "request":
        args["origin"] = "overleaf"
    if origin_source == "formalization":
        args["new_formalization"] = NewFormalizationRequest(display_title="Target", origin="overleaf")
    result = runs_route.create_run(RunRequest(message="Continue", **args))
    assert result["approval_mode"] == "none"
    assert store.get_project(project["id"])["overleaf_linked"] is True


@pytest.mark.parametrize("mismatch", ["session", "formalization"])
def test_failed_admission_cannot_establish_project_linkage(env, mismatch):
    left, right = store.create_project("left"), store.create_project("right")
    session = store.create_session("Left", project_id=left["id"])
    target = store.create_formalization(project_id=right["id"], loose_session_id=None, display_title="Right")
    args = {"session_id": session["id"], "origin": "overleaf"}
    args.update({"project_slug": "right"} if mismatch == "session" else {"focus_formalization_id": target["id"]})
    with pytest.raises(HTTPException) as exc:
        runs_route.create_run(RunRequest(message="Continue", **args))
    assert exc.value.status_code == 409
    assert all(project["overleaf_linked"] is False for project in store.list_projects())


def test_linkage_rolls_back_when_later_bundle_admission_fails(env, monkeypatch):
    project = store.create_project("paper")
    def rejected(*args, **kwargs):
        raise ValueError("Rejected dependency admission")
    from app import project_dependencies
    monkeypatch.setattr(project_dependencies, "admit_worker", rejected)
    with pytest.raises(ValueError):
        store.create_run_bundle(
            message="Prove", session_id=None, project_id=project["id"],
            session_origin="overleaf", session_origin_url=None, model="m", provider=None,
            max_turns=3, autonomous=False, dependency_id="invalid",
        )
    assert store.get_project(project["id"])["overleaf_linked"] is False
    assert store.list_sessions() == []


def test_idempotent_retry_returns_original_policy_after_project_linkage(env):
    request = RunRequest(message="Prove", project_slug="ordinary", idempotency_key="original")
    first = runs_route.create_run(request)
    store.mark_project_overleaf_linked(first["project_id"])
    repeated = runs_route.create_run(request)
    assert repeated["run_id"] == first["run_id"]
    assert repeated["approval_mode"] == "stepwise" and repeated["autonomous"] is False
    next_run = runs_route.create_run(RunRequest(message="Next", session_id=first["session_id"]))
    assert next_run["approval_mode"] == "none"


def test_empty_and_deleted_mirrors_keep_linkage(env):
    tasks = BackgroundTasks()
    empty = projects_route.mirror_overleaf_tex("empty", MirrorRequest(files=[]))
    assert store.get_project(empty["project_id"])["overleaf_linked"] is True
    mirrored = projects_route.mirror_overleaf_tex("paper", MirrorRequest(files=[MirrorFile(path="main.tex", content="Text")]))
    projects_route.mirror_overleaf_tex("paper", MirrorRequest(files=[]))
    assert store.list_project_files(mirrored["project_id"]) == []
    result = runs_route.create_run(RunRequest(message="Continue", project_slug="paper"))
    assert result["approval_mode"] == "none"


@pytest.mark.parametrize("operation", ["identity_create", "identity_rename", "import_preview", "import_confirm", "sync"])
def test_overleaf_project_operations_establish_linkage(env, monkeypatch, operation):
    project = None if operation == "identity_create" else store.create_project("paper")
    if operation.startswith("identity"):
        projects_route.update_project_identity_by_slug.worker("paper", ProjectIdentityUpdate(
            project_name="Renamed", mode="display-only", create_if_missing=True,
        ))
    elif operation == "import_preview":
        monkeypatch.setattr(projects_route, "_preview_github_import", lambda *args: {"ok": True})
        projects_route.preview_project_github_import_by_slug.worker("paper", GithubImportPreviewRequest(repository_url="https://github.com/example/paper"))
    elif operation == "import_confirm":
        monkeypatch.setattr(projects_route, "_confirm_github_import", lambda *args: {"ok": True})
        projects_route.confirm_project_github_import_by_slug.worker("paper", GithubImportConfirmRequest(preview_id="preview"))
    else:
        projects_route.sync_project_formalizations_by_slug.worker("paper", FormalizationTargetSyncRequest(targets=[]))
    assert store.get_project_by_slug("paper")["overleaf_linked"] is True


def test_failed_operation_and_reads_do_not_establish_linkage(env, monkeypatch):
    project = store.create_project("paper")
    projects_route.get_project.worker(project["id"])
    projects_route.get_project_identity_by_slug("paper")
    projects_route.list_projects.worker()
    def rejected(*args):
        raise HTTPException(422, "Invalid import")
    monkeypatch.setattr(projects_route, "_preview_github_import", rejected)
    with pytest.raises(HTTPException):
        projects_route.preview_project_github_import_by_slug.worker("paper", GithubImportPreviewRequest(repository_url="bad"))
    assert store.get_project(project["id"])["overleaf_linked"] is False


def test_linkage_is_idempotent_concurrent_and_survives_removed_session(env):
    result = _overleaf_run()
    project_id = result["project_id"]
    before = store.get_project(project_id)["updated_at"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        marked = list(pool.map(store.mark_project_overleaf_linked, [project_id] * 8))
    assert all(row["overleaf_linked"] is True and row["updated_at"] == before for row in marked)
    with db.write() as conn:
        conn.execute("delete from runs where session_id = ?", (result["session_id"],))
        conn.execute("delete from timeline where session_id = ?", (result["session_id"],))
        conn.execute("delete from sessions where id = ?", (result["session_id"],))
    assert runs_route.create_run(RunRequest(message="New chat", project_slug="paper"))["approval_mode"] == "none"


def test_recovered_dispatch_uses_the_run_snapshot(env, monkeypatch):
    first = _overleaf_run()
    store.update_run(first["run_id"], "answered")
    followup = runs_route.create_run(RunRequest(message="Continue", session_id=first["session_id"]))
    ordinary = runs_route.create_run(RunRequest(message="Ordinary", project_slug="ordinary"))
    # Neither a later config change nor later project linkage rewrites queued runs.
    monkeypatch.setattr(runs_route, "permission_tier", lambda: "none")
    store.mark_project_overleaf_linked(ordinary["project_id"])
    db.init_db()
    from app import run_commands,commands
    bridge.recover_runs_at_startup()
    recovered=[row['id'] for row in store.list_runs_by_status('pending') if run_commands.for_run(row['id'])]
    assert set(recovered) == {followup["run_id"], ordinary["run_id"]}
    contexts = []
    class RecordingThread:
        def __init__(self, *, target, args, **kwargs):
            self.context = args[0]
        def start(self):
            contexts.append(self.context)
            runregistry.registry.release(self.context.run_id)
    monkeypatch.setattr(bridge, "Thread", RecordingThread)
    epoch=commands.start_worker('recovered-policy')
    for _ in recovered:
        token=commands.claim('recovered-policy',epoch,'compute',kinds={'run.execute'})
        run_id=json.loads(token['payload_json'])['run_id']
        bridge._try_dispatch(run_id, {},command_token=token)
    policies = {ctx.run_id: (ctx.autonomous, ctx.approval_mode) for ctx in contexts}
    assert policies[followup["run_id"]] == (False, "none")
    assert policies[ordinary["run_id"]] == (False, "stepwise")
