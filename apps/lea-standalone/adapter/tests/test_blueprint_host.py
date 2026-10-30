"""Blueprint tools host: dispatch, idempotency, draft routing, bridge wiring, run purpose.

docs/PLAN-blueprint-tools.md §3 "P2 host", items 3-6. `blueprint_store` (P1) is
stubbed with a fake module carrying the §2.2 signatures, so these tests pin the
host's side of that contract only.
"""

import importlib
import sys
import types
from dataclasses import replace
from queue import Queue

import pytest
from fastapi import HTTPException

import app as app_package
from app import blueprint_host, bridge, db, graph, projects, runregistry, store
from app.config import LeaConfig
from app.runregistry import RunRegistry
from lea.blueprint_tools import TOOL_NAMES, BlueprintAck, BlueprintRequested
from lea.interface import Finished, TurnStarted
from lea.providers import Usage
from lea.registry import build_toolset

LIVE, DRAFT = ".lea/blueprint.md", ".lea/blueprint.draft.md"
NODES = [{"key": "main", "kind": "theorem", "statement": "The main result.", "uses": ["helper"]},
         {"key": "helper", "kind": "lemma", "statement": "A supporting fact."}]


def _drain(q: Queue) -> list[dict]:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


@pytest.fixture
def fake_store(monkeypatch):
    """A stand-in `app.blueprint_store` module with the §2.2 signatures."""
    module = types.ModuleType("app.blueprint_store")
    module.LIVE, module.DRAFT = LIVE, DRAFT
    module.calls = []
    module.parsed = {
        False: {"preamble": "# Blueprint — Demo\n", "blocks": [], "edges": [], "warnings": [], "nodes": [
            {"type": "node", "key": "main", "kind": "theorem", "lean": "Lea.Demo.main", "file": "Main.lean",
             "uses": ["helper", "other"], "statement": "The main result."},
            {"type": "node", "key": "helper", "kind": "lemma", "lean": "Lea.Demo.helper", "file": "Helper.lean",
             "uses": [], "statement": "A supporting fact."},
            {"type": "node", "key": "other", "kind": "lemma", "lean": "Lea.Demo.other", "file": "Other.lean",
             "uses": [], "statement": "Another fact."}]},
        True: {"preamble": "", "blocks": [], "edges": [], "warnings": [], "nodes": [
            {"type": "node", "key": "goal", "kind": "theorem", "lean": "Lea.Demo.goal", "file": "Goal.lean",
             "uses": ["step"], "statement": "The goal."},
            {"type": "node", "key": "step", "kind": "lemma", "lean": "Lea.Demo.step", "file": "Step.lean",
             "uses": [], "statement": "A step."}]},
    }

    def load(project, proofs_root, *, draft=False):
        module.calls.append(("load", project["id"], proofs_root, draft))
        return module.parsed[draft]

    def upsert(project, proofs_root, nodes, *, draft=False, actor="agent"):
        module.calls.append(("upsert", project["id"], proofs_root, nodes, draft, actor))
        applied = [{"key": n["key"], "kind": n.get("kind"), "lean": f"Lea.Demo.{n['key']}",
                    "file": n["key"].capitalize() + ".lean", "uses": n.get("uses", []), "status": "planned"}
                   for n in nodes]
        return {"applied": applied, "rejected": [], "ready": [n["key"] for n in nodes if not n.get("uses")],
                "counts": {"total": len(nodes), "proved": 0, "ready": 1, "stated": 0, "failed": 0,
                           "planned": len(nodes) - 1},
                "draft": draft, "commit_sha": "abc123"}

    def remove(project, proofs_root, keys, *, cascade=False, draft=False, actor="agent"):
        module.calls.append(("remove", project["id"], proofs_root, keys, cascade, draft, actor))
        return {"removed": list(keys), "rejected": [], "commit_sha": "def456"}

    module.load, module.upsert, module.remove = load, upsert, remove
    monkeypatch.setitem(sys.modules, "app.blueprint_store", module)
    monkeypatch.setattr(app_package, "blueprint_store", module, raising=False)
    return module


@pytest.fixture
def env(tmp_path, monkeypatch, fake_store):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)
    monkeypatch.setattr(blueprint_host, "load_config", lambda: config)
    return types.SimpleNamespace(tmp_path=tmp_path, config=config, store=fake_store,
                                 proofs_root=tmp_path / "workspace" / "proofs")


def _project_run(purpose="general"):
    project = store.get_or_create_project("demo", "Demo")
    session = store.create_session("Plan the blueprint", project_id=project["id"])
    run = store.create_run(session["id"], "gemini/test", None, 3, project_id=project["id"], purpose=purpose)
    return project, session, run


