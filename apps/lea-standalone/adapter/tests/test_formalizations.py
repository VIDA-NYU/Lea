"""First-class formalizations: schema, attribution, and derived evidence."""

import sqlite3

import pytest

from app import db, formalizations, store


def _fresh(tmp_path, monkeypatch):
    path = tmp_path / "test.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    return path


def _project():
    return store.create_project(
        "analysis",
        title="Analysis",
        description=None,
        namespace="Lea.Analysis",
        repo_path="Lea/Analysis",
    )


def test_migration_adds_scoped_tables_columns_constraints_and_indexes(
    tmp_path, monkeypatch
):
    path = _fresh(tmp_path, monkeypatch)
    with sqlite3.connect(path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
        assert {
            "formalizations",
            "session_formalizations",
            "formalization_files",
            "verification_events",
        } <= tables
        assert "focus_formalization_id" in {
            row[1] for row in conn.execute("pragma table_info(runs)")
        }
        assert "formalization_id" in {
            row[1] for row in conn.execute("pragma table_info(timeline)")
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type = 'index'"
            )
        }
        assert {
            "ux_formalizations_project_declaration",
            "ux_formalizations_loose_declaration",
            "ux_formalization_files_primary",
            "ix_verification_formalization_path",
        } <= indexes

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                insert into formalizations (
                    id, display_title, kind, origin, created_at, updated_at
                ) values ('bad', 'bad', 'theorem', 'ui', 't', 't')
                """
            )


def test_one_session_keeps_independent_formalization_validity(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    project = _project()
    session = store.create_session("Several related results", project_id=project["id"])
    proved = store.create_formalization(
        project_id=project["id"],
        loose_session_id=None,
        display_title="The proved theorem",
        declaration_name="proved_theorem",
    )
    failing = store.create_formalization(
        project_id=project["id"],
        loose_session_id=None,
        display_title="The failing theorem",
        declaration_name="failing_theorem",
    )
    for item in (proved, failing):
        store.link_session_formalization(session["id"], item["id"])

    run_a = store.create_run(
        session["id"], "m", None, 3,
        project_id=project["id"],
        focus_formalization_id=proved["id"],
    )
    step_a = store.add_code_step(
        session["id"], run_a["id"], "proved.lean",
        content="theorem proved_theorem : True := by trivial",
        check_status="ok", artifact_kind="proof",
        formalization_id=proved["id"],
    )
    store.link_formalization_file(proved["id"], "proved.lean", "primary")
    store.update_run(run_a["id"], "proved", result_kind="proved")
    store.upsert_artifact(
        project_id=project["id"], session_id=session["id"], run_id=run_a["id"],
        declaration_name="proved_theorem", kind="proof", path="proved.lean",
        module_name="Lea.Analysis.proved", formalization_id=proved["id"],
    )

    run_b = store.create_run(
        session["id"], "m", None, 3,
        project_id=project["id"],
        focus_formalization_id=failing["id"],
    )
    store.add_code_step(
        session["id"], run_b["id"], "failing.lean",
        content="theorem failing_theorem : False := by trivial",
        check_status="error", check_detail="type mismatch",
        formalization_id=failing["id"],
    )
    store.link_formalization_file(failing["id"], "failing.lean", "primary")
    store.update_run(run_b["id"], "failed")

    by_id = {item["id"]: item for item in formalizations.for_session(session["id"])}
    assert by_id[proved["id"]]["validity_status"] == "proved"
    assert by_id[failing["id"]]["validity_status"] == "failing"
    assert step_a["formalization_id"] == proved["id"]


def test_safe_verify_is_current_only_for_the_verified_snapshot(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    session = store.create_session("Verify one target")
    item = store.create_formalization(
        project_id=None,
        loose_session_id=session["id"],
        display_title="target",
        declaration_name="target",
    )
    store.link_session_formalization(session["id"], item["id"])
    store.link_formalization_file(item["id"], "target.lean", "primary")
    first = store.add_code_step(
        session["id"], None, "target.lean",
        content="theorem target : True := by trivial",
        check_status="ok", formalization_id=item["id"],
    )
    store.record_verification_event(
        session_id=session["id"], formalization_id=item["id"],
        path="target.lean", status="ok", detail=None,
        code_step_id=first["id"],
    )
    assert formalizations.get(item["id"])["safe_verify"]["current"] is True

    store.add_code_step(
        session["id"], None, "target.lean",
        content="theorem target : True := by\n  trivial",
        formalization_id=item["id"],
    )
    assert formalizations.get(item["id"])["safe_verify"]["current"] is False


def test_external_source_staleness_compares_current_and_artifact_hash(
    tmp_path, monkeypatch
):
    _fresh(tmp_path, monkeypatch)
    project = _project()
    session = store.create_session("External target", project_id=project["id"])
    item = store.create_formalization(
        project_id=project["id"], loose_session_id=None,
        display_title="external", declaration_name="external",
        origin="overleaf", origin_key="doc:theorem:external",
        source_hash="new",
    )
    store.link_session_formalization(session["id"], item["id"])
    store.link_formalization_file(item["id"], "external.lean", "primary")
    step = store.add_code_step(
        session["id"], None, "external.lean",
        content="theorem external : True := by trivial",
        check_status="ok", formalization_id=item["id"],
    )
    store.upsert_artifact(
        project_id=project["id"], session_id=session["id"], run_id=None,
        declaration_name="external", kind="proof", path="external.lean",
        module_name="Lea.Analysis.external", formalization_id=item["id"],
        source_hash="old",
    )
    assert step["check_status"] == "ok"
    assert formalizations.get(item["id"])["validity_status"] == "stale"


def test_current_snapshot_crosses_sessions_without_rewriting_history(
    tmp_path, monkeypatch
):
    _fresh(tmp_path, monkeypatch)
    project = _project()
    session_one = store.create_session("Theorem A", project_id=project["id"])
    session_two = store.create_session("Definition B", project_id=project["id"])
    theorem = store.create_formalization(
        project_id=project["id"],
        loose_session_id=None,
        display_title="Theorem A",
        declaration_name="theorem_a",
    )
    for session in (session_one, session_two):
        store.link_session_formalization(session["id"], theorem["id"])
    store.link_formalization_file(theorem["id"], "A.lean", "primary")

    first = store.add_code_step(
        session_one["id"], None, "A.lean",
        content="theorem theorem_a : True := by trivial",
        check_status="ok", artifact_kind="proof",
        formalization_id=theorem["id"],
    )
    before = formalizations.current_snapshot(
        theorem["id"], conversation_session_id=session_one["id"]
    )
    second = store.add_code_step(
        session_two["id"], None, "A.lean",
        content="theorem theorem_a : True := by\n  trivial",
        check_status="ok", artifact_kind="proof",
        formalization_id=theorem["id"],
    )

    current = formalizations.current_snapshot(
        theorem["id"], conversation_session_id=session_one["id"]
    )
    assert current["files"][0]["id"] == second["id"]
    assert current["files"][0]["code"].endswith("by\n  trivial")
    assert current["last_updated_session"] == {
        "id": session_two["id"],
        "title": "Definition B",
    }
    assert current["conversation"]["files"][0]["id"] == first["id"]
    assert current["conversation"]["is_current"] is False
    assert current["revision_token"] != before["revision_token"]
    assert store.session_detail(session_one["id"])["code_steps"][0]["id"] == first["id"]


def test_shared_file_change_controls_current_validity(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    project = _project()
    session_one = store.create_session("A", project_id=project["id"])
    session_two = store.create_session("B", project_id=project["id"])
    theorem = store.create_formalization(
        project_id=project["id"], loose_session_id=None,
        display_title="A", declaration_name="a",
    )
    other = store.create_formalization(
        project_id=project["id"], loose_session_id=None,
        display_title="B", declaration_name="b",
    )
    for item in (theorem, other):
        store.link_formalization_file(item["id"], "Shared.lean", "primary")
    store.link_session_formalization(session_one["id"], theorem["id"])
    store.link_session_formalization(session_two["id"], other["id"])
    store.add_code_step(
        session_one["id"], None, "Shared.lean",
        content="theorem a : True := by trivial",
        check_status="ok", artifact_kind="proof",
        formalization_id=theorem["id"],
    )
    assert formalizations.get(theorem["id"])["validity_status"] == "proved"

    store.add_code_step(
        session_two["id"], None, "Shared.lean",
        content="theorem a : True := by trivial\n\ntheorem b : False := by trivial",
        check_status="error", check_detail="type mismatch",
        formalization_id=other["id"],
    )
    assert formalizations.get(theorem["id"])["validity_status"] == "failing"


def test_a_formalizations_sessions_exclude_subagent_children(tmp_path, monkeypatch):
    """A sub-agent must never appear as one of a formalization's sessions.

    A child IS a session row (`parent_id` = the coordinator that spawned it), and the
    UI uses `sessions[0]` as the row's click target — while a child session opens
    READ-ONLY behind a provenance bar. So a formalization could send you into an
    internal child instead of the conversation you actually had, and a project's
    session list read as a mix of work you started and machinery the coordinator
    spawned. A child stays reachable from its coordinator's thread, where it means
    something.
    """
    _fresh(tmp_path, monkeypatch)
    project = _project()
    parent = store.create_session("Formalize the discrepancy bound", project_id=project["id"])
    child = store.create_session(
        "certificate c2", project_id=project["id"],
        parent_id=parent["id"], role="proof-candidate", spawned_at_turn=3,
    )
    item = store.create_formalization(
        project_id=project["id"], loose_session_id=None,
        display_title="The bound", declaration_name="discrepancy_bound",
    )
    # Both are linked — the child genuinely worked on it. Only the root is offered.
    store.link_session_formalization(parent["id"], item["id"])
    store.link_session_formalization(child["id"], item["id"])

    decorated = formalizations.decorate([store.get_formalization(item["id"])])[0]
    ids = [s["id"] for s in decorated["sessions"]]
    assert parent["id"] in ids
    assert child["id"] not in ids, "a sub-agent is not a session you can open"
