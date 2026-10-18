"""Public SQL/admission contract. Private operations are in app.routes.projects."""
from __future__ import annotations
from ..api_contracts import sql_query
from .. import peripheral_views
from .. import workspace_commands
import hashlib
from .. import workspace_commands
from fastapi import APIRouter, File, HTTPException, UploadFile, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from .. import project_intake as project_service
from .. import store
from .. import upload_intake as uploads
from ..alignment_schemas import SourceBundle
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
    source: str = 'overleaf'
    overleaf_project_title: str | None = None
    mode: str = 'reconcile'

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

def _project_identity_error(exc: project_service.ProjectIdentityError) -> HTTPException:
    detail = {'error': exc.code, 'message': str(exc), **exc.detail}
    return HTTPException(status_code=exc.status, detail=detail)

@router.get('/api/projects')
@sql_query('projects')
def list_projects() -> dict:
    raise RuntimeError('A SQL query cannot execute')

@router.post('/api/projects', status_code=202)
@workspace_commands.intake('project.create')
def create_project(request: ProjectCreate) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/projects/{project_id}')
@sql_query('project')
def get_project(project_id: str) -> dict:
    raise RuntimeError('A SQL query cannot execute')

@router.put('/api/projects/{project_id}', status_code=202)
@workspace_commands.intake('project.update')
def update_project(project_id: str, request: ProjectUpdate) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.post('/api/projects/namespace-preview')
def namespace_preview(request: NamespacePreviewRequest) -> dict:
    try:
        return project_service.namespace_preview(request.project_name, namespace=request.namespace, exclude_project_id=request.exclude_project_id)
    except project_service.ProjectIdentityError as exc:
        raise _project_identity_error(exc)

@router.post('/api/projects/{project_id}/sessions', status_code=201)
def create_session_in_project(project_id: str, request: SessionCreate) -> dict:
    """Create a session that lives inside the project (D23). The session is tagged
    with `project_id`; the run it later starts resolves the shared project repo +
    namespace from that tag (the bridge does this, D24/D25). Proofs land in
    `proofs/Lea/<Project>/` from turn 1, able to import sibling lemmas."""
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail='Project not found')
    title = (request.title or '').strip() or 'Untitled theorem'
    return store.create_session(title, project_id=project_id)

@router.get('/api/projects/{project_id}/instructions')
@peripheral_views.view('get_instructions')
def get_instructions(project_id: str) -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.put('/api/projects/{project_id}/instructions', status_code=202)
@workspace_commands.intake('project.instructions')
def put_instructions(project_id: str, request: DocUpdate) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/projects/{project_id}/memory')
@peripheral_views.view('get_memory')
def get_memory(project_id: str) -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.put('/api/projects/{project_id}/memory', status_code=202)
@workspace_commands.intake('project.memory')
def put_memory(project_id: str, request: DocUpdate) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/projects/{project_id}/blueprint')
@peripheral_views.view('get_blueprint')
def get_blueprint(project_id: str) -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.put('/api/projects/{project_id}/blueprint', status_code=202)
@workspace_commands.intake('project.blueprint')
def put_blueprint(project_id: str, request: DocUpdate) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/projects/{project_id}/graph')
@peripheral_views.view('get_graph')
def get_graph(project_id: str) -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.post('/api/projects/{project_id}/blueprint/generate', status_code=202)
@workspace_commands.intake('project.generate_blueprint')
def generate_blueprint(project_id: str) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/projects/{project_id}/tree')
def get_tree(project_id: str) -> dict:
    _require_project(project_id)
    from ..queries import files
    try:
        return files.tree(project_id)
    except files.ProjectionUnavailable as exc:
        raise HTTPException(409, detail={'error': 'projection_unavailable', 'reason': exc.reason}) from exc

