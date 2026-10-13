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
    source_bundle = {
        "version": 2,
        "targetKey": "sample",
        "targetKind": "theorem",
        "statement": "A sample theorem.",
        "proof": "A sample proof.",
        "proofAssociation": {
            "status": "associated",
            "method": "adjacent",
            "sourceFile": "main.tex",
            "sourceStartLine": 5,
            "sourceEndLine": 7,
            "proofHash": "",
        },
        "statementLocation": {
            "sourceFile": "main.tex",
            "sourceStartLine": 1,
            "sourceEndLine": 4,
        },
        "uses": [],
        "context": "",
        "relevantSource": [],
        "mirror": None,
        "bundleHash": "a" * 64,
    }

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
            targets=[{
                "origin_key": "doc:theorem:sample",
                "label": "sample",
                "declaration_name": "sample",
                "kind": "theorem",
                "display_title": "Sample",
                "source_bundle": source_bundle,
            }],
        ),
    )

    project = captured["project"]
    assert result["preview_id"] == "preview-1"
    assert project["slug"] == "overleaf-doc"
    assert project["namespace"] == "Lea.OverleafDocument"
    assert captured["proofs_root"] == proofs_root
    assert captured["targets"][0].source_bundle == source_bundle
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


def test_target_status_exposes_the_session_that_owns_the_current_artifact(tmp_path, monkeypatch):
    proofs_root = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(projects_route.ProjectCreate(title="Imported"))
    formalization = store.create_formalization(
        project_id=project["id"],
        loose_session_id=None,
        display_title="Imported proof",
        declaration_name="imported_proof",
        origin="overleaf",
        origin_key="doc:theorem:imported_proof",
    )
    session = store.create_session("GitHub import", project_id=project["id"], origin="github_import")
    store.link_session_formalization(session["id"], formalization["id"])
    store.link_formalization_file(formalization["id"], "Imported.lean", "primary")
    content = "theorem imported_proof : True := by trivial\n"
    repo = projects_route.project_service.project_repo_dir(project, proofs_root)
    (repo / "Imported.lean").write_text(content)
    store.add_code_step(
        session["id"],
        None,
        "Imported.lean",
        content=content,
        author="environment",
        check_status="ok",
        formalization_id=formalization["id"],
    )
    store.upsert_artifact(
        project_id=project["id"],
        session_id=session["id"],
        run_id=None,
        declaration_name="imported_proof",
        kind="proof",
        path="Imported.lean",
        module_name=f"{project['namespace']}.Imported",
        formalization_id=formalization["id"],
    )

    result = projects_route.project_target_status_by_slug(
        project["slug"], declarations="imported_proof"
    )

    assert result["targets"][0]["formalization_id"] == formalization["id"]
    assert result["targets"][0]["session_id"] == session["id"]
