"""Tests for the blueprint → graph derivation (T2, D28/D29).

Exercises status derivation (planned/stated/ready/proved/failed) and session
attribution against a scratch project repo + seeded code_steps — no Lean compile,
since status reuses the stored verdict."""

from __future__ import annotations

import pytest

from app import db, graph, projects, store


def _init_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def _write_lean(repo, name: str, body: str) -> None:
    (repo / name).write_text(f"import Mathlib\n\nnamespace Lea.Demo\n\n{body}\n\nend Lea.Demo\n")


def _setup_demo(tmp_path, monkeypatch):
    """A Demo project with four declared files + a blueprint covering five nodes."""
    _init_db(tmp_path, monkeypatch)
    proofs = tmp_path / "proofs"
    project = projects.provision_project("Demo", proofs)
    repo = projects.project_repo_dir(project, proofs)

    _write_lean(repo, "helper.lean", "lemma helper : True := trivial")
    _write_lean(repo, "main.lean", "theorem main : True := trivial")
    _write_lean(repo, "sketch.lean", "lemma sketch : True := by sorry")
    # `dependent` is proved on its own but its node `uses: helper` (also proved).

    blueprint_md = (
        "# Blueprint — Demo\n\n"
        "## helper\n- kind: lemma\n- lean: `Lea.Demo.helper`\n\nA proved helper.\n\n"
        "## main\n- kind: theorem\n- lean: `Lea.Demo.main`\n- uses: helper\n\nFails its check.\n\n"
        "## sketch\n- kind: lemma\n- lean: `Lea.Demo.sketch`\n\nHas a sorry.\n\n"
        "## todo\n- kind: lemma\n- lean: `Lea.Demo.todo`\n- uses: helper\n\nNot built; dep proved.\n\n"
        "## orphan\n- kind: lemma\n\nNo decl, no deps.\n"
    )
    (repo / ".lea" / "blueprint.md").write_text(blueprint_md)
    return project, proofs


def _nodes_by_key(result):
    return {n["key"]: n for n in result["nodes"]}


def test_status_derivation_covers_all_kinds(tmp_path, monkeypatch):
    project, proofs = _setup_demo(tmp_path, monkeypatch)
    pid = project["id"]

    # helper: checked ok, no sorry → proved.
    sess = store.create_session("work", project_id=pid)["id"]
    store.add_code_step(sess, None, "helper.lean", content=(projects.project_repo_dir(project, proofs) / "helper.lean").read_text(), check_status="ok")
    # main: latest check errored → failed (even though the body is trivial).
    store.add_code_step(sess, None, "main.lean", content=(projects.project_repo_dir(project, proofs) / "main.lean").read_text(), check_status="error")
    # sketch: has a `sorry` in its decl span → stated (regardless of any verdict).
    store.add_code_step(sess, None, "sketch.lean", content=(projects.project_repo_dir(project, proofs) / "sketch.lean").read_text(), check_status="ok")

    nodes = _nodes_by_key(graph.build_graph(project, proofs))
    assert nodes["helper"]["status"] == "proved"
    assert nodes["main"]["status"] == "failed"
    # sketch: stated (sorry in its decl span), and a leaf, so the ready overlay
    # applies: every one of its (zero) dependencies is proved (PLAN-blueprint-tools §2.8).
    assert nodes["sketch"]["status"] == "ready"
    # todo: no decl in code, but its only dep (helper) is proved → ready.
    assert nodes["todo"]["status"] == "ready"
    # orphan: no `lean:` at all → planned, and a leaf → ready.
    assert nodes["orphan"]["status"] == "ready"


def test_planned_when_decl_absent_and_no_ready_without_proved_deps(tmp_path, monkeypatch):
    project, proofs = _setup_demo(tmp_path, monkeypatch)
    # No code_steps at all: helper exists on disk but is unchecked → stated, not proved;
    # so todo (uses helper) is NOT ready, and main (uses helper) is stated too.
    nodes = _nodes_by_key(graph.build_graph(project, proofs))
    # helper: decl present on disk, never checked → stated; a leaf → ready (§2.8).
    assert nodes["helper"]["status"] == "ready"
    assert nodes["main"]["status"] == "stated"     # decl present, unchecked, no sorry
    # `todo` names a decl no file declares → planned; and helper isn't proved, so the
    # ready overlay does NOT fire.
    assert nodes["todo"]["status"] == "planned"


