from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import (
    alignment_store,
    db,
    formalizations,
    github_import_service,
    github_source,
    projects,
    store,
)
from app.github_project_import import (
    ImportLimits,
    ImportPlanningError,
    SourceInventory,
    TaggedTarget,
    inventory_source,
    plan_import,
)
from app.github_source import (
    GitHubRepository,
    GitHubSourceError,
    clone_repository,
    parse_github_repository_url,
)


def _git_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    for args in (
        ["init", "-q"],
        ["config", "user.name", "test"],
        ["config", "user.email", "test@example.com"],
        ["add", "-A"],
        ["commit", "-q", "-m", "fixture"],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return path


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "adapter.sqlite3")
    db.init_db()
    proofs_root = tmp_path / "workspace" / "proofs"
    monkeypatch.setattr(github_import_service, "STAGING_ROOT", tmp_path / "github-imports")
    monkeypatch.setattr(github_import_service, "github_token", lambda: None)
    return proofs_root


def _source_bundle(label: str) -> dict:
    payload = {
        "version": 2,
        "targetKey": label,
        "targetKind": "theorem",
        "statement": f"Statement for {label}.",
        "proof": f"Proof for {label}.",
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
    }
    identity = {
        "version": payload["version"],
        "targetKey": payload["targetKey"],
        "targetKind": payload["targetKind"],
        "statement": payload["statement"],
        "proof": payload["proof"],
        "proofAssociation": {
            key: payload["proofAssociation"][key]
            for key in ("status", "method", "sourceFile", "proofHash")
        },
        "uses": payload["uses"],
        "context": payload["context"],
        "relevantSource": payload["relevantSource"],
        "mirror": payload["mirror"],
    }
    payload["bundleHash"] = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def test_import_checks_lean_and_retains_source_without_independent_evaluator(tmp_path, monkeypatch):
    proofs_root = _setup(tmp_path, monkeypatch)
    project = projects.provision_project("Destination", proofs_root)
    source_repo = _git_repo(
        tmp_path / "semantic-remote",
        {
            "Bundle.lean": (
                "namespace Lea.Source\n"
                "theorem first : True := by trivial\n"
                "theorem second : True := by trivial\n"
                "end Lea.Source\n"
            ),
        },
    )
    repository = GitHubRepository(
        owner="owner",
        name="repo",
        source_url="https://github.com/owner/repo",
        clone_url=str(source_repo),
    )
    targets = [
        TaggedTarget(
            origin_key=f"doc:theorem:{name}",
            label=name,
            declaration_name=name,
            kind="theorem",
            display_title=name.title(),
            statement=f"Statement for {name}.",
            source_hash=character * 64,
            source_bundle=_source_bundle(name),
        )
        for name, character in (("first", "a"), ("second", "b"))
    ]
    lean_checks = []
    evaluator_submissions = []

    monkeypatch.setattr(github_import_service, "parse_github_repository_url", lambda _url: repository)
    monkeypatch.setattr(github_import_service, "enqueue_import", lambda *_args: None)

    def check(path):
        lean_checks.append(path)
        return SimpleNamespace(status="ok", detail="checked")

    monkeypatch.setattr(github_import_service, "interface_check", check)

    preview = github_import_service.preview_import(
        project=project,
        proofs_root=proofs_root,
        repository_url=repository.source_url,
        targets=targets,
    )
    progress = github_import_service.confirm_import(
        project=project,
        proofs_root=proofs_root,
        preview_id=preview["preview_id"],
    )
    github_import_service._check_import(progress["id"], proofs_root)

    assert len(lean_checks) == 1
    assert len(evaluator_submissions) == 0
    for name in ("first", "second"):
        formalization = store.find_formalization_by_declaration(
            project_id=project["id"],
            loose_session_id=None,
            declaration_name=name,
        )
        history = alignment_store.history(formalization["id"])
        assert history == []
    finished = store.github_import_progress(progress["id"])
    assert finished["status"] == "complete"
    assert "targets_json" not in finished
    persisted = store.get_github_import(progress["id"])
    assert len(json.loads(persisted["targets_json"])) == 2


def test_root_url_parser_rejects_lookalikes_and_non_root_paths():
    parsed = parse_github_repository_url("https://github.com/owner/repo.git")
    assert parsed.source_url == "https://github.com/owner/repo"
    assert parsed.clone_url.endswith("/owner/repo.git")
    for invalid in (
        "http://github.com/owner/repo",
        "https://github.example/owner/repo",
        "https://github.com/owner/repo/tree/main",
        "https://user:secret@github.com/owner/repo",
    ):
        try:
            parse_github_repository_url(invalid)
        except GitHubSourceError as exc:
            assert exc.code == "invalid_repository_url"
        else:  # pragma: no cover
            raise AssertionError(f"accepted unsafe URL: {invalid}")


