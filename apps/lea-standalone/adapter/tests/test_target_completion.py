"""A passing helper file must not certify a different requested declaration."""

from app import db, formalizations, projects, store
from app.routes.projects import project_target_status_by_slug
from app.target_completion import checked_target, target_declaration


def test_target_identity_rejects_helpers_claims_comments_and_ambiguous_names():
    helper = "-- theorem thmB : True := by trivial\ndef helper : Prop := True\n"
    assert target_declaration(helper, "thmB", "theorem") is None
    assert target_declaration("def thmB : Prop := True", "thmB", "theorem") is None
    assert target_declaration("def thmB : Prop := True", "thmB", "proof") is None
    assert target_declaration("theorem thmB : True := by trivial", "thmB", "theorem")
    assert target_declaration("def thmB : Prop := True", "thmB", "definition")
    namespaced = (
        "namespace A\ntheorem t : True := by trivial\nend A\n"
        "namespace B\ntheorem t : True := by trivial\nend B\n"
    )
    assert target_declaration(namespaced, "t", "theorem") is None
    assert target_declaration(namespaced, "A.t", "theorem").full_name == "A.t"
    top_level_and_namespaced = "theorem t : True := by trivial\n" + namespaced
    assert target_declaration(top_level_and_namespaced, "t", "theorem") is None
    assert checked_target(
        {"declaration_name": "t", "kind": "theorem"},
        [{"code": "namespace A\ntheorem t : True := by trivial\nend A", "check_status": "ok"},
         {"code": "namespace B\ntheorem t : True := by trivial\nend B", "check_status": "ok"}],
    )[0] is None
    assert target_declaration(namespaced, "renamed", "theorem") is None
    match, reason = checked_target(
        {"declaration_name": "thmD", "kind": "theorem"},
        [{"code": helper, "check_status": "ok"}],
    )
    assert match is None and "thmD" in reason


