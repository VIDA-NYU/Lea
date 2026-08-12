from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import db, store
from app.config import LeaConfig
from app.github_source import GitHubSourceError
from app.routes import projects as projects_route
from app.routes.projects import GithubImportPreviewRequest
from app.routes.projects import FilePut


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "adapter.sqlite3")
    db.init_db()
    monkeypatch.setattr(
        projects_route,
        "load_config",
        lambda: LeaConfig(model="m", max_turns=3, lea_root=tmp_path, max_spend_usd=None),
    )
    return tmp_path / "workspace" / "proofs"


def test_by_slug_preview_ensures_an_empty_destination_and_forwards_targets(tmp_path, monkeypatch):
    proofs_root = _setup(tmp_path, monkeypatch)
    captured = {}

    def fake_preview(**kwargs):
        captured.update(kwargs)
        return {"preview_id": "preview-1", "project": kwargs["project"]}

    monkeypatch.setattr(projects_route.github_import_service, "preview_import", fake_preview)
    result = projects_route.preview_project_github_import_by_slug(
        "overleaf-doc",
        GithubImportPreviewRequest(
            repository_url="https://github.com/owner/repo",
            project_name="Overleaf Document",
            namespace="Lea.OverleafDocument",
        ),
    )

    project = captured["project"]
    assert result["preview_id"] == "preview-1"
    assert project["slug"] == "overleaf-doc"
    assert project["namespace"] == "Lea.OverleafDocument"
    assert captured["proofs_root"] == proofs_root
    assert (proofs_root / "Lea" / "OverleafDocument" / ".lea" / "blueprint.md").is_file()


def test_import_routes_return_structured_source_errors_and_scope_progress(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    first = projects_route.create_project(projects_route.ProjectCreate(title="First"))
    second = projects_route.create_project(projects_route.ProjectCreate(title="Second"))

    def invalid_source(**_kwargs):
        raise GitHubSourceError("bad repository", "invalid_repository_url")

    monkeypatch.setattr(projects_route.github_import_service, "preview_import", invalid_source)
    with pytest.raises(HTTPException) as exc:
        projects_route.preview_project_github_import(
            first["id"],
            GithubImportPreviewRequest(repository_url="https://example.com/not-github"),
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == {
        "error": "invalid_repository_url",
        "message": "bad repository",
    }

    imported = store.create_github_import(
        project_id=first["id"],
        source_url="https://github.com/owner/repo",
        source_commit_sha="a" * 40,
        destination_namespace=first["namespace"],
    )
    with pytest.raises(HTTPException) as wrong_project:
        projects_route.get_project_github_import(second["id"], imported["id"])
    assert wrong_project.value.status_code == 404

    with pytest.raises(HTTPException) as busy:
        projects_route.write_project_file(
            first["id"], FilePut(path="New.lean", content="theorem new : True := by trivial\n")
        )
    assert busy.value.status_code == 409
    assert busy.value.detail["error"] == "project_busy"
