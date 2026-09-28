"""Invocation-local evidence and deadlines for Lean checks (no persistent state)."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
import signal
import subprocess
import time
from uuid import uuid4


class CheckFailure(RuntimeError):
    def __init__(self, kind: str, message: str, *, phase: str = "lsp",
                 retryable: bool = True, **evidence):
        super().__init__(message)
        self.kind = kind
        self.phase = phase
        self.retryable = retryable
        self.evidence = evidence

    def as_dict(self) -> dict:
        return {"kind": self.kind, "message": str(self), "phase": self.phase,
                **self.evidence}


class CheckBudget:
    def __init__(self, timeout: float | None = None, should_stop=None):
        self.started = time.monotonic()
        self.deadline = self.started + (
            float(os.environ.get("LEAN_CHECK_TIMEOUT", "900")) if timeout is None else timeout
        )
        self.should_stop = should_stop
        self.execution = {
            "check_id": uuid4().hex, "daemon_generation": None, "backend": "none",
            "cold_reason": None, "failure": None, "attempts": 0,
            "timings_ms": {k: 0.0 for k in ("queue", "initialization", "lsp", "cold", "total")},
        }

    def remaining(self, phase="lsp") -> float:
        if self.should_stop and self.should_stop():
            raise CheckFailure("cancelled", "Lean check cancelled", phase=phase, retryable=False)
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise CheckFailure("queue_timeout" if phase == "queue" else "timeout",
                               f"Lean check deadline exceeded during {phase}",
                               phase=phase, retryable=False)
        return left

    @contextmanager
    def measure(self, phase):
        start = time.monotonic()
        try:
            yield
        finally:
            self.execution["timings_ms"][phase] += (time.monotonic() - start) * 1000

    @contextmanager
    def acquire(self, lock):
        with self.measure("queue"):
            while not lock.acquire(timeout=min(0.1, self.remaining("queue"))):
                pass
        try:
            self.remaining("queue")
            yield
        finally:
            lock.release()

    def finish(self):
        self.execution["timings_ms"]["total"] = (time.monotonic() - self.started) * 1000
        return {**self.execution, "timings_ms": {
            key: round(value, 2) for key, value in self.execution["timings_ms"].items()
        }}


class CheckText(str):
    """A normal tool-result string carrying its own optional execution evidence."""
    def __new__(cls, text: str, execution: dict | None = None):
        result = super().__new__(cls, text)
        result.execution = execution
        return result


@dataclass(frozen=True)
class CheckOutcome:
    status: str
    output: str
    execution: dict

    def text(self) -> CheckText:
        return CheckText(self.output, self.execution)


def lsp_disabled() -> bool:
    return os.environ.get("LEA_DISABLE_LSP", "").strip().lower() in {"1", "true", "yes", "on"}


def stop_process(proc: subprocess.Popen, *, deadline: float | None = None) -> None:
    """Reap only this child's process group, with a five-second total grace."""
    deadline = deadline or time.monotonic() + 5
    def signal_group(sig):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, sig)
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass

    signal_group(signal.SIGTERM)
    try:
        proc.wait(timeout=max(0.001, min(2, deadline - time.monotonic())))
    except subprocess.TimeoutExpired:
        pass
    # The leader may have exited while its file workers still own pipes.
    signal_group(signal.SIGKILL)
    try:
        proc.wait(timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        pass