def test_partial_target_is_reviewed_until_successful_target_run(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    proof_root = tmp_path / "proofs"
    monkeypatch.setattr("app.routes.projects._proofs_root", lambda: proof_root)
    project = store.create_project(
        "analysis", title="Analysis", description=None,
        namespace="Lea.Analysis", repo_path="Lea/Analysis",
    )
    session = store.create_session("Formalize thmB", project_id=project["id"])
    target = store.create_formalization(
        project_id=project["id"], loose_session_id=None,
        display_title="thmB", declaration_name="thmB", kind="theorem",
    )
    store.link_session_formalization(session["id"], target["id"])
    store.link_formalization_file(target["id"], "Helper.lean", "support")
    repo = projects.project_repo_dir(project, proof_root)
    repo.mkdir(parents=True)
    artifact = repo / "Helper.lean"
    helper = "import Mathlib\n\ndef helper : Prop := True\n"
    artifact.write_text(helper)
    first = store.create_run(
        session["id"], "model", None, 3, project_id=project["id"],
        focus_formalization_id=target["id"],
    )
    store.add_code_step(
        session["id"], first["id"], "Helper.lean", content=helper,
        check_status="ok", artifact_kind="definition", formalization_id=target["id"],
    )
    store.update_run(first["id"], "needs_review", result_kind="needs_review")

    def status():
        return project_target_status_by_slug(
            project["slug"], declarations="thmB", formalization_ids=target["id"]
        )["targets"][0]

    initial_read = formalizations.get(target["id"])
    assert initial_read["validity_status"] == "needs_review"
    assert initial_read["check_current"] is True
    partial = status()
    assert partial["validity_status"] == "needs_review"
    assert partial["recorded"] is False
    assert partial["exists"] is True
    assert partial["declaration_present"] is False
    assert partial["check_current"] is True  # The helper itself passed Lean.
    assert project_target_status_by_slug(project["slug"], declarations="thmB")["targets"][0]["validity_status"] == "needs_review"

    # A later chat turn and a manual check cannot clear the review verdict.
    chat = store.create_run(session["id"], "model", None, 3,
                            project_id=project["id"], focus_formalization_id=target["id"])
    store.update_run(chat["id"], "answered", result_kind="answered")
    store.add_code_step(session["id"], None, "Helper.lean", content=helper,
                        check_status="ok", author="user", formalization_id=target["id"])
    assert formalizations.get(target["id"])["validity_status"] == "needs_review"
    assert status()["validity_status"] == "needs_review"

    # Editing the file on disk invalidates the recorded passing check.
    theorem = "import Mathlib\n\ntheorem thmB : True := by trivial\n"
    artifact.write_text(theorem)
    stale = status()
    assert stale["check_current"] is False
    assert stale["validity_status"] == "needs_review"

    # Only a later successful target run can certify the new current revision.
    second = store.create_run(session["id"], "model", None, 3,
                              project_id=project["id"], focus_formalization_id=target["id"])
    store.add_code_step(session["id"], second["id"], "Helper.lean", content=theorem,
                        check_status="ok", artifact_kind="proof", formalization_id=target["id"])
    store.update_run(second["id"], "proved", result_kind="proved")
    store.link_formalization_file(target["id"], "Helper.lean", "primary")
    assert formalizations.get(target["id"])["validity_status"] == "proved"
    complete = status()
    assert complete["validity_status"] == "proved"
    assert complete["completion_run_id"] == second["id"]
    assert complete["check_current"] is True
    store.add_code_step(session["id"], None, "Helper.lean", content=theorem,
                        check_status="ok", author="user", formalization_id=target["id"])
    assert formalizations.get(target["id"])["validity_status"] == "proved"


def test_definition_needs_a_definition_and_successful_target_run(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Define Predicate")
    target = store.create_formalization(
        project_id=None, loose_session_id=session["id"],
        display_title="Predicate", declaration_name="Predicate", kind="definition",
    )
    run = store.create_run(session["id"], "model", None, 3,
                           focus_formalization_id=target["id"])
    store.link_formalization_file(target["id"], "Predicate.lean", "primary")
    store.add_code_step(session["id"], run["id"], "Predicate.lean",
                        content="def Predicate : Prop := True", check_status="ok",
                        artifact_kind="definition", formalization_id=target["id"])
    store.update_run(run["id"], "proved", result_kind="defined")
    assert formalizations.get(target["id"])["validity_status"] == "defined"


def test_historical_compile_promoted_definition_is_now_reviewed_without_rewriting_history(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    session = store.create_session("Formalize thmD")
    target = store.create_formalization(
        project_id=None, loose_session_id=session["id"],
        display_title="thmD", declaration_name="thmD", kind="theorem",
    )
    store.link_session_formalization(session["id"], target["id"])
    store.link_formalization_file(target["id"], "Partial.lean", "support")
    run = store.create_run(session["id"], "model", None, 3,
                           focus_formalization_id=target["id"])
    store.add_code_step(session["id"], run["id"], "Partial.lean",
                        content="def claim : Prop := True", check_status="ok",
                        artifact_kind="definition", formalization_id=target["id"])
    store.update_run(run["id"], "proved", result_kind="defined")

    assert formalizations.get(target["id"])["validity_status"] == "needs_review"
    assert store.session_detail(session["id"])["status"] == "needs_review"
    assert next(item for item in store.list_sessions() if item["id"] == session["id"])["status"] == "needs_review"
    assert store.get_run(run["id"])["result_kind"] == "defined"

    # A historical definition result cannot certify even a present theorem.
    second = store.create_run(session["id"], "model", None, 3,
                              focus_formalization_id=target["id"])
    store.add_code_step(session["id"], second["id"], "Partial.lean",
                        content="theorem thmD : True := by trivial", check_status="ok",
                        artifact_kind="proof", formalization_id=target["id"])
    store.update_run(second["id"], "proved", result_kind="defined")
    assert formalizations.get(target["id"])["validity_status"] == "needs_review"
