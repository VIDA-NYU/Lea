"""W2 tests: Skill Factory CRUD + assignment routes (D50). Route functions are
called directly (as the other route tests do). Only the DB is patched — these
routes have no config/proofs-root coupling (skills are DB rows, D45)."""

import pytest
from fastapi import HTTPException

from app import db, ghimport, store
from app.routes import skills as skills_route
from app.routes.skills import SkillAssignment, SkillCreate, SkillImport, SkillUpdate


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def test_create_list_get_a_skill(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    created = skills_route.create_skill(SkillCreate(name="Ring Tactics", body="# use `ring`"))
    assert created["slug"] == "ring-tactics"
    assert created["is_global"] is False
    assert created["project_ids"] == []

    listed = skills_route.list_skills()["skills"]
    assert [s["id"] for s in listed] == [created["id"]]

    fetched = skills_route.get_skill(created["id"])
    assert fetched["id"] == created["id"]
    assert fetched["body"] == "# use `ring`"


def test_create_applies_scope_in_the_same_call(tmp_path, monkeypatch):
    # D58: "Add → choose scope" — is_global / project_ids on create come back applied.
    _setup(tmp_path, monkeypatch)
    p = store.create_project("proj-a", title="A")

    glob = skills_route.create_skill(SkillCreate(name="Global", body="g", is_global=True))
    assert glob["is_global"] is True

    scoped = skills_route.create_skill(
        SkillCreate(name="Scoped", body="s", project_ids=[p["id"]])
    )
    assert scoped["is_global"] is False
    assert scoped["project_ids"] == [p["id"]]


def test_create_rejects_blank_name(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        skills_route.create_skill(SkillCreate(name="   ", body="x"))
    assert exc.value.status_code == 400


def test_create_rejects_unknown_project_in_scope(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        skills_route.create_skill(SkillCreate(name="Bad scope", project_ids=["ghost"]))
    assert exc.value.status_code == 400


def test_update_edits_name_and_body(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    created = skills_route.create_skill(SkillCreate(name="Original", body="v1"))
    slug = created["slug"]

    updated = skills_route.update_skill(created["id"], SkillUpdate(name="Renamed", body="v2"))
    assert updated["name"] == "Renamed"
    assert updated["body"] == "v2"
    assert updated["slug"] == slug  # slug is immutable (D45)


def test_update_missing_is_404(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        skills_route.update_skill("no-such-id", SkillUpdate(name="x"))
    assert exc.value.status_code == 404


def test_assignment_sets_scope_and_rejects_unknown_project(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    p = store.create_project("proj-a", title="A")
    skill = skills_route.create_skill(SkillCreate(name="Scoped", body="s"))

    assigned = skills_route.set_skill_assignment(
        skill["id"], SkillAssignment(is_global=False, project_ids=[p["id"]])
    )
    assert assigned["project_ids"] == [p["id"]]

    with pytest.raises(HTTPException) as exc:
        skills_route.set_skill_assignment(
            skill["id"], SkillAssignment(is_global=False, project_ids=["ghost"])
        )
    assert exc.value.status_code == 400


def test_assignment_missing_is_404(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        skills_route.set_skill_assignment("no-such-id", SkillAssignment(is_global=True))
    assert exc.value.status_code == 404


def test_import_creates_and_scopes_a_skill(tmp_path, monkeypatch):
    # W4/D56: /import fetches from GitHub then creates + scopes in one call. The
    # fetch is stubbed (the ghimport clone path is covered in test_ghimport.py).
    _setup(tmp_path, monkeypatch)
    p = store.create_project("proj-a", title="A")
    monkeypatch.setattr(
        skills_route, "github_token", lambda: None
    )
    monkeypatch.setattr(
        skills_route.ghimport, "fetch_skill",
        lambda url, token: ghimport.ImportedSkill(
            name="Imported Skill", body="# body",
            source_url=url, source_ref="main",
        ),
    )

    created = skills_route.import_skill.worker(
        SkillImport(url="https://github.com/you/repo", project_ids=[p["id"]])
    )
    assert created["name"] == "Imported Skill"
    assert created["body"] == "# body"
    assert created["source_url"] == "https://github.com/you/repo"
    assert created["source_ref"] == "main"
    assert created["project_ids"] == [p["id"]]


def test_import_bad_url_is_400(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(skills_route, "github_token", lambda: None)

    def boom(url, token):
        raise ghimport.GitHubImportError("Not a GitHub URL.")

    monkeypatch.setattr(skills_route.ghimport, "fetch_skill", boom)
    with pytest.raises(HTTPException) as exc:
        skills_route.import_skill.worker(SkillImport(url="https://gitlab.com/o/r"))
    assert exc.value.status_code == 400


def test_delete_then_missing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    created = skills_route.create_skill(SkillCreate(name="Doomed", body="x"))

    assert skills_route.delete_skill(created["id"]) == {"deleted": True, "id": created["id"]}
    with pytest.raises(HTTPException) as exc:
        skills_route.delete_skill(created["id"])
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        skills_route.get_skill(created["id"])
    assert exc.value.status_code == 404
