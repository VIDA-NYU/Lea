"""One check policy shared by model tools and manual checks."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import time

from .check_runtime import CheckBudget, CheckFailure, CheckOutcome, lsp_disabled, stop_process
from .runctx import current_should_stop

_COLD_CHECK_CONCURRENCY = max(1, int(os.environ.get("LEA_COLD_CHECK_CONCURRENCY", "2")))
_cold_check_sem = threading.BoundedSemaphore(_COLD_CHECK_CONCURRENCY)
_log = logging.getLogger("lea.lean_checks")


def check_file(path: Path, lake_root: str | None, *, use_lsp=True, allow_cold=True,
               budget: CheckBudget | None = None) -> CheckOutcome:
    from . import diagnostics, lsp_daemon
    budget = budget or CheckBudget(should_stop=current_should_stop())
    caller_stop = budget.should_stop
    runtime_root = lake_root or str(path.parent)
    budget.should_stop = lambda: bool((caller_stop and caller_stop()) or lsp_daemon.runtime_is_closing(runtime_root))
    evidence = budget.execution
    output = ""
    status = "error"
    reason = "explicit" if not use_lsp else "disabled" if lsp_disabled() else "no_lake_root" if not lake_root else None
    try:
        budget.remaining()
        content = path.read_bytes()
        evidence["content_sha256"] = hashlib.sha256(content).hexdigest()
        if reason is not None and use_lsp and not allow_cold:
            raise CheckFailure("cascade_unavailable", "Cascade verification requires an available Lean server",
                               retryable=False)
        if reason is None:
            try:
                output = lsp_daemon.check_via_lsp(str(path), content.decode("utf-8"), lake_root, budget=budget)
                evidence["backend"] = "lsp"
            except CheckFailure as exc:
                evidence["failure"] = exc.as_dict()
                if exc.kind in {"cancelled", "timeout", "queue_timeout"}:
                    raise
                budget.remaining()
                if not allow_cold:
                    raise CheckFailure("cascade_unavailable",
                                       "Lean server recovery failed; cascade verification requires a fresh server",
                                       retryable=False, cause=exc.as_dict()) from exc
                reason = "fallback"
                evidence["cold_reason"] = reason
                runtime = lsp_daemon.runtime_snapshot(lake_root)
                diagnostics.report(
                    "degraded", "lean.lsp_cold_fallback", "Checking with full compilation",
                    source="lean_check", once=True, path=str(path),
                    check_id=evidence["check_id"], episode_id=runtime["episode_id"],
                    failure=evidence["failure"],
                )
                _log.warning("lean_check_fallback %s", json.dumps({**evidence, "path": str(path)}))
        if reason is not None:
            budget.remaining("cold")
            evidence["cold_reason"] = reason
            # The same path is necessary for Lean module/import identity. Do not
            # quietly check a temporary copy or a revision edited during recovery.
            if path.read_bytes() != content:
                raise CheckFailure("source_changed", "File changed while preparing its Lean check", retryable=False)
            cmd = ["lake", "env", "lean", str(path)] if lake_root else ["lean", str(path)]
            with budget.acquire(_cold_check_sem):
                budget.remaining("cold")
                with budget.measure("cold"):
                    proc = None
                    lsp_daemon.note_cold(lake_root or str(path.parent), 1)
                    try:
                        evidence["backend"] = "cold"
                        evidence["attempts"] += 1
                        proc = subprocess.Popen(cmd, cwd=lake_root or str(path.parent),
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                text=True, encoding="utf-8", errors="replace",
                                                start_new_session=True)
                        while True:
                            try:
                                stdout, stderr = proc.communicate(timeout=min(0.1, budget.remaining("cold")))
                                break
                            except subprocess.TimeoutExpired:
                                continue
                        output = (stdout + "\n" + stderr).strip()
                        if proc.returncode != 0:
                            output = f"Error: Lean exited with code {proc.returncode}.\n{output}".strip()
                        elif not output:
                            output = "OK — no errors, no warnings."
                    except FileNotFoundError as exc:
                        raise CheckFailure("executable_missing", str(exc), phase="cold", retryable=False) from exc
                    finally:
                        if proc:
                            # Reap surviving descendants even if the leader exited.
                            stop_process(proc)
                            for pipe in (proc.stdout, proc.stderr):
                                if pipe:
                                    pipe.close()
                        lsp_daemon.note_cold(lake_root or str(path.parent), -1)
        budget.remaining("lsp" if evidence["backend"] == "lsp" else "cold")
        if path.read_bytes() != content:
            raise CheckFailure("source_changed", "File changed during its Lean check; check the new revision",
                               retryable=False)
        from .tools import _lean_check_has_error
        status = "error" if _lean_check_has_error(output) else "ok"
    except (CheckFailure, OSError, UnicodeError) as raw:
        exc = raw if isinstance(raw, CheckFailure) else CheckFailure("file_io", str(raw), retryable=False)
        evidence["failure"] = exc.as_dict()
        output = f"Error: {exc}"
    execution = budget.finish()
    _log.info("lean_check_finished %s", json.dumps(execution))
    # This is durable evidence, but the UI need not render each successful check
    # as a separate notice: it uses this record for the activity's backend/timing.
    diagnostics.report("notice", "lean.check_execution", "Lean check finished", source="lean_check",
                       path=str(path), execution=execution, check_status=status)
    return CheckOutcome(status, output, execution)
