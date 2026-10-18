"""Project CRUD endpoints (D31).

The DB index half lives in `store`; the directory/repo half (provision + delete)
lives in the `projects` service. These routes are the composition layer: they
resolve the proofs root from config, then delegate. A project's proof/node counts
+ status mix are derived from live Lean state in later slices (the blueprint
graph), so detail here is just meta + the project's sessions.
"""

from __future__ import annotations

from .. import peripheral_views
from .. import workspace_commands

import re
import hashlib
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from ..core_query_dispatch import database_query
from .. import workspace_commands
from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile, Request
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
from .. import store, project_coordination, proof_files
from .. import uploads
from ..alignment_schemas import SourceBundle
from ..read_projections import read_projection

router = APIRouter()


def _guard_project(*, by_slug=False, write=False, metadata=False, environment_write=True):
    def resolve(arguments):
        if by_slug:
            try:
                project = store.get_project_by_slug(arguments["slug"])
            except ValueError:
                return None  # Preserve the route's existing invalid-slug response.
            return project["id"] if project else None
        return arguments["project_id"]
    return project_coordination.guarded(resolve, write=write, metadata=metadata,
                                        environment_write=environment_write)



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
    overleaf_project_title: str | None = None


class OverleafDefaultNameRequest(BaseModel):
    overleaf_project_title: str


class SessionCreate(BaseModel):
    title: str | None = None


class DocUpdate(workspace_commands.EditRequest):
    content: str


class FilePut(workspace_commands.EditRequest):
    path: str
    content: str


class MirrorFile(BaseModel):
    path: str
    content: str


class MirrorRequest(BaseModel):
    files: list[MirrorFile]
    source: str = "overleaf"
    overleaf_project_title: str | None = None
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
    source_bundle: SourceBundle | None = None


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
@database_query('projects')
def list_projects() -> dict:
    return {"projects": store.list_projects()}


@router.post('/api/projects', status_code=202)
@workspace_commands.register('project.create')
def create_project(request: ProjectCreate) -> dict:
    title = request.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Project title is required")
    return project_service.provision_project(title, _proofs_root(), description=request.description)


@router.get("/api/projects/{project_id}")
@database_query('project')
def get_project(project_id: str) -> dict:
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return {**project, "sessions": store.list_project_sessions(project_id)}


@router.put('/api/projects/{project_id}', status_code=202)
@workspace_commands.register('project.update')
@_guard_project(by_slug=False, write=True, metadata=False)
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
    content=project_service.read_doc(project, _proofs_root(), name)
    revision=workspace_commands.revision_for('p:'+project_id,'.lea/'+name)
    return {"content":content,'file_revision':revision if revision!='missing' else 'untracked:'+hashlib.sha256(content.encode()).hexdigest()}


@_guard_project(by_slug=False, write=True, metadata=False)
def _put_doc(project_id: str, name: str, content: str) -> dict:
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    sha = project_service.write_doc(project, _proofs_root(), name, content)
    return {"content": content, "commit_sha": sha,'file_revision':workspace_commands.revision_for('p:'+project_id,'.lea/'+name)}


@router.get("/api/projects/{project_id}/instructions")
@peripheral_views.query('get_instructions')
def get_instructions(project_id: str) -> dict:
    return _get_doc(project_id, "instructions.md")


@router.put('/api/projects/{project_id}/instructions', status_code=202)
@workspace_commands.register('project.instructions')
def put_instructions(project_id: str, request: DocUpdate) -> dict:
    return _put_doc(project_id, "instructions.md", request.content)


@router.get("/api/projects/{project_id}/memory")
@peripheral_views.query('get_memory')
def get_memory(project_id: str) -> dict:
    return _get_doc(project_id, "memory.md")


@router.put('/api/projects/{project_id}/memory', status_code=202)
@workspace_commands.register('project.memory')
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
    revision=workspace_commands.revision_for('p:'+project['id'],'.lea/blueprint.md')
    return {"content": content, "warnings": blueprint_doc.validate(content),
        'file_revision':revision if revision!='missing' else 'untracked:'+hashlib.sha256(content.encode()).hexdigest()}


@router.get("/api/projects/{project_id}/blueprint")
@peripheral_views.query('get_blueprint')
@_guard_project(by_slug=False, write=False, metadata=True)
def get_blueprint(project_id: str) -> dict:
    return _blueprint_payload(_require_project(project_id))


