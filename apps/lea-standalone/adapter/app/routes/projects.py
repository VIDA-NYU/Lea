"""Project CRUD endpoints (D31).

The DB index half lives in `store`; the directory/repo half (provision + delete)
lives in the `projects` service. These routes are the composition layer: they
resolve the proofs root from config, then delegate. A project's proof/node counts
+ status mix are derived from live Lean state in later slices (the blueprint
graph), so detail here is just meta + the project's sessions.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from lea.interface import check as interface_check

from ..config import load_config, github_token
from ..gitstore import GitStore, GitStoreError
from .. import blueprint as blueprint_doc
from .. import blueprint_seed
from .. import filesystem as fs_service
from .. import graph as graph_service
from .. import github_import_service
from ..github_project_import import ImportPlanningError, TaggedTarget
from ..github_source import GitHubSourceError
from .. import projects as project_service
from .. import store
from .. import uploads

router = APIRouter()


class ProjectCreate(BaseModel):
    title: str
    description: str | None = None


class ProjectUpdate(BaseModel):
    title: str | None = None
    description: str | None = None


class NamespacePreviewRequest(BaseModel):
    project_name: str
    namespace: str | None = None
    exclude_project_id: str | None = None


class ProjectIdentityUpdate(BaseModel):
    project_name: str
    mode: str
    namespace: str | None = None
    expected_namespace: str | None = None
    create_if_missing: bool = False


class SessionCreate(BaseModel):
    title: str | None = None


class DocUpdate(BaseModel):
    content: str


class FilePut(BaseModel):
    path: str
    content: str


class MirrorFile(BaseModel):
    path: str
    content: str


class MirrorRequest(BaseModel):
    files: list[MirrorFile]
    source: str = "overleaf"
    # "reconcile" (default) treats the payload as the full truth set — absent
    # files are deleted. "upsert" writes only the provided files (the active-
    # buffer tier of the tex mirror, PLAN-system-hardening 3.2).
    mode: str = "reconcile"


class RemoteUpdate(BaseModel):
    remote_url: str


class GithubImportTarget(BaseModel):
    origin_key: str
    label: str
    declaration_name: str
    kind: str
    display_title: str
    statement: str | None = None
    source_hash: str | None = None


class GithubImportPreviewRequest(BaseModel):
    repository_url: str
    targets: list[GithubImportTarget] = Field(default_factory=list)
    project_name: str | None = None
    namespace: str | None = None


class GithubImportConfirmRequest(BaseModel):
    preview_id: str


class FormalizationTargetSyncRequest(BaseModel):
    targets: list[GithubImportTarget]


# An https GitHub repo URL: `github.com` / owner / repo (repo may carry a `.git`).
#
# The host is pinned deliberately (AUDIT-2026-07-24 S2). This pattern used to be
# `https://[\w.\-]+/...` — any host — while the error message below promised
# "a GitHub repo URL". `_push_project` hands the stored value to `push_to_github`,
# which embeds the global GitHub token in it, so an unpinned host turned "set a
# remote" into "mail my PAT to a server of the caller's choosing". Each path
# segment must start with a word character or hyphen, so `..` can't appear as an
# owner/repo either. `gitstore._inject_token` enforces the same host rule at the
# credential boundary; this is the input-validation half.
_GITHUB_REMOTE_RE = re.compile(r"^https://(?:www\.)?github\.com/[\w\-][\w.\-]*/[\w\-][\w.\-]*$")


def _normalize_remote_url(remote_url: str) -> str:
    return str(remote_url or "").strip().rstrip("/")


def _is_web_remote(remote_url: str) -> bool:
    """True for an http(s) remote — the only shape that can carry the token to a
    server. Local paths and ssh remotes are never credential-bearing."""
    return urlparse(str(remote_url or "")).scheme in ("http", "https")


# Git's wording for "the remote has commits you don't" (a rejected, non-fast-forward push).
_DIVERGED_MARKERS = ("non-fast-forward", "fetch first", "updates were rejected", "[rejected]")


def _push_failure_detail(message: str) -> str:
    """Human-facing push error. When the failure is a *divergence* (the remote moved
    ahead of the project), point the user at Lea to reconcile — not a dead end (D34)."""
    detail = f"Push failed: {message}"
    if any(marker in message.lower() for marker in _DIVERGED_MARKERS):
        detail += (
            "\n\nThe GitHub repo has commits this project doesn't — the histories have diverged. "
            "You can ask Lea to reconcile them (it can run git from a session), then push again."
        )
    return detail


def _proofs_root() -> Path:
    """The git proofs root (`<lea_root>/workspace/proofs`) project repos live under.
    Mirrors how the session routes build their GitStore root."""
    config = load_config()
    if config.lea_root is None:
        raise HTTPException(status_code=422, detail="lea_root is not configured")
    return config.lea_root / "workspace" / "proofs"


def _project_identity_error(exc: project_service.ProjectIdentityError) -> HTTPException:
    detail = {"error": exc.code, "message": str(exc), **exc.detail}
    return HTTPException(status_code=exc.status, detail=detail)


def _import_targets(rows: list[GithubImportTarget]) -> list[TaggedTarget]:
    return [TaggedTarget(**row.model_dump()) for row in rows]


def _import_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ImportPlanningError):
        status = {
            "import_preview_expired": 410,
            "project_busy": 409,
            "import_preview_mismatch": 409,
            "repository_file_limit": 413,
            "repository_size_limit": 413,
        }.get(exc.code, 422)
        return HTTPException(status_code=status, detail={"error": exc.code, "message": str(exc)})
    if isinstance(exc, GitHubSourceError):
        message = str(exc)
        status = 400 if exc.code == "invalid_repository_url" else 502
        return HTTPException(status_code=status, detail={"error": exc.code, "message": message})
    return HTTPException(status_code=500, detail={"error": "github_import_failed", "message": str(exc)})


@router.get("/api/projects")
def list_projects() -> dict:
    return {"projects": store.list_projects()}


@router.post("/api/projects", status_code=201)
def create_project(request: ProjectCreate) -> dict:
    title = request.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Project title is required")
    return project_service.provision_project(title, _proofs_root(), description=request.description)


@router.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict:
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return {**project, "sessions": store.list_project_sessions(project_id)}


@router.put("/api/projects/{project_id}")
def update_project(project_id: str, request: ProjectUpdate) -> dict:
    updated = store.update_project(
        project_id, title=request.title, description=request.description
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return updated


@router.post("/api/projects/namespace-preview")
def namespace_preview(request: NamespacePreviewRequest) -> dict:
    try:
        return project_service.namespace_preview(
            request.project_name,
            namespace=request.namespace,
            exclude_project_id=request.exclude_project_id,
        )
    except project_service.ProjectIdentityError as exc:
        raise _project_identity_error(exc)


@router.post("/api/projects/{project_id}/sessions", status_code=201)
def create_session_in_project(project_id: str, request: SessionCreate) -> dict:
    """Create a session that lives inside the project (D23). The session is tagged
    with `project_id`; the run it later starts resolves the shared project repo +
    namespace from that tag (the bridge does this, D24/D25). Proofs land in
    `proofs/Lea/<Project>/` from turn 1, able to import sibling lemmas."""
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    title = (request.title or "").strip() or "Untitled theorem"
    return store.create_session(title, project_id=project_id)


# ── Instructions & Memory: the two user/agent-editable .lea/*.md docs (R1/R2) ────
# Raw markdown in, raw markdown out. Instructions (D25) is user-authored; Memory
# (D26) is co-authored — the agent also writes memory.md with its own write_file
# during a run. Both GETs return the seeded template when never edited; both PUTs
# write+commit and the new content feeds the next run's composed context.


def _get_doc(project_id: str, name: str) -> dict:
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"content": project_service.read_doc(project, _proofs_root(), name)}


def _put_doc(project_id: str, name: str, content: str) -> dict:
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    sha = project_service.write_doc(project, _proofs_root(), name, content)
    return {"content": content, "commit_sha": sha}


@router.get("/api/projects/{project_id}/instructions")
def get_instructions(project_id: str) -> dict:
    return _get_doc(project_id, "instructions.md")


@router.put("/api/projects/{project_id}/instructions")
def put_instructions(project_id: str, request: DocUpdate) -> dict:
    return _put_doc(project_id, "instructions.md", request.content)


@router.get("/api/projects/{project_id}/memory")
def get_memory(project_id: str) -> dict:
    return _get_doc(project_id, "memory.md")


@router.put("/api/projects/{project_id}/memory")
def put_memory(project_id: str, request: DocUpdate) -> dict:
    return _put_doc(project_id, "memory.md", request.content)


# ── Blueprint: the canonical .lea/blueprint.md decomposition (T1, D28) ────────────
# Same raw-markdown GET/PUT as the other docs, but each response also carries the
# parser's advisory `warnings` (duplicate keys, missing/unknown kind, dangling edges)
# so the authoring view can flag structure problems without blocking the save. The
# parsed graph (nodes + derived status) is a separate `/graph` endpoint in T2.


def _blueprint_payload(project: dict) -> dict:
    """Raw blueprint markdown + the parser's advisory warnings — shared by the
    by-id and by-slug GET routes."""
    content = project_service.read_doc(project, _proofs_root(), "blueprint.md")
    return {"content": content, "warnings": blueprint_doc.validate(content)}


@router.get("/api/projects/{project_id}/blueprint")
def get_blueprint(project_id: str) -> dict:
    return _blueprint_payload(_require_project(project_id))


@router.put("/api/projects/{project_id}/blueprint")
def put_blueprint(project_id: str, request: DocUpdate) -> dict:
    project = _require_project(project_id)
    sha = project_service.write_doc(project, _proofs_root(), "blueprint.md", request.content)
    return {"content": request.content, "commit_sha": sha, "warnings": blueprint_doc.validate(request.content)}


@router.get("/api/projects/{project_id}/graph")
def get_graph(project_id: str) -> dict:
    # T2: parse the blueprint + derive each node's live status and session
    # attribution (D28/D29). Cheap — reuses stored verdicts, never recompiles.
    project = _require_project(project_id)
    return graph_service.build_graph(project, _proofs_root())


@router.post("/api/projects/{project_id}/blueprint/generate")
def generate_blueprint(project_id: str) -> dict:
    # Populate the blueprint from formalized artifacts (additive, idempotent).
    # Writes + commits .lea/blueprint.md; returns {added, skipped, warnings, graph}.
    # Backfill first (like the other artifact-reading routes) so a pre-index project's
    # proofs are in the table before we read it — otherwise it would generate nothing.
    project = _require_project(project_id)
    _ensure_artifacts_backfilled(project)
    return blueprint_seed.generate(project, _proofs_root())


# ── Filesystem: tree / read / edit / export the project repo (U1/U2, D34) ─────────
# A project is already a git repo (D21/D22), so this is mostly *exposure*: a tree of
# the repo (hiding .git/.lake), a text read of any file, an edit that funnels through
# the same commit-on-write primitive as the v2 canvas (path-guarded), and a zip
# export. A `.lean` edit re-uses the standalone `check` for a verdict (like D2).

# Filesystem service error codes → HTTP status (path escape, missing, non-text).
_FS_ERROR_STATUS = {"bad_path": 400, "not_found": 404, "binary": 415}


@router.get("/api/projects/{project_id}/tree")
def get_tree(project_id: str) -> dict:
    project = _require_project(project_id)
    repo = project_service.project_repo_dir(project, _proofs_root())
    return {"tree": fs_service.build_tree(repo)}


@router.get("/api/projects/{project_id}/file")
def read_project_file(project_id: str, path: str) -> dict:
    project = _require_project(project_id)
    repo = project_service.project_repo_dir(project, _proofs_root())
    try:
        content = fs_service.read_text_file(repo, path)
    except fs_service.FilesystemError as exc:
        raise HTTPException(status_code=_FS_ERROR_STATUS.get(exc.code, 400), detail=str(exc))
    return {"path": path, "content": content, "lean": path.endswith(".lean")}


@router.put("/api/projects/{project_id}/file")
def write_project_file(project_id: str, request: FilePut) -> dict:
    """Write + commit any file under the project repo (path-guarded, author=user).
    Editing a `.lean` returns its standalone `check` verdict so the editor can flag
    a broken proof; editing `.lea/blueprint.md` lets the next `/graph` fetch re-derive."""
    project = _require_project(project_id)
    if store.project_has_active_import(project_id):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "project_busy",
                "message": "Wait for the active GitHub import before editing project files.",
            },
        )
    proofs_root = _proofs_root()
    try:
        sha = fs_service.write_text_file(project, proofs_root, request.path, request.content)
    except fs_service.FilesystemError as exc:
        raise HTTPException(status_code=_FS_ERROR_STATUS.get(exc.code, 400), detail=str(exc))
    check = None
    if request.path.endswith(".lean"):
        repo = project_service.project_repo_dir(project, proofs_root)
        result = interface_check(str(repo / request.path))
        check = {"status": result.status, "detail": result.detail}
    return {"path": request.path, "commit_sha": sha, "check": check}


def _export_project_response(project: dict) -> Response:
    """Zip the project repo (source + .lea assets; .git/.lake excluded) into a
    download response — shared by the by-id and by-slug export routes (D34)."""
    repo = project_service.project_repo_dir(project, _proofs_root())
    data = fs_service.export_zip(repo)
    filename = f"{project['slug']}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/projects/{project_id}/export")
def export_project(project_id: str) -> Response:
    """Stream a zip of the whole project repo (source + .lea assets; .git/.lake
    excluded) so the project is shareable as a single download (D34)."""
    return _export_project_response(_require_project(project_id))


# ── Additive GitHub Lean-file import ────────────────────────────────────────


def _preview_github_import(project: dict, request: GithubImportPreviewRequest) -> dict:
    try:
        return github_import_service.preview_import(
            project=project,
            proofs_root=_proofs_root(),
            repository_url=request.repository_url,
            targets=_import_targets(request.targets),
        )
    except (ImportPlanningError, GitHubSourceError) as exc:
        raise _import_error(exc) from None


def _confirm_github_import(project: dict, request: GithubImportConfirmRequest) -> dict:
    try:
        return github_import_service.confirm_import(
            project=project,
            proofs_root=_proofs_root(),
            preview_id=request.preview_id,
        )
    except (ImportPlanningError, GitHubSourceError) as exc:
        raise _import_error(exc) from None


def _github_import_progress(project: dict, import_id: str) -> dict:
    result = store.github_import_progress(import_id)
    if result is None or result["project_id"] != project["id"]:
        raise HTTPException(status_code=404, detail="GitHub import not found")
    return result


@router.post("/api/projects/{project_id}/github-imports/preview")
def preview_project_github_import(project_id: str, request: GithubImportPreviewRequest) -> dict:
    return _preview_github_import(_require_project(project_id), request)


@router.post("/api/projects/{project_id}/github-imports", status_code=202)
def confirm_project_github_import(project_id: str, request: GithubImportConfirmRequest) -> dict:
    return _confirm_github_import(_require_project(project_id), request)


@router.get("/api/projects/{project_id}/github-imports/{import_id}")
def get_project_github_import(project_id: str, import_id: str) -> dict:
    return _github_import_progress(_require_project(project_id), import_id)


@router.post("/api/projects/by-slug/{slug}/github-imports/preview")
def preview_project_github_import_by_slug(
    slug: str, request: GithubImportPreviewRequest
) -> dict:
    try:
        project = store.get_project_by_slug(slug)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if project is None:
        try:
            project = project_service.ensure_project(
                slug,
                _proofs_root(),
                title=(request.project_name or slug),
                namespace=request.namespace,
            )
        except (ValueError, project_service.ProjectIdentityError) as exc:
            if isinstance(exc, project_service.ProjectIdentityError):
                raise _project_identity_error(exc)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _preview_github_import(project, request)


@router.post("/api/projects/by-slug/{slug}/github-imports", status_code=202)
def confirm_project_github_import_by_slug(
    slug: str, request: GithubImportConfirmRequest
) -> dict:
    return _confirm_github_import(_require_project_by_slug(slug), request)


@router.get("/api/projects/by-slug/{slug}/github-imports/{import_id}")
def get_project_github_import_by_slug(slug: str, import_id: str) -> dict:
    return _github_import_progress(_require_project_by_slug(slug), import_id)


@router.post("/api/projects/by-slug/{slug}/formalizations/sync")
def sync_project_formalizations_by_slug(
    slug: str, request: FormalizationTargetSyncRequest
) -> dict:
    project = _require_project_by_slug(slug)
    return github_import_service.sync_targets(
        project,
        _import_targets(request.targets),
        _proofs_root(),
    )


# ── Git sharing: set remote + push to GitHub (6b/U3, D34) ─────────────────────────
# A project is already a git repo, so sharing is: store a per-project remote URL,
# snapshot the complete working tree, then push it with the global `github_token`
# (Settings). Pushing is outward-facing → always an explicit user action; the token
# is injected into the push URL only (never persisted to .git/config) and scrubbed
# from any output.


def _set_remote_on(project: dict, remote_url: str) -> dict:
    """Validate + store a project's GitHub remote URL — shared by the by-id and
    by-slug routes (D34). The token stays global in Settings."""
    url = _normalize_remote_url(remote_url)
    if not _GITHUB_REMOTE_RE.fullmatch(url):
        raise HTTPException(
            status_code=400,
            detail="Enter an https GitHub repo URL, e.g. https://github.com/you/repo",
        )
    updated = store.update_project(project["id"], remote_url=url)
    return {"id": updated["id"], "remote_url": updated["remote_url"]}


def _push_project(project: dict) -> dict:
    """Commit the current project tree, then push it to the configured remote.

    The explicit snapshot is what makes Share include files written outside the
    adapter's normal commit-on-write paths (for example, an existing Lean project
    copied into the repo). A clean tree is a no-op, so repeated pushes do not create
    empty commits. Shared by the by-id and by-slug routes (D34).
    """
    remote_url = project.get("remote_url")
    if not remote_url:
        raise HTTPException(status_code=400, detail="No GitHub remote set for this project.")
    token = github_token()
    if not token:
        raise HTTPException(status_code=400, detail="No GitHub token configured. Add one in Settings.")
    # Re-check the host at the moment the credential would be used (AUDIT-2026-07-24 S2).
    # `_set_remote_on` validates on the way in, but a row written before the host was
    # pinned — or by anything else that touches `projects.remote_url` — must not become
    # a token-delivery target on the way out. Only http(s) remotes can carry the token,
    # so a local-path or ssh remote is left alone.
    if _is_web_remote(remote_url) and not _GITHUB_REMOTE_RE.fullmatch(_normalize_remote_url(remote_url)):
        raise HTTPException(
            status_code=400,
            detail=(
                "This project's remote is not a GitHub URL, so pushing it would send your "
                "GitHub token to another server. Set an https://github.com/... remote first."
            ),
        )
    if store.project_has_active_run(project["id"]) or store.project_has_active_import(project["id"]):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "project_busy",
                "message": "Wait for the active project operation to finish before sharing.",
            },
        )
    proofs_root = _proofs_root()
    repo = project_service.project_repo_dir(project, proofs_root)
    git = GitStore(proofs_root)
    try:
        commit_sha = git.commit_all(repo, "share: snapshot project")
    except GitStoreError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not commit the project before push: {exc}",
        ) from None
    try:
        summary = git.push_to_github(repo, remote_url, token)
    except GitStoreError as exc:
        raise HTTPException(status_code=502, detail=_push_failure_detail(str(exc))) from None
    return {
        "pushed": True,
        "remote_url": remote_url,
        "commit_sha": commit_sha,
        "detail": summary or "Committed current project changes and pushed.",
    }


@router.put("/api/projects/{project_id}/git/remote")
def set_project_remote(project_id: str, request: RemoteUpdate) -> dict:
    """Set the project's GitHub remote URL (D34). Stored on the project row; the
    token stays global in Settings. Returns the (token-free) remote."""
    return _set_remote_on(_require_project(project_id), request.remote_url)


@router.post("/api/projects/{project_id}/git/push")
def push_project(project_id: str) -> dict:
    """Push the project repo to its configured GitHub remote using the global token
    (D34). Explicit user action only. 400 if no remote or no token is configured;
    502 (scrubbed) if git rejects the push (auth / non-fast-forward / unreachable)."""
    return _push_project(_require_project(project_id))


# ── By-slug variants: the Overleaf companion's view of export/share (D34) ─────────
# The companion identifies a project by the slug it derives from the Overleaf
# document (`slugProjectId`), the same way `/api/projects/by-slug/{slug}/mirror`
# does. Unlike mirror, these NEVER create the project (`ensure_project` is the
# formalize path's job): an unknown slug is a plain 404 — "nothing to export yet".


def _require_project_by_slug(slug: str) -> dict:
    try:
        project = store.get_project_by_slug(slug)
    except ValueError:
        project = None  # malformed slug ≡ no such project
    if project is None:
        raise HTTPException(status_code=404, detail="No Lea project exists for this document yet.")
    return project


@router.get("/api/projects/by-slug/{slug}/identity")
def get_project_identity_by_slug(slug: str) -> dict:
    return project_service.project_identity(_require_project_by_slug(slug))


@router.put("/api/projects/by-slug/{slug}/identity")
def update_project_identity_by_slug(slug: str, request: ProjectIdentityUpdate) -> dict:
    if request.mode not in {"display-only", "rename-namespace"}:
        raise HTTPException(status_code=422, detail="mode must be 'display-only' or 'rename-namespace'")
    try:
        project = store.get_project_by_slug(slug)
    except ValueError:
        project = None
    if project is None:
        if not request.create_if_missing:
            raise HTTPException(status_code=404, detail="No Lea project exists for this document yet.")
        try:
            project = project_service.ensure_project(
                slug,
                _proofs_root(),
                title=request.project_name,
                namespace=request.namespace,
            )
        except project_service.ProjectIdentityError as exc:
            raise _project_identity_error(exc)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"identity": project_service.project_identity(project)}

    title = request.project_name.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Project name is required")

    if request.mode == "display-only":
        project_service.refresh_project_title_docs(project, _proofs_root(), title)
        updated = store.update_project(project["id"], title=title)
        return {"identity": project_service.project_identity(updated)}

    try:
        result = project_service.migrate_project_namespace(
            project,
            _proofs_root(),
            title=title,
            namespace=request.namespace or title,
            expected_namespace=request.expected_namespace,
            check_fn=interface_check,
        )
    except project_service.ProjectIdentityError as exc:
        raise _project_identity_error(exc)
    return {
        "identity": project_service.project_identity(result["project"]),
        "migration": result["migration"],
    }


@router.get("/api/projects/by-slug/{slug}/share")
def get_share_status_by_slug(slug: str) -> dict:
    """The share state the Overleaf pane needs in one call: the project's remote
    (token-free) plus whether the global GitHub token is configured (presence only,
    like Settings — the raw token never reaches a client)."""
    project = _require_project_by_slug(slug)
    return {
        "id": project["id"],
        "slug": project["slug"],
        "remote_url": project.get("remote_url"),
        "token_configured": bool(github_token()),
    }


# ── Blueprint / graph by slug: the Overleaf companion's read-only view ────────────
# The companion resolves the project by the slug it derives from the Overleaf
# document, so the blueprint viewer in the Lean pane reads through these. Same
# derivation as the by-id routes (`_blueprint_payload` / `build_graph`); like every
# other by-slug route they NEVER create a project — an unknown slug is a plain 404.


@router.get("/api/projects/by-slug/{slug}/blueprint")
def get_blueprint_by_slug(slug: str) -> dict:
    return _blueprint_payload(_require_project_by_slug(slug))


@router.get("/api/projects/by-slug/{slug}/graph")
def get_graph_by_slug(slug: str) -> dict:
    return graph_service.build_graph(_require_project_by_slug(slug), _proofs_root())


@router.post("/api/projects/by-slug/{slug}/blueprint/generate")
def generate_blueprint_by_slug(slug: str) -> dict:
    # The Overleaf "Generate from formalized theorems" button lands here (slug-resolved,
    # never creates a project). Same additive synthesis as the by-id route, backfill included.
    project = _require_project_by_slug(slug)
    _ensure_artifacts_backfilled(project)
    return blueprint_seed.generate(project, _proofs_root())


def _ensure_artifacts_backfilled(project: dict) -> None:
    """4.3: one-time import of a pre-index project's registry markdown into
    the artifacts table, so the index answers for artifacts that predate 4.1
    and the markdown becomes write-only for machines. No-op once any row
    exists (run-recorded or previously backfilled)."""
    from .. import registry_backfill

    if store.list_artifacts_for_scope(project["id"]):
        return
    config = load_config()
    if not config.lea_root:
        return
    registry = Path(config.lea_root) / "workspace" / "projects" / f"{project['slug']}.md"
    registry_backfill.backfill_artifacts_from_registry(project, _proofs_root(), registry)


@router.get("/api/projects/by-slug/{slug}/artifacts")
def project_artifacts_by_slug(slug: str) -> dict:
    """Structured artifact index (PLAN-system-hardening 4.1): which declaration
    lives in which file, as recorded by the run finalizer. The Overleaf
    companion reads this instead of reverse-engineering artifacts from
    registry-markdown diffs. Like every by-slug route, never creates a
    project — an unknown slug is a 404 ("nothing recorded yet")."""
    project = _require_project_by_slug(slug)
    _ensure_artifacts_backfilled(project)
    return {
        "project_id": project["id"],
        "slug": project["slug"],
        "artifacts": store.list_artifacts_for_scope(project["id"]),
    }


# Cap on the file content echoed back by target-status: recorded proofs are a
# few KB; anything bigger is truncated for display (verdict fields are computed
# from the full content either way).
_TARGET_STATUS_CONTENT_CAP = 64 * 1024


@router.get("/api/projects/by-slug/{slug}/target-status")
def project_target_status_by_slug(slug: str, declarations: str = "") -> dict:
    """Ledger-side target evidence (PLAN-system-hardening 4.4): for each named
    declaration, what the adapter's own records say — the artifact row, whether
    the recorded file exists and still holds the declaration, whether it leans
    on sorry/admit, and the newest recorded check verdict (agent run, manual
    edit, or cascade re-check alike). This is one of the two sources the
    companion's ledger status engine merges; the other is its own job overlay.
    `content` rides along (capped) purely for display extraction client-side —
    the verdict fields here are authoritative."""
    from .. import artifacts as artifacts_service

    project = _require_project_by_slug(slug)
    _ensure_artifacts_backfilled(project)
    names = [name for name in (part.strip() for part in declarations.split(",")) if name]
    repo = project_service.project_repo_dir(project, _proofs_root())
    rows = {row["declaration_name"]: row for row in store.list_artifacts_for_scope(project["id"])}

    targets = []
    for name in names:
        row = rows.get(name)
        if not row:
            targets.append({"declaration_name": name, "recorded": False})
            continue
        absolute = repo / row["path"]
        exists = absolute.is_file()
        content = ""
        if exists:
            try:
                content = absolute.read_text()
            except OSError:
                exists = False
        check = store.latest_check_for_project_path(project["id"], row["path"])
        formalization = (
            store.get_formalization(row["formalization_id"])
            if row.get("formalization_id") else None
        )
        current_source_hash = (formalization or {}).get("source_hash")
        artifact_source_hash = row.get("source_hash")
        targets.append({
            "declaration_name": name,
            "recorded": True,
            "path": row["path"],
            "module_name": row["module_name"],
            "kind": row["kind"],
            "exists": exists,
            "declaration_present": artifacts_service.declaration_present(content, name) if exists else False,
            "has_sorry": artifacts_service.declaration_contains_sorry(content, name) if exists else None,
            "check_status": check["check_status"] if check else None,
            "check_detail": check["check_detail"] if check else None,
            "check_author": check["author"] if check else None,
            "formalization_id": row.get("formalization_id"),
            "current_source_hash": current_source_hash,
            "artifact_source_hash": artifact_source_hash,
            "stale": bool(
                current_source_hash
                and artifact_source_hash
                and current_source_hash != artifact_source_hash
            ),
            "content": content[:_TARGET_STATUS_CONTENT_CAP] if exists else None,
        })
    return {"project_id": project["id"], "slug": project["slug"], "targets": targets}


class ArtifactRetireRequest(BaseModel):
    path: str  # repo-relative, same convention as the artifact rows


class ArtifactRestoreRequest(BaseModel):
    path: str
    retire_commit: str


def _is_retirable_artifact(project: dict, rel: str) -> bool:
    """Whether this path is a proof artifact, as opposed to project infrastructure.

    Recorded in the `artifacts` index or carrying a code_step settles it. Failing that,
    a `.lean` source outside the `.lea/` asset tree still counts: a proof written
    before the index existed — or written by the agent through `bash` rather than
    `write_file` — has neither row, and `/artifacts/restore` explicitly supports
    exactly that case ("legacy/manual files that have no timeline row"), so refusing it
    here would break the retry flow this endpoint exists for.

    What that leaves out is the point: `lakefile.toml`, `.gitignore`, and everything
    under `.lea/` (instructions, memory, blueprint, uploads, the Overleaf mirror) are
    project infrastructure with no restore path, and an endpoint whose whole job is
    "delete the previous proof" has no business removing them (AUDIT-2026-07-24 S5)."""
    if any(row["path"] == rel for row in store.list_artifacts_for_scope(project["id"])):
        return True
    if store.code_steps_for_project_path(project["id"], rel, include_content=False):
        return True
    parts = PurePosixPath(rel).parts
    return rel.endswith(".lean") and bool(parts) and parts[0] != ".lea"


def _resolve_repo_file(project: dict, rel: str) -> tuple[Path, Path, str]:
    """(repo, absolute, normalized-rel) for a repo-relative path, rejecting
    anything that escapes the project repo."""
    repo = project_service.project_repo_dir(project, _proofs_root())
    candidate = Path(str(rel or ""))
    if candidate.is_absolute():
        raise HTTPException(status_code=422, detail="path must be repo-relative")
    absolute = (repo / candidate).resolve()
    if not absolute.is_relative_to(repo.resolve()):
        raise HTTPException(status_code=422, detail="path escapes the project repo")
    return repo, absolute, absolute.relative_to(repo.resolve()).as_posix()


@router.post("/api/projects/by-slug/{slug}/artifacts/retire")
def retire_project_artifact_by_slug(slug: str, request: ArtifactRetireRequest) -> dict:
    """Single-writer retirement (PLAN-system-hardening 4.5): a retry deletes the
    previous proof file through the adapter, then records that filesystem change
    in the project repo. Proof bytes remain SQL-owned; /artifacts/restore uses the
    latest verified timeline snapshot and falls back to the retire commit's parent
    for legacy/manual files that have no timeline row."""
    project = _require_project_by_slug(slug)
    repo, absolute, rel = _resolve_repo_file(project, request.path)
    if not absolute.is_file():
        raise HTTPException(status_code=404, detail="No recorded file at that path")
    # "No recorded file at that path" was only ever checking that a file EXISTED, so
    # this endpoint would unlink anything in the repo — `lakefile.toml`,
    # `.lea/instructions.md`, another session's proof (AUDIT-2026-07-24 S5). It is a
    # retry primitive for a *recorded artifact*, so require the path to be one:
    # indexed in `artifacts`, or carrying a code_step (which covers files written
    # before the index existed, and is what `/artifacts/restore` reads back).
    if not _is_retirable_artifact(project, rel):
        raise HTTPException(
            status_code=422,
            detail="That path is not a recorded proof artifact for this project.",
        )
    absolute.unlink()
    try:
        sha = GitStore(repo.parent).commit_all(repo, f"retire {rel} for retry", paths=[rel])
    except GitStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"retire_commit": sha, "path": rel}


@router.post("/api/projects/by-slug/{slug}/artifacts/restore")
def restore_project_artifact_by_slug(slug: str, request: ArtifactRestoreRequest) -> dict:
    """Undo a retirement after a retry that produced no verified replacement:
    restore the latest verified SQL snapshot, with the retire commit's parent as
    a compatibility fallback. Conservative like the companion's old stash-restore:
    never overwrite a file the failed run left at the path."""
    project = _require_project_by_slug(slug)
    repo, absolute, rel = _resolve_repo_file(project, request.path)
    if absolute.exists():
        return {"restored": False, "reason": "occupied", "path": rel}
    content = next(
        (
            step["code"]
            for step in store.code_steps_for_project_path(project["id"], rel)
            if step.get("check_status") == "ok" and not step.get("content_lost")
        ),
        None,
    )
    if content is None:
        gs = GitStore(repo.parent)
        try:
            content = gs.snapshot(repo.name, f"{request.retire_commit}^", rel)
        except GitStoreError:
            raise HTTPException(
                status_code=404,
                detail="No verified snapshot or file in the retire commit's parent",
            )
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_text(content)
    try:
        sha = GitStore(repo.parent).commit_all(
            repo, f"restore {rel} after unverified retry", paths=[rel],
        )
    except GitStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"restored": True, "commit": sha, "path": rel}


@router.get("/api/projects/by-slug/{slug}/export")
def export_project_by_slug(slug: str) -> Response:
    return _export_project_response(_require_project_by_slug(slug))


@router.put("/api/projects/by-slug/{slug}/git/remote")
def set_project_remote_by_slug(slug: str, request: RemoteUpdate) -> dict:
    return _set_remote_on(_require_project_by_slug(slug), request.remote_url)


@router.post("/api/projects/by-slug/{slug}/git/push")
def push_project_by_slug(slug: str) -> dict:
    return _push_project(_require_project_by_slug(slug))


# ── Files: upload / list / download / delete (S1/S2, D27) ────────────────────────
# Uploaded reference docs land in the project repo's .lea/files/ (git-versioned) and
# are indexed in project_files; Tier-2 (.pdf/.docx) also get a .txt sidecar the agent
# reads. The bytes are canonical in git — these rows are the pointer + metadata.

_UPLOAD_ERROR_STATUS = {"too_large": 413, "unsupported": 415}


def _require_project(project_id: str) -> dict:
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/api/projects/{project_id}/files")
def list_files(project_id: str) -> dict:
    _require_project(project_id)
    return {"files": store.list_project_files(project_id)}


@router.post("/api/projects/by-slug/{slug}/mirror")
def mirror_overleaf_tex(slug: str, request: MirrorRequest, background_tasks: BackgroundTasks) -> dict:
    """Mirror the Overleaf project's `.tex`/`.sty`/`.cls` sources into the matching project's
    `.lea/files/overleaf/` (resolving/creating the project by slug, like `/api/runs`).
    Reconcile is synchronous (files on disk + indexed before returning); the git commit
    is **deferred** to a background task so the formalize path never waits on git."""
    proofs_root = _proofs_root()
    try:
        project = project_service.ensure_project(slug, proofs_root)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    incoming = [{"path": f.path, "content": f.content} for f in request.files]
    mode = "upsert" if request.mode == "upsert" else "reconcile"
    try:
        summary = uploads.sync_overleaf_tex(project, proofs_root, incoming, commit=False, mode=mode)
    except uploads.UploadError as exc:
        raise HTTPException(status_code=_UPLOAD_ERROR_STATUS.get(exc.code, 400), detail=str(exc))
    if summary.get("changed"):
        background_tasks.add_task(uploads.commit_mirror, project, proofs_root)
    return {"project_id": project["id"], "slug": project["slug"], **summary}


async def _read_capped(file: UploadFile, cap: int) -> bytes:
    """Read an upload, refusing past `cap` instead of buffering it all first.

    `await file.read()` pulled the WHOLE body into memory and only then let
    `uploads._validate` check the 25 MB limit (AUDIT-2026-07-24 S7) — so a multi-GB
    POST could exhaust the adapter, and with it every in-flight run, since the prover
    runs in this same process. Streaming with a running total means an oversized upload
    costs one chunk, not the whole file."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise uploads.UploadError(
                f"File is too large; the cap is {cap // (1024 * 1024)} MB.",
                code="too_large",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/api/projects/{project_id}/files", status_code=201)
