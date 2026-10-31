"""P3 reconcile (docs/PLAN-blueprint-tools.md §2.6, §2.7, §2.10, §3 "P3 reconcile").

Scratch project repos with real ``.lean`` files and seeded code steps, as in
``test_graph.py``; the bridge is driven by a fake ``run_events`` as in
``test_bridge.py``. ``blueprint_store`` (P1) is replaced by a fake that applies a
binding to the markdown file itself, so every graph the assertions read is derived
by the real ``graph.build_graph`` from the file on disk.
"""

from __future__ import annotations

import re
import subprocess
import sys
import types
from pathlib import Path
from queue import Queue

import pytest

from lea.interface import CheckResult, FileChanged, Finished, ToolCalled, TurnStarted
from lea.providers import Usage

import app as app_package
from app import blueprint_reconcile, bridge, db, graph, projects, store
from app.config import LeaConfig
from app.gitstore import GitStore

LIVE = ".lea/blueprint.md"
LEA_MD = Path(__file__).resolve().parents[2] / "prover" / "workspace" / "lea.md"
_HEADER = re.compile(r"^-\s*(\w+)\s*:\s*(.*?)\s*$")


# ── A fake blueprint_store (P1 contract §2.2, the parts reconciliation uses) ─────


def _pascal(key: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in key.split("_") if part)


def _value(raw: str | None) -> str | None:
    if not raw:
        return None
    span = re.search(r"`([^`]*)`", raw)
    return (span.group(1) if span else raw.split()[0]).strip() or None


def _headers(text: str, key: str) -> tuple[int, int, dict[str, str]] | None:
    """(heading line, end of header block, {name: raw value}) of node ``key``."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == f"## {key}":
            j, found = i + 1, {}
            while j < len(lines) and _HEADER.match(lines[j].strip()):
                match = _HEADER.match(lines[j].strip())
                found[match.group(1).lower()] = match.group(2)
                j += 1
            return i, j, found
    return None


class FakeBlueprintStore:
    """Applies ``{"key", "lean"?, "file"?}`` to the node's header lines in the live
    file, assigning ``<namespace>.<key>`` / ``<Pascal>.lean`` when absent, and
    commits once per call that changed the file."""

    LIVE = LIVE
    DRAFT = ".lea/blueprint.draft.md"

    def __init__(self):
        self.calls: list[dict] = []

    @staticmethod
    def assign_names(namespace: str, key: str) -> tuple[str, str]:
        return f"{namespace}.{key}", f"{_pascal(key)}.lean"

    def upsert(self, project, proofs_root, nodes, *, draft=False, actor="agent"):
        self.calls.append({"nodes": [dict(n) for n in nodes], "draft": draft, "actor": actor})
        repo = projects.project_repo_dir(project, Path(proofs_root))
        path = repo / LIVE
        original = text = path.read_text()
        applied, rejected = [], []
        for node in nodes:
            key = node["key"]
            located = _headers(text, key)
            if located is None:
                rejected.append({"key": key, "code": "missing_kind", "message": "new node without kind"})
                continue
            start, end, found = located
            lean_name, file_name = self.assign_names(project["namespace"], key)
            found["lean"] = f"`{node.get('lean') or _value(found.get('lean')) or lean_name}`"
            found["file"] = f"`{node.get('file') or _value(found.get('file')) or file_name}`"
            order = ["kind", "lean", "file", "uses"]
            block = [f"- {name}: {found[name]}" for name in order if name in found]
            lines = text.split("\n")
            text = "\n".join(lines[: start + 1] + block + lines[end:])
            applied.append({"key": key, "lean": _value(found["lean"]), "file": _value(found["file"])})
        sha = None
        if text != original:
            path.write_text(text)
            sha = GitStore(Path(proofs_root)).commit_all(
                repo, f"blueprint: {actor} upsert " + ", ".join(a["key"] for a in applied), paths=[LIVE])
        return {"applied": applied, "rejected": rejected, "ready": [], "counts": {},
                "draft": draft, "commit_sha": sha}


@pytest.fixture
def fake_store(monkeypatch):
    # Modules that import blueprint_store at top level must bind the REAL module
    # before the fake goes in; otherwise a first import during this fixture keeps
    # the fake for the rest of the session (test_blueprint_store's route tests).
    import app.routes.projects  # noqa: F401
    import app.api_routes.projects  # noqa: F401
    fake = FakeBlueprintStore()
    module = types.ModuleType("app.blueprint_store")
    for name in ("LIVE", "DRAFT", "assign_names", "upsert"):
        setattr(module, name, getattr(fake, name))
    monkeypatch.setitem(sys.modules, "app.blueprint_store", module)
    monkeypatch.setattr(app_package, "blueprint_store", module, raising=False)

    # Contract shim for graph §2.8 while P1 is in flight: a node carries its planned
    # `file:` line as `planned_file`. Only fills the field when the real graph lacks
    # it, so once P1 lands this is a no-op and the real derivation is what's tested.
    real_build = graph.build_graph

    def build_graph(project, proofs_root):
        result = real_build(project, proofs_root)
        path = projects.project_repo_dir(project, Path(proofs_root)) / LIVE
        text = path.read_text() if path.is_file() else ""
        for node in result["nodes"]:
            if "planned_file" not in node:
                located = _headers(text, node["key"])
                node["planned_file"] = _value(located[2].get("file")) if located else None
        return result

    monkeypatch.setattr(graph, "build_graph", build_graph)
    return fake


# ── Scratch project helpers ──────────────────────────────────────────────────────


def _project(tmp_path, monkeypatch, blueprint_md: str):
    """A Demo project (namespace Lea.Demo) under ``tmp_path/workspace/proofs`` — the
    proofs root the bridge derives from ``lea_root=tmp_path``."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    proofs = tmp_path / "workspace" / "proofs"
    project = projects.provision_project("Demo", proofs)
    repo = projects.project_repo_dir(project, proofs)
    (repo / LIVE).write_text(blueprint_md)
    GitStore(proofs).commit_all(repo, "test blueprint", paths=[LIVE])
    return project, proofs, repo