@router.put('/api/projects/{project_id}/blueprint', status_code=202)
@workspace_commands.register('project.blueprint')
@_guard_project(by_slug=False, write=True, metadata=False)
def put_blueprint(project_id: str, request: DocUpdate) -> dict:
    project = _require_project(project_id)
    sha = project_service.write_doc(project, _proofs_root(), "blueprint.md", request.content)
    return {"content": request.content, "commit_sha": sha, "warnings": blueprint_doc.validate(request.content),
        'file_revision':workspace_commands.revision_for('p:'+project_id,'.lea/blueprint.md')}


@router.get("/api/projects/{project_id}/graph")
@peripheral_views.query('get_graph')
@_guard_project(by_slug=False, write=False, metadata=True)
def get_graph(project_id: str) -> dict:
    # T2: parse the blueprint + derive each node's live status and session
    # attribution (D28/D29). Cheap — reuses stored verdicts, never recompiles.
    project = _require_project(project_id)
    return graph_service.build_graph(project, _proofs_root())


@router.post('/api/projects/{project_id}/blueprint/generate', status_code=202)
@workspace_commands.register('project.generate_blueprint')
@_guard_project(by_slug=False, write=True, metadata=True)
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
    _require_project(project_id)
    from ..queries import files
    try:return files.tree(project_id)
    except files.ProjectionUnavailable as exc:raise HTTPException(409,detail={'error':'projection_unavailable','reason':exc.reason}) from exc


@router.get("/api/projects/{project_id}/file")
def read_project_file(project_id: str,path:str,observation_id:str|None=None) -> dict:
    _require_project(project_id)
    from ..queries import files
    from .workspace import file as sql_file
    try:
        if observation_id is not None:return {**files.content(project_id,path,observation_id),'file_revision':workspace_commands.revision_for('p:'+project_id,path)}
        value=sql_file(path,project_id=project_id)
        if value['content'] is not None:return {**value,'lean':path.endswith('.lean')}
        return {**files.content(project_id,path),'file_revision':value['file_revision']}
    except files.ProjectionUnavailable as exc:raise HTTPException({'not_found':404,'bad_path':400,'binary':415,'file_too_large':413}.get(exc.reason,409),detail={'error':'projection_unavailable','reason':exc.reason}) from exc
    except HTTPException as exc:
        if exc.status_code==422:raise HTTPException(400,exc.detail) from exc
        raise


@router.put('/api/projects/{project_id}/file', status_code=202)
@workspace_commands.register('project.save')
@_guard_project(by_slug=False, write=True, metadata=False)
def write_project_file(project_id: str, request: FilePut) -> dict:
    project = _require_project(project_id)
    if store.project_has_active_import(project_id):raise HTTPException(409,{'error':'project_busy','message':'Wait for the active GitHub import before editing project files.'})
    proofs_root = _proofs_root()
    repo = project_service.project_repo_dir(project, proofs_root)
    try:
        absolute = fs_service.safe_abs(repo, request.path)
    except fs_service.FilesystemError as exc:
        raise HTTPException(_FS_ERROR_STATUS.get(exc.code, 400), str(exc)) from exc
    rel = absolute.relative_to(repo.resolve()).as_posix()
    records = store.code_steps_for_project_path(project_id, rel, include_content=False) if rel.endswith('.lean') else []
    check = None
    if records:
        from .. import workspace_files
        session_id, fid = records[0]['session_id'], records[0].get('formalization_id')
        saved = proof_files.save(session_id, rel, absolute, request.content, formalization_id=fid,
            note='Edited in the project filesystem editor.')
        expected = store.current_proof_step(session_id, rel)
        try:
            sha = GitStore(proofs_root).commit_all(repo, f'edit {rel}', paths=[rel])
        except Exception as exc:
            workspace_files.provenance(saved.get('effect_id'), detail=str(exc))
            raise HTTPException(500, {'code':'content_saved_commit_failed',
                'message':'Content is saved in the database; Git provenance could not be recorded.', 'effect_id':saved.get('effect_id')}) from exc
        workspace_files.provenance(saved.get('effect_id'), sha)
        check = proof_files.check(session_id, rel, absolute, interface_check, formalization_id=fid, expected=expected)
    else:
        try:
            sha = fs_service.write_text_file(project, proofs_root, rel, request.content)
        except fs_service.FilesystemError as exc:
            raise HTTPException(_FS_ERROR_STATUS.get(exc.code, 400), str(exc)) from exc
        if rel.endswith('.lean'):
            result = interface_check(str(absolute))
            check = {'status':result.status, 'detail':result.detail}
    return {'path':rel, 'commit_sha':sha, 'check':check,
        'file_revision':workspace_commands.revision_for('p:'+project_id,rel)}


