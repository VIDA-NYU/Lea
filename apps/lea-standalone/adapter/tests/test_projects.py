"""P2 tests: the project service — provision the on-disk repo + seeds, resolve a
session's repo, delete with cascade. The proofs root is a tmp dir, so these never
touch the real workspace."""

from __future__ import annotations

import subprocess

from app import db, projects, store


def _init_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()


def test_slugify_and_unique_slug(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    assert projects.slugify("Epsilon Delta!") == "epsilon-delta"
    assert projects.slugify("") == "project"

    proofs = tmp_path / "proofs"
    first = projects.provision_project("Epsilon Delta", proofs)
    assert first["slug"] == "epsilon-delta"
    # A second project with the same title gets a distinct slug.
    second = projects.provision_project("Epsilon Delta", proofs)
    assert second["slug"] == "epsilon-delta-2"


def test_provision_creates_repo_with_committed_seed_docs(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    proofs = tmp_path / "proofs"

    project = projects.provision_project("Continuity", proofs, description="ε–δ work")
    assert project["namespace"] == "Lea.Continuity"
    assert project["repo_path"] == "proofs/Lea/Continuity"
    assert project["description"] == "ε–δ work"

    repo = proofs / "Lea" / "Continuity"
    assert (repo / ".git").is_dir()
    lea = repo / ".lea"
    for name in ("instructions.md", "memory.md", "blueprint.md"):
        assert (lea / name).is_file(), f"{name} should be seeded"
    # The blueprint seed points the agent at the real namespace (example decl).
    assert "Lea.Continuity.continuous_sq" in (lea / "blueprint.md").read_text()

    # The seeds are committed (not just on disk): the working tree is clean.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert status == ""
    # And there are two commits: the empty root + the seed commit.
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True
    ).stdout.strip().splitlines()
    assert len(log) == 2


def test_repo_for_session_loose_vs_in_project(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    proofs = tmp_path / "proofs"
    project = projects.provision_project("Graphs", proofs)

    loose = store.create_session("loose")
    in_proj = store.create_session("in project", project_id=project["id"])

    assert projects.repo_for_session(loose, proofs) == proofs / loose["id"]
    assert (
        projects.repo_for_session(in_proj, proofs, project)
        == proofs / "Lea" / "Graphs"
    )


def test_resolve_git_loose_vs_project(tmp_path, monkeypatch):
    # D24: the resolver returns (GitStore, repo_key) so git ops hit the right repo —
    # loose roots at proofs/ keyed by session id; project roots at proofs/Lea keyed
    # by <Project> (the shared repo).
    _init_db(tmp_path, monkeypatch)
    proofs = tmp_path / "proofs"
    project = projects.provision_project("Graphs", proofs)
    loose = store.create_session("loose")
    in_proj = store.create_session("in", project_id=project["id"])

    gs_l, key_l = projects.resolve_git(loose["id"], proofs)
    gs_p, key_p = projects.resolve_git(in_proj["id"], proofs)
    assert gs_l.root == proofs and key_l == loose["id"]
    assert gs_p.root == proofs / "Lea" and key_p == "Graphs"
    assert projects.resolve_git("missing-session", proofs) is None


def test_compose_context_message(tmp_path, monkeypatch):
    # D25: one marked user message folding instructions + memory + blueprint + files.
    _init_db(tmp_path, monkeypatch)
    proofs = tmp_path / "proofs"
    project = projects.provision_project("Eps", proofs)
    repo = proofs / "Lea" / "Eps"
    (repo / ".lea" / "instructions.md").write_text("# Instructions\nProve continuity.")
    (repo / ".lea" / "files").mkdir()
    (repo / ".lea" / "files" / "paper.txt").write_text("notes")
    overleaf = repo / ".lea" / "files" / "overleaf"
    overleaf.mkdir()
    (overleaf / "main.tex").write_text(
        "\\documentclass{article}\n\\input{sections/results}\n\\begin{document}\n"
    )
    (overleaf / "notation.sty").write_text("\\newcommand{\\NN}{\\mathbb{N}}\n")
    (repo / "existing.lean").write_text("theorem existing : True := by trivial\n")

    msg = projects.compose_context_message(project, repo)
    assert msg["role"] == "user"
    assert msg["content"].startswith(projects.CONTEXT_MARKER)
    assert "Lea.Eps" in msg["content"]
    assert "project title is a human-facing display name" in msg["content"]
    assert "namespace `Lea.Eps` is authoritative" in msg["content"]
    assert "Do not derive a namespace from the display name" in msg["content"]
    assert "Prove continuity." in msg["content"]
    assert "`.lea/files/paper.txt`" in msg["content"]  # inventory line
    assert "`.lea/files/overleaf/main.tex`" in msg["content"]
    assert "`.lea/files/overleaf/notation.sty`" in msg["content"]
    assert "Likely root document(s)" in msg["content"]
    assert "includes `sections/results`" in msg["content"]
    assert "read every mirrored LaTeX source before planning" in msg["content"]
    assert "## Project Lean modules" in msg["content"]
    assert "`existing.lean`" in msg["content"]
    # D26: the agent is told, concretely, to keep memory.md current with edit_file.
    assert ".lea/memory.md" in msg["content"]
    assert "edit_file" in msg["content"]
    assert projects.is_context_message(msg) is True
    assert projects.is_context_message({"role": "user", "content": "hi"}) is False
    assert projects.compose_context_message(None, repo) is None


def test_delete_project_removes_tree_and_cascades_rows(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    proofs = tmp_path / "proofs"
    project = projects.provision_project("Doomed", proofs)
    pid = project["id"]
    repo = proofs / "Lea" / "Doomed"

    # A session + run inside the project, to prove the cascade reaches dependents.
    sess = store.create_session("s", project_id=pid)
    store.create_run(sess["id"], "gpt-4o", "openai", 3, project_id=pid)
    assert repo.is_dir()

    assert projects.delete_project(pid, proofs) is True
    assert not repo.exists()
    assert store.get_project(pid) is None
    assert store.get_session(sess["id"]) is None
    # Deleting a missing project is a no-op False.
    assert projects.delete_project(pid, proofs) is False


# --- AUDIT-2026-07-24 C2: the namespace-migration busy interlock ---------------
# `migrate_project_namespace` rewrites every .lean/.md in the repo and then
# `shutil.move`s the directory. `_project_has_active_runs` is its only guard, and it
# used to test the session's DERIVED status for "running" — which D14 only ever
# returns for a session with no code yet. So it was blind to precisely the sessions
# worth protecting: an agent mid-run in a session that has already written a proof.

def _project_session_with_active_run(project, *, with_code):
    session = store.create_session("live work", project_id=project["id"])
    run = store.create_run(session["id"], "m", None, 3, project_id=project["id"])
    if with_code:
        store.add_code_step(
            session["id"], run["id"], "Lea/Demo/p.lean",
            content="theorem t : True := by trivial\n", author="agent", turn=1,
            check_status="ok",
        )
    store.update_run(run["id"], "running")
    return session, run


def test_active_run_is_detected_in_a_session_that_already_has_code(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    project = projects.provision_project("Demo", tmp_path / "proofs")
    session, _ = _project_session_with_active_run(project, with_code=True)

    # The derived status is the working-copy verdict ("ok" — the check passed), NOT
    # "running": this is the D14 behaviour the old interlock mistook for "no run is
    # active". The session's own active_run_count tells the truth.
    detail = store.session_detail(session["id"])
    assert detail["status"] == "ok"
    assert detail["active_run"] is not None
    assert projects._project_has_active_runs(project["id"]) is True


def test_active_run_still_detected_before_any_code_is_written(tmp_path, monkeypatch):
    """The one case the old check did catch — it must not regress."""
    _init_db(tmp_path, monkeypatch)
    project = projects.provision_project("Demo", tmp_path / "proofs")
    session, _ = _project_session_with_active_run(project, with_code=False)

    assert store.session_detail(session["id"])["status"] == "running"
    assert projects._project_has_active_runs(project["id"]) is True


def test_no_active_run_once_every_run_is_terminal(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    project = projects.provision_project("Demo", tmp_path / "proofs")
    _, run = _project_session_with_active_run(project, with_code=True)
    store.update_run(run["id"], "proved")

    assert projects._project_has_active_runs(project["id"]) is False


def test_migration_refuses_while_a_run_is_live_in_an_established_session(tmp_path, monkeypatch):
    """The consequence: renaming moved the repo out from under a running agent."""
    _init_db(tmp_path, monkeypatch)
    proofs = tmp_path / "proofs"
    project = projects.provision_project("Demo", proofs)
    _project_session_with_active_run(project, with_code=True)
    old_repo = projects.project_repo_dir(project, proofs)

    try:
        projects.migrate_project_namespace(
            project, proofs, title="Renamed", namespace="Lea.Renamed",
        )
    except projects.ProjectIdentityError as exc:
        assert exc.code == "project_busy"
        assert exc.status == 409
    else:
        raise AssertionError("Expected the migration to refuse while a run is active")

    # Nothing moved, and the index still points at the original namespace.
    assert old_repo.is_dir()
    assert not (proofs / "Lea" / "Renamed").exists()
    assert store.get_project(project["id"])["namespace"] == "Lea.Demo"


def test_migration_proceeds_once_no_run_is_active(tmp_path, monkeypatch):
    """The guard must not become a blanket refusal — an idle project still renames."""
    _init_db(tmp_path, monkeypatch)
    proofs = tmp_path / "proofs"
    project = projects.provision_project("Demo", proofs)
    session, run = _project_session_with_active_run(project, with_code=True)
    store.update_run(run["id"], "proved")
    repo = projects.project_repo_dir(project, proofs)
    (repo / "p.lean").write_text(
        "namespace Lea.Demo\n\ntheorem t : True := by trivial\n\nend Lea.Demo\n"
    )
    store.upsert_artifact(
        project_id=project["id"],
        session_id=session["id"],
        run_id=run["id"],
        declaration_name="t",
        kind="proof",
        path="p.lean",
        module_name="Lea.Demo.p",
    )
    # Prefixes without a dot boundary belong to a different namespace and
    # must never be rewritten by the migration.
    store.upsert_artifact(
        project_id=project["id"],
        session_id=session["id"],
        run_id=run["id"],
        declaration_name="other",
        kind="proof",
        path="other.lean",
        module_name="Lea.Demonstration.other",
    )

    result = projects.migrate_project_namespace(
        project, proofs, title="Renamed", namespace="Lea.Renamed",
    )

    assert result["project"]["namespace"] == "Lea.Renamed"
    assert result["migration"]["rebasedArtifactModules"] == 1
    assert (proofs / "Lea" / "Renamed").is_dir()
    assert not (proofs / "Lea" / "Demo").exists()
    artifacts = {
        row["declaration_name"]: row
        for row in store.list_artifacts_for_scope(project["id"])
    }
    assert artifacts["t"]["module_name"] == "Lea.Renamed.p"
    assert artifacts["other"]["module_name"] == "Lea.Demonstration.other"
    assert "namespace Lea.Renamed" in (proofs / "Lea" / "Renamed" / "p.lean").read_text()
