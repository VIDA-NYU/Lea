"""v2.5 E0e: per-session skill / MCP overrides.

The interesting property is that the session stores a DIFF, not a resulting set — so a
project-level change still reaches sessions that already exist. A snapshot would pass a
naive "can I toggle it" test and fail exactly there, which is why that case is first.
"""

import pytest
from fastapi import HTTPException

from app import db, store
from app.routes import mcp_servers as route
from app.routes.mcp_servers import SkillMcpToggle, ServerPayload


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def _session(project_id=None):
    return store.create_session("s1", project_id=project_id)["id"]


def test_project_changes_still_reach_an_existing_session(tmp_path, monkeypatch):
    """THE reason this is a diff. A session created before a skill was assigned to its
    project must still pick that skill up — a stored absolute list would not."""
    _setup(tmp_path, monkeypatch)
    project = store.create_project("p", title="P")["id"]
    session = _session(project)
    assert store.skills_for_run(project, session) == []

    later = store.create_skill("Added Later", "body")
    store.set_skill_assignment(later["id"], is_global=False, project_ids=[project])

    assert [s["slug"] for s in store.skills_for_run(project, session)] == ["added-later"]


def test_session_can_add_and_drop_for_itself(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    project = store.create_project("p", title="P")["id"]
    session, other = _session(project), _session(project)

    from_project = store.create_skill("From Project", "b")
    store.set_skill_assignment(from_project["id"], is_global=False, project_ids=[project])
    extra = store.create_skill("Extra", "b")

    # Add one just for this session, drop the project's one just for this session.
    store.set_session_skill_mcp(session, "skill", extra["id"], "add")
    store.set_session_skill_mcp(session, "skill", from_project["id"], "remove")

    assert [s["slug"] for s in store.skills_for_run(project, session)] == ["extra"]
    # The sibling session is untouched — an override is per session, not per project.
    assert [s["slug"] for s in store.skills_for_run(project, other)] == ["from-project"]


def test_clearing_an_override_returns_to_the_project_default(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    project = store.create_project("p", title="P")["id"]
    session = _session(project)
    skill = store.create_skill("S", "b")
    store.set_skill_assignment(skill["id"], is_global=False, project_ids=[project])

    store.set_session_skill_mcp(session, "skill", skill["id"], "remove")
    assert store.skills_for_run(project, session) == []
    store.set_session_skill_mcp(session, "skill", skill["id"], None)   # clear
    assert [s["slug"] for s in store.skills_for_run(project, session)] == ["s"]


def test_a_loose_session_can_use_a_skill(tmp_path, monkeypatch):
    """Before E0e a project-less session resolved to NO skills by construction, with no
    way to opt one in. That gap is the reason `/skills` exists."""
    _setup(tmp_path, monkeypatch)
    session = _session(None)
    skill = store.create_skill("Handy", "b")
    assert store.skills_for_run(None, session) == []
    store.set_session_skill_mcp(session, "skill", skill["id"], "add")
    assert [s["slug"] for s in store.skills_for_run(None, session)] == ["handy"]


def test_deleted_item_in_a_diff_is_soft_dropped(tmp_path, monkeypatch):
    """An override naming something since removed from the library must be ignored, not
    raise — the same policy as a sub-agent role that names a deleted tool."""
    _setup(tmp_path, monkeypatch)
    session = _session(None)
    skill = store.create_skill("Doomed", "b")
    store.set_session_skill_mcp(session, "skill", skill["id"], "add")
    assert len(store.skills_for_run(None, session)) == 1

    store.delete_skill(skill["id"])
    assert store.skills_for_run(None, session) == []          # no raise


def test_mcp_overrides_reach_the_resolved_specs(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    session = _session(None)
    server = route.create_server(ServerPayload(name="Lean LSP", command="uvx",
                                               args=["lean-lsp-mcp"]))
    assert store.mcp_server_specs(None, session) == {}
    store.set_session_skill_mcp(session, "mcp_server", server["id"], "add")
    assert list(store.mcp_server_specs(None, session)) == ["lean-lsp"]


def test_a_session_cannot_opt_into_a_disabled_server(tmp_path, monkeypatch):
    """Turning a server off in the Library is a global "stop using this". A per-session
    opt-in must not quietly resurrect it."""
    _setup(tmp_path, monkeypatch)
    session = _session(None)
    server = route.create_server(ServerPayload(name="Off", command="x", enabled=False))
    store.set_session_skill_mcp(session, "mcp_server", server["id"], "add")
    assert store.mcp_server_specs(None, session) == {}


def test_capabilities_endpoint_reports_provenance(tmp_path, monkeypatch):
    """`source` is what makes the two tiers legible — and why a global item is `locked`."""
    _setup(tmp_path, monkeypatch)
    project = store.create_project("p", title="P")["id"]
    session = _session(project)
    glob = store.create_skill("Global", "b")
    store.set_skill_assignment(glob["id"], is_global=True)
    proj = store.create_skill("Proj", "b")
    store.set_skill_assignment(proj["id"], is_global=False, project_ids=[project])
    mine = store.create_skill("Mine", "b")
    store.set_session_skill_mcp(session, "skill", mine["id"], "add")

    by_slug = {s["slug"]: s for s in route.session_skills_mcp(session)["skills"]}
    assert (by_slug["global"]["source"], by_slug["global"]["locked"]) == ("global", True)
    assert (by_slug["proj"]["source"], by_slug["proj"]["on"]) == ("project", True)
    assert (by_slug["mine"]["source"], by_slug["mine"]["on"]) == ("session", True)


def test_validation_and_404s(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    session = _session(None)
    with pytest.raises(HTTPException) as exc:
        route.session_skills_mcp("nope")
    assert exc.value.status_code == 404
    for bad in (SkillMcpToggle(kind="nonsense", item_id="x", action="add"),
                SkillMcpToggle(kind="skill", item_id="x", action="sideways")):
        with pytest.raises(HTTPException) as exc:
            route.update_session_skills_mcp(session, bad)
        assert exc.value.status_code == 400
