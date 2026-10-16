"""Unit tests for `rebuild_module` (lea/tools.py) -- specifically its handling of
a genuine `lake build` failure whose own log doesn't happen to contain the literal
text "error:" the way `lean`'s direct CLI/LSP diagnostics do.

Regression test for a real bug: `rebuild_module` originally returned Lake's raw
output verbatim on a non-zero exit, and `interface.rebuild()` classified success/
failure by regex-scanning that text for "error:" (`_lean_check_has_error`,
reused from the `lean_check`/`check()` path, where that format is reliable because
it comes straight from `lean`'s own diagnostic printer). `lake build`'s own
build-progress log ("(check the real Lake output shape below) doesn't use that
format at all -- it's Lake's own "✗ [n/total] Building <module> (Ns)" plus a
`trace:` block, not a `file.lean:L:C: error: ...` line -- so a real build failure
could be silently classified as "ok". Caught live: editing a theorem's proof to
break it, then saving through the Overleaf lean pane, produced a rebuild that
reported success and a cascade that (wrongly) reported a dependent as still
valid, while VS Code's own "Restart File" against the same module showed Lake's
build genuinely failing ("✗ [8248/8249] Building Lea.<Project>.epsilon_one
(4.2s)"). See docs/FEATURE-overleaf-lean-pane-manual-edit.md ("Cascade
verification").

Run:  uv run python -m tests.tools.test_rebuild_module
Exits 0 if every check passes, 1 otherwise.
"""

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import lea.tools as tools
from lea.tools import _first_error_line, _lean_check_has_error, rebuild_module

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_lake_project():
    """A real lakefile.lean + proofs/Lea/Proj/Foo.lean -- rebuild_module resolves
    paths on disk (lake root discovery, source-root-relative module naming), so
    this needs real files, not just mocked subprocess output."""
    tmp = Path(tempfile.mkdtemp())
    ws = tmp / "workspace"
    proofs = ws / "proofs" / "Lea" / "Proj"
    proofs.mkdir(parents=True)
    (ws / "lakefile.lean").write_text("package lea\n")
    foo = proofs / "Foo.lean"
    foo.write_text("theorem t : True := trivial\n")
    return tmp, foo


def test_real_lake_build_failure_shape_is_still_classified_as_an_error():
    """The exact failure shape observed live: Lake's own progress/trace log,
    with NO line matching `error[:\\s]` anywhere in it, and a non-zero exit."""
    tmp, foo = _make_lake_project()
    try:
        # Lake's real failure output for a target whose build step failed --
        # a "✗ [n/total] Building <module> (Ns)" line plus a `trace:` block
        # showing the invocation it ran. Deliberately contains no "error:" text
        # anywhere, mirroring what was actually captured live.
        lake_failure_stderr = (
            "✗ [8248/8249] Building Lea.Proj.Foo (4.2s)\n"
            "trace: .> LEAN_PATH=/some/path:/other/path\n"
            "trace: .> /some/toolchain/bin/lean ...\n"
        )
        real_run = tools.subprocess.run
        tools.subprocess.run = lambda *a, **k: _FakeCompletedProcess(1, "", lake_failure_stderr)
        try:
            out = rebuild_module(str(foo))
        finally:
            tools.subprocess.run = real_run

        check("no literal 'error:' in Lake's own raw log",
              "error:" not in lake_failure_stderr.lower())
        check("rebuild_module's returned text IS classified as an error",
              _lean_check_has_error(out))
        check("a usable one-line summary is extractable",
              _first_error_line(out) is not None)
        check("the summary names the failing module",
              "Lea.Proj.Foo" in (_first_error_line(out) or ""))
    finally:
        shutil.rmtree(tmp)


def test_successful_build_is_still_ok():
    """Regression guard: the fix must not turn a real success into a false error."""
    tmp, foo = _make_lake_project()
    try:
        real_run = tools.subprocess.run
        tools.subprocess.run = lambda *a, **k: _FakeCompletedProcess(0, "", "")
        try:
            out = rebuild_module(str(foo))
        finally:
            tools.subprocess.run = real_run

        check("returncode 0 -> not classified as an error", not _lean_check_has_error(out))
    finally:
        shutil.rmtree(tmp)


