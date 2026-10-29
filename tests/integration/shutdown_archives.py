"""Verify worker shutdown exports using temporary data and an existing image.

The serving worker and Mathlib preparation are fixtures; the production worker
entrypoint and exporter run unchanged. No API keys, ports, or provider calls.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
from uuid import uuid4
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def docker(*args, timeout=60):
    return subprocess.check_output(["docker", *args], text=True, timeout=timeout,
                                   stderr=subprocess.STDOUT).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--image-sources", action="store_true",
                        help="Exercise the entrypoint and exporter baked into the image")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lea-worker-shutdown-") as temp:
        home = Path(temp)
        for name in ("data", "config", "proofs", "projects", "overleaf-state"):
            (home / name).mkdir()
        (home / "prepare.sh").write_text('#!/bin/sh\necho "PREPARATION READY"\nsleep "${LEA_TEST_PREPARE_SLEEP:-0}"\n')
        (home / "prepare.sh").chmod(0o755)
        (home / "worker.py").write_text('''import signal, time
from pathlib import Path
def stop(*args):
    with Path('/app/data/final.log').open('a') as target:
        target.write('flushed on shutdown fixture-docker-credential\\n')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
print('WORKER READY', flush=True)
while True: time.sleep(.01)
''')
        (home / "config/lea.local.toml").write_text('openai_api_key="fixture-docker-credential"\nmodel="test/model"\n')
        (home / "proofs/Proof.lean").write_text("theorem archived : True := by trivial\n")
        with sqlite3.connect(home / "data/lea-interface.sqlite3") as conn:
            conn.execute("create table messages(content text)")
            conn.execute("insert into messages values ('Saved conversation fixture-docker-credential')")
        (home / "data/backups").mkdir()
        (home / "data/backups/invalid.pdf").write_bytes(b"%PDF-not-a-valid-document")
        (home / "data/lea-interface-old.sqlite3").write_bytes(b"%PDF-not-a-valid-document")
        mounts = []
        for source, target in (
            (home / "data", "/app/data"), (home / "config", "/app/config"),
            (home / "proofs", "/app/prover/workspace/proofs"),
            (home / "projects", "/app/prover/workspace/projects"),
            (home / "overleaf-state", "/app/apps/overleaf-extension/.overleaf-lean-stub"),
            (home / "prepare.sh", "/app/prepare-mathlib.sh"),
            (home / "worker.py", "/app/adapter/run_worker.py"),
        ):
            mounts += ["-v", f"{source}:{target}"]
        if not args.image_sources:
            for source, target in ((ROOT / "scripts", "/app/scripts"),
                                   (ROOT / "apps/lea-standalone/docker/entrypoint.sh", "/app/entrypoint.sh")):
                mounts += ["-v", f"{source}:{target}:ro"]
        previous = {}
        for attempt, mode in enumerate(("SIGTERM", "SIGINT", "preparation")):
            container = "lea-shutdown-archive-test-" + uuid4().hex[:12]
            try:
                docker("run", "-d", "--init", "--network", "none", "--name", container,
                       *mounts, "-e", "LEA_SHARED_DATA_DIR=/app/data", "-e", "LEA_ROOT=/app/prover",
                       "-e", f"LEA_TEST_PREPARE_SLEEP={30 if mode == 'preparation' else 0}",
                       "--entrypoint", "/bin/bash", args.image, "/app/entrypoint.sh", "worker")
                deadline = time.monotonic() + 15
                ready = "PREPARATION READY" if mode == "preparation" else "WORKER READY"
                while ready not in docker("logs", container):
                    assert docker("inspect", "--format", "{{.State.Running}}", container) == "true"
                    assert time.monotonic() < deadline, "Fixture worker did not start"
                    time.sleep(.1)
                if mode == "SIGINT":
                    docker("kill", "--signal", "SIGINT", container)
                else:
                    docker("stop", "--time", "30", container)
                assert docker("wait", container) == "0", docker("logs", container)
                archives = sorted((home / "data/shutdown-archives").glob("*.zip"))
                assert len(archives) == attempt + 1
                assert all(path.read_bytes() == data for path, data in previous.items())
                previous = {path: path.read_bytes() for path in archives}
                with zipfile.ZipFile(archives[-1]) as archive:
                    names = archive.namelist()
                    data = b"\n".join(archive.read(name) for name in names)
                    assert b"fixture-docker-credential" not in data
                    assert b"Saved conversation" in data and b"theorem archived" in data
                    assert b"flushed on shutdown" in archive.read("data/final.log")
                    assert not any("backups/" in name or "shutdown-archives/" in name for name in names)
                    assert "data/lea-interface-old.sqlite3" not in names
                    manifest = json.loads(archive.read("manifest.json"))
                    assert manifest["reason"] == "docker-worker"
                    assert manifest["historical_backups_included"] is False
            except BaseException:
                print(docker("logs", container))
                raise
            finally:
                docker("rm", "-f", container)
    print("Worker SIGTERM, SIGINT, and stop during preparation: sanitized current-data exports passed.")


if __name__ == "__main__":
    main()
