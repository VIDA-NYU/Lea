"""Exercise the real kernel restrictions, including inherited shell children."""
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

if os.environ.get("LEA_TEST_PACKAGED_EXECUTOR") == "1":
    from lea import execution
else:
    ROOT = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("execution", ROOT / "apps/lea-standalone/prover/lea/execution.py")
    execution = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(execution)


@unittest.skipUnless(sys.platform == "linux", "Linux kernel restrictions")
class LinuxExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        capability = execution.capability()
        if not capability["available"]:
            if os.environ.get("LEA_REQUIRE_LINUX_EXECUTOR") == "1":
                raise AssertionError(capability)
            raise unittest.SkipTest(str(capability))

    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix="lea-confinement-test-")
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.allowed = self.root / "allowed"
        self.allowed.mkdir()
        self.sentinel = self.root / "sentinel"
        self.sentinel.write_text("original")
        self.policy = {"writable_roots": [str(self.allowed)], "temporary_root": str(self.allowed)}

    def run_code(self, code):
        with execution.scope(self.policy):
            return execution.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)

    def test_allowed_write_and_subprocess(self):
        result = self.run_code("import subprocess; subprocess.run(['sh','-c', 'printf allowed > \"$TMPDIR/output\"'],check=True)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.allowed / "output").read_text(), "allowed")

    def test_write_truncate_unlink_and_rename_outside_are_denied(self):
        source = self.allowed / "source"
        source.write_text("replacement")
        for expression in ("p.write_text('changed')", "p.unlink()", "p.open('r+').truncate(0)",
                           f"Path({str(source)!r}).rename(p)"):
            with self.subTest(expression=expression):
                result = self.run_code(f"from pathlib import Path; import sys; p=Path({str(self.sentinel)!r}); {expression}")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("PermissionError", result.stderr)
                self.assertEqual(self.sentinel.read_text(), "original")

    def test_symlink_and_hardlink_escape_are_denied(self):
        (self.allowed / "escape").symlink_to(self.sentinel)
        result = self.run_code(f"from pathlib import Path; Path({str(self.allowed / 'escape')!r}).write_text('changed')")
        self.assertNotEqual(result.returncode, 0)
        result = self.run_code(f"import os; os.link({str(self.sentinel)!r},{str(self.allowed / 'linked')!r})")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.sentinel.read_text(), "original")

    def test_network_creation_is_denied_for_tcp_udp_and_unix(self):
        for family, kind in ((socket.AF_INET, socket.SOCK_STREAM), (socket.AF_INET, socket.SOCK_DGRAM),
                             (socket.AF_UNIX, socket.SOCK_STREAM)):
            result = self.run_code(f"import socket; socket.socket({int(family)}, {int(kind)})")
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("Operation not permitted", result.stderr)

    def test_explicit_file_does_not_authorize_sibling(self):
        self.policy["writable_files"] = [str(self.sentinel)]
        result = self.run_code(f"from pathlib import Path; Path({str(self.sentinel)!r}).write_text('allowed')")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_code(f"from pathlib import Path; Path({str(self.root / 'sibling')!r}).write_text('denied')")
        self.assertNotEqual(result.returncode, 0)

    def test_invalid_policy_never_executes_command(self):
        self.policy["writable_roots"] = ["/"]
        result = self.run_code(f"from pathlib import Path; Path({str(self.sentinel)!r}).write_text('changed')")
        self.assertEqual(result.returncode, 126)
        self.assertEqual(self.sentinel.read_text(), "original")

    def test_unavailable_executor_has_no_unconfined_fallback(self):
        with patch.object(execution, "capability", return_value={"available": False}):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                self.run_code(f"from pathlib import Path; Path({str(self.sentinel)!r}).write_text('changed')")
        self.assertEqual(self.sentinel.read_text(), "original")


if __name__ == "__main__":
    unittest.main()
