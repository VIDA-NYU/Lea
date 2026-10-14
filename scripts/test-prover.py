"""Run standalone prover entry points with bounded execution and owned-worker cleanup."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PROVER = ROOT / "apps/lea-standalone/prover"
REAL = {"test_concurrent_checks", "test_cascade_rename_integration", "test_recovery_integration"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=["unit", "lean"], default="unit")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.suite == "lean":
        if os.environ.get("LEA_SKIP_LEAN_TESTS") or not shutil.which("lake"):
            parser.error("real Lean tests require lake and must not be disabled")
        subprocess.run(["lake", "env", "lean", "--version"], cwd=PROVER / "workspace", check=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for path in sorted((PROVER / "tests").rglob("test_*.py")):
        if "__main__" not in path.read_text() or (path.stem in REAL) != (args.suite == "lean"):
            continue
        module = ".".join(path.relative_to(PROVER).with_suffix("").parts)
        started = time.monotonic()
        proc = subprocess.Popen([sys.executable, "-m", module], cwd=PROVER,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
        try:
            output, _ = proc.communicate(timeout=900 if args.suite == "lean" else 180)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            output, _ = proc.communicate()
            code = 124
        finally:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        (args.output_dir / f"{module}.log").write_text(output)
        if "Exception in thread" in output or "PytestUnhandledThreadExceptionWarning" in output:
            code = code or 1
        result = {"module": module, "exit_code": code,
                  "seconds": round(time.monotonic() - started, 2)}
        results.append(result)
        print(json.dumps(result), flush=True)
    (args.output_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    return int(not results or any(r["exit_code"] for r in results))


if __name__ == "__main__":
    sys.exit(main())