def _write_lean(repo: Path, name: str, body: str, namespace: str = "Lea.Demo") -> Path:
    path = repo / name
    path.write_text(f"import Mathlib\n\nnamespace {namespace}\n\n{body}\n\nend {namespace}\n")
    return path


def _log(repo: Path, path: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "log", "--oneline", "--", path],
                         capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def _nodes(project, proofs) -> dict[str, dict]:
    return {node["key"]: node for node in graph.build_graph(project, proofs)["nodes"]}


def _drain(queue: Queue) -> list[dict]:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


HELPER_MAIN = (
    "# Blueprint — Demo\n\n"
    "## helper\n- kind: lemma\n\nA helper with no dependencies.\n\n"
    "## main\n- kind: theorem\n- uses: helper\n\nThe main theorem.\n"
)


# ── 1. diff_graph ────────────────────────────────────────────────────────────────


def test_diff_graph_reports_the_proved_node_and_unlocks_its_dependent():
    before = [
        {"key": "a", "status": "planned", "lean": None, "file": None, "uses": []},
        {"key": "b", "status": "planned", "lean": None, "file": None, "uses": ["a"]},
    ]
    after = [
        {"key": "a", "status": "proved", "lean": "Lea.Demo.a", "resolved_lean": "Lea.Demo.a",
         "file": "A.lean", "uses": []},
        {"key": "b", "status": "ready", "lean": None, "file": None, "uses": ["a"]},
    ]
    diff = blueprint_reconcile.diff_graph(before, after)
    assert diff["changed"] == [
        {"key": "a", "from": "planned", "to": "proved", "lean": "Lea.Demo.a", "file": "A.lean"}]
    assert diff["unlocked"] == ["b"]
    assert blueprint_reconcile.diff_graph(after, after) == {"changed": [], "unlocked": []}
    assert blueprint_reconcile.diff_graph(before, before) == {"changed": [], "unlocked": []}

    events = blueprint_reconcile.stream_events(
        "p1", {"bound": [], **diff}, run_id="r1", session_id="s1", changed_path="A.lean")
    assert events == [
        ("blueprint_node_changed", {"project_id": "p1", "key": "a", "from": "planned", "to": "proved",
                                    "lean": "Lea.Demo.a", "file": "A.lean", "run_id": "r1",
                                    "session_id": "s1"}),
        ("blueprint_unlocked", {"project_id": "p1", "keys": ["b"], "cause": "a", "run_id": "r1",
                                "session_id": "s1"}),
    ]
    # An unlock nothing in this diff proved (e.g. a dependency dropped) has no cause.
    assert blueprint_reconcile.stream_events(
        "p1", {"changed": [], "unlocked": ["b"]}, run_id="r1", session_id="s1"
    )[-1][1]["cause"] is None


# ── 2. reconcile ─────────────────────────────────────────────────────────────────


