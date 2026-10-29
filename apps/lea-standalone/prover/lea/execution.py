"""Filesystem restrictions for commands evaluating project-controlled Lean code.

The adapter supplies writable roots; this module never imports the adapter. File
tools retain their path checks. Every compiler/shell child inherits the OS policy.
Unsupported hosts fail closed rather than calling a working directory a sandbox.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

_policy = ContextVar("lea_filesystem_policy", default=None)


@lru_cache(maxsize=1)
def capability() -> dict:
    if sys.platform == "linux":
        try:
            with tempfile.TemporaryDirectory(prefix="lea-executor-probe-") as root:
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).with_name("linux_execution.py")),
                     json.dumps({"writable_roots": [root]}), "/usr/bin/true"],
                    capture_output=True, text=True, timeout=10,
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"available": False, "backend": "linux-landlock-seccomp", "reason": str(error)}
        return {"available": result.returncode == 0, "backend": "linux-landlock-seccomp",
                "reason": result.stderr.strip() if result.returncode else None}
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        return {"available": False, "reason": "This runtime has no supported filesystem executor."}
    try:
        result = subprocess.run(["sandbox-exec", "-p", "(version 1)(allow default)(deny file-write*)",
                                 "/usr/bin/true"], capture_output=True, text=True, timeout=5)
        return {"available": result.returncode == 0, "backend": "macos-seatbelt",
                "reason": result.stderr.strip() if result.returncode else None}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": str(exc)}


def current():
    return _policy.get()


def install(policy):
    return _policy.set(policy)


def reset(token):
    _policy.reset(token)


@contextmanager
def scope(policy):
    token = install(policy)
    try:
        yield
    finally:
        reset(token)


def profile(policy: dict) -> str:
    roots = sorted({str(Path(p).resolve()) for p in policy["writable_roots"]})
    if not roots or "/" in roots:
        raise ValueError("An executor requires bounded writable roots")
    # Children cannot use a local HTTP/IPC service as a filesystem write proxy.
    # Provider access and typed host operations stay outside this command sandbox.
    return "(version 1)(allow default)(deny network*)(deny file-write*)" + "".join(
        f"(allow file-write* (subpath {json.dumps(p)}))" for p in roots
    ) + ''.join(f'(allow file-write* (literal {json.dumps(str(Path(p).resolve()))}))'
                for p in policy.get('writable_files', []) if str(Path(p).resolve()) != '/') + '(allow file-write* (literal "/dev/null"))'


def _prepare(args, kwargs):
    policy = current()
    if not policy:
        return args, kwargs
    if not capability()["available"]:
        raise RuntimeError("Filesystem-restricted execution is unavailable")
    kwargs = dict(kwargs)
    if kwargs.pop("shell", False):
        args = [kwargs.pop("executable", "/bin/sh"), "-c", args]
    if sys.platform == "linux":
        args = [sys.executable, str(Path(__file__).with_name("linux_execution.py")),
                json.dumps(policy), *map(str, args)]
    else:
        args = ["sandbox-exec", "-p", profile(policy), *map(str, args)]
    env = dict(kwargs.get("env") or os.environ)
    if policy.get("temporary_root"):
        env["TMPDIR"] = str(policy["temporary_root"])
    kwargs["env"] = env
    return args, kwargs


def run(args, **kwargs):
    args, kwargs = _prepare(args, kwargs)
    from .runctx import current_should_stop
    if current_should_stop() is None:
        return subprocess.run(args, **kwargs)
    from .cancellation import check_cancelled
    from .check_runtime import stop_process
    import time

    check_cancelled()
    timeout = kwargs.pop("timeout", None)
    check = kwargs.pop("check", False)
    data = kwargs.pop("input", None)
    if data is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if kwargs.pop("capture_output", False):
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr may not be used with capture_output")
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    kwargs["start_new_session"] = True
    started = time.monotonic()
    with subprocess.Popen(args, **kwargs) as proc:
        try:
            while True:
                check_cancelled()
                remaining = None if timeout is None else timeout - (time.monotonic() - started)
                if remaining is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    stdout, stderr = proc.communicate(data, timeout=min(0.05, remaining) if remaining is not None else 0.05)
                    break
                except subprocess.TimeoutExpired:
                    data = None
            if check and proc.returncode:
                raise subprocess.CalledProcessError(proc.returncode, args, output=stdout, stderr=stderr)
            return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
        except BaseException:
            stop_process(proc, deadline=time.monotonic() + 0.5)
            raise


def popen(args, **kwargs):
    args, kwargs = _prepare(args, kwargs)
    return subprocess.Popen(args, **kwargs)
