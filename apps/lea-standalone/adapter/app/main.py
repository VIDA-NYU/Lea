"""FastAPI app construction + wiring only (D2). Endpoints live in `routes/`,
split by resource; this module builds the app, registers the routers, and mounts
the bundled frontend last so every `/api/*` route takes priority."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .db import init_db
from .routes import (custom_tools, formalizations, mcp_servers, projects, runs, search,
                     sessions, settings, skills, subagents)
from . import bridge, github_import_service, netguard, store
from .config import load_config

app = FastAPI(title="Lea Interface API")


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
    init_db()
    # No worker survives a restart: reap orphaned 'running' rows (their derived
    # session status would read 'thinking' forever), then re-enqueue the
    # still-pending queue so a restart doesn't strand queued work (Phase 2).
    store.fail_stale_active_runs()
    bridge.recover_runs_at_startup()
    config = load_config()
    proofs_root = config.lea_root / "workspace" / "proofs" if config.lea_root else None
    github_import_service.recover_github_imports_at_startup(proofs_root)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


app.include_router(sessions.router)
app.include_router(runs.router)
app.include_router(settings.router)
app.include_router(projects.router)
app.include_router(formalizations.router)
app.include_router(search.router)
app.include_router(skills.router)
app.include_router(subagents.router)
app.include_router(mcp_servers.router)
app.include_router(custom_tools.router)


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