def test_reconcile_binds_an_unresolved_node_to_its_declaration_in_one_commit(
        tmp_path, monkeypatch, fake_store):
    project, proofs, repo = _project(tmp_path, monkeypatch, HELPER_MAIN)
    _write_lean(repo, "Helper.lean", "lemma helper : True := trivial")
    commits = len(_log(repo, LIVE))
    assert _nodes(project, proofs)["helper"]["resolved_lean"] is None

    result = blueprint_reconcile.reconcile(project, proofs, changed_path="Helper.lean")

    binding = {"key": "helper", "lean": "Lea.Demo.helper", "file": "Helper.lean"}
    assert result["bound"] == [binding]
    assert fake_store.calls == [{"nodes": [binding], "draft": False, "actor": "system"}]
    assert len(_log(repo, LIVE)) == commits + 1
    helper = _nodes(project, proofs)["helper"]
    assert helper["resolved_lean"] == "Lea.Demo.helper"
    assert helper["file"] == "Helper.lean"
    # `main` has no declaration yet: nothing to bind it to.
    assert _nodes(project, proofs)["main"]["resolved_lean"] is None

    # Bound nodes are left alone: a second pass binds nothing and writes nothing.
    assert blueprint_reconcile.reconcile(project, proofs)["bound"] == []
    assert len(fake_store.calls) == 1
    assert len(_log(repo, LIVE)) == commits + 1


def test_reconcile_never_binds_an_ambiguous_short_name(tmp_path, monkeypatch, fake_store):
    project, proofs, repo = _project(tmp_path, monkeypatch, HELPER_MAIN)
    _write_lean(repo, "A.lean", "lemma helper : True := trivial", namespace="Foo")
    _write_lean(repo, "B.lean", "lemma helper : True := trivial", namespace="Bar")
    text = (repo / LIVE).read_text()

    result = blueprint_reconcile.reconcile(project, proofs)

    assert result["bound"] == []
    assert fake_store.calls == []
    assert (repo / LIVE).read_text() == text

    # The exact `<namespace>.<key>` name wins over any number of short-name matches.
    _write_lean(repo, "Helper.lean", "lemma helper : True := trivial")
    assert blueprint_reconcile.reconcile(project, proofs)["bound"] == [
        {"key": "helper", "lean": "Lea.Demo.helper", "file": "Helper.lean"}]


# ── 3. Bridge: a passing check commits, reconciles and streams the unlock ───────