async def upload_file(project_id: str, file: UploadFile = File(...)) -> dict:
    project = _require_project(project_id)
    try:
        data = await _read_capped(file, uploads.MAX_UPLOAD_BYTES)
    except uploads.UploadError as exc:
        raise HTTPException(status_code=_UPLOAD_ERROR_STATUS.get(exc.code, 400), detail=str(exc))
    try:
        return uploads.save_upload(
            project, _proofs_root(), file.filename or "file", data, mime=file.content_type
        )
    except uploads.UploadError as exc:
        raise HTTPException(status_code=_UPLOAD_ERROR_STATUS.get(exc.code, 400), detail=str(exc))


@router.get("/api/projects/{project_id}/files/{file_id}")
def download_file(project_id: str, file_id: str) -> FileResponse:
    project = _require_project(project_id)
    row = store.get_project_file(file_id)
    if row is None or row["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="File not found")
    path = uploads.file_disk_path(project, _proofs_root(), row)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File bytes are missing")
    return FileResponse(path, media_type=row.get("mime") or "application/octet-stream", filename=row["filename"])


@router.delete("/api/projects/{project_id}/files/{file_id}")
def delete_file(project_id: str, file_id: str) -> dict:
    project = _require_project(project_id)
    row = store.get_project_file(file_id)
    if row is None or row["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="File not found")
    uploads.delete_file(project, _proofs_root(), row)
    return {"deleted": True, "id": file_id}


@router.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> dict:
    if store.project_has_active_import(project_id):
        raise HTTPException(status_code=409, detail={
            "error": "project_busy",
            "message": "Wait for the active GitHub import before deleting this project.",
        })
    if not project_service.delete_project(project_id, _proofs_root()):
        raise HTTPException(status_code=404, detail="Project not found")
    return {"deleted": True, "id": project_id}
