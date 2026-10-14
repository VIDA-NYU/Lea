"""Canonical adapter launcher with coordinated restarts and bounded HTTP shutdown."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import sys

import uvicorn
from app import runtime

GRACEFUL_SHUTDOWN_SECONDS = 3


class DrainingServer(uvicorn.Server):
    def handle_exit(self, sig, frame) -> None:
        self.config.app.state.shutting_down = True
        if getattr(self, "_shutdown_requested", False) and sig == signal.SIGINT:
            # A second interrupt also bypasses stalled Python/provider teardown,
            # including while the runtime is waiting for active work to finish.
            self.force_exit = True
            os._exit(130)
            return
        self._shutdown_requested = True
        if getattr(self, "_runtime_bound", False):
            runtime.begin_drain(restart=False)
        else:
            # Standalone embedders have no runtime coordinator to drain.
            super().handle_exit(sig, frame)


LeaServer = DrainingServer


def create_server(app, *, host="127.0.0.1", port=8001, log_level="debug"):
    app.state.shutting_down = False
    return DrainingServer(uvicorn.Config(
        app, host=host, port=port, loop="asyncio", http="h11",
        log_level=log_level, timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    ))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--log-level", default="debug")
    args = parser.parse_args(argv)
    from app.main import app
    server = create_server(app, host=args.host, port=args.port, log_level=args.log_level)
    runtime.bind_server(server)
    server._runtime_bound = True
    try:
        server.run()
        if not server.started:
            raise RuntimeError("Adapter startup failed")
    finally:
        server._runtime_bound = False
        runtime.bind_server(None)
    if runtime.restart_requested():
        original = list(sys.argv[1:] if argv is None else argv)
        os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), *original])


if __name__ == "__main__":
    main()