def _run_helper_check(tmp_path, monkeypatch, project):
    """Drive one project run that writes ``Helper.lean`` and checks it ok; returns
    the session, the run and the streamed events."""
    session = store.create_session("prove helper", project_id=project["id"])
    run = store.create_run(session["id"], "gemini/test", None, 3, project_id=project["id"])
    queue: Queue = Queue()
    ctx = bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"], task="prove helper",
        config=LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path), events=queue,
    )

    def fake(config, messages, *, namespace=None, session_id=None, working_dir=None,
             should_stop=None, gate=None, **kwargs):
        proof = str(_write_lean(Path(working_dir), "Helper.lean", "lemma helper : True := trivial"))
        yield TurnStarted(1)
        yield ToolCalled("write_file", {"path": proof})
        yield FileChanged(proof)
        yield ToolCalled("lean_check", {"path": proof})
        yield CheckResult(proof, "ok", None)
        yield Finished("completed", "Proved helper.", 1, session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(ctx)
    return session, run, _drain(queue)


def test_passing_check_commits_the_file_and_streams_the_unlock(tmp_path, monkeypatch, fake_store):
    project, proofs, repo = _project(tmp_path, monkeypatch, HELPER_MAIN)
    session, run, events = _run_helper_check(tmp_path, monkeypatch, project)

    # The checked file is committed in the project repo, once.
    assert len(_log(repo, "Helper.lean")) == 1
    assert "agent lean_check ok: Helper.lean" in _log(repo, "Helper.lean")[0]
    # Reconciliation bound `helper` to the declaration it found.
    assert fake_store.calls == [{"nodes": [{"key": "helper", "lean": "Lea.Demo.helper",
                                            "file": "Helper.lean"}],
                                 "draft": False, "actor": "system"}]
    nodes = _nodes(project, proofs)
    assert nodes["helper"]["status"] == "proved"
    assert nodes["main"]["status"] == "ready"

    changed = [e["payload"] for e in events if e["type"] == "blueprint_node_changed"]
    assert [c["key"] for c in changed] == ["helper"]
    helper_change = changed[0]
    assert helper_change["from"] in {"planned", "ready"}  # a leaf is ready once P1 lands
    assert helper_change == {
        "project_id": project["id"], "key": "helper", "from": helper_change["from"],
        "to": "proved", "lean": "Lea.Demo.helper", "file": "Helper.lean",
        "run_id": run["id"], "session_id": session["id"],
    }
    unlocked = [e["payload"] for e in events if e["type"] == "blueprint_unlocked"]
    assert unlocked == [{"project_id": project["id"], "keys": ["main"], "cause": "helper",
                         "run_id": run["id"], "session_id": session["id"]}]
    types_ = [e["type"] for e in events]
    assert types_.index("blueprint_unlocked") < types_.index("done")


def test_a_node_named_before_its_check_still_streams_the_change(tmp_path, monkeypatch, fake_store):
    # The usual case for a bound session: `helper` already names its declaration, so
    # reconciliation binds nothing. The change must still be reported — it is the
    # check that proves the node, and the bridge diffs against the graph as it was
    # before the verdict was recorded.
    project, proofs, repo = _project(tmp_path, monkeypatch, HELPER_MAIN.replace(
        "## helper\n- kind: lemma\n", "## helper\n- kind: lemma\n- lean: `Lea.Demo.helper`\n"
        "- file: `Helper.lean`\n"))
    session, run, events = _run_helper_check(tmp_path, monkeypatch, project)

    assert fake_store.calls == []
    changed = [e["payload"] for e in events if e["type"] == "blueprint_node_changed"]
    assert [(c["key"], c["to"]) for c in changed] == [("helper", "proved")]
    unlocked = [e["payload"] for e in events if e["type"] == "blueprint_unlocked"]
    assert [(u["keys"], u["cause"]) for u in unlocked] == [(["main"], "helper")]

    # A run whose check does not pass streams no blueprint events and commits nothing.
    _, _, failing = _run_failing_check(tmp_path, monkeypatch, project)
    assert not [e for e in failing if e["type"].startswith("blueprint_")]


def _run_failing_check(tmp_path, monkeypatch, project):
    session = store.create_session("break main", project_id=project["id"])
    run = store.create_run(session["id"], "gemini/test", None, 3, project_id=project["id"])
    queue: Queue = Queue()

    def fake(config, messages, *, session_id=None, working_dir=None, **kwargs):
        proof = str(_write_lean(Path(working_dir), "Main.lean", "theorem main : True := by exact?"))
        yield TurnStarted(1)
        yield FileChanged(proof)
        yield CheckResult(proof, "error", "unsolved goals")
        yield Finished("completed", "Stuck.", 1, session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"], task="break main",
        config=LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path), events=queue,
    ))
    repo = projects.project_repo_dir(project, tmp_path / "workspace" / "proofs")
    assert _log(repo, "Main.lean") == []
    return session, run, _drain(queue)


# ── 4. Session binding ───────────────────────────────────────────────────────────


def test_session_binding_round_trips_and_names_the_node_before_the_run(
        tmp_path, monkeypatch, fake_store):
    project, proofs, repo = _project(tmp_path, monkeypatch, HELPER_MAIN)
    pid = project["id"]

    session = store.create_session("prove main", project_id=pid, blueprint_node="main")
    assert session["blueprint_node"] == "main"
    assert [s["blueprint_node"] for s in store.list_project_sessions(pid)] == ["main"]
    assert store.create_session("loose")["blueprint_node"] is None

    # POST /api/projects/{id}/sessions accepts it, in both route modules.
    from app.api_routes import projects as api_projects
    from app.routes import projects as projects_route
    for module in (projects_route, api_projects):
        assert module.SessionCreate().blueprint_node is None
        made = module.create_session_in_project(
            pid, module.SessionCreate(title="node session", blueprint_node="helper"))
        assert made["blueprint_node"] == "helper" and made["project_id"] == pid
    by_id = {s["id"]: s["blueprint_node"] for s in store.list_project_sessions(pid)}
    assert sorted(by_id.values()) == ["helper", "helper", "main"]

    # A run in the bound session names `main` before the context is composed.
    run = store.create_run(session["id"], "gemini/test", None, 3, project_id=pid)
    captured: dict = {}

    def fake(config, messages, *, session_id=None, **kwargs):
        captured["messages"] = list(messages)
        captured["blueprint"] = (repo / LIVE).read_text()
        yield TurnStarted(1)
        yield Finished("completed", "Planned.", 1, session_id, "gemini/test",
                       Usage(input_tokens=1, output_tokens=1), 0.0, {})

    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(bridge.RunnerContext(
        session_id=session["id"], run_id=run["id"], task="prove main",
        config=LeaConfig(model="gemini/test", max_turns=3, lea_root=tmp_path), events=Queue(),
    ))

    assert fake_store.calls == [{"nodes": [{"key": "main"}], "draft": False, "actor": "system"}]
    _, _, headers = _headers(captured["blueprint"], "main")
    assert _value(headers["lean"]) == "Lea.Demo.main"
    assert _value(headers["file"]) == "Main.lean"
    context = captured["messages"][0]["content"]
    assert context.startswith(projects.CONTEXT_MARKER)
    assert "This session proves `main` (theorem)" in context
    assert "Declare it as `Lea.Demo.main` in `Main.lean`." in context


