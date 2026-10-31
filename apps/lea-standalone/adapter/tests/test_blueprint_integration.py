"""Blueprint tools end to end, through the bridge, with the real modules.

docs/PLAN-blueprint-tools.md §2.2–§2.6, §2.10. P2's and P3's own tests replace
``blueprint_store`` with fakes; here nothing is stubbed except the model and Lean:
a fake ``run_events`` yields the events a real loop would (``BlueprintRequested``
answered by ``BlueprintAck`` through ``gen.send``, file writes, a passing check),
and every assertion reads what the real ``blueprint_store`` wrote to disk and git,
what the real ``blueprint_host`` acknowledged, what the real
``blueprint_reconcile`` streamed, and what the real ``graph.build_graph`` derives.

The host resolves the proofs root from ``config.load_config()`` (it is not handed
the run's config), so each test points ``LEA_CONFIG_FILE`` at a scratch config whose
``lea_root`` is ``tmp_path`` — the same root the bridge derives from the run's
``LeaConfig``. Nothing is written outside ``tmp_path``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from queue import Queue

import pytest

from lea.blueprint_tools import BlueprintAck, BlueprintRequested
from lea.interface import CheckResult, FileChanged, Finished, ToolCalled, TurnStarted
from lea.providers import Usage

from app import blueprint, blueprint_store, bridge, db, graph, projects, store
from app.config import LeaConfig
from app.gitstore import GitStore

# This module mocks compiler behavior; bind those mock verdicts to their inputs.
pytestmark = pytest.mark.usefixtures("certified_unit_inputs")

LIVE, DRAFT = blueprint_store.LIVE, blueprint_store.DRAFT
PLAN = {"nodes": [
    {"key": "helper", "kind": "lemma", "statement": "A helper."},
    {"key": "main", "kind": "theorem", "statement": "The goal.", "uses": ["helper"]},
]}


# ── helpers ───────────────────────────────────────────────────────────────────


def _env(tmp_path, monkeypatch, title="Demo"):
    """A scratch DB, a provisioned project under ``tmp_path/workspace/proofs`` and the
    run config, both the bridge's and the host's ``lea_root`` pointed at ``tmp_path``."""
    config_file = tmp_path / "lea.local.toml"
    config_file.write_text(f'lea_root = "{tmp_path.as_posix()}"\n')
    monkeypatch.setenv("LEA_CONFIG_FILE", str(config_file))
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    proofs_root = tmp_path / "workspace" / "proofs"
    project = projects.provision_project(title, proofs_root)
    repo = projects.project_repo_dir(project, proofs_root)
    config = LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path)
    return project, proofs_root, repo, config


def _run(project, config, fake, monkeypatch, *, task="Plan", purpose="general", blueprint_node=None):
    """Create a project session + run (with ``purpose``) and drive it with ``fake``.
    Returns ``(session, run, streamed events)``."""
    session = store.create_session(task, project_id=project["id"], blueprint_node=blueprint_node)
    run = store.create_run(session["id"], "gemini/test", None, 3, project_id=project["id"], purpose=purpose)
    queue: Queue = Queue()
    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(bridge.RunnerContext(session_id=session["id"], run_id=run["id"], task=task,
                                        config=config, events=queue, purpose=purpose))
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return session, run, events


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def _commits(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD"))


def _log(repo: Path, path: str) -> list[str]:
    return [line for line in _git(repo, "log", "--oneline", "--", path).splitlines() if line.strip()]


def _nodes_on_disk(repo: Path, rel: str = LIVE) -> dict[str, dict]:
    parsed = blueprint.parse((repo / rel).read_text())
    assert parsed["warnings"] == []
    return {node["key"]: node for node in parsed["nodes"]}


def _statuses(project, proofs_root) -> dict[str, str]:
    return {node["key"]: node["status"] for node in graph.build_graph(project, proofs_root)["nodes"]}


def _payloads(events: list[dict], kind: str) -> list[dict]:
    return [event["payload"] for event in events if event["type"] == kind]


def _finished(session_id, text="Done."):
    return Finished("completed", text, 1, session_id, "gemini/test",
                    Usage(input_tokens=1, output_tokens=1), 0.0, {})


# ── 1–3: plan, read, prove a leaf, unlock its dependent ────────────────────────


def test_upsert_read_and_a_passing_check_drive_the_real_blueprint(tmp_path, monkeypatch):
    project, proofs_root, repo, config = _env(tmp_path, monkeypatch)
    ns = project["namespace"]
    assert ns == "Lea.Demo"
    head_before = _git(repo, "rev-parse", "HEAD")
    commits_before = _commits(repo)
    seen: dict = {}

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None, should_stop=None,
             gate=None, **kwargs):
        seen["config"] = config
        yield TurnStarted(1)
        # 1. Plan two nodes in one call.
        seen["upsert"] = yield BlueprintRequested("blueprint_upsert", PLAN, "1:0:x")
        seen["after_upsert"] = {"head": _git(repo, "rev-parse", "HEAD"), "commits": _commits(repo),
                                "text": (repo / LIVE).read_text()}
        # 2. Read it back.
        seen["read"] = yield BlueprintRequested("blueprint_read", {}, "1:1:x")
        seen["graph_at_read"] = _statuses(project, proofs_root)
        # 3. Prove the leaf under the assigned name, in the assigned file.
        proof = Path(working_dir) / "Helper.lean"
        proof.write_text(f"import Mathlib\n\nnamespace {ns}\n\nlemma helper : True := trivial\n\nend {ns}\n")
        yield ToolCalled("write_file", {"path": str(proof)})
        yield FileChanged(str(proof))
        yield ToolCalled("lean_check", {"path": str(proof)})
        yield CheckResult(str(proof), "ok", None)
        yield _finished(session_id, "Proved helper.")

    session, run, events = _run(project, config, fake, monkeypatch)
    assert [e["payload"].get("message") for e in events if e["type"] == "run_error"] == []

    # The run was given the blueprint tools and the protected paths (§2.3).
    assert seen["config"].blueprint_tools is True
    assert seen["config"].protected_paths == [LIVE, DRAFT]

    # ── 1. upsert ─────────────────────────────────────────────────────────────
    ack = seen["upsert"]
    assert isinstance(ack, BlueprintAck) and ack.error is None
    result = ack.result
    assert result["rejected"] == [] and result["draft"] is False
    applied = {entry["key"]: entry for entry in result["applied"]}
    assert list(applied) == ["helper", "main"]
    assert (applied["helper"]["lean"], applied["helper"]["file"]) == (f"{ns}.helper", "Helper.lean")
    assert (applied["main"]["lean"], applied["main"]["file"]) == (f"{ns}.main", "Main.lean")
    assert applied["main"]["uses"] == ["helper"]
    assert result["ready"] == ["helper"]
    assert result["counts"] == {"total": 2, "proved": 0, "ready": 1, "stated": 0, "failed": 0, "planned": 1}

    # On disk: canonical, both nodes with their assigned names.
    on_disk = blueprint.parse(seen["after_upsert"]["text"])
    assert on_disk["warnings"] == []
    nodes = {node["key"]: node for node in on_disk["nodes"]}
    assert list(nodes) == ["helper", "main"]
    assert (nodes["helper"]["lean"], nodes["helper"]["file"]) == (f"{ns}.helper", "Helper.lean")
    assert (nodes["main"]["lean"], nodes["main"]["file"]) == (f"{ns}.main", "Main.lean")
    assert nodes["main"]["uses"] == ["helper"] and nodes["helper"]["uses"] == []
    assert nodes["helper"]["statement"] == "A helper." and nodes["main"]["statement"] == "The goal."
    assert blueprint.render(on_disk) == seen["after_upsert"]["text"]

    # One new commit, the one the ack names, touching only the live blueprint.
    assert seen["after_upsert"]["commits"] == commits_before + 1
    assert seen["after_upsert"]["head"] != head_before
    assert result["commit_sha"] == seen["after_upsert"]["head"]
    assert _git(repo, "show", "--name-only", "--format=", result["commit_sha"]).splitlines() == [LIVE]

    # The stream carried one blueprint_updated for the write (the read emits none).
    assert _payloads(events, "blueprint_updated") == [{
        "project_id": project["id"], "draft": False, "applied": ["helper", "main"], "removed": [],
        "run_id": run["id"], "session_id": session["id"]}]

    # ── 2. read ───────────────────────────────────────────────────────────────
    read = seen["read"]
    assert isinstance(read, BlueprintAck) and read.error is None
    read = read.result
    assert read["draft"] is False
    statuses = {node["key"]: node["status"] for node in read["nodes"]}
    assert statuses == {"helper": "ready", "main": "planned"}
    assert statuses == seen["graph_at_read"]  # the host reports graph.build_graph's derivation
    assert [(n["key"], n["lean"], n["file"]) for n in read["nodes"]] == [
        ("helper", f"{ns}.helper", "Helper.lean"), ("main", f"{ns}.main", "Main.lean")]
    assert read["ready"] == ["helper"]
    assert read["blocked"] == [{"key": "main", "waits_on": ["helper"]}]
    assert read["counts"]["total"] == 2

    # ── 3. the passing check ──────────────────────────────────────────────────
    changed = _payloads(events, "blueprint_node_changed")
    assert [(c["key"], c["to"]) for c in changed] == [("helper", "proved")]
    assert changed[0] == {"project_id": project["id"], "key": "helper", "from": "ready", "to": "proved",
                          "lean": f"{ns}.helper", "file": "Helper.lean",
                          "run_id": run["id"], "session_id": session["id"]}
    assert _payloads(events, "blueprint_unlocked") == [{
        "project_id": project["id"], "keys": ["main"], "cause": "helper",
        "run_id": run["id"], "session_id": session["id"]}]
    types_ = [event["type"] for event in events]
    assert types_.index("blueprint_updated") < types_.index("blueprint_node_changed") \
        < types_.index("blueprint_unlocked") < types_.index("done")

    assert _statuses(project, proofs_root) == {"helper": "proved", "main": "ready"}
    # The checked file was committed to the project repo exactly once.
    helper_log = _log(repo, "Helper.lean")
    assert len(helper_log) == 1 and "agent lean_check ok: Helper.lean" in helper_log[0]
    # Names were assigned up front, so reconciliation had nothing to bind: the live
    # blueprint is exactly what the upsert wrote.
    assert (repo / LIVE).read_text() == seen["after_upsert"]["text"]
    assert _git(repo, "status", "--porcelain", "--", LIVE) == ""


# ── 4: a session bound to a node names it before the context is composed ──────


def test_bound_session_names_its_node_through_the_real_store(tmp_path, monkeypatch):
    project, proofs_root, repo, config = _env(tmp_path, monkeypatch)
    ns = project["namespace"]
    # A human-written blueprint (the "Edit markdown" path): no lean/file lines yet.
    canonical, warnings = blueprint_store.normalize(
        "# Blueprint — Demo\n\n"
        "## helper\n- kind: lemma\n\nA helper.\n\n"
        "## main\n- kind: theorem\n- uses: helper\n\nThe goal.\n")
    assert warnings == []
    (repo / LIVE).write_text(canonical)
    GitStore(proofs_root).commit_all(repo, "human blueprint", paths=[LIVE])
    assert _nodes_on_disk(repo)["main"]["lean"] is None
    commits_before = _commits(repo)
    captured: dict = {}

    def fake(config, messages, *, session_id=None, **kwargs):
        captured["messages"] = list(messages)
        captured["blueprint"] = (repo / LIVE).read_text()
        yield TurnStarted(1)
        yield _finished(session_id, "Planned.")

    session, _, events = _run(project, config, fake, monkeypatch, task="Prove main", blueprint_node="main")
    assert session["blueprint_node"] == "main"
    assert [e["payload"].get("message") for e in events if e["type"] == "run_error"] == []

    nodes = _nodes_on_disk(repo)
    assert (nodes["main"]["lean"], nodes["main"]["file"]) == (f"{ns}.main", "Main.lean")
    assert nodes["main"]["uses"] == ["helper"] and nodes["main"]["statement"] == "The goal."
    # Only the bound node is named.
    assert (nodes["helper"]["lean"], nodes["helper"]["file"]) == (None, None)
    # Named before the run started, in one system commit.
    assert captured["blueprint"] == (repo / LIVE).read_text()
    assert _commits(repo) == commits_before + 1
    assert _git(repo, "log", "-1", "--format=%s") == f"blueprint: bind main → {ns}.main"

    context = captured["messages"][0]["content"]
    assert context.startswith(projects.CONTEXT_MARKER)
    assert "This session proves `main` (theorem): The goal." in context
    assert f"Declare it as `{ns}.main` in `Main.lean`" in context
    assert "## continuous_sq" not in context
    assert _statuses(project, proofs_root) == {"helper": "ready", "main": "planned"}


# ── 5: a drafting run writes the draft; accepting moves nodes into the live file ─


def test_drafting_run_writes_the_draft_and_accept_moves_it_live(tmp_path, monkeypatch):
    project, proofs_root, repo, config = _env(tmp_path, monkeypatch)
    ns = project["namespace"]
    live_before = (repo / LIVE).read_text()
    seen: dict = {}

    def fake(config, messages, *, session_id=None, **kwargs):
        seen["config"] = config
        yield TurnStarted(1)
        seen["upsert"] = yield BlueprintRequested("blueprint_upsert", PLAN, "1:0:x")
        seen["read"] = yield BlueprintRequested("blueprint_read", {}, "1:1:x")
        yield _finished(session_id, "Drafted.")

    session, run, events = _run(project, config, fake, monkeypatch, task="Draft the blueprint",
                                purpose="blueprint_draft")
    assert store.get_run(run["id"])["purpose"] == "blueprint_draft"
    assert seen["config"].tools == ["read_file", "search_mathlib", "blueprint_read", "blueprint_upsert",
                                    "blueprint_remove"]

    ack = seen["upsert"]
    assert isinstance(ack, BlueprintAck) and ack.error is None
    assert ack.result["draft"] is True and ack.result["rejected"] == []
    assert {e["key"]: (e["lean"], e["file"], e["status"]) for e in ack.result["applied"]} == {
        "helper": (f"{ns}.helper", "Helper.lean", "ready"), "main": (f"{ns}.main", "Main.lean", "planned")}
    assert ack.result["ready"] == ["helper"]
    read = seen["read"].result
    assert read["draft"] is True
    assert {n["key"]: n["status"] for n in read["nodes"]} == {"helper": "ready", "main": "planned"}

    # The draft holds the plan; the live blueprint is untouched.
    draft_nodes = _nodes_on_disk(repo, DRAFT)
    assert list(draft_nodes) == ["helper", "main"]
    assert (draft_nodes["main"]["lean"], draft_nodes["main"]["file"]) == (f"{ns}.main", "Main.lean")
    assert (repo / LIVE).read_text() == live_before
    assert blueprint.parse(live_before)["nodes"] == []
    assert _statuses(project, proofs_root) == {}
    assert _git(repo, "status", "--porcelain", "--", DRAFT) == ""  # committed
    assert _payloads(events, "blueprint_updated") == [{
        "project_id": project["id"], "draft": True, "applied": ["helper", "main"], "removed": [],
        "run_id": run["id"], "session_id": session["id"]}]

    # Accept only `helper`: it moves live with its assigned names; the draft is gone.
    commits_before = _commits(repo)
    accepted = blueprint_store.accept_draft(project, proofs_root, ["helper"])
    assert accepted["rejected"] == []
    assert [(e["key"], e["lean"], e["file"]) for e in accepted["applied"]] == [
        ("helper", f"{ns}.helper", "Helper.lean")]
    assert not (repo / DRAFT).exists()
    assert not blueprint_store.exists_draft(project, proofs_root)
    live = _nodes_on_disk(repo)
    assert list(live) == ["helper"]
    assert (live["helper"]["lean"], live["helper"]["file"], live["helper"]["statement"]) == (
        f"{ns}.helper", "Helper.lean", "A helper.")
    assert _commits(repo) == commits_before + 1
    assert accepted["commit_sha"] == _git(repo, "rev-parse", "HEAD")
    assert sorted(_git(repo, "show", "--name-only", "--format=", accepted["commit_sha"]).splitlines()) == [
        DRAFT, LIVE]
    assert _git(repo, "status", "--porcelain", "--", ".lea") == ""
    assert _statuses(project, proofs_root) == {"helper": "ready"}
