"""Test-only adapter entrypoint for the cross-layer integration harness
(PLAN-system-hardening Phase 1 / review B6).

Boots the REAL FastAPI adapter with exactly one substitution: bridge.run_events
is replaced by a scripted stub prover (no model calls, no Lean toolchain). The
stub yields the prover's real typed events (imported from the vendored
lea.interface), so everything downstream of the seam — event mapping, git
commits, code steps, SQLite persistence, SSE encoding — is production code
under test.

Hermetic: LEA_ITEST_HOME points at a temp dir; the adapter's config file, DB,
and prover workspace all resolve under it (the module-level ROOT / DB_PATH
attributes are patched before app.main is imported; settings.py captured ROOT
at import, so its copy is patched too).

Scenarios are selected per run by markers in the user message:
  (default)         happy path — writes a .lean file, checks ok, finishes proved
  [stub:fail]       raises mid-run — bridge surfaces run_error + done(failed)
  [stub:max_turns]  finishes with reason="max_turns"

The declaration name is parsed from the companion prompt's first line
("Formalize the Overleaf theorem labeled <label>."), falling back to
stub_theorem — so artifact contents match what the companion expects.
"""

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_DIR = REPO_ROOT / "apps" / "lea-standalone" / "adapter"
sys.path.insert(0, str(ADAPTER_DIR))

HOME = Path(os.environ["LEA_ITEST_HOME"]).resolve()

from app import config as app_config  # noqa: E402
from app import db as app_db  # noqa: E402

app_config.ROOT = HOME
app_db.ROOT = HOME
app_db.DB_PATH = HOME / "data" / "lea-interface.sqlite3"

from app import settings as app_settings  # noqa: E402

app_settings.ROOT = HOME

from lea.interface import (  # noqa: E402
    AssistantTextDelta,
    CheckResult,
    FileChanged,
    Finished,
    ToolCalled,
    TurnStarted,
    UsageUpdated,
)
from lea.providers import Usage  # noqa: E402

from app import bridge  # noqa: E402


def _declaration_name(task: str) -> str:
    match = re.search(r"labeled ([A-Za-z_][A-Za-z0-9_]*)", task)
    return match.group(1) if match else "stub_theorem"


def stub_run_events(config, messages, *, namespace=None, session_id=None,
                    working_dir=None, should_stop=None, gate=None):
    task = str(messages[-1]["content"]) if messages else ""
    decl = _declaration_name(task)

    yield TurnStarted(1)
    yield AssistantTextDelta("Stub prover engaged. ")

    if "[stub:fail]" in task:
        raise RuntimeError("scripted stub failure")
    if "[stub:max_turns]" in task:
        yield Finished("max_turns", "Ran out of turns.", 1, session_id, config.model,
                       Usage(input_tokens=3, output_tokens=2), 0.001, {"messages": []})
        return

    proof_path = Path(working_dir) / f"{decl}.lean"
    yield ToolCalled("write_file", {"path": str(proof_path)})
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    proof_path.write_text(
        f"import Mathlib\n\ntheorem {decl} : True := by\n  trivial\n"
    )
    yield FileChanged(str(proof_path))
    yield UsageUpdated(11, 7, 0.002)
    yield ToolCalled("lean_check", {"path": str(proof_path)})
    yield CheckResult(str(proof_path), "ok", None)
    yield Finished("completed", f"Proved {decl}.", 1, session_id, config.model,
                   Usage(input_tokens=11, output_tokens=7), 0.002, {"messages": []})


bridge.run_events = stub_run_events

from app.main import app  # noqa: E402

import uvicorn  # noqa: E402

uvicorn.run(
    app,
    host="127.0.0.1",
    port=int(os.environ["LEA_ITEST_PORT"]),
    log_level="warning",
)
