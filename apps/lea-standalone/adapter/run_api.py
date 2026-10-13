"""Canonical adapter launcher with bounded connection shutdown."""
from __future__ import annotations

import argparse
import os
import signal
import uvicorn

GRACEFUL_SHUTDOWN_SECONDS = 3


class LeaServer(uvicorn.Server):
    def handle_exit(self, sig, frame) -> None:
        self.config.app.state.shutting_down = True
        if self.should_exit and sig == signal.SIGINT:
            # Python teardown can still wait on provider threads or atexit
            # handlers after Uvicorn sets force_exit. A second interrupt is an
            # explicit request to bypass that teardown as well.
            self.force_exit = True
            os._exit(130)
        super().handle_exit(sig, frame)


def create_server(app, *, host="127.0.0.1", port=8001, log_level="debug"):
    app.state.shutting_down = False
    return LeaServer(uvicorn.Config(
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
    create_server(app, host=args.host, port=args.port, log_level=args.log_level).run()


if __name__ == "__main__":
    main()