@router.get('/api/projects/{project_id}/file')
def read_project_file(project_id: str, path: str, observation_id: str | None=None) -> dict:
    _require_project(project_id)
    from ..queries import files
    from .workspace import file as sql_file
    try:
        if observation_id is not None:
            return {**files.content(project_id, path, observation_id), 'file_revision': workspace_commands.revision_for('p:' + project_id, path)}
        value = sql_file(path, project_id=project_id)
        if value['content'] is not None:
            return {**value, 'lean': path.endswith('.lean')}
        return {**files.content(project_id, path), 'file_revision': value['file_revision']}
    except files.ProjectionUnavailable as exc:
        raise HTTPException({'not_found': 404, 'bad_path': 400, 'binary': 415, 'file_too_large': 413}.get(exc.reason, 409), detail={'error': 'projection_unavailable', 'reason': exc.reason}) from exc
    except HTTPException as exc:
        if exc.status_code == 422:
            raise HTTPException(400, exc.detail) from exc
        raise

@router.put('/api/projects/{project_id}/file', status_code=202)
@workspace_commands.intake('project.save')
def write_project_file(project_id: str, request: FilePut) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/projects/{project_id}/export')
@peripheral_views.view('export_project', automatic=False, lifetime=86400)
def export_project(project_id: str) -> Response:
    raise RuntimeError('A SQL view cannot execute')

def _github_import_progress(project: dict, import_id: str) -> dict:
    result = store.github_import_progress(import_id)
    if result is None or result['project_id'] != project['id']:
        raise HTTPException(404, 'GitHub import not found')
    return result

@router.post('/api/projects/{project_id}/github-imports/preview')
@workspace_commands.intake('peripheral.preview_project_github_import')
def preview_project_github_import(project_id: str, request: GithubImportPreviewRequest) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.post('/api/projects/{project_id}/github-imports', status_code=202)
@workspace_commands.intake('peripheral.confirm_project_github_import')
def confirm_project_github_import(project_id: str, request: GithubImportConfirmRequest) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/projects/{project_id}/github-imports/{import_id}')
def get_project_github_import(project_id: str, import_id: str) -> dict:
    return _github_import_progress(_require_project(project_id), import_id)

@router.post('/api/projects/by-slug/{slug}/github-imports/preview')
@workspace_commands.intake('peripheral.preview_project_github_import_by_slug')
def preview_project_github_import_by_slug(slug: str, request: GithubImportPreviewRequest) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.post('/api/projects/by-slug/{slug}/github-imports', status_code=202)
@workspace_commands.intake('peripheral.confirm_project_github_import_by_slug')
def confirm_project_github_import_by_slug(slug: str, request: GithubImportConfirmRequest) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/projects/by-slug/{slug}/github-imports/{import_id}')
def get_project_github_import_by_slug(slug: str, import_id: str) -> dict:
    return _github_import_progress(_require_project_by_slug(slug), import_id)

@router.post('/api/projects/by-slug/{slug}/formalizations/sync', status_code=202)
@workspace_commands.intake('project.sync_formalizations')
def sync_project_formalizations_by_slug(slug: str, request: FormalizationTargetSyncRequest) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.put('/api/projects/{project_id}/git/remote', status_code=202)
@workspace_commands.intake('project.remote')
def set_project_remote(project_id: str, request: RemoteUpdate) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.post('/api/projects/{project_id}/git/push', status_code=202)
@workspace_commands.intake('project.push')
def push_project(project_id: str) -> dict:
    raise RuntimeError('An intake contract cannot execute')

def _require_project_by_slug(slug: str) -> dict:
    try:
        project = store.get_project_by_slug(slug)
    except ValueError:
        project = None
    if project is None:
        raise HTTPException(status_code=404, detail='No Lea project exists for this document yet.')
    return project

@router.get('/api/projects/by-slug/{slug}/identity')
def get_project_identity_by_slug(slug: str) -> dict:
    from ..queries.identity import project_identity
    return project_identity(_require_project_by_slug(slug))