# ── 5. Context message ───────────────────────────────────────────────────────────


def test_context_message_summarises_the_blueprint_instead_of_dumping_it(
        tmp_path, monkeypatch, fake_store):
    project, proofs, repo = _project(tmp_path, monkeypatch, (
        "# Blueprint — Demo\n\n"
        "## helper\n- kind: lemma\n- lean: `Lea.Demo.helper`\n\nA proved helper.\n\n"
        "## sketch\n- kind: lemma\n- lean: `Lea.Demo.sketch`\n\nStated with a sorry.\n\n"
        "## todo\n- kind: lemma\n\nNot written yet.\n\n"
        "## main\n- kind: theorem\n- uses: helper, sketch, todo\n\nThe main theorem.\n"
    ))
    helper = _write_lean(repo, "Helper.lean", "lemma helper : True := trivial")
    sketch = _write_lean(repo, "Sketch.lean", "lemma sketch : True := by sorry")
    sid = store.create_session("work", project_id=project["id"])["id"]
    store.add_code_step(sid, None, "Helper.lean", content=helper.read_text(), check_status="ok")
    store.add_code_step(sid, None, "Sketch.lean", content=sketch.read_text(), check_status="ok")
    blueprint_reconcile.bind_session_node(project, proofs, "main")

    content = projects.compose_context_message(project, repo, blueprint_node="main")["content"]

    section = content.split("## Project Blueprint\n", 1)[1].split("\n\n## Project files", 1)[0]
    counts = section.split("\n", 1)[0]
    assert counts.startswith("4 nodes · 1 proved · ")
    assert counts == blueprint_reconcile.counts_line(list(_nodes(project, proofs).values()))
    assert "This session proves `main` (theorem): " in section and "The main theorem." in section
    assert "Declare it as `Lea.Demo.main` in `Main.lean`." in section
    assert "- `helper` — proved as `Lea.Demo.helper` (`Helper.lean`)" in section
    assert "- `sketch` — stated with sorry (`Sketch.lean`): use as a hypothesis" in section
    assert "- `todo` — planned: state what you need as a hypothesis" in section
    assert section.endswith(
        "Use blueprint_read for the full plan and blueprint_upsert to add or change nodes "
        "(one call for many nodes). The system renders .lea/blueprint.md; do not edit it.")
    # No blueprint dump and no format tutorial.
    assert "## continuous_sq" not in content
    assert "## helper" not in content
    assert "One section per node" not in content
    assert "- kind:" not in content and "- uses:" not in content

    # Unbound sessions get the counts line and the instruction only.
    plain = projects.compose_context_message(project, repo)["content"]
    assert "This session proves" not in plain
    assert blueprint_reconcile.TOOL_INSTRUCTION in plain

    # The prover's workspace instructions no longer teach a blueprint file.
    lea_md = LEA_MD.read_text()
    assert "blueprint.md" not in lea_md
    assert "Maintain" not in lea_md and "template" not in lea_md
    assert "## If you have the `spawn_subagent` tool — you are the COORDINATOR" in lea_md
    assert "## If you do NOT have `spawn_subagent` — you are a delegated WORKER" in lea_md
    assert "`premise-search`" in lea_md and "`proof-candidate`" in lea_md


def test_counts_line_matches_the_plan_example():
    nodes = ([{"key": "sumChoosePow", "status": "proved"}]
             + [{"key": key, "status": "ready"} for key in ("T_three", "dvd_of_prime_pow_dvd")]
             + [{"key": f"n{i}", "status": "planned"} for i in range(16)])
    assert blueprint_reconcile.counts_line(nodes) == (
        "19 nodes · 1 proved · 2 ready (T_three, dvd_of_prime_pow_dvd) · 16 planned")
    assert blueprint_reconcile.counts_line([]) == "0 nodes"


# This module mocks compiler behavior; bind those mock verdicts to their inputs.
pytestmark = pytest.mark.usefixtures("certified_unit_inputs")
