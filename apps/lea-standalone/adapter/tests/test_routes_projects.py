"""P3 tests: project CRUD routes (D31). Route functions are called directly (as the
other route tests do), with `load_config` patched so the proofs root is a tmp dir."""

import asyncio
import io

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app import db, store
from app.config import LeaConfig
from app.routes import projects as projects_route
from app.routes.projects import (
    DocUpdate,
    FilePut,
    NamespacePreviewRequest,
    ProjectCreate,
    ProjectIdentityUpdate,
    ProjectUpdate,
    SessionCreate,
)


def _upload(project_id, filename, data, content_type=None):
    """Call the async upload route directly with a constructed UploadFile."""
    headers = Headers({"content-type": content_type}) if content_type else None
    uf = UploadFile(filename=filename, file=io.BytesIO(data), headers=headers)
    return asyncio.run(projects_route.upload_file(project_id, uf))


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    monkeypatch.setattr(
        projects_route, "load_config",
        lambda: LeaConfig(model="m", max_turns=3, lea_root=tmp_path, max_spend_usd=None),
    )
    return tmp_path / "workspace" / "proofs"


def test_create_lists_and_gets_a_project(tmp_path, monkeypatch):
    proofs = _setup(tmp_path, monkeypatch)

    created = projects_route.create_project(ProjectCreate(title="Real Analysis", description="ε–δ"))
    assert created["slug"] == "real-analysis"
    assert created["namespace"] == "Lea.RealAnalysis"
    assert (proofs / "Lea" / "RealAnalysis" / ".lea" / "blueprint.md").is_file()

    listed = projects_route.list_projects()["projects"]
    assert len(listed) == 1
    assert listed[0]["id"] == created["id"]
    assert listed[0]["session_count"] == 0

    detail = projects_route.get_project(created["id"])
    assert detail["id"] == created["id"]
    assert detail["sessions"] == []  # no sessions yet


