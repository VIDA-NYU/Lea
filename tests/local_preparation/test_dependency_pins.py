"""Release builds must retain exact dependency and runtime selections."""
import json
from pathlib import Path
import re
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DependencyPinTests(unittest.TestCase):
    def test_node_manifests_match_the_committed_lock(self):
        lock = json.loads((ROOT / "package-lock.json").read_text())["packages"]
        for name in ("package.json", "apps/lea-standalone/package.json", "apps/overleaf-extension/package.json"):
            manifest = json.loads((ROOT / name).read_text())
            workspace = "" if name == "package.json" else str(Path(name).parent)
            for kind in ("dependencies", "devDependencies", "peerDependencies"):
                for package, version in manifest.get(kind, {}).items():
                    with self.subTest(manifest=name, package=package):
                        self.assertRegex(version, r"^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$")
                        self.assertEqual(lock[workspace][kind][package], version)

    def test_python_requirements_and_build_backend_are_exact(self):
        for component in ("adapter", "prover"):
            root = ROOT / "apps/lea-standalone" / component
            manifest = tomllib.loads((root / "pyproject.toml").read_text())
            locked = {p["name"]: p["version"] for p in tomllib.loads((root / "uv.lock").read_text())["package"] if "version" in p}
            requirements = manifest["project"]["dependencies"] + manifest["dependency-groups"]["dev"]
            for requirement in requirements:
                if requirement == "lea-prover":
                    continue
                with self.subTest(component=component, requirement=requirement):
                    match = re.fullmatch(r"([\w.-]+)(?:\[[^\]]+\])?==([\w.+-]+)", requirement)
                    self.assertIsNotNone(match)
                    package, version = match.groups()
                    self.assertEqual(locked[re.sub(r"[-_.]+", "-", package.lower())], version)
            for requirement in manifest.get("build-system", {}).get("requires", []):
                self.assertRegex(requirement, r"^[\w.-]+==[\w.+-]+$")
            self.assertRegex((root / ".python-version").read_text().strip(), r"^3\.13\.\d+$")

    def test_every_external_docker_stage_is_digest_pinned(self):
        text = (ROOT / "apps/lea-standalone/Dockerfile").read_text()
        for line in text.splitlines():
            if line.startswith("FROM "):
                image = line.split(" AS ")[0].split()[-1]
                if image in {"runtime", "companion-node"}:
                    continue
                self.assertRegex(image, r"@sha256:[0-9a-f]{64}$", line)
        self.assertRegex(text.splitlines()[0], r"@sha256:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
