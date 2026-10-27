"""Pull and boot the exact public download with disposable data and no credentials."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4
import zipfile


def smoke(archive, source):
    with tempfile.TemporaryDirectory(prefix="lea-download-smoke-") as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(root)
        app = root / "Lea"
        manifest = json.loads((app / "release.json").read_text())
        assert manifest["source"] == source
        config = root / "docker-config"
        config.mkdir()
        original = Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
        (config / "config.json").write_text(json.dumps({"cliPluginsExtraDirs": [str(original / "cli-plugins")]}))
        project = "lea-download-" + uuid4().hex[:12]
        env = {**os.environ, "DOCKER_CONFIG": str(config), "LEA_WORKSPACE_OWNERSHIP_VOLUME": project + "-ownership"}
        compose = ["docker", "compose", "-p", project, "-f", str(app / "compose.yaml")]
        def run(*args, check=True):
            return subprocess.run([*compose, *args], env=env, check=check, timeout=900)
        def request(path):
            with urlopen("http://127.0.0.1:8001" + path, timeout=5) as response:
                return json.load(response)
        try:
            run("pull")
            run("up", "-d")
            deadline = time.monotonic() + 600
            while True:
                try:
                    ready = request("/api/ready")
                    with urlopen("http://127.0.0.1:31245/health", timeout=5) as response:
                        assert response.status == 200
                    if ready.get("ready"):
                        break
                except (OSError, URLError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Public release did not become ready within ten minutes")
                time.sleep(2)
            health = request("/api/health")
            assert health["release"]["source"] == source
            assert health["capabilities"]["execution"]["available"], health
            with urlopen("http://127.0.0.1:8001/", timeout=5) as response:
                assert b"<html" in response.read().lower()
            request("/api/settings")
            request("/api/sessions")
            return {"source": source, "passed": True, "anonymous_pull": True,
                    "download_archive": archive.name, "all_services_ready": True, "linux_execution": True}
        finally:
            run("logs", "--tail", "60", check=False)
            run("down", "--volumes", "--remove-orphans", check=False)
            # Docker creates private root-owned fixture data. Restore only this
            # disposable fixture's ownership so the host can remove it.
            subprocess.run(["docker", "run", "--rm", "--pull=never", "--network", "none",
                            "-v", str(root) + ":/fixture", "--entrypoint", "chown",
                            manifest["images"]["worker"], "-R", f"{os.getuid()}:{os.getgid()}", "/fixture"],
                           env=env, check=False, timeout=60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(smoke(args.archive, args.source), indent=2) + "\n")
