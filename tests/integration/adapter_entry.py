"""Test-only adapter entrypoint for the cross-layer integration harness
(PLAN-system-hardening Phase 1 / review B6).

Boots the REAL FastAPI adapter with deterministic prover substitutions: bridge.run_events
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
  [stub:approvals]  honors the configured write_file gate before writing

The declaration name is parsed from the companion prompt's first line
("Formalize the Overleaf theorem labeled <label>."), falling back to
stub_theorem — so artifact contents match what the companion expects.
"""

import os
import hashlib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_DIR = REPO_ROOT / "apps" / "lea-standalone" / "adapter"
sys.path[:0] = [str(ADAPTER_DIR), str(REPO_ROOT / "apps/lea-standalone/prover")]

HOME = Path(os.environ["LEA_ITEST_HOME"]).resolve()
os.environ.setdefault('OPENAI_API_KEY','itest-key')
os.environ['LEA_MODEL_CATALOG_OFFLINE']='true'
os.environ['LEA_COMPANION_IMPORT_DIR']=str(HOME/'companion')

# The harness owns two real production processes. Only this test worker loads
# scripted prover events; the API runs the same denied-import launcher as users.
if '--worker' not in sys.argv:
    import subprocess,signal,time
    env={**os.environ,'LEA_SHARED_DATA_DIR':str(HOME/'data'),
         'LEA_CONFIG_FILE':str(HOME/'config/lea.local.toml'),
         'LEA_API_DENY_PATHS':os.pathsep.join(str(HOME/n) for n in ('prover','config','companion')),
         'LITELLM_LOCAL_MODEL_COST_MAP':'True'}
    subprocess.run([sys.executable,str(ADAPTER_DIR/'bootstrap.py')],cwd=ADAPTER_DIR,env=env,check=True)
    children=[]
    def stop(*_):
        for child in children:
            if child.poll() is None:child.terminate()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        children.append(subprocess.Popen([sys.executable,str(ADAPTER_DIR/'run_api.py'),'--port',os.environ['LEA_ITEST_PORT'],'--log-level','warning'],cwd=ADAPTER_DIR,env=env))
        children.append(subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker'],cwd=ADAPTER_DIR,env={**env,'LEA_PROCESS_ROLE':'worker'}))
        while all(child.poll() is None for child in children):time.sleep(.1)
    finally:
        stop()
        for child in children:
            try:child.wait(timeout=8)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=3)
    raise SystemExit(max(child.returncode or 0 for child in children))

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
    ToolApprovalRequested,
    ToolCalled,
    TurnStarted,
    UsageUpdated,
)
from lea.providers import Usage  # noqa: E402

from app import bridge  # noqa: E402


def _declaration_name(task: str) -> str:
    match = re.search(r"(?:labeled |Label: )([A-Za-z_][A-Za-z0-9_]*)", task)
    return match.group(1) if match else "stub_theorem"


def stub_run_events(config, messages, *, namespace=None, session_id=None,
                    working_dir=None, should_stop=None, gate=None, **kwargs):
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
    args = {"path": str(proof_path)}
    if "[stub:approvals]" in task and gate and gate("write_file", args):
        decision = yield ToolApprovalRequested("write_file", args)
        if decision not in {"allow", "always_session"}:
            yield Finished("interrupted" if decision=='cancelled' else "completed", "Write declined.", 1, session_id, config.model,
                           Usage(input_tokens=1, output_tokens=1), 0.0, {"messages": []})
            return
    yield ToolCalled("write_file", {"path": str(proof_path)})
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    proof_path.write_text(
        f"theorem {decl} : True := by\n  trivial\n"
    )
    yield FileChanged(str(proof_path))
    yield UsageUpdated(11, 7, 0.002)
    yield ToolCalled("lean_check", {"path": str(proof_path)})
    yield stub_check(str(proof_path))
    yield Finished("completed", f"Proved {decl}.", 1, session_id, config.model,
                   Usage(input_tokens=11, output_tokens=7), 0.002, {"messages": []},
                   result_kind="needs_review" if "[stub:review]" in task else "proved")


bridge.run_events = stub_run_events

# Manual checks report the same execution hash contract as the real compiler.
# Integration tests validate transport and persistence; real Lean has its own suite.
from app.routes import sessions as session_routes, projects as project_routes

def stub_check(path, **kwargs):
    content = Path(path).read_bytes()
    bad = b"bad_tactic" in content
    from lea.check_evidence import environment_hash, lake_root, _configuration, source_identity
    from uuid import uuid4
    root = lake_root(path)
    environment = {'configuration': _configuration(root) if root else {}, 'files': {}}
    return CheckResult(path, "error" if bad else "ok", "unknown tactic" if bad else None,
        execution={'check_id': uuid4().hex, 'evidence_version': 1,
            'content_sha256': hashlib.sha256(content).hexdigest(), 'source_path': path, 'source_identity': source_identity(path),
            'environment': environment, 'environment_sha256': environment_hash(environment)},
        content=content.decode())

session_routes.interface_check = project_routes.interface_check = stub_check
session_routes.interface_rebuild = stub_check
session_routes.lean_check_runtime = lambda session_id: {"state": "healthy"}

# Report synthesis remains deterministic and makes no external model request.
from app import formalize_batch_reports as batch_reports
from lea.providers import Done,TextDelta

def stub_report(*args,**kwargs):
    yield TextDelta('Scripted batch report from persisted outcomes.')
    yield Done(Usage(input_tokens=1,output_tokens=1),0.0)
batch_reports.stream=stub_report

if os.environ.get('LEA_ITEST_DEBUG'):
    import faulthandler
    faulthandler.dump_traceback_later(60,repeat=True)
from run_worker import main
code=0
try:main()
except BaseException:
    import traceback
    traceback.print_exc();code=1
finally:os._exit(code)