def test_session_attribution_newest_first(tmp_path, monkeypatch):
    project, proofs = _setup_demo(tmp_path, monkeypatch)
    pid = project["id"]
    older = store.create_session("first pass", project_id=pid)["id"]
    newer = store.create_session("second pass", project_id=pid)["id"]
    # Two sessions both touch helper.lean; the newer insert is the latest verdict.
    store.add_code_step(older, None, "helper.lean", content=(projects.project_repo_dir(project, proofs) / "helper.lean").read_text(), check_status="error")
    store.add_code_step(newer, None, "helper.lean", content=(projects.project_repo_dir(project, proofs) / "helper.lean").read_text(), check_status="ok")

    nodes = _nodes_by_key(graph.build_graph(project, proofs))
    helper = nodes["helper"]
    # Newest verdict wins the status, and drives last_modified_by + sessions order.
    assert helper["status"] == "proved"
    assert helper["last_modified_by"] == newer
    assert [s["session_id"] for s in helper["sessions"]] == [newer, older]
    assert {s["title"] for s in helper["sessions"]} == {"first pass", "second pass"}
    # A node with no touching session has empty attribution.
    assert nodes["orphan"]["sessions"] == []
    assert nodes["orphan"]["last_modified_by"] is None


def _verify_ok(session_id: str, project_id: str) -> None:
    """Give a session a passing SafeVerify verdict on its latest run (what the
    /verify endpoint does): create a run, then back-fill the verdict onto it."""
    store.create_run(session_id, "demo-model", None, None, project_id=project_id)
    store.set_session_safe_verify(session_id, "ok", None)


def test_verified_true_when_safeverify_ok(tmp_path, monkeypatch):
    project, proofs = _setup_demo(tmp_path, monkeypatch)
    pid = project["id"]
    sess = store.create_session("work", project_id=pid)["id"]
    store.add_code_step(sess, None, "helper.lean", content=(projects.project_repo_dir(project, proofs) / "helper.lean").read_text(), check_status="ok")
    _verify_ok(sess, pid)

    nodes = _nodes_by_key(graph.build_graph(project, proofs))
    # proved + SafeVerify-audited → verified.
    assert nodes["helper"]["status"] == "proved"
    assert nodes["helper"]["verified"] is True
    # A non-proved node is never verified, even though no verdict applies to it.
    # (sketch is stated with a sorry; a leaf, so the overlay makes it ready, §2.8.)
    assert nodes["sketch"]["status"] == "ready"
    assert nodes["sketch"]["verified"] is False
    assert nodes["orphan"]["verified"] is False


def test_verified_false_when_only_lean_checked(tmp_path, monkeypatch):
    project, proofs = _setup_demo(tmp_path, monkeypatch)
    pid = project["id"]
    sess = store.create_session("work", project_id=pid)["id"]
    # Lean check passes, but SafeVerify was never run → proved but audit pending.
    store.add_code_step(sess, None, "helper.lean", content=(projects.project_repo_dir(project, proofs) / "helper.lean").read_text(), check_status="ok")

    nodes = _nodes_by_key(graph.build_graph(project, proofs))
    assert nodes["helper"]["status"] == "proved"
    assert nodes["helper"]["verified"] is False


def test_verified_false_when_newer_run_supersedes_verdict(tmp_path, monkeypatch):
    project, proofs = _setup_demo(tmp_path, monkeypatch)
    pid = project["id"]
    sess = store.create_session("work", project_id=pid)["id"]
    store.add_code_step(sess, None, "helper.lean", content=(projects.project_repo_dir(project, proofs) / "helper.lean").read_text(), check_status="ok")
    _verify_ok(sess, pid)
    # A fresh run (e.g. the agent ran again) becomes the latest and carries no verdict,
    # so the audit no longer applies — verified reverts to False until re-verified.
    store.create_run(sess, "demo-model", None, None, project_id=pid)

    nodes = _nodes_by_key(graph.build_graph(project, proofs))
    assert nodes["helper"]["status"] == "proved"
    assert nodes["helper"]["verified"] is False


