"""A release download must select all images by digest and include its extension."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("package_download", ROOT / ".github/scripts/package-download.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DownloadBundleTests(unittest.TestCase):
    def test_download_has_no_mutable_image_references(self):
        digests = {role: "sha256:" + letter * 64 for role, letter in zip(module.IMAGES, "abc")}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            module.package("a" * 40, digests, output)
            manifest = json.loads((output / "release.json").read_text())
            compose = (output / "compose.yaml").read_text()
            self.assertNotIn(":main", compose)
            self.assertEqual(set(manifest["images"]), set(module.IMAGES))
            for reference in manifest["images"].values():
                self.assertIn(reference, compose)
            with zipfile.ZipFile(output / "Lea.zip") as archive:
                self.assertIn("Lea/extension/manifest.json", archive.namelist())
                self.assertIn("Lea/compose.yaml", archive.namelist())
                self.assertIn("Lea/start-lea.command", archive.namelist())
                self.assertEqual(archive.getinfo("Lea/start-lea.command").external_attr >> 16 & 0o777, 0o755)
            self.assertIn("Lea.zip", (output / "SHA256SUMS").read_text())

    def test_incomplete_or_invalid_bundle_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            for digests in ({}, {role: "main" for role in module.IMAGES}):
                with self.assertRaises(ValueError):
                    module.package("a" * 40, digests, Path(temporary))


if __name__ == "__main__":
    unittest.main()
