"""Verify every role's anonymous multi-arch manifest and packaged source."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

IMAGES = {"worker": "ghcr.io/vida-nyu/leaui", "api": "ghcr.io/vida-nyu/leaui-api",
          "companion": "ghcr.io/vida-nyu/leaui-companion"}


def verify(source):
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("Expected full source SHA")
    result = {}
    with tempfile.TemporaryDirectory(prefix="lea-anonymous-registry-") as config:
        original = Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
        (Path(config) / "config.json").write_text(json.dumps({"cliPluginsExtraDirs": [str(original / "cli-plugins")]}))
        env = {**os.environ, "DOCKER_CONFIG": config}
        for role, image in IMAGES.items():
            ref = image + ":sha-" + source
            def inspect(*args):
                return subprocess.check_output(["docker", "buildx", "imagetools", "inspect", ref, *args], env=env, text=True)
            text = inspect()
            digest = re.search(r"^Digest:\s+(sha256:[0-9a-f]{64})$", text, re.MULTILINE)
            if not digest:
                raise ValueError(f"Missing manifest digest for {role}")
            manifest = json.loads(inspect("--raw"))
            platforms = {(m.get("platform", {}).get("os"), m.get("platform", {}).get("architecture"))
                         for m in manifest["manifests"] if m.get("platform", {}).get("os") != "unknown"}
            if platforms != {("linux", "amd64"), ("linux", "arm64")}:
                raise ValueError(f"Incomplete architecture coverage: {role} {platforms}")
            images = json.loads(inspect("--format", "{{json .Image}}"))
            for platform in ("linux/amd64", "linux/arm64"):
                if images[platform]["config"]["Labels"].get("org.opencontainers.image.revision") != source:
                    raise ValueError(f"Source mismatch: {role} {platform}")
            result[role] = digest[1]
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(verify(args.source), indent=2) + "\n")
