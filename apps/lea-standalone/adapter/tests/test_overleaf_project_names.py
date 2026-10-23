"""Readable defaults remain independent of durable Overleaf/proof identity."""

from concurrent.futures import ThreadPoolExecutor
import subprocess

import pytest
from alembic import command
from fastapi import BackgroundTasks, HTTPException

from app import db, migrations, projects, store
from app.config import LeaConfig
from app.routes import projects as routes
from app.routes.projects import MirrorRequest, OverleafDefaultNameRequest, ProjectIdentityUpdate


@pytest.fixture
def proofs(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "names.sqlite3")
    db.init_db()
    monkeypatch.setattr(routes, "load_config", lambda: LeaConfig(
        model="m", max_turns=3, lea_root=tmp_path, max_spend_usd=None))
    return tmp_path / "workspace" / "proofs"


def test_default_names_avoid_all_titles_and_normalized_namespaces(proofs):
    custom = projects.provision_project("  topology notes  ", proofs)
    first = projects.ensure_project("doc-one", proofs, overleaf_project_title="Topology Notes")
    second = projects.ensure_project("doc-two", proofs, overleaf_project_title="Topology Notes")
    assert (first["title"], second["title"]) == ("Topology Notes (2)", "Topology Notes (3)")
    assert first["namespace"] == "Lea.TopologyNotes2"
    assert first["slug"] == "doc-one" and first["name_source"] == "overleaf"
    assert custom["name_source"] == "custom"
    hyphen = projects.ensure_project("hyphen", proofs, overleaf_project_title="A-B")
    space = projects.ensure_project("space", proofs, overleaf_project_title="A B")
    assert hyphen["namespace"] == "Lea.AB"
    assert space["namespace"] == "Lea.AB2"
    # A directory left on disk must not be reused by a different project.
    (proofs / "Lea" / "Orphan").mkdir()
    orphan = projects.ensure_project("orphan", proofs, overleaf_project_title="Orphan")
    assert orphan["namespace"] == "Lea.Orphan2"


def test_concurrent_creation_reserves_names_and_same_slug_is_idempotent(proofs):
    with ThreadPoolExecutor(max_workers=8) as pool:
        created = list(pool.map(lambda i: projects.ensure_project(
            f"doc-{i}", None, overleaf_project_title="Same Title"), range(12)))
        repeated = list(pool.map(lambda _: projects.ensure_project(
            "shared-id", None, overleaf_project_title="Shared Title"), range(12)))
    assert len({p["title"].casefold() for p in created}) == 12
    assert len({p["namespace"] for p in created}) == 12
    assert len({p["id"] for p in repeated}) == 1
    assert len(store.list_projects()) == 13


def test_legacy_upgrade_preserves_proof_content_paths_and_custom_docs(proofs):
    legacy = projects.ensure_project("6a4584b313b8ddc4ba20e377", proofs)
    repo = projects.project_repo_dir(legacy, proofs)
    proof = repo / "Saved.lean"
    proof.write_text(f"namespace {legacy['namespace']}\ntheorem saved : True := by trivial\n")
    (repo / ".lea" / "memory.md").write_text("# My own heading\nKeep this text.\n")
    git_before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    proof_before = proof.read_bytes()
    upgraded = routes.initialize_overleaf_default_name.worker(legacy["slug"],
        OverleafDefaultNameRequest(overleaf_project_title="Fourier Notes"))["identity"]
    assert upgraded["projectName"] == "Fourier Notes" and upgraded["nameSource"] == "overleaf"
    assert upgraded["namespace"] == legacy["namespace"] and upgraded["repoPath"] == legacy["repo_path"]
    assert proof.read_bytes() == proof_before
    assert (repo / ".lea" / "instructions.md").read_text().startswith("# Instructions — Fourier Notes")
    assert (repo / ".lea" / "memory.md").read_text() == "# My own heading\nKeep this text.\n"
    # The rename commit preserves history and does not sweep up an untracked proof.
    subprocess.run(["git", "merge-base", "--is-ancestor", git_before, "HEAD"], cwd=repo, check=True)
    assert "?? Saved.lean" in subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)
    unchanged = projects.ensure_project(legacy["slug"], proofs, overleaf_project_title="Renamed in Overleaf")
    assert unchanged["title"] == "Fourier Notes"


