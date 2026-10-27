"""Package a matching, digest-pinned Compose/extension release from this source."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
IMAGES = {"worker": "ghcr.io/vida-nyu/leaui", "api": "ghcr.io/vida-nyu/leaui-api",
          "companion": "ghcr.io/vida-nyu/leaui-companion"}


def package(source, digests, output, root=ROOT):
    if not re.fullmatch(r"[0-9a-f]{40}", source) or set(digests) != set(IMAGES):
        raise ValueError("A full source SHA and all three image digests are required")
    if any(not re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in digests.values()):
        raise ValueError("Invalid image digest")
    output.mkdir(parents=True, exist_ok=True)
    compose = (root / "apps/lea-standalone/docker-compose.yml").read_text()
    for role, image in IMAGES.items():
        original = image + ":main"
        if original not in compose:
            raise ValueError(f"Compose is missing the {role} image")
        compose = compose.replace(original, image + "@" + digests[role])
    extension = root / "apps/overleaf-extension/extension"
    version = json.loads((extension / "manifest.json").read_text())["version"]
    manifest = {"version": 1, "source": source, "extension_version": version,
                "images": {role: image + "@" + digests[role] for role, image in IMAGES.items()}}
    instructions = f"""Lea release {source[:12]}

Fresh install
1. Install and start Docker Desktop.
2. Extract this ZIP into a permanent directory.
3. Run `docker compose -f compose.yaml pull`, then `docker compose -f compose.yaml up`.
4. Wait for http://localhost:8001/api/ready to return ready, then open http://localhost:8001.
5. Add your provider key in Settings and select a model for that provider.
6. For Overleaf, load the included extension/ directory at chrome://extensions with Developer Mode enabled.

Upgrade an existing checkout
Stop the old stack and retain a backup of data, config, proofs, projects and overleaf-state.
Keep all existing volumes, including the workspace ownership volume. Do not reset local state.
Run the new Compose file with --project-directory pointing at the EXISTING apps/lea-standalone
directory and the SAME Compose project name as before (normally -p lea-standalone).
For example: docker compose -p lea-standalone --project-directory /path/to/Lea/apps/lea-standalone -f /path/to/download/compose.yaml pull
Then repeat with `up`. Reload Chrome's unpacked extension from this release and refresh Overleaf.

All three image references are immutable digests. release.json records their common source.
Linux requires Landlock ABI 3 or newer and seccomp. Unsupported kernels fail closed;
health diagnostics explain when restricted execution is unavailable.
"""
    with tempfile.TemporaryDirectory(prefix="lea-download-") as temporary:
        bundle = Path(temporary) / "Lea"
        bundle.mkdir()
        (bundle / "compose.yaml").write_text(compose)
        (bundle / "release.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (bundle / "README.txt").write_text(instructions)
        shutil.copytree(extension, bundle / "extension")
        for filename, directory in (("Lea.zip", bundle), ("Lea-Overleaf-extension.zip", bundle / "extension")):
            with zipfile.ZipFile(output / filename, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(directory.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(directory.parent))
        shutil.copyfile(bundle / "compose.yaml", output / "compose.yaml")
        shutil.copyfile(bundle / "release.json", output / "release.json")
        shutil.copyfile(bundle / "README.txt", output / "README.txt")
    checksums = []
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name != "SHA256SUMS":
            checksums.append(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name)
    (output / "SHA256SUMS").write_text("\n".join(checksums) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--digests", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package(args.source, json.loads(args.digests.read_text()), args.output)