def _graph(statuses, verified=()):
    def build_graph(project, proofs_root):
        return {"nodes": [{"key": k, "status": s, "verified": k in verified, "file": None}
                          for k, s in statuses.items()], "edges": [], "warnings": []}
    return build_graph


# ── item 3: dispatch + idempotency ────────────────────────────────────────────

def test_read_shapes_nodes_with_derived_status(env, monkeypatch):
    project, _, run = _project_run()
    monkeypatch.setattr(graph, "build_graph", _graph({"main": "planned", "helper": "proved", "other": "ready"},
                                                     verified={"helper"}))
    result = blueprint_host.tool_request(run["id"], "1:0:a", "blueprint_read", {})
    assert env.store.calls == [("load", project["id"], env.proofs_root, False)]
    assert result["draft"] is False
    assert [n["key"] for n in result["nodes"]] == ["main", "helper", "other"]
    main = result["nodes"][0]
    # Node fields come from the blueprint (assigned file, even though nothing declares it yet).
    assert main == {"key": "main", "kind": "theorem", "statement": "The main result.",
                    "uses": ["helper", "other"], "lean": "Lea.Demo.main", "file": "Main.lean",
                    "status": "planned", "verified": False}
    assert result["nodes"][1]["verified"] is True
    assert result["ready"] == ["other"]
    assert result["blocked"] == [{"key": "main", "waits_on": ["other"]}]
    assert result["counts"] == {"total": 3, "proved": 1, "ready": 1, "stated": 0, "failed": 0, "planned": 1}

    subset = blueprint_host.tool_request(run["id"], "1:1:b", "blueprint_read", {"keys": ["main"]})
    assert [n["key"] for n in subset["nodes"]] == ["main"]
    assert subset["ready"] == [] and subset["blocked"] == [{"key": "main", "waits_on": ["other"]}]
    assert subset["counts"]["total"] == 3


def test_upsert_and_remove_dispatch_to_the_store(env):
    project, _, run = _project_run()
    nodes = [dict(NODES[0], status="proved"), NODES[1]]
    result = blueprint_host.tool_request(run["id"], "2:0:u", "blueprint_upsert", {"nodes": nodes})
    assert env.store.calls == [("upsert", project["id"], env.proofs_root, NODES, False, "agent")]
    assert [n["key"] for n in result["applied"]] == ["main", "helper"]
    assert result["draft"] is False

    removed = blueprint_host.tool_request(run["id"], "2:1:r", "blueprint_remove",
                                          {"keys": ["helper"], "cascade": True})
    assert env.store.calls[-1] == ("remove", project["id"], env.proofs_root, ["helper"], True, False, "agent")
    assert removed == {"removed": ["helper"], "rejected": [], "commit_sha": "def456"}


def test_same_invocation_key_replays_and_different_arguments_are_refused(env):
    _, _, run = _project_run()
    first = blueprint_host.tool_request(run["id"], "3:0:x", "blueprint_upsert", {"nodes": NODES})
    second = blueprint_host.tool_request(run["id"], "3:0:x", "blueprint_upsert", {"nodes": NODES})
    assert second == first
    assert [c[0] for c in env.store.calls] == ["upsert"]
    with pytest.raises(ValueError, match="reused for different arguments"):
        blueprint_host.tool_request(run["id"], "3:0:x", "blueprint_upsert", {"nodes": NODES[:1]})
    assert [c[0] for c in env.store.calls] == ["upsert"]


@pytest.mark.parametrize("tool,arguments", [
    ("blueprint_upsert", {"nodes": []}),
    ("blueprint_upsert", {"nodes": [{"key": f"n{i}"} for i in range(101)]}),
    ("blueprint_upsert", {"nodes": [{"kind": "lemma"}]}),
    ("blueprint_upsert", {"nodes": [{"key": "a", "uses": "b, c"}]}),
    ("blueprint_remove", {"keys": []}),
    ("blueprint_remove", {"keys": ["a"], "cascade": "yes"}),
    ("blueprint_read", {"keys": "main"}),
    ("blueprint_rename", {}),
])
def test_malformed_calls_are_refused_without_a_store_write(env, tool, arguments):
    _, _, run = _project_run()
    with pytest.raises(ValueError):
        blueprint_host.tool_request(run["id"], "4:0:m", tool, arguments)
    assert env.store.calls == []