@router.put('/api/projects/by-slug/{slug}/identity', status_code=202)
@workspace_commands.intake('project.identity')
def update_project_identity_by_slug(slug: str, request: ProjectIdentityUpdate) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.post('/api/projects/by-slug/{slug}/identity/default-name', status_code=202)
@workspace_commands.intake('project.default_name')
def initialize_overleaf_default_name(slug: str, request: OverleafDefaultNameRequest) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/projects/by-slug/{slug}/share')
@peripheral_views.view('get_share_status_by_slug')
def get_share_status_by_slug(slug: str) -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.get('/api/projects/by-slug/{slug}/blueprint')
@peripheral_views.view('get_blueprint_by_slug')
def get_blueprint_by_slug(slug: str) -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.get('/api/projects/by-slug/{slug}/graph')
@peripheral_views.view('get_graph_by_slug')
def get_graph_by_slug(slug: str) -> dict:
    raise RuntimeError('A SQL view cannot execute')

@router.post('/api/projects/by-slug/{slug}/blueprint/generate', status_code=202)
@workspace_commands.intake('project.generate_blueprint_slug')
def generate_blueprint_by_slug(slug: str) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/projects/by-slug/{slug}/artifacts')
def project_artifacts_by_slug(slug: str) -> dict:
    """Structured artifact index (PLAN-system-hardening 4.1): which declaration
    lives in which file, as recorded by the run finalizer. The Overleaf
    companion reads this instead of reverse-engineering artifacts from
    registry-markdown diffs. Like every by-slug route, never creates a
    project — an unknown slug is a 404 ("nothing recorded yet")."""
    project = _require_project_by_slug(slug)
    return {'project_id': project['id'], 'slug': project['slug'], 'artifacts': store.list_artifacts_for_scope(project['id'])}

@router.get('/api/projects/by-slug/{slug}/target-status')
@sql_query('target_status')
def project_target_status_by_slug(slug: str, declarations: str='', formalization_ids: str='') -> dict:
    raise RuntimeError('A SQL query cannot execute')

class ArtifactRetireRequest(BaseModel):
    path: str

class ArtifactRestoreRequest(BaseModel):
    path: str
    retire_commit: str

@router.post('/api/projects/by-slug/{slug}/artifacts/retire', status_code=202)
@workspace_commands.intake('project.retire')
def retire_project_artifact_by_slug(slug: str, request: ArtifactRetireRequest) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.post('/api/projects/by-slug/{slug}/artifacts/restore', status_code=202)
@workspace_commands.intake('project.restore')
def restore_project_artifact_by_slug(slug: str, request: ArtifactRestoreRequest) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.get('/api/projects/by-slug/{slug}/export')
@peripheral_views.view('export_project_by_slug', automatic=False, lifetime=86400)
def export_project_by_slug(slug: str) -> Response:
    raise RuntimeError('A SQL view cannot execute')

@router.put('/api/projects/by-slug/{slug}/git/remote', status_code=202)
@workspace_commands.intake('project.remote_slug')
def set_project_remote_by_slug(slug: str, request: RemoteUpdate) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.post('/api/projects/by-slug/{slug}/git/push', status_code=202)
@workspace_commands.intake('project.push_slug')
def push_project_by_slug(slug: str) -> dict:
    raise RuntimeError('An intake contract cannot execute')
_UPLOAD_ERROR_STATUS = {'too_large': 413, 'unsupported': 415}

def _require_project(project_id: str) -> dict:
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail='Project not found')
    return project

@router.get('/api/projects/{project_id}/files')
def list_files(project_id: str) -> dict:
    _require_project(project_id)
    return {'files': store.list_project_files(project_id)}

