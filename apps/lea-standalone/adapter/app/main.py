"""FastAPI app construction + wiring only. Public endpoints live in `api_routes/`,
split by resource; this module builds the app, registers the routers, and mounts
the bundled frontend last so every `/api/*` route takes priority."""

from __future__ import annotations

from .api_boundary import install
install()

from . import peripheral_views

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel


from .api_routes import (workspace, observation, project_library, alignment_checks, lea_status, overleaf_source, formalize_batch_reports, custom_tools, formalizations, mcp_servers, projects, runs, search,
                     sessions, settings, skills, subagents)
from . import netguard


app = FastAPI(title="Lea Interface API")
_startup_log = logging.getLogger("uvicorn.error")


@app.middleware("http")
async def enforce_local_boundary(request: Request, call_next):
    """Reject requests that a page on another site could have caused (S1).

    This runs *before* CORS and does something CORS structurally cannot: it stops the
    request from executing. `CORSMiddleware` only governs whether the response is
    readable, so a cross-site "simple" request still reached these handlers — which
    start runs that execute shell commands, delete projects, and push to GitHub — and
    merely denied the attacker the reply. The `Host` check is the DNS-rebinding guard;
    CORS is blind to that attack entirely, because rebinding makes the attacker's page
    same-origin with this server. See `netguard` for why both match on loopback
    identity rather than an exact allowlist.

    The WebSocket endpoint is NOT covered here — HTTP middleware never sees a
    handshake. The retired direct LSP endpoint rejects every client before accept.
    """
    if not netguard.is_allowed_host(request.headers.get("host")):
        return JSONResponse(
            status_code=403,
            content={"detail": "This host is not allowed to reach the Lea adapter."},
        )
    if not netguard.is_allowed_origin(request.headers.get("origin")):
        return JSONResponse(
            status_code=403,
            content={"detail": f"Requests from origin {request.headers['origin']} are not allowed."},
        )
    if request.method in {'POST','PUT','PATCH'} and request.url.path.startswith('/api/'):
        limit=64*1024*1024
        try:
            if int(request.headers.get('content-length','0'))>limit:return JSONResponse(status_code=413,content={'detail':'Request exceeds its 64 MiB transport bound'})
        except ValueError:return JSONResponse(status_code=400,content={'detail':'Invalid Content-Length'})
        chunks=[];size=0
        async for chunk in request.stream():
            size+=len(chunk)
            if size>limit:return JSONResponse(status_code=413,content={'detail':'Request exceeds its 64 MiB transport bound'},headers={'Connection':'close'})
            chunks.append(chunk)
        request._body=b''.join(chunks)
    from . import db
    epoch=request.headers.get('x-lea-worker-epoch')
    if epoch or request.headers.get('x-lea-worker-parent') or request.headers.get('x-lea-batch-id'):
        from .companion_service import authenticate
        if not authenticate(epoch,request.headers.get('x-lea-worker-proof')):return JSONResponse(status_code=403,content={'detail':'A private worker credential is required'})
        if request.method in {'POST','PUT','PATCH','DELETE'} and not request.headers.get('x-lea-worker-parent'):
            return JSONResponse(status_code=409,content={'detail':'An accepted business intent is required before the worker may submit new work'})
    marker=db.caller_worker_epoch.set(epoch)
    parent=db.caller_parent_command.set(request.headers.get('x-lea-worker-parent'))
    batch=db.caller_batch.set(request.headers.get('x-lea-batch-id'))
    try:
        response=await call_next(request)
        response.headers['X-Lea-Database-Boundary']='1'
        return response
    finally:
        db.caller_worker_epoch.reset(marker)
        db.caller_parent_command.reset(parent)
        db.caller_batch.reset(batch)


# Kept for the response headers a browser needs on an allowed cross-origin call (the
# Vite dev server on :5173 talking to :8001). The middleware above is what actually
# enforces the boundary — this only decorates replies to requests already permitted.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    from .bootstrap import require_schema
    require_schema()



@app.get("/api/health")
def health():
    from .process_state import health
    return health()


@app.get("/api/ready")
def readiness():
    from .process_state import snapshot
    value=snapshot()
    if not value['ready']:raise HTTPException(503,detail=value,headers={'Retry-After':'5'})
    return value


class RestartRequest(BaseModel):
    force: bool = False
    cancel: bool = False
    shutdown: bool = False


@app.post("/api/runtime/restart")
def restart_adapter(request: RestartRequest) -> dict:
    from . import run_commands
    try:return run_commands.runtime_control(force=request.force,cancel=request.cancel,shutdown=request.shutdown)
    except run_commands.commands.QueueError as exc:
        raise HTTPException(exc.status,detail={'error':exc.code},headers={'Retry-After':'5'} if exc.status in {429,503} else None) from exc


@app.on_event("shutdown")
def shutdown_api():
    app.state.shutting_down=True


from .api_routes import editor as editor_routes
app.include_router(editor_routes.router)
from .api_routes import commands as command_routes
from .api_routes import companion as companion_routes
app.include_router(companion_routes.router)
app.include_router(command_routes.router)
app.include_router(workspace.router)
app.include_router(sessions.router)
app.include_router(observation.router)
app.include_router(runs.router)
app.include_router(settings.router)
app.include_router(projects.router)
app.include_router(project_library.router)
app.include_router(formalizations.router)
app.include_router(search.router)
app.include_router(skills.router)
app.include_router(subagents.router)
app.include_router(mcp_servers.router)
app.include_router(custom_tools.router)
app.include_router(alignment_checks.router)
app.include_router(lea_status.router)
app.include_router(overleaf_source.router)
app.include_router(formalize_batch_reports.router)


# --- Static frontend (bundled / single-container deploy) --------------------
# In dev, Vite (:5173) serves the UI and proxies /api here, so this is skipped
# (LEA_WEB_DIST is unset). In the Docker image LEA_WEB_DIST points at the built
# `dist/`; the adapter then serves it on :8001 with SPA fallback. Registered LAST,
# so every /api/* route above takes priority over this catch-all.
_WEB_DIST = os.environ.get("LEA_WEB_DIST")
if _WEB_DIST and Path(_WEB_DIST).is_dir():
    _web_root = Path(_WEB_DIST).resolve()

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        # API paths are handled by the routers above; never hand them index.html.
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        # Serve a real built asset when it exists and stays inside the dist root.
        candidate = (_web_root / full_path).resolve()
        if full_path and candidate.is_relative_to(_web_root) and candidate.is_file():
            return FileResponse(candidate)
        # Otherwise hand back index.html for the SPA / client-side routing.
        return FileResponse(_web_root / "index.html")