def test_a_loose_run_has_no_blueprint(env):
    session = store.create_session("Loose")
    run = store.create_run(session["id"], "gemini/test", None, 3)
    with pytest.raises(ValueError, match="project-scoped run"):
        blueprint_host.tool_request(run["id"], "1:0:", "blueprint_read", {})


def test_project_delete_drops_recorded_invocations(env):
    project, _, run = _project_run()
    blueprint_host.tool_request(run["id"], "1:0:a", "blueprint_upsert", {"nodes": NODES})
    with db.connect() as conn:
        assert conn.execute("select count(*) from blueprint_invocations").fetchone()[0] == 1
    assert store.delete_project_cascade(project["id"])
    with db.connect() as conn:
        assert conn.execute("select count(*) from blueprint_invocations").fetchone()[0] == 0


# ── item 4: drafting runs ─────────────────────────────────────────────────────

def test_a_drafting_run_reads_and_writes_the_draft(env, monkeypatch):
    project, _, run = _project_run("blueprint_draft")
    monkeypatch.setattr(graph, "build_graph", lambda *a: pytest.fail("a draft has no derived graph"))
    read = blueprint_host.tool_request(run["id"], "1:0:r", "blueprint_read", {})
    assert env.store.calls[-1] == ("load", project["id"], env.proofs_root, True)
    assert read["draft"] is True
    assert {n["key"]: n["status"] for n in read["nodes"]} == {"goal": "planned", "step": "ready"}
    assert read["ready"] == ["step"] and read["blocked"] == [{"key": "goal", "waits_on": ["step"]}]
    blueprint_host.tool_request(run["id"], "1:1:u", "blueprint_upsert", {"nodes": NODES})
    blueprint_host.tool_request(run["id"], "1:2:x", "blueprint_remove", {"keys": ["helper"]})
    assert env.store.calls[1] == ("upsert", project["id"], env.proofs_root, NODES, True, "agent")
    assert env.store.calls[2] == ("remove", project["id"], env.proofs_root, ["helper"], False, True, "agent")


def test_a_drafting_run_config_cannot_write_or_check(env):
    project, _, run = _project_run("blueprint_draft")
    cfg = bridge._blueprint_tools_config(env.config, project, run)
    assert cfg.tools == ["read_file", "search_mathlib", *TOOL_NAMES]
    assert cfg.blueprint_tools is True and cfg.protected_paths == [LIVE, DRAFT]
    names = [schema["name"] for schema in build_toolset(cfg.tools, cfg.extra_tools)[0]]
    assert names == ["read_file", "search_mathlib", *TOOL_NAMES]
    assert not {"write_file", "edit_file", "bash", "lean_check"} & set(names)
    # An explicit allowlist from earlier builders is replaced, not extended.
    narrowed = bridge._blueprint_tools_config(replace(env.config, tools=["bash", "write_file"]), project, run)
    assert narrowed.tools == ["read_file", "search_mathlib", *TOOL_NAMES]


def test_an_ordinary_project_run_keeps_its_tools_and_adds_blueprint_tools(env):
    project, _, run = _project_run()
    cfg = bridge._blueprint_tools_config(env.config, project, run)
    assert cfg.tools is None and set(TOOL_NAMES) <= set(cfg.extra_tools)
    explicit = bridge._blueprint_tools_config(replace(env.config, tools=["write_file"]), project, run)
    assert explicit.tools == ["write_file", *TOOL_NAMES]
    assert bridge._blueprint_tools_config(env.config, None, run) is env.config


# ── item 5: the bridge ────────────────────────────────────────────────────────

def _bridge_run(env, monkeypatch, *, project=True, purpose="general", request=None):
    proofs_root = env.proofs_root
    if project:
        p = projects.provision_project("Demo", proofs_root)
        session = store.create_session("Plan", project_id=p["id"])
        run = store.create_run(session["id"], "gemini/test", None, 3, project_id=p["id"], purpose=purpose)
    else:
        p = None
        session = store.create_session("Plan")
        run = store.create_run(session["id"], "gemini/test", None, 3, purpose=purpose)
    queue: Queue = Queue()
    ctx = bridge.RunnerContext(session_id=session["id"], run_id=run["id"], task="Plan",
                               config=env.config, events=queue, purpose=purpose)
    seen: dict = {"acks": []}

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None,
             gate=None, **kwargs):
        seen["config"] = config
        yield TurnStarted(1)
        if request is not None:
            seen["acks"].append((yield request))
        yield Finished("assistant", "Planned.", 1, session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {"messages": []})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)
    return p, session, run, seen, _drain(queue)