def _export_project_response(project: dict) -> Response:
    repo = project_service.project_repo_dir(project, _proofs_root())
    try:
        data = fs_service.export_zip(repo)
    except fs_service.FilesystemError as exc:
        raise HTTPException({'too_large':413,'changed':409,'bad_path':400}.get(exc.code,400),str(exc)) from exc
    from ..peripheral_views import disposition
    return Response(data, media_type='application/zip', headers={'Content-Disposition':disposition(project['slug']+'.zip')})


@router.get('/api/projects/{project_id}/export')
@peripheral_views.query('export_project', automatic=False, lifetime=86400)
@_guard_project(by_slug=False, write=False, metadata=True)
def export_project(project_id: str) -> Response:
    return _export_project_response(_require_project(project_id))


def _preview_github_import(project: dict, request: GithubImportPreviewRequest) -> dict:
    try:
        return github_import_service.preview_import(project=project, proofs_root=_proofs_root(),
            repository_url=request.repository_url, targets=_import_targets(request.targets))
    except (ImportPlanningError, GitHubSourceError) as exc:
        raise _import_error(exc) from exc


def _confirm_github_import(project: dict, request: GithubImportConfirmRequest) -> dict:
    try:
        return github_import_service.confirm_import(project=project, proofs_root=_proofs_root(),preview_id=request.preview_id)
    except (ImportPlanningError, GitHubSourceError) as exc:
        raise _import_error(exc) from exc


def _github_import_progress(project: dict, import_id: str) -> dict:
    result = store.github_import_progress(import_id)
    if result is None or result['project_id'] != project['id']:
        raise HTTPException(404,'GitHub import not found')
    return result


@router.post("/api/projects/{project_id}/github-imports/preview")
@workspace_commands.register('peripheral.preview_project_github_import')
def preview_project_github_import(project_id: str, request: GithubImportPreviewRequest) -> dict:
    return _preview_github_import(_require_project(project_id), request)


@router.post("/api/projects/{project_id}/github-imports", status_code=202)
@workspace_commands.register('peripheral.confirm_project_github_import')
@_guard_project(by_slug=False, write=True, metadata=False)
def confirm_project_github_import(project_id: str, request: GithubImportConfirmRequest) -> dict:
    return _confirm_github_import(_require_project(project_id), request)


@router.get("/api/projects/{project_id}/github-imports/{import_id}")
def get_project_github_import(project_id: str, import_id: str) -> dict:
    return _github_import_progress(_require_project(project_id), import_id)


@router.post("/api/projects/by-slug/{slug}/github-imports/preview")
@workspace_commands.register('peripheral.preview_project_github_import_by_slug')
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
    result = _preview_github_import(project, request)
    store.mark_project_overleaf_linked(project["id"])
    return result


@router.post("/api/projects/by-slug/{slug}/github-imports", status_code=202)
@workspace_commands.register('peripheral.confirm_project_github_import_by_slug')
@_guard_project(by_slug=True, write=True, metadata=False)
def confirm_project_github_import_by_slug(
    slug: str, request: GithubImportConfirmRequest
) -> dict:
    project = _require_project_by_slug(slug)
    result = _confirm_github_import(project, request)
    store.mark_project_overleaf_linked(project["id"])
    return result


@router.get("/api/projects/by-slug/{slug}/github-imports/{import_id}")
def get_project_github_import_by_slug(slug: str, import_id: str) -> dict:
    return _github_import_progress(_require_project_by_slug(slug), import_id)


@router.post('/api/projects/by-slug/{slug}/formalizations/sync', status_code=202)
@workspace_commands.register('project.sync_formalizations')
@_guard_project(by_slug=True, write=True, metadata=False, environment_write=False)
def sync_project_formalizations_by_slug(
    slug: str, request: FormalizationTargetSyncRequest
) -> dict:
    project = _require_project_by_slug(slug)
    # This updates target metadata and SQL import bindings only. It must still
    # reserve the project against namespace changes, but cannot queue inventory
    # refreshes behind Lean checks or dependency builds.
    result = github_import_service.sync_targets(
        project,
        _import_targets(request.targets),
        _proofs_root(),
    )
    store.mark_project_overleaf_linked(project["id"])
    return result


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