def test_verified_follows_latest_touching_session(tmp_path, monkeypatch):
    project, proofs = _setup_demo(tmp_path, monkeypatch)
    pid = project["id"]
    older = store.create_session("audited pass", project_id=pid)["id"]
    newer = store.create_session("later pass", project_id=pid)["id"]
    # The older session audited the file; a newer session then re-touched it (still a
    # clean check) but never ran SafeVerify. The verdict belongs to the older state,
    # so the node — now owned by the newer session — is not audited.
    store.add_code_step(older, None, "helper.lean", content=(projects.project_repo_dir(project, proofs) / "helper.lean").read_text(), check_status="ok")
    _verify_ok(older, pid)
    store.add_code_step(newer, None, "helper.lean", content=(projects.project_repo_dir(project, proofs) / "helper.lean").read_text(), check_status="ok")

    nodes = _nodes_by_key(graph.build_graph(project, proofs))
    assert nodes["helper"]["status"] == "proved"
    assert nodes["helper"]["last_modified_by"] == newer
    assert nodes["helper"]["verified"] is False


def test_edges_preserved_from_blueprint(tmp_path, monkeypatch):
    project, proofs = _setup_demo(tmp_path, monkeypatch)
    result = graph.build_graph(project, proofs)
    assert {"from": "main", "to": "helper"} in result["edges"]
    assert {"from": "todo", "to": "helper"} in result["edges"]


def test_audit_is_bound_to_the_exact_file_revision(tmp_path, monkeypatch):
    project, proofs = _setup_demo(tmp_path, monkeypatch)
    repo = projects.project_repo_dir(project, proofs)
    sid = store.create_session('two files', project_id=project['id'])['id']
    for name in ('helper.lean', 'main.lean'):
        store.add_code_step(sid, None, name, content=(repo / name).read_text(), check_status='ok')
    _verify_ok(sid, project['id'])  # Only the latest file, main.lean, was audited.
    nodes = _nodes_by_key(graph.build_graph(project, proofs))
    assert nodes['helper']['status'] == 'proved'
    assert nodes['helper']['verified'] is False
    assert nodes['main']['verified'] is True
    (repo / 'main.lean').write_text((repo / 'main.lean').read_text() + '\n-- changed\n')
    nodes = _nodes_by_key(graph.build_graph(project, proofs))
    assert nodes['main']['status'] != 'proved'
    assert nodes['main']['verified'] is False


def test_qualified_names_never_fall_back_and_short_names_must_be_unique(tmp_path, monkeypatch):
    project, proofs = _setup_demo(tmp_path, monkeypatch)
    repo = projects.project_repo_dir(project, proofs)
    sid = store.create_session('qualified', project_id=project['id'])['id']
    store.add_code_step(sid, None, 'helper.lean', content=(repo / 'helper.lean').read_text(), check_status='ok')
    (repo / '.lea/blueprint.md').write_text(
        '## missing\n- kind: lemma\n- lean: Missing.Namespace.helper\n\n'
        '## legacy\n- kind: lemma\n- lean: helper\n')
    nodes = _nodes_by_key(graph.build_graph(project, proofs))
    # Unresolved (planned) leaf → ready under the leaf rule (§2.8); never resolved.
    assert nodes['missing']['status'] == 'ready'
    assert nodes['missing']['file'] is None
    assert nodes['missing']['declared'] is False
    assert nodes['legacy']['status'] == 'proved'
    assert nodes['legacy']['resolved_lean'] == 'Lea.Demo.helper'
    (repo / 'Other.lean').write_text('namespace Other\nlemma helper : True := trivial\nend Other\n')
    nodes = _nodes_by_key(graph.build_graph(project, proofs))
    # Ambiguous short name → unresolved (planned), a leaf → ready (§2.8).
    assert nodes['legacy']['status'] == 'ready'
    assert nodes['legacy']['resolved_lean'] is None
    (repo / 'Duplicate.lean').write_text((repo / 'helper.lean').read_text())
    names, _ = graph._scan_lean_decls(repo)
    assert graph._resolve_file('Lea.Demo.helper', names) is None


def test_recorded_check_for_different_bytes_cannot_certify_a_graph_node(tmp_path, monkeypatch):
    project, proofs = _setup_demo(tmp_path, monkeypatch)
    sid = store.create_session('different bytes', project_id=project['id'])['id']
    store.add_code_step(sid, None, 'helper.lean', content='theorem unrelated : True := trivial', check_status='ok')
    # Not proved: stated, and a leaf, so ready under §2.8 — never proved.
    assert _nodes_by_key(graph.build_graph(project, proofs))['helper']['status'] == 'ready'


# This module mocks compiler behavior; bind those mock verdicts to their inputs.
pytestmark = pytest.mark.usefixtures("certified_unit_inputs")