def test_private_clone_path_sanitizes_the_persisted_origin(tmp_path, monkeypatch):
    source_repo = _git_repo(tmp_path / "private-source", {"Proof.lean": "theorem p : True := by trivial\n"})
    destination = tmp_path / "clone"
    seen = {}

    def fake_inject(url, token):
        seen["token"] = token
        return url

    monkeypatch.setattr(github_source, "_inject_token", fake_inject)
    clone_repository(str(source_repo), destination, token="secret-token")
    origin = subprocess.run(
        ["git", "-C", str(destination), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config = (destination / ".git" / "config").read_text()
    assert seen["token"] == "secret-token"
    assert origin == str(source_repo)
    assert "secret-token" not in config


def test_planner_is_additive_and_rewrites_only_incoming_namespace(tmp_path):
    source_repo = _git_repo(
        tmp_path / "source",
        {
            "Goal.lean": "namespace Lea.Source\ntheorem goal : True := by trivial\nend Lea.Source\n",
            "Helper.lean": "namespace Lea.Source\nlemma helper : True := by trivial\nend Lea.Source\n",
            "bad-name.lean": "namespace Lea.Source\nlemma bad : True := by trivial\nend Lea.Source\n",
            "vendor/Skip.lean": "namespace Lea.Source\nlemma skip : True := by trivial\nend Lea.Source\n",
        },
    )
    destination = tmp_path / "destination"
    destination.mkdir()
    original = "namespace Lea.Target\ntheorem goal : True := by sorry\nend Lea.Target\n"
    (destination / "Goal.lean").write_text(original)

    inventory = inventory_source(source_repo, ImportLimits())
    plan = plan_import(inventory, destination, "Lea.Target")
    rows = {row.source_path: row for row in plan.files}

    assert rows["Goal.lean"].disposition == "path_conflict"
    assert rows["Helper.lean"].disposition == "add"
    assert "namespace Lea.Target" in rows["Helper.lean"].rewritten_content
    assert rows["bad-name.lean"].disposition == "unsupported_module_layout"
    assert rows["vendor/Skip.lean"].disposition == "excluded"
    assert (destination / "Goal.lean").read_text() == original


def test_planner_ignores_namespace_mentions_in_prose_and_blocks_empty_inventory(tmp_path):
    source_repo = _git_repo(
        tmp_path / "source-prose",
        {
            "Only.lean": (
                '-- Lea.Unrelated and "Lea.AlsoUnrelated" are prose\n'
                "namespace Lea.Source\nlemma only : True := by trivial\nend Lea.Source\n"
            ),
        },
    )
    plan = plan_import(inventory_source(source_repo), tmp_path / "destination", "Lea.Target")
    assert plan.source_namespace == "Lea.Source"
    assert plan.files[0].disposition == "add"
    assert "Lea.Unrelated" in plan.files[0].rewritten_content

    empty = plan_import(SourceInventory(files=[], total_bytes=0), tmp_path / "destination", "Lea.Target")
    assert empty.blocking_error == {
        "code": "no_lean_files",
        "message": "The repository has no tracked Lean files.",
    }


def test_confirm_import_populates_matching_target_and_keeps_helper_reusable(tmp_path, monkeypatch):
    proofs_root = _setup(tmp_path, monkeypatch)
    project = projects.provision_project("Destination", proofs_root)
    repo = projects.project_repo_dir(project, proofs_root)
    (repo / "Conflict.lean").write_text(
        "namespace Lea.Destination\nlemma conflict : True := by trivial\nend Lea.Destination\n"
    )
    formalization = store.create_formalization(
        project_id=project["id"],
        loose_session_id=None,
        display_title="Goal",
        declaration_name="goal",
        kind="theorem",
    )
    source_repo = _git_repo(
        tmp_path / "remote",
        {
            "Goal.lean": "namespace Lea.Source\ntheorem goal : True := by trivial\nend Lea.Source\n",
            "Helper.lean": "namespace Lea.Source\nlemma helper : True := by trivial\nend Lea.Source\n",
            "Another.lean": "namespace Lea.Source\nlemma another : True := by trivial\nend Lea.Source\n",
            "Conflict.lean": "namespace Lea.Source\nlemma other : True := by trivial\nend Lea.Source\n",
        },
    )
    repository = GitHubRepository(
        owner="owner",
        name="repo",
        source_url="https://github.com/owner/repo",
        clone_url=str(source_repo),
    )
    monkeypatch.setattr(github_import_service, "parse_github_repository_url", lambda _url: repository)
    monkeypatch.setattr(github_import_service, "enqueue_import", lambda *_args: None)
    monkeypatch.setattr(
        github_import_service,
        "interface_check",
        lambda _path: SimpleNamespace(status="ok", detail="checked"),
    )

    preview = github_import_service.preview_import(
        project=project,
        proofs_root=proofs_root,
        repository_url=repository.source_url,
    )
    rows = {row["source_path"]: row for row in preview["plan"]["files"]}
    assert rows["Goal.lean"]["disposition"] == "add"
    assert rows["Helper.lean"]["disposition"] == "add"
    assert rows["Conflict.lean"]["disposition"] == "path_conflict"

    monkeypatch.setattr(store, "project_has_active_run", lambda _project_id: True)
    with pytest.raises(ImportPlanningError, match="currently writing"):
        github_import_service.confirm_import(
            project=project,
            proofs_root=proofs_root,
            preview_id=preview["preview_id"],
        )
    monkeypatch.setattr(store, "project_has_active_run", lambda _project_id: False)
    progress = github_import_service.confirm_import(
        project=project,
        proofs_root=proofs_root,
        preview_id=preview["preview_id"],
    )
    github_import_service._check_import(progress["id"], proofs_root)
    finished = store.github_import_progress(progress["id"])

    assert finished["status"] == "complete"
    assert (repo / "Goal.lean").is_file()
    assert (repo / "Helper.lean").is_file()
    assert "lemma conflict" in (repo / "Conflict.lean").read_text()
    decorated = formalizations.get(formalization["id"])
    assert decorated["primary_path"] == "Goal.lean"
    assert decorated["validity_status"] == "unchecked"
    reusable = store.find_unbound_imported_declarations(project["id"], "helper")
    assert len(reusable) == 1
    assert all(
        row["declaration_name"] != "helper"
        for row in store.list_raw_project_formalizations(project["id"])
    )

    helper_formalization = store.create_formalization(
        project_id=project["id"],
        loose_session_id=None,
        display_title="Helper",
        declaration_name="helper",
        kind="lemma",
    )
    adoption = github_import_service.try_adopt_imported_declaration(
        project, helper_formalization, proofs_root
    )
    assert adoption == {"adopted": True, "path": "Helper.lean", "import_id": progress["id"]}
    assert formalizations.get(helper_formalization["id"])["primary_path"] == "Helper.lean"

    incompatible = store.create_formalization(
        project_id=project["id"],
        loose_session_id=None,
        display_title="Another",
        declaration_name="another",
        kind="definition",
    )
    rejected = github_import_service.try_adopt_imported_declaration(
        project, incompatible, proofs_root
    )
    assert rejected["adopted"] is False
    assert "kind is incompatible" in rejected["reason"]

    import_count = len(store.list_project_github_imports(project["id"]))
    repeat_preview = github_import_service.preview_import(
        project=project,
        proofs_root=proofs_root,
        repository_url=repository.source_url,
    )
    repeat = github_import_service.confirm_import(
        project=project,
        proofs_root=proofs_root,
        preview_id=repeat_preview["preview_id"],
    )
    assert repeat["id"] == progress["id"]
    assert repeat["reused"] is True
    assert len(store.list_project_github_imports(project["id"])) == import_count


def test_startup_recovery_requeues_checks_and_settles_missing_apply_staging(tmp_path, monkeypatch):
    proofs_root = _setup(tmp_path, monkeypatch)
    project = projects.provision_project("Recovery", proofs_root)
    checking = store.create_github_import(
        project_id=project["id"],
        source_url="https://github.com/owner/repo",
        source_commit_sha="b" * 40,
        destination_namespace=project["namespace"],
    )
    store.set_github_import_status(checking["id"], "checking")
    applying = store.create_github_import(
        project_id=project["id"],
        source_url="https://github.com/owner/other",
        source_commit_sha="c" * 40,
        destination_namespace=project["namespace"],
    )
    queued = []
    monkeypatch.setattr(
        github_import_service,
        "enqueue_import",
        lambda import_id, root: queued.append((import_id, root)),
    )

    github_import_service.recover_github_imports_at_startup(proofs_root)

    assert queued == [(checking["id"], proofs_root)]
    interrupted = store.get_github_import(applying["id"])
    assert interrupted["status"] == "complete_with_issues"
    assert "interrupted" in interrupted["error_detail"].lower()