@router.put('/api/projects/{project_id}/git/remote', status_code=202)
@workspace_commands.register('project.remote')
@_guard_project(by_slug=False, write=True)
def set_project_remote(project_id: str, request: RemoteUpdate) -> dict:
    """Set the project's GitHub remote URL (D34). Stored on the project row; the
    token stays global in Settings. Returns the (token-free) remote."""
    return _set_remote_on(_require_project(project_id), request.remote_url)


@router.post('/api/projects/{project_id}/git/push', status_code=202)
@workspace_commands.register('project.push')
@_guard_project(by_slug=False, write=True)
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
    from ..queries.identity import project_identity
    return project_identity(_require_project_by_slug(slug))


@router.put('/api/projects/by-slug/{slug}/identity', status_code=202)
@workspace_commands.register('project.identity')
def update_project_identity_by_slug(slug: str, request: ProjectIdentityUpdate) -> dict:
    if request.mode not in {"display-only", "rename-namespace", "default-name"}:
        raise HTTPException(status_code=422, detail="mode must be 'display-only', 'rename-namespace', or 'default-name'")
    try:
        project = store.get_project_by_slug(slug)
    except ValueError:
        project = None
    if request.mode == "default-name":
        if project is None and not request.create_if_missing:
            raise HTTPException(status_code=404, detail="No Lea project exists for this document yet.")
        try:
            project = project_service.ensure_project(
                slug, _proofs_root(), overleaf_project_title=request.overleaf_project_title or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        project = store.mark_project_overleaf_linked(project["id"])
        return {"identity": project_service.project_identity(project)}
    if project is None:
        if not request.create_if_missing:
            raise HTTPException(status_code=404, detail="No Lea project exists for this document yet.")
        try:
            project = project_service.ensure_project(
                slug,
                _proofs_root(),
                title=request.project_name,
                namespace=request.namespace,
                name_source="custom",
            )
        except project_service.ProjectIdentityError as exc:
            raise _project_identity_error(exc)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        project = store.mark_project_overleaf_linked(project["id"])
        return {"identity": project_service.project_identity(project)}

    title = request.project_name.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Project name is required")

    if request.mode == "display-only":
        with project_coordination.mutation(project["id"]):
            project = store.get_project(project["id"])
            project_service.refresh_project_title_docs(project, _proofs_root(), title)
            updated = store.update_project(project["id"], title=title)
            updated = store.mark_project_overleaf_linked(updated["id"])
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
    result["project"] = store.mark_project_overleaf_linked(result["project"]["id"])
    return {
        "identity": project_service.project_identity(result["project"]),
        "migration": result["migration"],
    }


@router.post('/api/projects/by-slug/{slug}/identity/default-name', status_code=202)
@workspace_commands.register('project.default_name')
@_guard_project(by_slug=True, write=True, environment_write=False)
def initialize_overleaf_default_name(slug: str, request: OverleafDefaultNameRequest) -> dict:
    try:
        previous = store.get_project_by_slug(slug)
        project = project_service.adopt_overleaf_project_name(slug, _proofs_root(), request.overleaf_project_title)
    except project_service.ProjectIdentityError as exc:
        raise _project_identity_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    changed = previous is not None and (previous["title"], previous["name_source"]) != (project["title"], project["name_source"])
    return {"identity": project_service.project_identity(project), "changed": changed}


@router.get("/api/projects/by-slug/{slug}/share")
@peripheral_views.query('get_share_status_by_slug')
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
@peripheral_views.query('get_blueprint_by_slug')
@_guard_project(by_slug=True, write=False, metadata=True)
def get_blueprint_by_slug(slug: str) -> dict:
    return _blueprint_payload(_require_project_by_slug(slug))


@router.get("/api/projects/by-slug/{slug}/graph")
@peripheral_views.query('get_graph_by_slug')
@_guard_project(by_slug=True, write=False, metadata=True)
def get_graph_by_slug(slug: str) -> dict:
    return graph_service.build_graph(_require_project_by_slug(slug), _proofs_root())


@router.post('/api/projects/by-slug/{slug}/blueprint/generate', status_code=202)
@workspace_commands.register('project.generate_blueprint_slug')
@_guard_project(by_slug=True, write=True, metadata=True)
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
    return {
        "project_id": project["id"],
        "slug": project["slug"],
        "artifacts": store.list_artifacts_for_scope(project["id"]),
    }


# Cap on the file content echoed back by target-status: recorded proofs are a
# few KB; anything bigger is truncated for display (verdict fields are computed
# from the full content either way).
_TARGET_STATUS_CONTENT_CAP = 64 * 1024


def _shared_artifact(project_id: str, proof_path: str, content: str, owners: list[dict]) -> bool:
    from ..artifacts import has_unrelated_declarations
    from ..db import connect

    # Explicitly linked targets remain separate owners even when one happens
    # to be declared inside another definition's namespace.
    with connect() as conn:
        linked = conn.execute(
            """select count(distinct f.id) from formalization_files ff
               join formalizations f on f.id = ff.formalization_id
               where f.project_id = ? and ff.path = ?""",
            (project_id, proof_path),
        ).fetchone()[0]
    owner_name = owners[0]["declaration_name"] if len(owners) == 1 else None
    return len(owners) > 1 or linked > 1 or has_unrelated_declarations(content, owner_name)


@router.get("/api/projects/by-slug/{slug}/target-status")
@database_query('target_status')
@_guard_project(by_slug=True, write=False, metadata=True)
@read_projection
def project_target_status_by_slug(slug: str, declarations: str = "", formalization_ids: str = "") -> dict:
    """Ledger-side target evidence (PLAN-system-hardening 4.4): for each named
    declaration, what the adapter's own records say — the artifact row, whether
    the recorded file exists and still holds the declaration, whether it leans
    on sorry/admit, and the newest recorded check verdict (agent run, manual
    edit, or cascade re-check alike). This is one of the two sources the
    companion's ledger status engine merges; the other is its own job overlay.
    `content` rides along (capped) purely for display extraction client-side —
    the verdict fields here are authoritative."""
    from .. import artifacts as artifacts_service
    from .. import formalizations as formalization_service
    from ..target_completion import target_declaration

    project = _require_project_by_slug(slug)
    _ensure_artifacts_backfilled(project)
    from ..formalize_batch_reports import latest_target_outcomes
    from ..db import connect
    batch_outcomes = latest_target_outcomes(project["id"])
    names = [name for name in (part.strip() for part in declarations.split(",")) if name]
    requested_ids = [fid for fid in (part.strip() for part in formalization_ids.split(",")) if fid]
    repo = project_service.project_repo_dir(project, _proofs_root())
    rows = {row["declaration_name"]: row for row in store.list_artifacts_for_scope(project["id"])}
    formalizations_by_name = {}
    for fid in requested_ids:
        formalization = formalization_service.get(fid)
        if formalization and formalization.get("project_id") == project["id"]:
            identity_label = formalization.get("declaration_name") or formalization.get("display_title") or fid
            formalizations_by_name[identity_label] = formalization
            if identity_label not in names:
                names.append(identity_label)

    targets = []
    for name in names:
        last_batch_outcome = batch_outcomes.get(("theorem", name)) or batch_outcomes.get(("definition", name))
        row = rows.get(name)
        formalization = formalizations_by_name.get(name)
        if not formalization and row and row.get("formalization_id"):
            formalization = formalization_service.get(row["formalization_id"])
        if not formalization:
            identity = store.find_formalization_by_declaration(
                project_id=project["id"], loose_session_id=None,
                declaration_name=name,
            )
            if identity:
                formalization = formalization_service.get(identity["id"])
        if not row and not formalization:
            targets.append({"declaration_name": name, "recorded": False,
                            "validity_status": "unavailable", "validity_reason": "No formalization identity is available.",
                            "completion_run_id": None, "declaration_kind": None, "check_current": False,
                            "last_batch_outcome": last_batch_outcome})
            continue
        steps = store.current_code_steps_for_formalization(formalization["id"]) if formalization else []
        proof_path = row["path"] if row else ((formalization or {}).get("primary_path") or (steps[0]["path"] if steps else None))
        proof_path = ((formalization or {}).get('review') or {}).get('candidate_path') or proof_path
        if formalization and formalization.get("completion_run_id") and not proof_path:
            from ..db import connect
            with connect() as conn:
                partial = conn.execute(
                    """select path from timeline where kind = 'code' and run_id = ?
                       and lower(path) like '%.lean' order by id desc limit 1""",
                    (formalization["completion_run_id"],),
                ).fetchone()
            proof_path = partial["path"] if partial else proof_path
        absolute = repo / proof_path if proof_path else None
        exists = bool(absolute and absolute.is_file())
        content = ""
        if exists:
            try:
                content = absolute.read_bytes().decode('utf-8')
            except OSError:
                exists = False
        shared_file = exists and _shared_artifact(
            project["id"], proof_path, content,
            [candidate for candidate in rows.values() if candidate["path"] == proof_path],
        )
        check = store.latest_check_for_project_path(project["id"], proof_path) if proof_path else None
        current_step = None
        editing_session_id = None
        if formalization:
            current_step = next((step for step in steps if step.get("path") == proof_path), None)
            associated_sessions = store.session_ids_for_formalization(formalization["id"])
            current_session_id = current_step.get("session_id") if current_step else None
            editing_session_id = (
                current_session_id
                if current_session_id in associated_sessions
                else (associated_sessions[0] if associated_sessions else None)
            )
        current_source_hash = (formalization or {}).get("source_hash")
        if formalization:
            origin_label = (formalization.get("origin_key") or "").rsplit(":", 1)[-1]
            last_batch_outcome = batch_outcomes.get((formalization["kind"], origin_label)) or last_batch_outcome
            if last_batch_outcome:
                with connect() as conn:
                    outcome_run = conn.execute("select created_at from runs where id=?",
                        (last_batch_outcome.get("runId"),)).fetchone()
                    # A retry may finish while independent batch items are
                    # still running, before this report's final timestamp.
                    since = outcome_run["created_at"] if outcome_run else last_batch_outcome["started_at"]
                    newer = conn.execute("select id from runs where focus_formalization_id=? and created_at>? and id!=? limit 1",
                        (formalization["id"], since, last_batch_outcome.get("runId") or "")).fetchone()
                if newer:
                    last_batch_outcome = None
        artifact_source_hash = (row or {}).get("source_hash")
        checked_sha = (check or {}).get("checked_sha256")
        content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest() if exists else None
        check_current = bool(checked_sha and checked_sha == content_sha and check.get("check_status") == "ok" and check.get("check_current"))
        declared = target_declaration(content, name, (formalization or {}).get("kind") or "theorem") if exists else None
        validity = (formalization or {}).get("validity_status") or "unavailable"
        validity_reason = (formalization or {}).get("validity_reason") or "No authoritative target verdict is available."
        if validity in {"proved", "defined"} and not check_current:
            validity, validity_reason = "unchecked", "The current file contents do not match a passing Lean check."
        if validity in {"proved", "defined"} and not declared:
            validity, validity_reason = "unchecked", "The current file does not declare the requested target."
        targets.append({
            "declaration_name": name,
            "recorded": bool(row),
            "path": proof_path,
            "module_name": (row or {}).get("module_name") if row and row["path"] == proof_path else None,
            "kind": (row or {}).get("kind"),
            "exists": exists,
            "shared_file": shared_file,
            "declaration_present": bool(declared),
            "has_sorry": artifacts_service.declaration_contains_sorry(content, name) if exists else None,
            "check_status": check["check_status"] if check else None,
            "check_detail": check["check_detail"] if check else None,
            "check_author": check["author"] if check else None,
            "check_created_at": check["created_at"] if check else None,
            "artifact_updated_at": current_step["created_at"] if current_step else None,
            "formalization_id": (formalization or {}).get("id") or (row or {}).get("formalization_id"),
            "validity_status": validity,
            "validity_reason": validity_reason,
            "review": (formalization or {}).get('review'),
            'reformalization': (formalization or {}).get('reformalization'),
            "certification": (formalization or {}).get('certification'),
            "completion_run_id": (formalization or {}).get("completion_run_id"),
            "declaration_kind": declared.kind if declared else (formalization or {}).get("declaration_kind"),
            "check_current": check_current,
            "last_batch_outcome": last_batch_outcome,
            # The session that wrote the current artifact revision is the
            # durable editing context. Companion jobs are only a projection
            # and may not exist for GitHub-imported formalizations.
            "session_id": editing_session_id,
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


@router.post('/api/projects/by-slug/{slug}/artifacts/retire', status_code=202)
@workspace_commands.register('project.retire')
@_guard_project(by_slug=True, write=True, metadata=False)
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
    # A definition may include its own namespace helpers. Separate target
    # owners or unrelated declarations still prevent a whole-file retry.
    owners = [
        row for row in store.list_artifacts_for_scope(project["id"])
        if row["path"] == rel
    ]
    try:
        shared = _shared_artifact(project["id"], rel, absolute.read_text(), owners)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not inspect {rel}: {exc}") from exc
    if shared:
        raise HTTPException(
            status_code=409,
            detail="This Lean file contains multiple declarations; it cannot be retired for one formalization.",
        )
    # A code step can be SQL-owned while its materialized project file is still
    # untracked (for example after importing legacy state or a tool that wrote
    # outside the normal FileChanged commit path). Commit the exact bytes at this
    # path before deleting them. This is a no-op for an already tracked, clean
    # artifact, is path-scoped for concurrent project writers, and guarantees the
    # following deletion has a Git object to stage and a revision to restore from.
    gs = GitStore(repo.parent)
    try:
        gs.commit_all(repo, f"record {rel} before retry retirement", paths=[rel])
        from ..workspace_effects import install_from_snapshot
        install_from_snapshot(absolute,None)
        sha = gs.commit_all(repo, f"retire {rel} for retry", paths=[rel])
    except GitStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"retire_commit": sha, "path": rel}


@router.post('/api/projects/by-slug/{slug}/artifacts/restore', status_code=202)
@workspace_commands.register('project.restore')
@_guard_project(by_slug=True, write=True, metadata=False)
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
    from ..workspace_effects import install_from_snapshot
    install_from_snapshot(absolute,content)
    try:
        sha = GitStore(repo.parent).commit_all(
            repo, f"restore {rel} after unverified retry", paths=[rel],
        )
    except GitStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"restored": True, "commit": sha, "path": rel}


@router.get("/api/projects/by-slug/{slug}/export")
@peripheral_views.query('export_project_by_slug',automatic=False,lifetime=86400)
@_guard_project(by_slug=True, write=False, metadata=True)
def export_project_by_slug(slug: str) -> Response:
    return _export_project_response(_require_project_by_slug(slug))


@router.put('/api/projects/by-slug/{slug}/git/remote', status_code=202)
@workspace_commands.register('project.remote_slug')
@_guard_project(by_slug=True, write=True)
def set_project_remote_by_slug(slug: str, request: RemoteUpdate) -> dict:
    return _set_remote_on(_require_project_by_slug(slug), request.remote_url)


@router.post('/api/projects/by-slug/{slug}/git/push', status_code=202)
@workspace_commands.register('project.push_slug')
@_guard_project(by_slug=True, write=True)
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
def mirror_overleaf_tex(slug: str, request: MirrorRequest) -> dict:
    """Publish a SQL archive; only an accepted worker binds it to workspace files."""
    from .. import db
    from uuid import uuid4
    incoming={workspace_commands.normalize_path(f.path):f.content for f in request.files}
    if len(incoming)>10000 or any(len(c.encode())>4*1024*1024 for c in incoming.values()) or sum(len(p.encode())+len(c.encode()) for p,c in incoming.items())>32*1024*1024:
        raise HTTPException(413,'Source archive exceeds its file/input bounds')
    with db.write() as conn:
        project=store.get_project_by_slug(slug) or project_service.ensure_project(slug,None,overleaf_project_title=request.overleaf_project_title)
        previous=conn.execute('select files_json from source_archives where project_id=? order by created_at desc limit 1',(project['id'],)).fetchone()
        import json
        files=json.loads(previous[0]) if previous and request.mode=='upsert' else {}
        files.update(incoming)
        if sum(len(p.encode())+len(c.encode()) for p,c in files.items())>32*1024*1024:raise HTTPException(413,'Source archive exceeds 32 MiB')
        archive=uuid4().hex
        conn.execute('insert into source_archives(id,project_id,files_json,mode,created_at) values(?,?,?,?,?)',(archive,project['id'],json.dumps(files),request.mode,db.utc_now()))
        store.mark_project_overleaf_linked(project['id'])
    hashes={p:hashlib.sha256(c.encode()).hexdigest() for p,c in files.items()}
    revision=hashlib.sha256('\0'.join(p+'\0'+h for p,h in sorted(hashes.items())).encode()).hexdigest()
    return {'project_id':project['id'],'slug':slug,'identity':project_service.project_identity(project),
            'source_archive_id':archive,'mirrorRevision':revision,'mirroredFiles':hashes,'changed':True,
            'materialization':'deferred_until_worker_binding','summary':{'published':len(files)}}


@router.post('/api/projects/{project_id}/export',status_code=202)
@workspace_commands.register('project.export')
def prepare_project_export(project_id:str):
    return peripheral_views.capture('export_project',{'project_id':project_id})


@router.post('/api/projects/by-slug/{slug}/export',status_code=202)
@workspace_commands.register('project.export_slug')
def prepare_project_export_by_slug(slug:str):
    return peripheral_views.capture('export_project_by_slug',{'slug':slug})


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


class AttachmentRequest(BaseModel):
    filename: str
    mime: str | None = None
    upload_blob_id: str


@workspace_commands.register('project.upload')
@_guard_project(by_slug=False, write=True, metadata=False)
def save_uploaded_file(project_id: str, request: AttachmentRequest) -> dict:
    import base64
    project = _require_project(project_id)
    try:
        return uploads.save_upload(project, _proofs_root(), request.filename,
            base64.b64decode(store.blob_content(request.upload_blob_id)), mime=request.mime)
    except uploads.UploadError as exc:
        raise HTTPException(status_code=_UPLOAD_ERROR_STATUS.get(exc.code,400),detail=str(exc)) from exc


@router.post("/api/projects/{project_id}/files", status_code=202)
async def upload_file(project_id: str, file: UploadFile = File(...), http_request: Request = None) -> dict:
    import base64
    from .. import db
    _require_project(project_id)
    try:
        data = await _read_capped(file, uploads.MAX_UPLOAD_BYTES)
    except uploads.UploadError as exc:
        raise HTTPException(status_code=_UPLOAD_ERROR_STATUS.get(exc.code, 400), detail=str(exc))
    with db.write() as conn:
        blob = store._put_blob(conn, base64.b64encode(data).decode())
        try:
            return workspace_commands.admit('project.upload', {'project_id': project_id,
                'request': {'filename': file.filename or 'file', 'mime': file.content_type, 'upload_blob_id': blob}},
                idempotency_key=http_request.headers.get('Idempotency-Key') if http_request else None,connection=conn)
        except workspace_commands.commands.QueueError as exc:
            raise HTTPException(exc.status, detail={'error':exc.code}) from exc


@peripheral_views.query('download_file')
def _capture_download_file(project_id: str, file_id: str) -> FileResponse:
    project = _require_project(project_id)
    row = store.get_project_file(file_id)
    if row is None or row["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="File not found")
    path = uploads.file_disk_path(project, _proofs_root(), row)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File bytes are missing")
    return FileResponse(path, media_type=row.get("mime") or "application/octet-stream", filename=row["filename"])


@router.get('/api/projects/{project_id}/files/{file_id}')
def download_file(project_id:str,file_id:str) -> Response:
    import base64
    from .. import db
    row=store.get_project_file(file_id)
    if not row or row['project_id']!=project_id:raise HTTPException(404,'File not found')
    with db.connect() as conn:binding=conn.execute('select * from workspace_files where scope=? and path=?',('p:'+project_id,row['stored_path'])).fetchone()
    if not binding:return peripheral_views.get('download_file',{'project_id':project_id,'file_id':file_id})
    if binding['materialization']=='deleted':raise HTTPException(404,'File was deleted')
    raw=store.blob_content(binding['blob_id'])
    content=base64.b64decode(raw) if binding['encoding']=='base64' else raw.encode()
    return Response(content,media_type=row.get('mime') or 'application/octet-stream',headers={'Content-Disposition':peripheral_views.disposition(row['filename'])})


@router.delete('/api/projects/{project_id}/files/{file_id}', status_code=202)
@workspace_commands.register('project.delete_file')
@_guard_project(by_slug=False, write=True, metadata=False)
def delete_file(project_id: str, file_id: str) -> dict:
    project = _require_project(project_id)
    row = store.get_project_file(file_id)
    if row is None or row["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="File not found")
    uploads.delete_file(project, _proofs_root(), row)
    return {"deleted": True, "id": file_id}


@router.delete('/api/projects/{project_id}', status_code=202)
@workspace_commands.register('project.delete')
def delete_project(project_id: str) -> dict:
    if store.project_has_active_import(project_id):
        raise HTTPException(status_code=409, detail={
            "error": "project_busy",
            "message": "Wait for the active GitHub import before deleting this project.",
        })
    if not project_service.delete_project(project_id, _proofs_root()):
        raise HTTPException(status_code=404, detail="Project not found")
    return {"deleted": True, "id": project_id}
