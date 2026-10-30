"""The blueprint store (PLAN-blueprint-tools §2.2, §2.5, §2.8, §2.9; §3 "P1 store").

One test per acceptance item 2–10 (item 1 is `test_blueprint_fixtures.py`), plus
the name assignment rule and the store's behaviour inside a real workspace command
(the production path of the HTTP routes). Scratch project repos, no Lean compile:
graph status reuses stored verdicts."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from app import blueprint, blueprint_store, db, graph, peripheral_views, projects, store
from app.config import LeaConfig

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "blueprint"


def _project(tmp_path, monkeypatch, title="Demo"):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    proofs = tmp_path / "proofs"
    project = projects.provision_project(title, proofs)
    return project, proofs, projects.project_repo_dir(project, proofs)


def _git(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _commits(repo) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD"))


def _live(repo) -> dict:
    return blueprint.parse((repo / blueprint_store.LIVE).read_text())


def _by_key(nodes) -> dict:
    return {n["key"]: n for n in nodes}


def test_assign_names_namespace_and_pascal_file():
    assert blueprint_store.assign_names("Lea.Demo", "sum_choose_pow") == ("Lea.Demo.sum_choose_pow", "SumChoosePow.lean")
    assert blueprint_store.assign_names("Lea.Demo", "sumChoosePow") == ("Lea.Demo.sumChoosePow", "SumChoosePow.lean")
    assert blueprint_store.assign_names("Lea.Demo", "T_three") == ("Lea.Demo.T_three", "TThree.lean")


# 2 ─────────────────────────────────────────────────────────────────────────────
def test_upsert_on_empty_blueprint_assigns_names_reports_ready_leaf_and_is_idempotent(tmp_path, monkeypatch):
    project, proofs, repo = _project(tmp_path, monkeypatch)
    batch = [
        {"key": "main", "kind": "theorem", "statement": "The main theorem.", "uses": ["helper"]},
        {"key": "helper", "kind": "lemma", "statement": "A helper lemma."},
        {"key": "corollary", "kind": "theorem", "statement": "Follows from main.", "uses": ["main"]},
    ]
    before = _commits(repo)
    result = blueprint_store.upsert(project, proofs, batch)

    assert result["rejected"] == [] and result["draft"] is False
    applied = _by_key(result["applied"])
    assert list(applied) == ["main", "helper", "corollary"]
    assert applied["main"] == {"key": "main", "kind": "theorem", "lean": "Lea.Demo.main", "file": "Main.lean",
                               "uses": ["helper"], "status": "planned"}
    assert applied["helper"]["lean"] == "Lea.Demo.helper" and applied["helper"]["file"] == "Helper.lean"
    assert applied["helper"]["status"] == "ready"
    assert result["ready"] == ["helper"]
    assert result["counts"] == {"total": 3, "proved": 0, "ready": 1, "stated": 0, "failed": 0, "planned": 2}

    # Committed, canonical, and the title preamble kept.
    assert len(result["commit_sha"]) == 40 and _commits(repo) == before + 1
    assert _git(repo, "status", "--porcelain", "--", blueprint_store.LIVE) == ""
    text = (repo / blueprint_store.LIVE).read_text()
    assert text.startswith("# Blueprint — Demo\n") and blueprint.render(blueprint.parse(text)) == text
    assert [n["key"] for n in _live(repo)["nodes"]] == ["main", "helper", "corollary"]

    again = blueprint_store.upsert(project, proofs, batch)
    assert again["commit_sha"] is None and _commits(repo) == before + 1
    assert (repo / blueprint_store.LIVE).read_text() == text
    assert again["ready"] == ["helper"]


# 3 ─────────────────────────────────────────────────────────────────────────────
def test_upsert_rejects_invalid_nodes_and_writes_the_valid_rest(tmp_path, monkeypatch):
    project, proofs, repo = _project(tmp_path, monkeypatch)
    result = blueprint_store.upsert(project, proofs, [
        {"key": "a", "kind": "lemma", "statement": "A."},
        {"key": "c", "kind": "lemma", "statement": "Uses a key from the same batch.", "uses": ["a"]},
        {"key": "dangles", "kind": "lemma", "uses": ["missing"]},
        {"key": "x", "kind": "lemma", "uses": ["y"]},
        {"key": "y", "kind": "lemma", "uses": ["x"]},
        {"key": "prop", "kind": "proposition"},
        {"key": "Bad Key", "kind": "lemma"},
        {"key": "loop", "kind": "lemma", "uses": ["loop"]},
        {"key": "nokind", "statement": "A new node needs a kind."},
        {"key": "long", "kind": "lemma", "statement": "x" * 4001},
    ])
    assert [a["key"] for a in result["applied"]] == ["a", "c"]
    assert {(r["key"], r["code"]) for r in result["rejected"]} == {
        ("dangles", "dangling_dependency"), ("x", "cycle"), ("y", "cycle"), ("prop", "unknown_kind"),
        ("Bad Key", "invalid_key"), ("loop", "self_dependency"), ("nokind", "missing_kind"),
        ("long", "statement_too_long"),
    }
    assert all(r["message"] for r in result["rejected"])
    assert [n["key"] for n in _live(repo)["nodes"]] == ["a", "c"]
    assert _live(repo)["warnings"] == []


# 4 ─────────────────────────────────────────────────────────────────────────────
def test_upsert_merges_onto_an_existing_node(tmp_path, monkeypatch):
    project, proofs, repo = _project(tmp_path, monkeypatch)
    blueprint_store.upsert(project, proofs, [
        {"key": "a", "kind": "lemma", "statement": "A."},
        {"key": "b", "kind": "lemma", "statement": "B."},
        {"key": "n", "kind": "theorem", "statement": "The original statement.", "uses": ["a"]},
    ])
    result = blueprint_store.upsert(project, proofs, [{"key": "n", "uses": ["b"]}])
    assert result["rejected"] == []
    node = _by_key(_live(repo)["nodes"])["n"]
    assert node["statement"] == "The original statement."
    assert node["uses"] == ["b"] and node["kind"] == "theorem"
    assert node["lean"] == "Lea.Demo.n" and node["file"] == "N.lean"

    blueprint_store.upsert(project, proofs, [{"key": "custom", "kind": "lemma", "lean": "Lea.Elsewhere.custom_name"}])
    blueprint_store.upsert(project, proofs, [{"key": "custom", "statement": "Now with a statement."}])
    custom = _by_key(_live(repo)["nodes"])["custom"]
    assert custom["lean"] == "Lea.Elsewhere.custom_name"  # explicit lean kept, never reassigned
    assert custom["file"] == "Custom.lean"
    assert custom["statement"] == "Now with a statement."


# 5 ─────────────────────────────────────────────────────────────────────────────
def test_remove_refuses_an_in_use_key_unless_cascade(tmp_path, monkeypatch):
    project, proofs, repo = _project(tmp_path, monkeypatch)
    blueprint_store.upsert(project, proofs, [
        {"key": "helper", "kind": "lemma"},
        {"key": "main", "kind": "theorem", "uses": ["helper"]},
    ])
    refused = blueprint_store.remove(project, proofs, ["helper", "ghost"])
    assert refused["removed"] == [] and refused["commit_sha"] is None
    assert {(r["key"], r["code"]) for r in refused["rejected"]} == {("helper", "in_use"), ("ghost", "unknown_key")}
    assert "main" in next(r["message"] for r in refused["rejected"] if r["key"] == "helper")

    removed = blueprint_store.remove(project, proofs, ["helper"], cascade=True)
    assert removed["removed"] == ["helper"] and removed["rejected"] == []
    assert len(removed["commit_sha"]) == 40
    nodes = _by_key(_live(repo)["nodes"])
    assert list(nodes) == ["main"] and nodes["main"]["uses"] == []


# 6 ─────────────────────────────────────────────────────────────────────────────
def test_draft_upsert_accept_and_discard(tmp_path, monkeypatch):
    project, proofs, repo = _project(tmp_path, monkeypatch)
    live_before = (repo / blueprint_store.LIVE).read_text()
    result = blueprint_store.upsert(project, proofs, [
        {"key": "a", "kind": "lemma", "statement": "Leaf a."},
        {"key": "b", "kind": "theorem", "statement": "Uses a.", "uses": ["a"]},
    ], draft=True)
    assert result["draft"] is True and result["ready"] == ["a"]
    assert [(a["key"], a["status"], a["lean"]) for a in result["applied"]] == [
        ("a", "ready", "Lea.Demo.a"), ("b", "planned", "Lea.Demo.b")]
    assert (repo / blueprint_store.LIVE).read_text() == live_before  # only the draft is written
    assert blueprint_store.exists_draft(project, proofs)
    assert [n["key"] for n in blueprint_store.load(project, proofs, draft=True)["nodes"]] == ["a", "b"]

    accepted = blueprint_store.accept_draft(project, proofs, keys=["a"])
    assert [a["key"] for a in accepted["applied"]] == ["a"] and accepted["rejected"] == []
    assert accepted["applied"][0]["lean"] == "Lea.Demo.a" and accepted["applied"][0]["file"] == "A.lean"
    assert [n["key"] for n in _live(repo)["nodes"]] == ["a"]
    assert not blueprint_store.exists_draft(project, proofs)
    assert _git(repo, "status", "--porcelain") == ""  # live write + draft deletion in one commit
    assert set(_git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()) == {
        blueprint_store.LIVE, blueprint_store.DRAFT}

    blueprint_store.upsert(project, proofs, [{"key": "c", "kind": "lemma"}], draft=True)
    assert blueprint_store.discard_draft(project, proofs) == {"discarded": True}
    assert not blueprint_store.exists_draft(project, proofs)
    assert blueprint_store.discard_draft(project, proofs) == {"discarded": False}
    assert [n["key"] for n in _live(repo)["nodes"]] == ["a"]


# 7 ─────────────────────────────────────────────────────────────────────────────
def test_normalize_the_realanalysis_blend():
    text = (FIXTURES / "02-realanalysis-blend.md").read_text(encoding="utf-8")
    canonical, warnings = blueprint_store.normalize(text)
    parsed = blueprint.parse(canonical)
    assert blueprint.render(parsed) == canonical
    assert len(parsed["nodes"]) == 18
    assert "Goal" not in {n["key"] for n in parsed["nodes"]}
    assert [b["heading"] for b in parsed["blocks"] if b["type"] == "prose"] == ["Goal"]
    assert _by_key(parsed["nodes"])["sumChoosePow"]["lean"] == "Lea.RealAnalysis.sumChoosePow"
    assert "status:" not in canonical
    assert {(w["code"], w["node"]) for w in warnings} == {("unknown_header", "sumChoosePow")}


# 8 ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.usefixtures("certified_unit_inputs")
def test_graph_leaf_ready_declared_and_unresolved_lean(tmp_path, monkeypatch):
    project, proofs, repo = _project(tmp_path, monkeypatch)
    blueprint_store.upsert(project, proofs, [
        {"key": "helper", "kind": "lemma", "statement": "Declared and checked."},
        {"key": "main", "kind": "theorem", "statement": "Uses helper.", "uses": ["helper"]},
        {"key": "leaf", "kind": "lemma", "statement": "Planned leaf."},
    ])
    nodes = _by_key(graph.build_graph(project, proofs)["nodes"])
    assert nodes["leaf"]["status"] == "ready"  # planned leaf → ready
    assert nodes["leaf"]["declared"] is False
    assert nodes["leaf"]["planned_file"] == "Leaf.lean" and nodes["leaf"]["file"] is None
    assert nodes["main"]["status"] == "planned"
    result = graph.build_graph(project, proofs)
    assert {"unresolved_lean"} == {w["code"] for w in result["warnings"]}
    assert {w["node"] for w in result["warnings"]} == {"helper", "main", "leaf"}

    (repo / "Helper.lean").write_text("import Mathlib\nnamespace Lea.Demo\nlemma helper : True := trivial\nend Lea.Demo\n")
    session = store.create_session("prove helper", project_id=project["id"])["id"]
    store.add_code_step(session, None, "Helper.lean", content=(repo / "Helper.lean").read_text(), check_status="ok")
    result = graph.build_graph(project, proofs)
    nodes = _by_key(result["nodes"])
    assert nodes["helper"]["status"] == "proved" and nodes["helper"]["declared"] is True
    assert nodes["helper"]["file"] == "Helper.lean" and nodes["helper"]["planned_file"] == "Helper.lean"
    assert nodes["main"]["status"] == "ready"  # its only dependency is proved
    assert {w["node"] for w in result["warnings"] if w["code"] == "unresolved_lean"} == {"main", "leaf"}


# 9 ─────────────────────────────────────────────────────────────────────────────
def test_upsert_invalidates_project_views(tmp_path, monkeypatch):
    project, proofs, _ = _project(tmp_path, monkeypatch)
    with db.connect() as conn:
        before = peripheral_views.input_revision(conn, project["id"])
    blueprint_store.upsert(project, proofs, [{"key": "a", "kind": "lemma"}])
    with db.connect() as conn:
        after = peripheral_views.input_revision(conn, project["id"])
        row = conn.execute("select entity_type, entity_id, action, project_id from domain_changes "
                           "order by id desc limit 1").fetchone()
    assert after > before
    assert tuple(row) == ("blueprint", project["id"], "update", project["id"])


# 10 ────────────────────────────────────────────────────────────────────────────
def _route_setup(tmp_path, monkeypatch):
    from app.routes import projects as projects_route
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    monkeypatch.setattr(projects_route, "load_config",
                        lambda: LeaConfig(model="m", max_turns=3, lea_root=tmp_path, max_spend_usd=None))
    import app.config
    monkeypatch.setattr(app.config, "load_config", projects_route.load_config)
    return projects_route, tmp_path / "workspace" / "proofs"


def test_routes_normalize_expose_nodes_and_draft_and_register_actions(tmp_path, monkeypatch):
    from app import workspace_commands, workspace_effects
    from app.routes.projects import (BlueprintDraftAccept, BlueprintNodesRemove, BlueprintNodesUpsert,
                                     DocUpdate, ProjectCreate)
    routes, proofs = _route_setup(tmp_path, monkeypatch)
    pid = routes.create_project.worker(ProjectCreate(title="Demo"))["id"]

    messy = "# Blueprint — Demo\n\n\n## b\n- uses: a\n- kind: Theorem\n- lean: Lea.Demo.b (soon)\nB.\n## a\n- kind: lemma\n"
    put = routes.put_blueprint.worker(pid, DocUpdate(content=messy))
    assert put["content"] == ("# Blueprint — Demo\n\n## b\n- kind: theorem\n- lean: `Lea.Demo.b`\n- uses: a\n\nB.\n\n"
                              "## a\n- kind: lemma\n")
    assert put["warnings"] == [] and len(put["commit_sha"]) == 40
    got = routes.get_blueprint.worker(pid)
    assert got["content"] == put["content"] and got["draft"] is None
    assert [(n["key"], n["lean"]) for n in got["nodes"]] == [("b", "Lea.Demo.b"), ("a", None)]

    upserted = routes.upsert_blueprint_nodes.worker(pid, BlueprintNodesUpsert(
        nodes=[{"key": "c", "kind": "lemma", "statement": "Drafted."}], draft=True))
    assert upserted["draft"] is True and [a["key"] for a in upserted["applied"]] == ["c"]
    draft = routes.get_blueprint.worker(pid)["draft"]
    assert [n["key"] for n in draft["nodes"]] == ["c"] and draft["nodes"][0]["file"] == "C.lean"
    assert draft["warnings"] == [] and draft["content"].startswith("# Blueprint draft — Demo")

    edited = routes.put_blueprint_draft.worker(pid, DocUpdate(content=draft["content"] + "\n## d\n- kind: lemma\n"))
    assert [n["key"] for n in blueprint.parse(edited["content"])["nodes"]] == ["c", "d"]
    accepted = routes.accept_blueprint_draft.worker(pid, BlueprintDraftAccept(keys=["c"]))
    assert [a["key"] for a in accepted["applied"]] == ["c"]
    assert routes.get_blueprint.worker(pid)["draft"] is None
    assert [n["key"] for n in routes.get_blueprint.worker(pid)["nodes"]] == ["b", "a", "c"]

    removed = routes.remove_blueprint_nodes.worker(pid, BlueprintNodesRemove(keys=["c"]))
    assert removed["removed"] == ["c"]
    routes.upsert_blueprint_nodes.worker(pid, BlueprintNodesUpsert(nodes=[{"key": "e", "kind": "lemma"}], draft=True))
    assert routes.discard_blueprint_draft.worker(pid) == {"discarded": True}
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as missing:
        routes.accept_blueprint_draft.worker(pid, BlueprintDraftAccept())
    assert missing.value.status_code == 404

    actions = {"project.blueprint_upsert", "project.blueprint_remove",
               "project.blueprint_accept_draft", "project.blueprint_discard_draft"}
    assert actions | {"project.blueprint_draft"} <= set(workspace_commands._actions)
    assert actions <= workspace_effects.SNAPSHOT_ACTIONS
    assert "project.blueprint_draft" in workspace_commands.EDIT_ACTIONS
    assert workspace_commands.DOC_PATHS["project.blueprint_draft"] == blueprint_store.DRAFT
    assert "blueprint.draft.md" in projects.EDITABLE_DOCS


def _execute(receipt):
    """Claim and execute an admitted workspace command, as the worker does."""
    from app import commands, workspace_commands
    owner = "blueprint-test-" + uuid4().hex
    epoch = commands.start_worker(owner)
    token = commands.claim(owner, epoch, "compute", kinds=workspace_commands.KINDS)
    assert token and token["id"] == receipt["command"]["id"]
    workspace_commands.execute(token)
    return workspace_commands.get(token["id"])["command"]


def test_command_writes_keep_human_saves_conflict_free(tmp_path, monkeypatch):
    """The production path: a route upsert runs inside a workspace command (snapshot
    effect), an agent upsert runs outside one, and a human markdown save after either
    still succeeds instead of reporting an external conflict."""
    from app.routes.projects import BlueprintNodesUpsert, DocUpdate, ProjectCreate
    routes, proofs = _route_setup(tmp_path, monkeypatch)
    project = routes.create_project.worker(ProjectCreate(title="Demo"))
    pid = project["id"]
    repo = projects.project_repo_dir(project, proofs)

    saved = _execute(routes.put_blueprint(pid, DocUpdate(content="# Blueprint — Demo\n\n## a\n- kind: lemma\n")))
    assert saved["state"] == "succeeded", saved

    done = _execute(routes.upsert_blueprint_nodes(pid, BlueprintNodesUpsert(nodes=[{"key": "b", "kind": "lemma"}])))
    assert done["state"] == "succeeded", done
    assert [a["key"] for a in done["result"]["applied"]] == ["b"]
    assert [n["key"] for n in _live(repo)["nodes"]] == ["a", "b"]
    # The snapshot effect bound the SQL file identity to the bytes the store wrote.
    with db.connect() as conn:
        binding = conn.execute("select blob_id, materialization from workspace_files where scope=? and path=?",
                               ("p:" + pid, blueprint_store.LIVE)).fetchone()
    assert binding["materialization"] == "materialized"
    assert store.blob_content(binding["blob_id"]) == (repo / blueprint_store.LIVE).read_text()

    revision = routes.get_blueprint.worker(pid)["file_revision"]
    saved = _execute(routes.put_blueprint(pid, DocUpdate(content=(repo / blueprint_store.LIVE).read_text() + "\nMore.\n",
                                                         file_revision=revision)))
    assert saved["state"] == "succeeded", saved

    # The agent's tools call the store outside any workspace command.
    blueprint_store.upsert(project, proofs, [{"key": "c", "kind": "lemma"}])
    revision = routes.get_blueprint.worker(pid)["file_revision"]
    saved = _execute(routes.put_blueprint(pid, DocUpdate(content=(repo / blueprint_store.LIVE).read_text() + "\nLast.\n",
                                                         file_revision=revision)))
    assert saved["state"] == "succeeded", saved
    assert [n["key"] for n in _live(repo)["nodes"]] == ["a", "b", "c"]
    with db.connect() as conn:
        assert conn.execute("select count(*) from workspace_effects where state='conflict'").fetchone()[0] == 0


def test_public_write_routes_accept_optional_bodies_and_extra_fields(tmp_path, monkeypatch):
    """The UI sends `idempotency_key` on every write and `draft_id` on the draft PUT;
    discard posts `{}` (or nothing). Admission goes through the public contracts."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api_routes import projects as public
    from app.routes.projects import ProjectCreate
    routes, proofs = _route_setup(tmp_path, monkeypatch)
    project = routes.create_project.worker(ProjectCreate(title="Demo"))
    pid = project["id"]
    app = FastAPI()
    app.include_router(public.router)
    client = TestClient(app)

    def run(response):
        assert response.status_code == 202, response.text
        done = _execute(response.json())
        assert done["state"] == "succeeded", done
        return done["result"]

    result = run(client.post(f"/api/projects/{pid}/blueprint/nodes", json={
        "nodes": [{"key": "a", "kind": "lemma", "extra": 1}], "draft": True,
        "idempotency_key": "upsert-1", "draft_id": "ignored"}))
    assert [a["key"] for a in result["applied"]] == ["a"]
    revision = routes.get_blueprint.worker(pid)["draft"]["file_revision"]
    assert revision
    content = blueprint_store.load(project, proofs, draft=True)
    run(client.put(f"/api/projects/{pid}/blueprint/draft", json={
        "content": blueprint.render(content) + "\n## b\n- kind: lemma\n",
        "file_revision": revision, "draft_id": "draft-1", "idempotency_key": "draft-put-1"}))
    assert [n["key"] for n in blueprint_store.load(project, proofs, draft=True)["nodes"]] == ["a", "b"]

    first = client.post(f"/api/projects/{pid}/blueprint/draft/discard", json={"idempotency_key": "discard-1"})
    assert run(first) == {"discarded": True}
    replay = client.post(f"/api/projects/{pid}/blueprint/draft/discard", json={"idempotency_key": "discard-1"})
    assert replay.json()["command"]["id"] == first.json()["command"]["id"]
    assert run(client.post(f"/api/projects/{pid}/blueprint/draft/discard")) == {"discarded": False}

    run(client.post(f"/api/projects/{pid}/blueprint/nodes", json={
        "nodes": [{"key": "c", "kind": "lemma"}], "draft": True, "idempotency_key": "upsert-2"}))
    accepted = run(client.post(f"/api/projects/{pid}/blueprint/draft/accept", json={}))
    assert [a["key"] for a in accepted["applied"]] == ["c"]
    removed = run(client.post(f"/api/projects/{pid}/blueprint/nodes/remove", json={
        "keys": ["c"], "idempotency_key": "remove-1", "draft_id": None}))
    assert removed["removed"] == ["c"]