@router.post('/api/projects/by-slug/{slug}/mirror')
def mirror_overleaf_tex(slug: str, request: MirrorRequest) -> dict:
    """Publish a SQL archive; only an accepted worker binds it to workspace files."""
    from .. import db
    from uuid import uuid4
    incoming = {workspace_commands.normalize_path(f.path): f.content for f in request.files}
    if len(incoming) > 10000 or any((len(c.encode()) > 4 * 1024 * 1024 for c in incoming.values())) or sum((len(p.encode()) + len(c.encode()) for p, c in incoming.items())) > 32 * 1024 * 1024:
        raise HTTPException(413, 'Source archive exceeds its file/input bounds')
    with db.write() as conn:
        project = store.get_project_by_slug(slug) or project_service.ensure_project(slug, None, overleaf_project_title=request.overleaf_project_title)
        previous = conn.execute('select files_json from source_archives where project_id=? order by created_at desc limit 1', (project['id'],)).fetchone()
        import json
        files = json.loads(previous[0]) if previous and request.mode == 'upsert' else {}
        files.update(incoming)
        if sum((len(p.encode()) + len(c.encode()) for p, c in files.items())) > 32 * 1024 * 1024:
            raise HTTPException(413, 'Source archive exceeds 32 MiB')
        archive = uuid4().hex
        conn.execute('insert into source_archives(id,project_id,files_json,mode,created_at) values(?,?,?,?,?)', (archive, project['id'], json.dumps(files), request.mode, db.utc_now()))
        store.mark_project_overleaf_linked(project['id'])
    hashes = {p: hashlib.sha256(c.encode()).hexdigest() for p, c in files.items()}
    revision = hashlib.sha256('\x00'.join((p + '\x00' + h for p, h in sorted(hashes.items()))).encode()).hexdigest()
    return {'project_id': project['id'], 'slug': slug, 'identity': project_service.project_identity(project), 'source_archive_id': archive, 'mirrorRevision': revision, 'mirroredFiles': hashes, 'changed': True, 'materialization': 'deferred_until_worker_binding', 'summary': {'published': len(files)}}

@router.post('/api/projects/{project_id}/export', status_code=202)
@workspace_commands.intake('project.export')
def prepare_project_export(project_id: str):
    raise RuntimeError('An intake contract cannot execute')

@router.post('/api/projects/by-slug/{slug}/export', status_code=202)
@workspace_commands.intake('project.export_slug')
def prepare_project_export_by_slug(slug: str):
    raise RuntimeError('An intake contract cannot execute')

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
            raise uploads.UploadError(f'File is too large; the cap is {cap // (1024 * 1024)} MB.', code='too_large')
        chunks.append(chunk)
    return b''.join(chunks)

class AttachmentRequest(BaseModel):
    filename: str
    mime: str | None = None
    upload_blob_id: str

@workspace_commands.intake('project.upload')
def save_uploaded_file(project_id: str, request: AttachmentRequest) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.post('/api/projects/{project_id}/files', status_code=202)
async def upload_file(project_id: str, file: UploadFile=File(...), http_request: Request=None) -> dict:
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
            return workspace_commands.admit('project.upload', {'project_id': project_id, 'request': {'filename': file.filename or 'file', 'mime': file.content_type, 'upload_blob_id': blob}}, idempotency_key=http_request.headers.get('Idempotency-Key') if http_request else None, connection=conn)
        except workspace_commands.commands.QueueError as exc:
            raise HTTPException(exc.status, detail={'error': exc.code}) from exc

@router.get('/api/projects/{project_id}/files/{file_id}')
def download_file(project_id: str, file_id: str) -> Response:
    import base64
    from .. import db
    row = store.get_project_file(file_id)
    if not row or row['project_id'] != project_id:
        raise HTTPException(404, 'File not found')
    with db.connect() as conn:
        binding = conn.execute('select * from workspace_files where scope=? and path=?', ('p:' + project_id, row['stored_path'])).fetchone()
    if not binding:
        return peripheral_views.get('download_file', {'project_id': project_id, 'file_id': file_id})
    if binding['materialization'] == 'deleted':
        raise HTTPException(404, 'File was deleted')
    raw = store.blob_content(binding['blob_id'])
    content = base64.b64decode(raw) if binding['encoding'] == 'base64' else raw.encode()
    return Response(content, media_type=row.get('mime') or 'application/octet-stream', headers={'Content-Disposition': peripheral_views.disposition(row['filename'])})

@router.delete('/api/projects/{project_id}/files/{file_id}', status_code=202)
@workspace_commands.intake('project.delete_file')
def delete_file(project_id: str, file_id: str) -> dict:
    raise RuntimeError('An intake contract cannot execute')

@router.delete('/api/projects/{project_id}', status_code=202)
@workspace_commands.intake('project.delete')
def delete_project(project_id: str) -> dict:
    raise RuntimeError('An intake contract cannot execute')