def test_create_rejects_blank_title(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        projects_route.create_project(ProjectCreate(title="   "))
    assert exc.value.status_code == 400


def test_detail_includes_project_sessions(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Topology"))
    sess = store.create_session("a proof", project_id=project["id"])

    detail = projects_route.get_project(project["id"])
    assert [s["id"] for s in detail["sessions"]] == [sess["id"]]
    assert projects_route.list_projects()["projects"][0]["session_count"] == 1


def test_create_session_in_project(tmp_path, monkeypatch):
    # D23: a session created inside a project is tagged with project_id and appears
    # in the project's session list.
    _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Topology"))

    sess = projects_route.create_session_in_project(project["id"], SessionCreate(title="lemma A"))
    assert sess["project_id"] == project["id"]
    assert sess["title"] == "lemma A"
    assert [s["id"] for s in projects_route.get_project(project["id"])["sessions"]] == [sess["id"]]

    # blank title falls back; missing project → 404
    fallback = projects_route.create_session_in_project(project["id"], SessionCreate())
    assert fallback["title"] == "Untitled theorem"
    with pytest.raises(HTTPException) as exc:
        projects_route.create_session_in_project("nope", SessionCreate(title="x"))
    assert exc.value.status_code == 404


def test_update_edits_title_and_description(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Old"))

    updated = projects_route.update_project(
        project["id"], ProjectUpdate(title="New", description="now described")
    )
    assert updated["title"] == "New"
    assert updated["description"] == "now described"
    assert updated["slug"] == project["slug"]  # immutable


def test_project_identity_preview_and_display_rename_by_slug(tmp_path, monkeypatch):
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Old Name"))
    repo = proofs / "Lea" / "OldName"
    assert "# Instructions — Old Name" in (repo / ".lea" / "instructions.md").read_text()

    preview = projects_route.namespace_preview(NamespacePreviewRequest(project_name="Fourier Series"))
    assert preview["namespace"] == "Lea.FourierSeries"
    assert preview["available"] is True

    identity = projects_route.get_project_identity_by_slug(project["slug"])
    assert identity["projectName"] == "Old Name"
    assert identity["exists"] is True

    result = projects_route.update_project_identity_by_slug(
        project["slug"],
        ProjectIdentityUpdate(project_name="Readable Name", mode="display-only"),
    )
    assert result["identity"]["projectName"] == "Readable Name"
    assert result["identity"]["namespace"] == project["namespace"]
    assert "# Instructions — Readable Name" in (repo / ".lea" / "instructions.md").read_text()
    assert "# Memory — Readable Name" in (repo / ".lea" / "memory.md").read_text()
    assert "# Blueprint — Readable Name" in (repo / ".lea" / "blueprint.md").read_text()


def test_project_identity_put_can_create_without_get_side_effect(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    with pytest.raises(HTTPException) as missing:
        projects_route.get_project_identity_by_slug("doc-a")
    assert missing.value.status_code == 404
    assert store.list_projects() == []

    result = projects_route.update_project_identity_by_slug(
        "doc-a",
        ProjectIdentityUpdate(
            project_name="Fourier Notes",
            mode="rename-namespace",
            namespace="Lea.FourierNotes",
            create_if_missing=True,
        ),
    )
    assert result["identity"]["projectName"] == "Fourier Notes"
    assert result["identity"]["namespace"] == "Lea.FourierNotes"
    assert store.get_project_by_slug("doc-a") is not None


def test_project_namespace_migration_rewrites_files_and_updates_row(tmp_path, monkeypatch):
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Old Name"))
    repo = proofs / "Lea" / "OldName"
    (repo / "helper.lean").write_text(
        "import Mathlib\nnamespace Lea.OldName\n\nlemma helper : True := by\n  trivial\n\nend Lea.OldName\n"
    )

    class CheckOk:
        status = "ok"
        detail = ""

    monkeypatch.setattr(projects_route, "interface_check", lambda _path: CheckOk())
    result = projects_route.update_project_identity_by_slug(
        project["slug"],
        ProjectIdentityUpdate(
            project_name="New Name",
            mode="rename-namespace",
            namespace="Lea.NewName",
            expected_namespace=project["namespace"],
        ),
    )

    assert result["identity"]["namespace"] == "Lea.NewName"
    new_repo = proofs / "Lea" / "NewName"
    assert new_repo.is_dir()
    assert not repo.exists()
    assert "namespace Lea.NewName" in (new_repo / "helper.lean").read_text()
    assert "# Instructions — New Name" in (new_repo / ".lea" / "instructions.md").read_text()
    assert result["migration"]["checkedFiles"] == 1
    assert result["migration"]["failedFiles"] == []


def test_delete_removes_repo_and_rows(tmp_path, monkeypatch):
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Doomed"))
    repo = proofs / "Lea" / "Doomed"
    assert repo.is_dir()

    result = projects_route.delete_project(project["id"])
    assert result["deleted"] is True
    assert not repo.exists()
    assert store.get_project(project["id"]) is None


def test_missing_project_is_404(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    for call in (
        lambda: projects_route.get_project("nope"),
        lambda: projects_route.update_project("nope", ProjectUpdate(title="x")),
        lambda: projects_route.delete_project("nope"),
        lambda: projects_route.get_instructions("nope"),
        lambda: projects_route.put_instructions("nope", DocUpdate(content="x")),
        lambda: projects_route.get_memory("nope"),
        lambda: projects_route.put_memory("nope", DocUpdate(content="x")),
        lambda: projects_route.get_blueprint("nope"),
        lambda: projects_route.put_blueprint("nope", DocUpdate(content="x")),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404


@pytest.mark.parametrize("doc", ["instructions", "memory", "blueprint"])
def test_doc_get_returns_seeded_then_roundtrips_put(tmp_path, monkeypatch, doc):
    # R1/R2: GET returns the seeded template; PUT writes+commits; GET returns the
    # new content; and the composed run context reflects the edit.
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    get = getattr(projects_route, f"get_{doc}")
    put = getattr(projects_route, f"put_{doc}")

    seeded = get(project["id"])["content"]
    assert doc[:4].lower() in seeded.lower()  # "inst"/"memo" header present

    result = put(project["id"], DocUpdate(content="# Edited\n\n- a durable fact\n"))
    assert result["content"].startswith("# Edited")
    assert len(result["commit_sha"]) == 40  # a real commit landed

    assert get(project["id"])["content"] == "# Edited\n\n- a durable fact\n"
    on_disk = proofs / "Lea" / "Analysis" / ".lea" / f"{doc}.md"
    assert on_disk.read_text() == "# Edited\n\n- a durable fact\n"


def test_file_upload_list_download_delete_roundtrip(tmp_path, monkeypatch):
    # S1: POST stores+indexes; GET lists; GET/{fid} downloads the bytes; DELETE removes.
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    pid = project["id"]
    assert projects_route.list_files(pid)["files"] == []

    row = _upload(pid, "notes.tex", b"\\section{x}", "text/x-tex")
    assert row["stored_path"] == ".lea/files/notes.tex"
    assert [f["id"] for f in projects_route.list_files(pid)["files"]] == [row["id"]]

    resp = projects_route.download_file(pid, row["id"])
    assert str(resp.path) == str(proofs / "Lea" / "Analysis" / ".lea" / "files" / "notes.tex")
    assert resp.filename == "notes.tex"

    assert projects_route.delete_file(pid, row["id"])["deleted"] is True
    assert projects_route.list_files(pid)["files"] == []


def test_file_upload_rejects_unsupported_type(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    with pytest.raises(HTTPException) as exc:
        _upload(project["id"], "evil.sh", b"rm -rf /")
    assert exc.value.status_code == 415


def test_file_routes_404_on_missing_project_or_file(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    pid = project["id"]
    with pytest.raises(HTTPException) as e1:
        projects_route.list_files("nope")
    assert e1.value.status_code == 404
    with pytest.raises(HTTPException) as e2:
        projects_route.download_file(pid, "no-such-file")
    assert e2.value.status_code == 404
    with pytest.raises(HTTPException) as e3:
        projects_route.delete_file(pid, "no-such-file")
    assert e3.value.status_code == 404


def test_blueprint_route_returns_validator_warnings(tmp_path, monkeypatch):
    # T1: PUT/GET /blueprint attach advisory warnings (a dangling `uses` edge), but
    # still save (warnings never block the write).
    _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    pid = project["id"]

    bad = "## a\n- kind: lemma\n- uses: ghost\n\nstatement\n"
    put = projects_route.put_blueprint(pid, DocUpdate(content=bad))
    assert len(put["commit_sha"]) == 40  # the malformed blueprint still committed
    assert any("ghost" in w["message"] for w in put["warnings"])

    # The warning is recomputed on GET too, not just returned from the PUT.
    assert any("ghost" in w["message"] for w in projects_route.get_blueprint(pid)["warnings"])


def test_graph_route_derives_node_status(tmp_path, monkeypatch):
    # T2: GET /graph parses the blueprint and derives status from the stored verdict.
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    pid = project["id"]
    repo = proofs / "Lea" / "Analysis"

    (repo / "helper.lean").write_text("import Mathlib\nnamespace Lea.Analysis\nlemma helper : True := trivial\nend Lea.Analysis\n")
    projects_route.put_blueprint(pid, DocUpdate(content="## helper\n- kind: lemma\n- lean: `Lea.Analysis.helper`\n\nA helper.\n"))
    sess = store.create_session("w", project_id=pid)["id"]
    store.add_code_step(sess, None, "helper.lean", content="proof-a", check_status="ok")

    result = projects_route.get_graph(pid)
    node = result["nodes"][0]
    assert node["key"] == "helper"
    assert node["status"] == "proved"
    assert node["last_modified_by"] == sess

    with pytest.raises(HTTPException) as exc:
        projects_route.get_graph("nope")
    assert exc.value.status_code == 404


def test_filesystem_tree_read_edit_export_roundtrip(tmp_path, monkeypatch):
    # U1/U2: the project repo is browsable, any file is readable + editable
    # (write+commit), and the whole thing exports as a zip (D34).
    import io
    import zipfile

    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    pid = project["id"]

    # Tree: the seeded .lea/ dir shows; .git is hidden.
    tree = projects_route.get_tree(pid)["tree"]
    names = [e["name"] for e in tree]
    assert ".lea" in names and ".git" not in names

    # Read a seeded doc through the generic file endpoint.
    bp = projects_route.read_project_file(pid, ".lea/blueprint.md")
    assert bp["content"].startswith("# Blueprint")
    assert bp["lean"] is False

    # Edit a brand-new file → write + commit; reading it back returns the content.
    put = projects_route.write_project_file(pid, FilePut(path="notes/scratch.md", content="# hi\n"))
    assert len(put["commit_sha"]) == 40
    assert put["check"] is None  # not a .lean file
    assert projects_route.read_project_file(pid, "notes/scratch.md")["content"] == "# hi\n"

    # Export: a zip carrying the source + assets, excluding internals.
    resp = projects_route.export_project(pid)
    assert resp.media_type == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.body)) as zf:
        entries = zf.namelist()
    assert any(n.endswith("/.lea/blueprint.md") for n in entries)
    assert any(n.endswith("/notes/scratch.md") for n in entries)
    assert not any("/.git/" in n for n in entries)


def test_write_lean_file_returns_check_verdict(tmp_path, monkeypatch):
    # A .lean edit re-uses the standalone check for a verdict (like the v2 canvas, D2).
    # interface_check is patched so the route doesn't invoke real Lean.
    _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))

    class _Result:
        status = "ok"
        detail = None

    monkeypatch.setattr(projects_route, "interface_check", lambda _path: _Result())
    put = projects_route.write_project_file(
        project["id"], FilePut(path="Foo.lean", content="theorem foo : True := trivial\n")
    )
    assert put["check"] == {"status": "ok", "detail": None}


def test_filesystem_path_guard_and_404s(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    pid = project["id"]

    # Path escape → 400 on both read and write.
    with pytest.raises(HTTPException) as e1:
        projects_route.read_project_file(pid, "../escape.txt")
    assert e1.value.status_code == 400
    with pytest.raises(HTTPException) as e2:
        projects_route.write_project_file(pid, FilePut(path="../sneaky.txt", content="x"))
    assert e2.value.status_code == 400

    # Missing file → 404; hidden dir → 400.
    with pytest.raises(HTTPException) as e3:
        projects_route.read_project_file(pid, "ghost.lean")
    assert e3.value.status_code == 404
    with pytest.raises(HTTPException) as e4:
        projects_route.read_project_file(pid, ".git/config")
    assert e4.value.status_code == 400

    # Missing project → 404 across all filesystem routes.
    for call in (
        lambda: projects_route.get_tree("nope"),
        lambda: projects_route.read_project_file("nope", "x"),
        lambda: projects_route.write_project_file("nope", FilePut(path="x", content="y")),
        lambda: projects_route.export_project("nope"),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404


def test_put_doc_feeds_composed_context(tmp_path, monkeypatch):
    # The edited Instructions/Memory must show up in the next run's context (D25/D26).
    from app import projects as project_service

    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    projects_route.put_instructions(project["id"], DocUpdate(content="Prove √2 is irrational."))
    projects_route.put_memory(project["id"], DocUpdate(content="- explicit witnesses preferred"))

    repo = proofs / "Lea" / "Analysis"
    msg = project_service.compose_context_message(store.get_project(project["id"]), repo)
    assert "Prove √2 is irrational." in msg["content"]
    assert "explicit witnesses preferred" in msg["content"]


# ── Git sharing: set remote + push (6b/U3, D34) ────────────────────────────────

def test_set_project_remote_valid_and_invalid(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    pid = projects_route.create_project(ProjectCreate(title="Share Me"))["id"]
    from app.routes.projects import RemoteUpdate

    res = projects_route.set_project_remote(pid, RemoteUpdate(remote_url="https://github.com/me/share-me.git/"))
    assert res["remote_url"] == "https://github.com/me/share-me.git"  # trailing slash stripped
    assert store.get_project(pid)["remote_url"] == "https://github.com/me/share-me.git"

    with pytest.raises(HTTPException) as ei:
        projects_route.set_project_remote(pid, RemoteUpdate(remote_url="ftp://example.com/x"))
    assert ei.value.status_code == 400


def test_set_project_remote_pins_the_github_host(tmp_path, monkeypatch):
    """AUDIT-2026-07-24 S2: the pattern used to accept ANY host while the error
    message promised a GitHub URL — and `push` embeds the user's PAT in whatever
    was stored, so a foreign host was credential exfiltration, not a typo."""
    _setup(tmp_path, monkeypatch)
    pid = projects_route.create_project(ProjectCreate(title="Pin Me"))["id"]
    from app.routes.projects import RemoteUpdate

    for hostile in (
        "https://evil.example.com/me/repo",         # the exfiltration target
        "https://github.com.evil.example.com/a/b",  # suffix lookalike
        "https://gitlab.com/me/repo",               # a real, wrong forge
        "http://github.com/me/repo",                # cleartext
        "https://github.com/../..",                 # traversal-ish path segments
    ):
        with pytest.raises(HTTPException) as ei:
            projects_route.set_project_remote(pid, RemoteUpdate(remote_url=hostile))
        assert ei.value.status_code == 400, hostile
    assert store.get_project(pid)["remote_url"] is None

    # The legitimate shapes still round-trip.
    for good in ("https://github.com/me/repo", "https://www.github.com/me/repo.git"):
        assert projects_route.set_project_remote(
            pid, RemoteUpdate(remote_url=good)
        )["remote_url"] == good


def test_push_refuses_a_non_github_remote_stored_directly(tmp_path, monkeypatch):
    """The row can hold a remote that predates the pinned host (or was written by
    something other than the route). Re-check where the credential is actually
    used, so an old row can't become a token-delivery target."""
    _setup(tmp_path, monkeypatch)
    pid = projects_route.create_project(ProjectCreate(title="Legacy Remote"))["id"]
    store.update_project(pid, remote_url="https://evil.example.com/me/repo")
    monkeypatch.setattr(projects_route, "github_token", lambda: "ghp_dummy")

    def explode(*args, **kwargs):  # push must not be reached at all
        raise AssertionError("push_to_github was called for a non-GitHub remote")

    monkeypatch.setattr(projects_route.GitStore, "push_to_github", explode)

    with pytest.raises(HTTPException) as ei:
        projects_route.push_project(pid)
    assert ei.value.status_code == 400
    assert "github" in ei.value.detail.lower()


def test_push_guards_no_remote_then_no_token(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from app.routes.projects import RemoteUpdate
    pid = projects_route.create_project(ProjectCreate(title="NoShare"))["id"]

    with pytest.raises(HTTPException) as e1:
        projects_route.push_project(pid)
    assert e1.value.status_code == 400 and "remote" in e1.value.detail.lower()

    projects_route.set_project_remote(pid, RemoteUpdate(remote_url="https://github.com/me/noshare"))
    monkeypatch.setattr(projects_route, "github_token", lambda: None)
    with pytest.raises(HTTPException) as e2:
        projects_route.push_project(pid)
    assert e2.value.status_code == 400 and "token" in e2.value.detail.lower()


def test_push_to_local_bare_repo_functionally(tmp_path, monkeypatch):
    """Push really lands commits on the remote (token=None, a local bare repo as the
    remote). Proves push_to_github targets HEAD:refs/heads/main."""
    import subprocess
    from app.gitstore import GitStore

    proofs = _setup(tmp_path, monkeypatch)
    pid = projects_route.create_project(ProjectCreate(title="Pushy"))["id"]
    repo = projects_route.project_service.project_repo_dir(store.get_project(pid), proofs)
    (repo / "hello.txt").write_text("hi")
    GitStore(proofs).commit_all(repo, "add hello")

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    GitStore(proofs).push_to_github(repo, str(bare), token=None, branch="main")

    log = subprocess.run(
        ["git", "--git-dir", str(bare), "log", "--oneline", "main"],
        capture_output=True, text=True,
    )
    assert "add hello" in log.stdout


def test_push_failure_detail_suggests_lea_only_on_divergence():
    from app.routes.projects import _push_failure_detail
    diverged = _push_failure_detail("! [rejected] main -> main (non-fast-forward)\nUpdates were rejected")
    assert "reconcile" in diverged.lower() and "lea" in diverged.lower()
    auth = _push_failure_detail("fatal: Authentication failed for 'https://github.com/me/repo'")
    assert "reconcile" not in auth.lower()


def test_push_to_diverged_remote_points_at_lea(tmp_path, monkeypatch):
    """A real non-fast-forward push surfaces the 'ask Lea to reconcile' hint."""
    import subprocess
    from app.gitstore import GitStore

    proofs = _setup(tmp_path, monkeypatch)
    pid = projects_route.create_project(ProjectCreate(title="Diverged"))["id"]
    repo = projects_route.project_service.project_repo_dir(store.get_project(pid), proofs)
    (repo / "local.txt").write_text("local")
    GitStore(proofs).commit_all(repo, "local commit")

    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    for k, v in (("user.email", "x@y.z"), ("user.name", "x")):
        subprocess.run(["git", "-C", str(seed), "config", k, v], check=True)
    (seed / "remote.txt").write_text("remote")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "remote commit"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", str(bare), "HEAD:refs/heads/main"], check=True)

    store.update_project(pid, remote_url=str(bare))
    monkeypatch.setattr(projects_route, "github_token", lambda: "ghp_dummy")

    with pytest.raises(HTTPException) as ei:
        projects_route.push_project(pid)
    assert ei.value.status_code == 502
    assert "reconcile" in ei.value.detail.lower() and "lea" in ei.value.detail.lower()


# ── By-slug export/share: the Overleaf companion's path (D34) ──────────────────


def test_by_slug_share_status_export_and_remote(tmp_path, monkeypatch):
    """The companion's whole surface: share status (remote + token presence),
    zip export, and set-remote — all resolved by slug, mirroring the by-id routes."""
    import io
    import zipfile
    from app.routes.projects import RemoteUpdate

    _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Doc One"))
    slug = project["slug"]

    # Share status: no remote yet; token presence mirrors Settings.
    monkeypatch.setattr(projects_route, "github_token", lambda: None)
    status = projects_route.get_share_status_by_slug(slug)
    assert status == {"id": project["id"], "slug": slug, "remote_url": None, "token_configured": False}

    monkeypatch.setattr(projects_route, "github_token", lambda: "ghp_dummy")
    projects_route.set_project_remote_by_slug(slug, RemoteUpdate(remote_url="https://github.com/me/doc-one/"))
    status = projects_route.get_share_status_by_slug(slug)
    assert status["remote_url"] == "https://github.com/me/doc-one"  # trailing slash stripped
    assert status["token_configured"] is True
    assert status["remote_url"] == store.get_project(project["id"])["remote_url"]

    # Export by slug: same zip as the by-id route.
    resp = projects_route.export_project_by_slug(slug)
    assert resp.media_type == "application/zip"
    assert f'filename="{slug}.zip"' in resp.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(resp.body)) as zf:
        entries = zf.namelist()
    assert any(n.endswith("/.lea/blueprint.md") for n in entries)
    assert not any("/.git/" in n for n in entries)


def test_by_slug_push_functionally(tmp_path, monkeypatch):
    """Overleaf Share snapshots untracked changes before pushing the project."""
    import subprocess

    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Slug Push"))
    repo = projects_route.project_service.project_repo_dir(store.get_project(project["id"]), proofs)
    (repo / "hello.txt").write_text("hi")

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    store.update_project(project["id"], remote_url=str(bare))
    monkeypatch.setattr(projects_route, "github_token", lambda: "ghp_dummy")

    res = projects_route.push_project_by_slug(project["slug"])
    assert res["pushed"] is True
    assert res["commit_sha"]
    log = subprocess.run(
        ["git", "--git-dir", str(bare), "log", "--oneline", "main"],
        capture_output=True, text=True,
    )
    assert "share: snapshot project" in log.stdout
    exported = subprocess.run(
        ["git", "--git-dir", str(bare), "show", "main:hello.txt"],
        capture_output=True, text=True, check=True,
    )
    assert exported.stdout == "hi"

    # A second Share still pushes, but a clean project does not gain an empty commit.
    before = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    second = projects_route.push_project_by_slug(project["slug"])
    after = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert second["commit_sha"] == res["commit_sha"]
    assert after == before


def test_by_slug_never_creates_a_project(tmp_path, monkeypatch):
    """Unlike mirror's ensure_project, an unknown or malformed slug is a plain 404
    on every by-slug route — export must never create a project."""
    from app.routes.projects import RemoteUpdate

    _setup(tmp_path, monkeypatch)
    for call in (
        lambda: projects_route.get_share_status_by_slug("no-such-doc"),
        lambda: projects_route.export_project_by_slug("no-such-doc"),
        lambda: projects_route.set_project_remote_by_slug("no-such-doc", RemoteUpdate(remote_url="https://github.com/a/b")),
        lambda: projects_route.push_project_by_slug("no-such-doc"),
        lambda: projects_route.get_blueprint_by_slug("no-such-doc"),
        lambda: projects_route.get_graph_by_slug("no-such-doc"),
        lambda: projects_route.export_project_by_slug("Bad Slug!!"),  # malformed ≡ missing
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404
    assert store.list_projects() == []  # nothing was created as a side effect


def test_blueprint_by_slug_matches_by_id_and_returns_warnings(tmp_path, monkeypatch):
    """The companion's read-only blueprint fetch: same payload as the by-id route,
    warnings included (a dangling `uses` edge)."""
    _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Doc One"))
    pid, slug = project["id"], project["slug"]

    bad = "## a\n- kind: lemma\n- uses: ghost\n\nstatement\n"
    projects_route.put_blueprint(pid, DocUpdate(content=bad))

    by_slug = projects_route.get_blueprint_by_slug(slug)
    assert by_slug == projects_route.get_blueprint(pid)  # same derivation, slug-resolved
    assert any("ghost" in w["message"] for w in by_slug["warnings"])


def test_graph_by_slug_matches_by_id_and_derives_status(tmp_path, monkeypatch):
    """GET graph-by-slug parses the blueprint and derives node status from the
    stored verdict — identical to the by-id route for the same project."""
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    pid, slug = project["id"], project["slug"]
    repo = proofs / "Lea" / "Analysis"

    (repo / "helper.lean").write_text(
        "import Mathlib\nnamespace Lea.Analysis\nlemma helper : True := trivial\nend Lea.Analysis\n"
    )
    projects_route.put_blueprint(
        pid, DocUpdate(content="## helper\n- kind: lemma\n- lean: `Lea.Analysis.helper`\n\nA helper.\n")
    )
    sess = store.create_session("w", project_id=pid)["id"]
    store.add_code_step(
        sess, None, "helper.lean",
        content=(repo / "helper.lean").read_text(), check_status="ok",
    )

    by_slug = projects_route.get_graph_by_slug(slug)
    assert by_slug == projects_route.get_graph(pid)  # slug-resolved, same graph
    node = by_slug["nodes"][0]
    assert node["key"] == "helper"
    assert node["status"] == "proved"
    assert node["last_modified_by"] == sess


def _record_artifact(project_id, session_id, name, kind, path):
    run = store.create_run(session_id, "m", None, 3, project_id=project_id)
    store.upsert_artifact(
        project_id=project_id, session_id=session_id, run_id=run["id"],
        declaration_name=name, kind=kind, path=path, module_name=None,
    )


def test_generate_blueprint_synthesizes_nodes_edges_and_is_idempotent(tmp_path, monkeypatch):
    """Populate the blueprint from formalized artifacts: one node per recorded decl,
    kind from the Lean keyword, `uses` edges from decl references — additive + idempotent."""
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    pid = project["id"]
    repo = proofs / "Lea" / "Analysis"
    session = store.create_session("prove", project_id=pid)["id"]

    # A definition and a lemma that uses it, each recorded as an artifact.
    (repo / "base.lean").write_text(
        "import Mathlib\nnamespace Lea.Analysis\ndef base : Nat := 0\nend Lea.Analysis\n"
    )
    (repo / "uses_base.lean").write_text(
        "import Mathlib\nnamespace Lea.Analysis\ntheorem uses_base : base = base := rfl\nend Lea.Analysis\n"
    )
    _record_artifact(pid, session, "Lea.Analysis.base", "definition", "base.lean")
    _record_artifact(pid, session, "Lea.Analysis.uses_base", "proof", "uses_base.lean")

    result = projects_route.generate_blueprint(pid)
    assert result["added"] == 2 and result["skipped"] == 0

    nodes = {n["key"]: n for n in result["graph"]["nodes"]}
    assert nodes["base"]["kind"] == "definition"
    assert nodes["uses_base"]["kind"] == "theorem"          # from the Lean keyword
    assert "base" in nodes["uses_base"]["uses"]             # derived edge
    assert {"from": "uses_base", "to": "base"} in result["graph"]["edges"]

    # Idempotent: a second run adds nothing and preserves the existing nodes.
    again = projects_route.generate_blueprint(pid)
    assert again["added"] == 0 and again["skipped"] == 2
    assert len(again["graph"]["nodes"]) == 2


def test_generate_blueprint_backfills_pre_index_artifacts(tmp_path, monkeypatch):
    """A project whose proofs predate the artifacts index (registry markdown only, no
    artifact rows) must still generate nodes — the route backfills before reading."""
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Legacy"))
    pid = project["id"]
    repo = proofs / "Lea" / "Legacy"
    (repo / "old.lean").write_text(
        "import Mathlib\nnamespace Lea.Legacy\nlemma old : True := trivial\nend Lea.Legacy\n"
    )

    # No artifact rows exist; instead the proof lives in the legacy registry markdown
    # (HTML-comment markers) that _ensure_artifacts_backfilled imports from.
    assert store.list_artifacts_for_scope(pid) == []
    registry = tmp_path / "workspace" / "projects" / f"{project['slug']}.md"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        '# Legacy\n\n<!-- lea:theorem name="Lea.Legacy.old" '
        'proof="workspace/proofs/Lea/Legacy/old.lean" module="Lea.Legacy.Old" -->\n'
    )

    result = projects_route.generate_blueprint(pid)
    assert result["added"] == 1, "backfill should surface the registry proof before generating"
    assert result["graph"]["nodes"][0]["key"] == "old"


def test_generate_blueprint_edge_derivation_ignores_comments_and_ambiguous_shorts(tmp_path, monkeypatch):
    """Edges come from real references only: a name mentioned in a comment yields no
    edge, and a short name shared by two decls is too ambiguous to edge (skipped)."""
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Edges"))
    pid = project["id"]
    repo = proofs / "Lea" / "Edges"
    session = store.create_session("prove", project_id=pid)["id"]

    (repo / "base.lean").write_text(
        "import Mathlib\nnamespace Lea.Edges\nlemma base : True := trivial\nend Lea.Edges\n"
    )
    # References `base` only in a comment — must NOT create an edge.
    (repo / "commented.lean").write_text(
        "import Mathlib\nnamespace Lea.Edges\n-- like base but standalone\nlemma commented : True := trivial\nend Lea.Edges\n"
    )
    # Two decls share the short name `dup` (different namespaces) — ambiguous.
    (repo / "dup_a.lean").write_text(
        "import Mathlib\nnamespace Lea.Edges.A\nlemma dup : True := trivial\nend Lea.Edges.A\n"
    )
    (repo / "cites_dup.lean").write_text(
        "import Mathlib\nnamespace Lea.Edges\ntheorem cites_dup : True := by have := dup; trivial\nend Lea.Edges\n"
    )
    for name, path in [
        ("Lea.Edges.base", "base.lean"), ("Lea.Edges.commented", "commented.lean"),
        ("Lea.Edges.A.dup", "dup_a.lean"), ("Lea.Edges.B.dup", "dup_a.lean"),
        ("Lea.Edges.cites_dup", "cites_dup.lean"),
    ]:
        _record_artifact(pid, session, name, "proof", path)

    result = projects_route.generate_blueprint(pid)
    nodes = {n["key"]: n for n in result["graph"]["nodes"]}
    assert nodes["commented"]["uses"] == []                       # comment ref → no edge
    assert all(u not in ("dup", "dup_2") for u in nodes["cites_dup"]["uses"])  # ambiguous short → skipped


def test_generate_blueprint_preserves_multiline_signature(tmp_path, monkeypatch):
    """A signature that spans multiple source lines keeps its line breaks in the node
    statement (so the pane can render it like the Lean pane), not collapsed to one line."""
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Multi"))
    pid = project["id"]
    repo = proofs / "Lea" / "Multi"
    session = store.create_session("prove", project_id=pid)["id"]
    (repo / "wide.lean").write_text(
        "import Mathlib\nnamespace Lea.Multi\n"
        "theorem wide (x : Nat)\n    (hx : 0 < x) :\n    x + 0 = x := by\n  simp\nend Lea.Multi\n"
    )
    _record_artifact(pid, session, "Lea.Multi.wide", "proof", "wide.lean")

    result = projects_route.generate_blueprint(pid)
    stmt = result["graph"]["nodes"][0]["statement"]
    assert "\n" in stmt, "multi-line signature must keep its line breaks"
    lines = stmt.splitlines()
    assert lines[0] == "(x : Nat)"
    assert "x + 0 = x" in lines[-1]


def test_generate_blueprint_closes_dangling_fence_before_appending(tmp_path, monkeypatch):
    """An unclosed ``` fence in blueprint.md must not swallow the appended nodes — the
    node stays a real section, so a second run is still idempotent."""
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Fence"))
    pid = project["id"]
    repo = proofs / "Lea" / "Fence"
    session = store.create_session("prove", project_id=pid)["id"]
    (repo / "t.lean").write_text(
        "import Mathlib\nnamespace Lea.Fence\nlemma t : True := trivial\nend Lea.Fence\n"
    )
    _record_artifact(pid, session, "Lea.Fence.t", "proof", "t.lean")

    # A blueprint that ends inside an open fence (odd number of ``` lines).
    projects_route.put_blueprint(pid, DocUpdate(content="# Blueprint\n\n```\nunterminated example\n"))

    first = projects_route.generate_blueprint(pid)
    assert first["added"] == 1 and first["graph"]["nodes"][0]["key"] == "t"
    # Idempotent despite the dangling fence: the node was written outside it.
    second = projects_route.generate_blueprint(pid)
    assert second["added"] == 0 and len(second["graph"]["nodes"]) == 1


def test_generate_blueprint_by_slug_and_404(tmp_path, monkeypatch):
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Doc One"))
    slug = project["slug"]
    repo = proofs / "Lea" / "DocOne"
    session = store.create_session("prove", project_id=project["id"])["id"]
    (repo / "thm.lean").write_text(
        "import Mathlib\nnamespace Lea.DocOne\nlemma thm : True := trivial\nend Lea.DocOne\n"
    )
    _record_artifact(project["id"], session, "Lea.DocOne.thm", "proof", "thm.lean")

    result = projects_route.generate_blueprint_by_slug(slug)
    assert result["added"] == 1
    assert result["graph"]["nodes"][0]["key"] == "thm"
    assert result["graph"]["nodes"][0]["kind"] == "lemma"

    with pytest.raises(HTTPException) as exc:
        projects_route.generate_blueprint_by_slug("no-such-doc")
    assert exc.value.status_code == 404


def test_artifacts_by_slug_returns_recorded_rows_and_404s_unknown(tmp_path, monkeypatch):
    """PLAN-system-hardening 4.1: the by-slug artifact index the Overleaf
    companion reads instead of diffing registry markdown. Never creates a
    project — unknown slug is a 404."""
    _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    session = store.create_session("prove", project_id=project["id"])
    run = store.create_run(session["id"], "m", None, 3, project_id=project["id"])
    store.upsert_artifact(
        project_id=project["id"], session_id=session["id"], run_id=run["id"],
        declaration_name="cauchy_bound", kind="proof",
        path="cauchy_bound.lean", module_name="Lea.Analysis.cauchy_bound",
    )

    payload = projects_route.project_artifacts_by_slug(project["slug"])
    assert payload["project_id"] == project["id"]
    assert len(payload["artifacts"]) == 1
    row = payload["artifacts"][0]
    assert row["declaration_name"] == "cauchy_bound"
    assert row["module_name"] == "Lea.Analysis.cauchy_bound"

    with pytest.raises(HTTPException) as exc:
        projects_route.project_artifacts_by_slug("no-such-slug")
    assert exc.value.status_code == 404


def test_artifact_retire_and_restore_round_trip_through_git(tmp_path, monkeypatch):
    """PLAN-system-hardening 4.5 (single writer): a retry retires the previous
    proof through the adapter's git layer (a commit, not a bare unlink), and an
    unverified retry restores it from that commit's parent."""
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    repo = proofs / "Lea" / "Analysis"
    proof = repo / "cauchy_bound.lean"
    proof.write_text("theorem cauchy_bound : True := by\n  trivial\n")
    from app.gitstore import GitStore
    GitStore(repo.parent).commit_all(repo, "record cauchy_bound")

    retired = projects_route.retire_project_artifact_by_slug(
        project["slug"], projects_route.ArtifactRetireRequest(path="cauchy_bound.lean")
    )
    assert not proof.exists(), "the file is gone from the working tree"
    assert len(retired["retire_commit"]) == 40

    restored = projects_route.restore_project_artifact_by_slug(
        project["slug"],
        projects_route.ArtifactRestoreRequest(path="cauchy_bound.lean", retire_commit=retired["retire_commit"]),
    )
    assert restored["restored"] is True
    assert proof.read_text().startswith("theorem cauchy_bound")


def test_artifact_restore_prefers_verified_sql_snapshot(tmp_path, monkeypatch):
    """Agent proof bytes live in the timeline, even when project Git never saw
    that version. A failed retry must restore those exact verified bytes."""
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    repo = proofs / "Lea" / "Analysis"
    proof = repo / "sql_owned.lean"
    stale = "theorem sql_owned : True := by\n  exact True.intro\n"
    verified = "theorem sql_owned : True := by\n  trivial\n"
    proof.write_text(stale)
    from app.gitstore import GitStore
    GitStore(repo.parent).commit_all(repo, "record stale sql_owned")

    session = store.create_session("prove", project_id=project["id"])
    run = store.create_run(session["id"], "m", None, 3, project_id=project["id"])
    store.add_code_step(
        session["id"], run["id"], "sql_owned.lean",
        content=verified, check_status="ok", artifact_kind="proof",
    )
    proof.write_text(verified)  # deliberately not committed: SQL owns this version

    retired = projects_route.retire_project_artifact_by_slug(
        project["slug"], projects_route.ArtifactRetireRequest(path="sql_owned.lean")
    )
    restored = projects_route.restore_project_artifact_by_slug(
        project["slug"],
        projects_route.ArtifactRestoreRequest(
            path="sql_owned.lean", retire_commit=retired["retire_commit"]
        ),
    )

    assert restored["restored"] is True
    assert proof.read_text() == verified


def test_artifact_restore_never_overwrites_an_occupied_path(tmp_path, monkeypatch):
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    repo = proofs / "Lea" / "Analysis"
    proof = repo / "kept.lean"
    proof.write_text("theorem kept : True := trivial\n")
    from app.gitstore import GitStore
    GitStore(repo.parent).commit_all(repo, "record kept")
    retired = projects_route.retire_project_artifact_by_slug(
        project["slug"], projects_route.ArtifactRetireRequest(path="kept.lean")
    )
    # The failed retry left its own partial file at the path.
    proof.write_text("theorem kept : True := by sorry\n")

    result = projects_route.restore_project_artifact_by_slug(
        project["slug"],
        projects_route.ArtifactRestoreRequest(path="kept.lean", retire_commit=retired["retire_commit"]),
    )
    assert result["restored"] is False
    assert "sorry" in proof.read_text(), "the partial file is untouched"


def test_artifact_retire_rejects_escapes_and_missing_files(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))

    with pytest.raises(HTTPException) as escape:
        projects_route.retire_project_artifact_by_slug(
            project["slug"], projects_route.ArtifactRetireRequest(path="../../etc/passwd")
        )
    assert escape.value.status_code == 422

    with pytest.raises(HTTPException) as missing:
        projects_route.retire_project_artifact_by_slug(
            project["slug"], projects_route.ArtifactRetireRequest(path="never_recorded.lean")
        )
    assert missing.value.status_code == 404


def test_target_status_serves_ledger_evidence(tmp_path, monkeypatch):
    """PLAN-system-hardening 4.4: per-declaration ledger evidence — artifact
    row, file existence, sorry scan, and the newest check verdict across all
    the project's sessions — for the companion's ledger status engine."""
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    repo = proofs / "Lea" / "Analysis"
    session = store.create_session("prove", project_id=project["id"])
    run = store.create_run(session["id"], "m", None, 3, project_id=project["id"])

    (repo / "proved_one.lean").write_text("import Mathlib\n\ntheorem proved_one : True := by\n  trivial\n")
    store.upsert_artifact(project_id=project["id"], session_id=session["id"], run_id=run["id"],
                          declaration_name="proved_one", kind="proof",
                          path="proved_one.lean", module_name="Lea.Analysis.proved_one")
    store.add_code_step(session["id"], run["id"], "proved_one.lean",
                        content=(repo / "proved_one.lean").read_text(), check_status="ok")

    (repo / "stubbed_one.lean").write_text("theorem stubbed_one : True := by\n  sorry\n")
    store.upsert_artifact(project_id=project["id"], session_id=session["id"], run_id=run["id"],
                          declaration_name="stubbed_one", kind="proof",
                          path="stubbed_one.lean", module_name="Lea.Analysis.stubbed_one")

    store.upsert_artifact(project_id=project["id"], session_id=session["id"], run_id=run["id"],
                          declaration_name="retired_one", kind="proof",
                          path="retired_one.lean", module_name="Lea.Analysis.retired_one")

    payload = projects_route.project_target_status_by_slug(
        project["slug"], declarations="proved_one,stubbed_one,retired_one,never_seen"
    )
    by_name = {t["declaration_name"]: t for t in payload["targets"]}

    proved = by_name["proved_one"]
    assert proved["recorded"] and proved["exists"] and proved["declaration_present"]
    assert proved["has_sorry"] is False
    assert proved["check_status"] == "ok"
    assert "theorem proved_one" in proved["content"]

    stubbed = by_name["stubbed_one"]
    assert stubbed["exists"] and stubbed["has_sorry"] is True

    retired = by_name["retired_one"]
    assert retired["recorded"] is True and retired["exists"] is False

    assert by_name["never_seen"] == {"declaration_name": "never_seen", "recorded": False}


def test_target_status_check_verdict_spans_sessions(tmp_path, monkeypatch):
    """A cascade/user re-check recorded in a DIFFERENT session of the same
    project supersedes the original run's verdict — the repo is shared (D24)."""
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    repo = proofs / "Lea" / "Analysis"
    (repo / "x.lean").write_text("theorem x : True := trivial\n")

    first = store.create_session("run", project_id=project["id"])
    first_run = store.create_run(first["id"], "m", None, 3, project_id=project["id"])
    store.upsert_artifact(project_id=project["id"], session_id=first["id"], run_id=first_run["id"],
                          declaration_name="x", kind="proof", path="x.lean", module_name="Lea.Analysis.x")
    store.add_code_step(
        first["id"], first_run["id"], "x.lean",
        content=(repo / "x.lean").read_text(), check_status="ok",
    )

    second = store.create_session("edit", project_id=project["id"])
    store.add_code_step(second["id"], None, "x.lean", content=(repo / "x.lean").read_text(),
                        author="cascade", check_status="error", check_detail="broke downstream")

    payload = projects_route.project_target_status_by_slug(project["slug"], declarations="x")
    target = payload["targets"][0]
    assert target["check_status"] == "error"
    assert target["check_detail"] == "broke downstream"
    assert target["check_author"] == "cascade"


def test_artifacts_backfill_from_legacy_registry_markdown(tmp_path, monkeypatch):
    """PLAN-system-hardening 4.3: a pre-index project's registry markdown is
    imported into the artifacts table once, so the index answers for artifacts
    that predate 4.1. Backfilled rows have no run (session/run NULL); a second
    read adds nothing."""
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Analysis"))
    repo = proofs / "Lea" / "Analysis"
    (repo / "old_thm.lean").write_text("import Mathlib\n\ntheorem old_thm : True := by\n  trivial\n")

    registry = tmp_path / "workspace" / "projects" / f"{project['slug']}.md"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        "# Lea Project\n\n## Theorem: old_thm\n\n"
        '<!-- lea:theorem name="old_thm" proof="workspace/proofs/Lea/Analysis/old_thm.lean" module="Lea.Analysis.old_thm" -->\n'
        "\n## Theorem: gone_thm\n\n"
        '<!-- lea:theorem name="gone_thm" proof="workspace/proofs/Lea/Analysis/gone_thm.lean" -->\n'
    )

    payload = projects_route.project_artifacts_by_slug(project["slug"])
    by_name = {row["declaration_name"]: row for row in payload["artifacts"]}
    assert by_name["old_thm"]["path"] == "old_thm.lean"
    assert by_name["old_thm"]["kind"] == "proof"
    assert by_name["old_thm"]["module_name"] == "Lea.Analysis.old_thm"
    assert by_name["old_thm"]["run_id"] is None
    assert by_name["gone_thm"]["kind"] is None

    again = projects_route.project_artifacts_by_slug(project["slug"])
    assert len(again["artifacts"]) == 2

    # And the ledger endpoint serves backfilled targets like any other.
    status = projects_route.project_target_status_by_slug(project["slug"], declarations="old_thm,gone_thm")
    by_status = {t["declaration_name"]: t for t in status["targets"]}
    assert by_status["old_thm"]["exists"] is True and by_status["old_thm"]["has_sorry"] is False
    assert by_status["gone_thm"]["recorded"] is True and by_status["gone_thm"]["exists"] is False


# --- AUDIT-2026-07-24 S5: retire is for proofs, not project infrastructure -----

@pytest.mark.parametrize("infrastructure", [
    "lakefile.toml",
    ".lea/instructions.md",
    ".lea/memory.md",
    ".lea/files/paper.pdf",
    ".gitignore",
])
def test_retire_refuses_project_infrastructure(tmp_path, monkeypatch, infrastructure):
    """"No recorded file at that path" only ever checked that a file EXISTED, so this
    endpoint would unlink anything in the repo. It is a retry primitive for a proof
    artifact; project infrastructure has no restore path."""
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Infra"))
    repo = proofs / "Lea" / "Infra"
    target = repo / infrastructure
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("important\n")

    with pytest.raises(HTTPException) as ei:
        projects_route.retire_project_artifact_by_slug(
            project["slug"], projects_route.ArtifactRetireRequest(path=infrastructure)
        )

    assert ei.value.status_code == 422
    assert target.exists(), f"{infrastructure} must survive"
    assert target.read_text() == "important\n"


def test_retire_still_accepts_a_legacy_proof_with_no_recorded_row(tmp_path, monkeypatch):
    """A proof written before the artifact index existed — or written by the agent
    through `bash` rather than `write_file` — has neither an artifact row nor a
    code_step. `/artifacts/restore` explicitly supports that case, so refusing it here
    would break the retry flow this endpoint exists for."""
    proofs = _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Legacy"))
    repo = proofs / "Lea" / "Legacy"
    proof = repo / "old_theorem.lean"
    proof.write_text("theorem old_theorem : True := by trivial\n")
    from app.gitstore import GitStore
    GitStore(repo.parent).commit_all(repo, "record old_theorem")

    result = projects_route.retire_project_artifact_by_slug(
        project["slug"], projects_route.ArtifactRetireRequest(path="old_theorem.lean")
    )

    assert not proof.exists()
    assert len(result["retire_commit"]) == 40


# --- AUDIT-2026-07-24 S7: the upload cap is enforced while reading -------------

def test_an_oversized_upload_is_refused_without_buffering_it(tmp_path, monkeypatch):
    """`await file.read()` pulled the WHOLE body into memory and only then checked the
    25 MB cap, so a multi-GB POST could exhaust the adapter — and with it every
    in-flight run, since the prover runs in this same process."""
    _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Uploads"))

    read_calls = []

    class _EndlessUpload:
        """Never returns empty: a client streaming without end. The route must stop it."""
        filename = "huge.pdf"
        content_type = "application/pdf"

        async def read(self, size=-1):
            read_calls.append(size)
            return b"\0" * (size if size and size > 0 else 1024)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(projects_route.upload_file(project["id"], _EndlessUpload()))

    assert ei.value.status_code == 413
    # Bounded work: the cap is 25 MB and we read 1 MB at a time, so this must stop
    # after ~26 reads rather than running until memory does.
    assert len(read_calls) < 40, f"{len(read_calls)} chunks read before refusing"
    assert all(size == 1024 * 1024 for size in read_calls), "must read in bounded chunks"


def test_a_normal_upload_still_works(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    project = projects_route.create_project(ProjectCreate(title="Uploads OK"))

    row = _upload(project["id"], "notes.md", b"# notes\n", "text/markdown")

    assert row["filename"] == "notes.md"
    assert row["stored_path"] == ".lea/files/notes.md"
