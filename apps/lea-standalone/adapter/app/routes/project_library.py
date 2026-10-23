"""Search and coordinate mathematics within an existing project."""
from ..core_query_dispatch import database_query
from .. import workspace_commands
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from .. import project_library as library, project_dependencies as dependencies, store
from ..read_projections import read_projection

router = APIRouter()
PREFIX = "/api/projects/by-slug/{slug}"


def project(slug):
    value = store.get_project_by_slug(slug)
    if not value:
        raise HTTPException(404, "Project not found")
    return value


def dependency(slug, dependency_id, *, validate=True):
    value = dependencies.get(dependency_id, validate=validate)
    if not value or value["project_id"] != project(slug)["id"]:
        raise HTTPException(404, "Dependency not found in this project")
    return value


class EnsureRequest(BaseModel):
    specification: dict
    run_id: str | None = None
    requester_id: str | None = None
    mode: str = "deferred"


class WorkerRequest(BaseModel):
    initiating_run_id: str | None = None
    model: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)


@router.get(PREFIX + "/library")
@database_query('library')
def search(slug: str, q: str = "", limit: int = 20, offset: int = 0, validate: bool = True):
    if not 1 <= limit <= 100 or offset < 0:
        raise HTTPException(422, "Invalid page")
    return library.search(project(slug), q, limit=limit, offset=offset, validate=validate)


@router.get(PREFIX + "/dependencies")
@database_query('dependencies')
@read_projection
def list_dependencies(slug: str, requester_id: str | None = None):
    # Inventory reads project existing evidence without entering the compiler
    # environment. Explicit dependency operations still prepare/validate it.
    return {"dependencies": dependencies.list_dependencies(project(slug)["id"], requester_id, validate=False)}


@router.get(PREFIX + "/library/declarations/{name:path}")
@database_query('declaration')
def inspect_declaration(slug: str, name: str):
    p = project(slug)
    match = next((d for d in library.refresh(p) if name in {d["full_name"], d["user_name"]}), None)
    if not match:
        raise HTTPException(404, "Declaration not found")
    entries = library.validate_module(p, match["path"])
    return next((d for d in entries if name in {d["full_name"], d["user_name"]}), match)


@router.get(PREFIX + "/dependencies/{dependency_id}")
@database_query('dependency')
def get_dependency(slug: str, dependency_id: str):
    return dependency(slug, dependency_id, validate=False)


@router.post(PREFIX + '/dependencies/ensure', status_code=202)
@workspace_commands.register('dependency.ensure')
def ensure(slug: str, request: EnsureRequest):
    try:
        return dependencies.ensure(project(slug), request.specification, run_id=request.run_id,
                                   requester_id=request.requester_id, mode=request.mode)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post(PREFIX + '/dependencies/{dependency_id}/retry', status_code=202)
@workspace_commands.register('dependency.retry')
def retry(slug: str, dependency_id: str):
    dependency(slug, dependency_id)
    try:
        return dependencies.retry(dependency_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post(PREFIX + '/dependencies/{dependency_id}/run', status_code=202)
@workspace_commands.register('dependency.run')
def start_worker(slug: str, dependency_id: str, request: WorkerRequest):
    from .runs import create_run, RunRequest
    value = dependency(slug, dependency_id)
    if value.get("ready"):
        return {"ready": True, "dependency": value}
    p = project(slug)
    return create_run(RunRequest(message=dependencies.worker_message(value),
        project_slug=slug, project_title=p["title"], project_namespace=p["namespace"],
        model=request.model, autonomous=True, origin="overleaf", purpose="project_dependency",
        focus_formalization_id=value["formalization_id"], dependency_id=dependency_id,
        initiating_run_id=request.initiating_run_id,
        timeout_seconds=request.timeout_seconds,
        idempotency_key=f"project-dependency:{dependency_id}:{value['generation']}"))


@router.post(PREFIX + '/dependencies/{dependency_id}/resume', status_code=202)
@workspace_commands.register('dependency.resume')
def resume_worker(slug: str, dependency_id: str):
    from .runs import continue_run, ContinueRequest
    value = dependency(slug, dependency_id)
    if value["state"] != "interrupted" or not value["attempts"][-1].get("run_id"):
        raise HTTPException(409, "Only an interrupted attempt can be resumed")
    run_id = value["attempts"][-1]["run_id"]
    return continue_run(run_id, ContinueRequest(dependency_resume=True,
        idempotency_key=f"dependency-resume:{run_id}"))
