from __future__ import annotations

import sys
import os

print("[startup] importing uvicorn", flush=True)
import uvicorn

print("[startup] importing app.main", flush=True)
from app.main import app
from app import runtime


class DrainingServer(uvicorn.Server):
    def handle_exit(self, sig: int, frame) -> None:
        # Uvicorn's default handler closes listeners before lifespan shutdown.
        # Keep SSE and stop/approval routes available until workers have settled.
        runtime.begin_drain(restart=False)

print("[startup] creating uvicorn config", flush=True)
config = uvicorn.Config(
    app,
    host="127.0.0.1",
    port=8001,
    loop="asyncio",
    http="h11",
    log_level="debug",
)

print("[startup] starting server", flush=True)
try:
    server = DrainingServer(config)
    runtime.bind_server(server)
    server.run()
    if not server.started:
        raise RuntimeError("Adapter startup failed")
    if runtime.restart_requested():
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
except Exception as exc:
    print(f"[startup] failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    raise
