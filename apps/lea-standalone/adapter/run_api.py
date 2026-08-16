from __future__ import annotations

import os
import sys

# The adapter's listen port. Everything that talks to it in dev (scripts/dev.mjs,
# the Vite /api proxy, scripts/doctor.mjs) reads the same variable, so overriding
# it in one place moves the whole dev stack off :8001.
PORT = int(os.environ.get("LEA_ADAPTER_PORT") or 8001)

print("[startup] importing uvicorn", flush=True)
import uvicorn

print("[startup] importing app.main", flush=True)
from app.main import app

print("[startup] creating uvicorn config", flush=True)
config = uvicorn.Config(
    app,
    host="127.0.0.1",
    port=PORT,
    loop="asyncio",
    http="h11",
    log_level="debug",
)

print("[startup] starting server", flush=True)
try:
    uvicorn.Server(config).run()
except Exception as exc:
    print(f"[startup] failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    raise
