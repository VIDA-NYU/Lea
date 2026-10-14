"""FastAPI app construction + wiring only (D2). Endpoints live in `routes/`,
split by resource; this module builds the app, registers the routers, and mounts
the bundled frontend last so every `/api/*` route takes priority."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .db import init_db
from .routes import (project_library, alignment_checks, lea_status, overleaf_source, formalize_batch_reports, custom_tools, formalizations, mcp_servers, projects, runs, search,
                     sessions, settings, skills, subagents)
from . import alignment_store, bridge, formalize_batch_reports as batch_report_service, github_import_service, netguard, store, recovery, runtime
from .config import load_config

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
    handshake — so `routes/sessions.lsp_socket` performs the same check itself.
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
    return await call_next(request)


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
def startup() -> None:
    runtime.set_state("starting")
    runtime.acquire_data_lock()
    try:
        from lea import process_ownership
        os.environ["LEA_OWNED_PROCESS_DIR"] = str(runtime.owned_process_dir())
        _startup_log.info("Startup: cleaning up previously owned workers.")
        process_ownership.reap_previous()
        _startup_log.info("Startup: preparing the database.")
        init_db()
        runtime.set_state("recovering")
        _startup_log.info("Startup: recovering interrupted work.")
        recovery.recover_running()
        alignment_store.recover_interrupted()
        batch_report_service.recover_interrupted()
        bridge.recover_runs_at_startup()
        from .project_dependencies import recover
        recover()
        from . import project_library, store
        projects = store.list_projects()
        _startup_log.info("Startup: indexing source files in %d projects; compiler checks deferred.", len(projects))
        for project in projects:
            project_library.refresh(project, resolve_environment=False)
        config = load_config()
        proofs_root = config.lea_root / "workspace" / "proofs" if config.lea_root else None
        _startup_log.info("Startup: recovering interrupted GitHub imports.")
        github_import_service.recover_github_imports_at_startup(proofs_root)
        runtime.set_state("ready")
    except BaseException:
        runtime.release_data_lock()
        raise


@app.get("/api/health")
def health() -> dict:
    from .lea_status import admission_enabled
    return {"ok": True, **runtime.snapshot(), "capabilities": {"lea_status": {"version": 1, "admission_enabled": admission_enabled(), "independent_checks": False, "source_pause_policy": 1}, "overleaf_source_refresh": {"version": 1}, "project_library": {"version": 1, "dependency_coordination": True}}}


@app.get("/api/ready")
def readiness() -> dict:
    if not runtime.accepting_work():
        raise HTTPException(503, detail=runtime.snapshot(), headers={"Retry-After": "5"})
    return runtime.snapshot()


class RestartRequest(BaseModel):
    force: bool = False
    cancel: bool = False
    shutdown: bool = False


@app.post("/api/runtime/restart")
def restart_adapter(request: RestartRequest) -> dict:
    if request.cancel:
        return runtime.cancel_drain()
    return runtime.begin_drain(force=request.force, restart=not request.shutdown)


@app.on_event("shutdown")
def shutdown_lean_checks() -> None:
    from lea.lsp_daemon import _shutdown_all
    from lea.mcp import shutdown_pool
    try:
        try:
            _shutdown_all()
        finally:
            shutdown_pool()
    finally:
        runtime.release_data_lock()


app.include_router(sessions.router)
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
        if full_path and candidate.is_file() and candidate.is_relative_to(_web_root):
            return FileResponse(candidate)
        # Otherwise hand back index.html for the SPA / client-side routing.
        return FileResponse(_web_root / "index.html")
