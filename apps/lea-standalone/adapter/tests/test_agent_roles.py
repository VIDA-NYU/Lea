"""v2.5 B2/B3: user-authored sub-agent roles.

The load-bearing test is the last one: a role created here must end up in the schema the
coordinator is offered. Everything else is plumbing that can pass while the feature does
nothing — which is exactly the failure mode B1 exists to close.
"""

import pytest
import yaml
from fastapi import HTTPException

from app import db, roles_catalog, store
from app.routes import subagents as route
from app.routes.subagents import RoleCreate, RoleUpdate


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def test_create_list_update_delete(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    created = route.create_role(RoleCreate(
        name="Counterexample Hunter",
        description="Look for a counterexample before committing to a proof.",
        system_prompt="You hunt counterexamples.",
        tools=["read_file", "lean_check"], max_turns=8))
    assert created["name"] == "counterexample-hunter"
    assert created["origin"] == "user"
    assert created["effective"]["max_turns"] == 8

    listed = route.list_profiles()["profiles"]
    assert {p["origin"] for p in listed} == {"builtin", "user"}
    assert any(p["name"] == "counterexample-hunter" for p in listed)

    updated = route.update_role(created["id"], RoleUpdate(max_turns=3))
    assert updated["effective"]["max_turns"] == 3
    assert updated["effective"]["system_prompt"] == "You hunt counterexamples."  # untouched
    assert updated["name"] == "counterexample-hunter"                            # slug stable

    assert route.delete_role(created["id"])["deleted"] is True
    assert all(p["origin"] == "builtin" for p in route.list_profiles()["profiles"])


def test_a_role_needs_instructions(tmp_path, monkeypatch):
    """A role with no prompt is not a role — it would spawn a generalist wearing a name."""
    _setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        route.create_role(RoleCreate(name="Empty", system_prompt="   "))
    assert exc.value.status_code == 400
    assert "instructions" in exc.value.detail


def test_a_builtin_name_is_reserved(tmp_path, monkeypatch):
    """Two roles answering to one name makes "which one ran?" unanswerable."""
    _setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        route.create_role(RoleCreate(name="proof candidate", system_prompt="x"))
    assert exc.value.status_code == 400
    assert "built-in" in exc.value.detail


def test_duplicate_user_names_are_refused(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    route.create_role(RoleCreate(name="Scout", system_prompt="x"))
    with pytest.raises(HTTPException):
        route.create_role(RoleCreate(name="Scout", system_prompt="y"))


def test_bad_max_turns_is_refused(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException):
        route.create_role(RoleCreate(name="Zero", system_prompt="x", max_turns=0))


def test_404s(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    for call in (lambda: route.update_role("nope", RoleUpdate(name="x")),
                 lambda: route.delete_role("nope")):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404


def test_materialization_writes_a_profile_the_prover_accepts(tmp_path, monkeypatch):
    """Rows in, files out. The YAML must satisfy the prover's own parser, and must carry
    ONLY keys it allows — an unknown key makes it refuse the whole file, turning a stray
    column into a role that silently vanishes."""
    _setup(tmp_path, monkeypatch)
    route.create_role(RoleCreate(
        name="Scout", description="Scouts.", system_prompt="You scout.",
        tools=["read_file"], max_turns=4))
    tempdir, skipped = roles_catalog.materialize_roles()
    assert skipped == []
    try:
        written = yaml.safe_load((__import__("pathlib").Path(tempdir) / "scout.yaml").read_text())
        assert written["name"] == "scout"
        assert written["system_prompt"] == "You scout."
        assert written["tools"] == ["read_file"]

        from lea.profiles import _ALLOWED_KEYS, parse_profile
        assert set(written) <= _ALLOWED_KEYS
        parsed = parse_profile("scout", written)      # the prover's own validator
        assert parsed.max_turns == 4
    finally:
        roles_catalog.cleanup(tempdir)


def test_a_user_role_reaches_the_coordinators_schema(tmp_path, monkeypatch):
    """THE test. Everything above can pass while the coordinator is never told the role
    exists — the silent failure B1 was built to close. This walks the whole path:
    row → YAML → `agent_dirs` → discovery → the enum the model is actually offered."""
    _setup(tmp_path, monkeypatch)
    route.create_role(RoleCreate(
        name="Counterexample Hunter",
        description="Look for a counterexample first.",
        system_prompt="You hunt counterexamples."))

    tempdir, _ = roles_catalog.materialize_roles()
    try:
        from lea.config import LeaConfig
        from lea.runctx import run_context
        from lea.subagents import build_spawn_schema

        cfg = LeaConfig(model="x/y", max_turns=None, agent_dirs=[tempdir])
        with run_context(working_dir=None, run_key="t", depth=0, config=cfg):
            prop = build_spawn_schema()["input_schema"]["properties"]["subagent_type"]

        assert "counterexample-hunter" in prop["enum"]
        assert "Look for a counterexample first." in prop["description"]
        # ...and the built-ins are still offered alongside it.
        assert "proof-candidate" in prop["enum"]
    finally:
        roles_catalog.cleanup(tempdir)