def test_successful_build_marks_the_lsp_daemon_stale():
    """Regression test for the Overleaf lean pane's manual-edit rename bug: a
    persistent LSP daemon (lsp_daemon.py) that already imported this module
    keeps serving its pre-rebuild environment in memory even after this real
    `lake build` refreshes the .olean on disk -- from a *different* process,
    which can't reach into the daemon at all. rebuild_module must flag the
    daemon when a loaded artifact changes so it restarts on its next
    use rather than silently staying wrong. See lea/lsp_daemon.py and
    docs/FEATURE-overleaf-lean-pane-manual-edit.md ('Cascade verification')."""
    tmp, foo = _make_lake_project()
    try:
        import lea.lsp_daemon as lsp_daemon
        from lea import check_evidence
        root = (tmp / 'workspace').resolve()
        artifact = root / 'Foo.olean'
        artifact.write_text('original artifact')
        environment = {'configuration': check_evidence._configuration(root), 'files': {
            'workspace:Foo.olean': {'path': str(artifact), 'kind': 'compiled',
                                  'sha256': check_evidence.file_hash(artifact)}}}
        calls = []
        real_mark_stale = lsp_daemon.mark_stale
        lsp_daemon.mark_stale = lambda lake_root: calls.append(lake_root)
        real_run = tools.subprocess.run
        def build(*a, **k):
            artifact.write_text('rebuilt artifact')
            return _FakeCompletedProcess(0, '', '')
        tools.subprocess.run = build
        try:
            with patch.object(lsp_daemon, 'runtime_snapshot', return_value={'generation': 'fixture'}):
                check_evidence.observe(root, environment, generation='fixture')
                rebuild_module(str(foo))
        finally:
            tools.subprocess.run = real_run
            lsp_daemon.mark_stale = real_mark_stale

        check("mark_stale was called exactly once on success", len(calls) == 1)
        check("mark_stale was called with this build's lake_root",
              bool(calls) and calls[0] == str((tmp / "workspace").resolve()))
        assert calls == [str(root)]
    finally:
        shutil.rmtree(tmp)


def test_failed_build_does_not_mark_the_daemon_stale():
    """A failure without a serving daemon has no cache to invalidate. Partial
    failures with changed loaded artifacts are covered by the boundary suite."""
    tmp, foo = _make_lake_project()
    try:
        import lea.lsp_daemon as lsp_daemon
        calls = []
        real_mark_stale = lsp_daemon.mark_stale
        lsp_daemon.mark_stale = lambda lake_root: calls.append(lake_root)
        real_run = tools.subprocess.run
        tools.subprocess.run = lambda *a, **k: _FakeCompletedProcess(1, "", "error: boom")
        try:
            rebuild_module(str(foo))
        finally:
            tools.subprocess.run = real_run
            lsp_daemon.mark_stale = real_mark_stale

        check("mark_stale was not called on a failed build", calls == [])
    finally:
        shutil.rmtree(tmp)


def test_a_failure_that_does_mention_error_text_still_works():
    """Lake failures that DO forward `lean`'s own file:line:col error line
    (the common case, just not guaranteed) must still classify correctly."""
    tmp, foo = _make_lake_project()
    try:
        stderr = (
            "✗ [1/1] Building Lea.Proj.Foo (0.1s)\n"
            "Foo.lean:2:2: error: unsolved goals\n"
        )
        real_run = tools.subprocess.run
        tools.subprocess.run = lambda *a, **k: _FakeCompletedProcess(1, "", stderr)
        try:
            out = rebuild_module(str(foo))
        finally:
            tools.subprocess.run = real_run

        check("still classified as an error", _lean_check_has_error(out))
    finally:
        shutil.rmtree(tmp)


def main():
    print("rebuild_module (tools.py) tests:")
    test_real_lake_build_failure_shape_is_still_classified_as_an_error()
    test_successful_build_is_still_ok()
    test_successful_build_marks_the_lsp_daemon_stale()
    test_failed_build_does_not_mark_the_daemon_stale()
    test_a_failure_that_does_mention_error_text_still_works()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        sys.exit(1)
    print("All rebuild_module tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
