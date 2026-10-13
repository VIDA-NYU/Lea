"""Canonical adapter launcher; importable without starting a server."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import sys
import uvicorn
from app import runtime


class DrainingServer(uvicorn.Server):
    def handle_exit(self, sig: int, frame) -> None:
        runtime.begin_drain(restart=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--log-level", default="debug")
    args = parser.parse_args(argv)
    from app.main import app
    server = DrainingServer(uvicorn.Config(app, host=args.host, port=args.port,
        loop="asyncio", http="h11", log_level=args.log_level))
    runtime.bind_server(server)
    try:
        server.run()
        if not server.started:
            raise RuntimeError("Adapter startup failed")
    finally:
        runtime.bind_server(None)
    if runtime.restart_requested():
        original = list(sys.argv[1:] if argv is None else argv)
        os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), *original])


if __name__ == "__main__":
    main()