def test_missing_title_can_be_adopted_later_but_custom_title_is_protected(proofs):
    fallback = projects.ensure_project("fallback", proofs, overleaf_project_title="")
    assert fallback["title"] == "Overleaf Project" and fallback["name_source"] == "generated"
    assert projects.adopt_overleaf_project_name("fallback", proofs, "")["name_source"] == "generated"
    store.update_project(fallback["id"], description="New description")
    assert store.get_project(fallback["id"])["name_source"] == "generated"
    store.update_project(fallback["id"], title="Overleaf Project")
    protected = projects.adopt_overleaf_project_name("fallback", proofs, "Paper")
    assert protected["title"] == "Overleaf Project" and protected["name_source"] == "custom"
    explicit = routes.update_project_identity_by_slug.worker("explicit",
        ProjectIdentityUpdate(project_name="Overleaf Project", mode="display-only", create_if_missing=True))
    assert explicit["identity"]["nameSource"] == "custom"


def test_tab_open_cannot_create_and_mirror_metadata_names_first_creation(proofs):
    with pytest.raises(HTTPException) as missing:
        routes.initialize_overleaf_default_name.worker("untouched", OverleafDefaultNameRequest(overleaf_project_title="Paper"))
    assert missing.value.status_code == 404 and store.list_projects() == []
    result = routes.mirror_overleaf_tex("paper-id", MirrorRequest(
        files=[], overleaf_project_title="My Paper"))
    assert result["identity"]["projectName"] == "My Paper"
    assert result["identity"]["namespace"] == "Lea.MyPaper"
    old_client = routes.mirror_overleaf_tex("legacy-client", MirrorRequest(files=[]))
    assert old_client["identity"]["projectName"] == "legacy-client"
    assert old_client["identity"]["nameSource"] == "generated"


def test_run_identity_initialization_uses_source_title_and_ignores_stale_namespace(proofs):
    result = routes.update_project_identity_by_slug.worker("no-mirror", ProjectIdentityUpdate(
        project_name="Old cached title", mode="default-name", create_if_missing=True,
        overleaf_project_title="Readable Paper", namespace="Lea.OpaqueId"))["identity"]
    assert result["projectName"] == "Readable Paper" and result["namespace"] == "Lea.ReadablePaper"
    again = routes.update_project_identity_by_slug.worker("no-mirror", ProjectIdentityUpdate(
        project_name="New title", mode="default-name", create_if_missing=True,
        overleaf_project_title="New title"))["identity"]
    assert again == result
    repeated = routes.initialize_overleaf_default_name.worker("no-mirror",
        OverleafDefaultNameRequest(overleaf_project_title="Renamed Overleaf title"))
    assert repeated["changed"] is False
    assert repeated["identity"] == result


def test_migration_classifies_only_known_linked_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "legacy.sqlite3")
    command.upgrade(migrations._config(), "0034_overleaf_approval_policy")
    for slug, title, linked in [("doc-id", "doc-id", 1), ("6a45", "P6a45", 1),
                               ("friendly", "Overleaf Project", 1), ("custom", "My Name", 1),
                               ("ordinary", "ordinary", 0)]:
        row = store.create_project(slug, title=title)
        if linked:
            store.mark_project_overleaf_linked(row["id"])
    db.init_db()
    assert {p["slug"]: p["name_source"] for p in store.list_projects()} == {
        "doc-id": "generated", "6a45": "generated", "friendly": "generated",
        "custom": "custom", "ordinary": "custom"}
    store.update_project(store.get_project_by_slug("doc-id")["id"], title="doc-id")
    with db.write() as conn:
        conn.execute("delete from alembic_version")
    db.init_db()
    assert store.get_project_by_slug("doc-id")["name_source"] == "custom"