def test_bridge_acknowledges_an_upsert_and_streams_blueprint_updated(env, monkeypatch):
    request = BlueprintRequested("blueprint_upsert", {"nodes": NODES}, "1:0:call")
    project, session, run, seen, frames = _bridge_run(env, monkeypatch, request=request)
    assert len(seen["acks"]) == 1
    ack = seen["acks"][0]
    assert isinstance(ack, BlueprintAck) and ack.error is None
    assert [n["key"] for n in ack.result["applied"]] == ["main", "helper"]
    assert env.store.calls[0][:2] == ("upsert", project["id"])
    updated = [f["payload"] for f in frames if f["type"] == "blueprint_updated"]
    assert updated == [{"project_id": project["id"], "draft": False, "applied": ["main", "helper"],
                        "removed": [], "run_id": run["id"], "session_id": session["id"]}]
    cfg = seen["config"]
    assert cfg.blueprint_tools is True
    assert cfg.protected_paths == [LIVE, DRAFT]
    assert set(TOOL_NAMES) <= set(cfg.extra_tools)


def test_bridge_routes_a_drafting_run_to_the_draft(env, monkeypatch):
    request = BlueprintRequested("blueprint_remove", {"keys": ["step"]}, "1:0:call")
    project, session, run, seen, frames = _bridge_run(env, monkeypatch, purpose="blueprint_draft",
                                                       request=request)
    assert seen["acks"][0].result["removed"] == ["step"]
    assert env.store.calls == [("remove", project["id"], env.proofs_root, ["step"], False, True, "agent")]
    assert seen["config"].tools == ["read_file", "search_mathlib", *TOOL_NAMES]
    updated = [f["payload"] for f in frames if f["type"] == "blueprint_updated"]
    assert updated == [{"project_id": project["id"], "draft": True, "applied": [], "removed": ["step"],
                        "run_id": run["id"], "session_id": session["id"]}]


def test_bridge_turns_a_host_refusal_into_an_error_ack(env, monkeypatch):
    request = BlueprintRequested("blueprint_upsert", {"nodes": []}, "1:0:call")
    _, _, _, seen, frames = _bridge_run(env, monkeypatch, request=request)
    ack = seen["acks"][0]
    assert isinstance(ack, BlueprintAck) and ack.result is None and "1 to 100 nodes" in ack.error
    assert not [f for f in frames if f["type"] == "blueprint_updated"]
    assert env.store.calls == []


def test_a_loose_run_gets_no_blueprint_tools(env, monkeypatch):
    request = BlueprintRequested("blueprint_read", {}, "1:0:call")
    _, _, _, seen, frames = _bridge_run(env, monkeypatch, project=False, request=request)
    cfg = seen["config"]
    assert cfg.blueprint_tools is False
    assert cfg.protected_paths == []
    assert not set(TOOL_NAMES) & set(cfg.extra_tools)
    assert not set(TOOL_NAMES) & set(cfg.tools or [])
    assert "project-scoped run" in seen["acks"][0].error
    assert not [f for f in frames if f["type"] == "blueprint_updated"]


# ── item 6: run purpose ───────────────────────────────────────────────────────

@pytest.fixture
def routes_env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    from app import runbroker
    monkeypatch.setattr(runbroker, "_brokers", {})
    monkeypatch.setattr(runregistry, "registry", RunRegistry(max_concurrent=1))
    monkeypatch.setattr(bridge, "enqueue_run", lambda run_id: None)
    config = LeaConfig(model="m", max_turns=3, lea_root=tmp_path, max_spend_usd=None)

    def module(name):
        mod = importlib.import_module(name)
        monkeypatch.setattr(mod, "load_config", lambda: config)
        monkeypatch.setattr(mod.runtime, "accepting_work", lambda: True)
        return mod
    return module


@pytest.mark.parametrize("module_name", ["app.routes.runs", "app.api_routes.runs"])
def test_run_creation_accepts_blueprint_draft_and_rejects_unknown_purposes(routes_env, module_name):
    runs = routes_env(module_name)
    admitted = runs.create_run(runs.RunRequest(message="Draft the blueprint", project_slug="demo",
                                               purpose="blueprint_draft"))
    assert store.get_run(admitted["run_id"])["purpose"] == "blueprint_draft"
    with pytest.raises(HTTPException) as unknown:
        runs.create_run(runs.RunRequest(message="Draft", project_slug="demo", purpose="blueprint_freestyle"))
    assert unknown.value.status_code == 422
    with pytest.raises(HTTPException) as loose:
        runs.create_run(runs.RunRequest(message="Draft", purpose="blueprint_draft"))
    assert loose.value.status_code == 422
