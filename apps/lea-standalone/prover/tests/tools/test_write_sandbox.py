"""Unit tests for the write-path sandbox (v2.3 concurrency/hosting hardening).

`write_file`/`edit_file` used to `mkdir -p` and write to *any* path the model
supplied — no confinement. A model path typo (seen in the wild:
`apps/lean-standalone` for `apps/lea-standalone`), a hallucination, or a
prompt-injected path could therefore create/clobber files outside the session's
workspace — worse once runs are concurrent and web-hosted.

`_sandboxed_write_path` confines writes to the activation's `working_dir`
(from `run_context`): relative paths resolve against it (not the shared process
cwd), and any path that escapes it is rejected. With no run context (standalone
CLI / tests) the old behavior is preserved.

Run:  uv run python -m tests.tools.test_write_sandbox
Exits 0 if every check passes, 1 otherwise.
"""

import tempfile
from pathlib import Path

import lea.tools as tools
from lea.runctx import run_context

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def test_relative_path_resolves_against_working_dir_not_process_cwd() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        with run_context(working_dir=str(root)):
            out = tools.write_file("Lea/Misc/p.lean", "theorem t : True := trivial")
        landed = root / "Lea" / "Misc" / "p.lean"
        check("relative write lands under working_dir", landed.exists())
        check("relative write is not left in the process cwd",
              not (Path.cwd() / "Lea" / "Misc" / "p.lean").exists())
        check("success message names the confined path", str(landed) in out)


def test_absolute_path_inside_working_dir_is_allowed() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        target = root / "Lea" / "Misc" / "q.lean"
        with run_context(working_dir=str(root)):
            tools.write_file(str(target), "ok")
        check("absolute write inside the workspace is allowed", target.exists())


def test_absolute_path_typo_escaping_working_dir_is_rejected() -> None:
    # The exact shape of the real incident: a sibling tree one character off.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve() / "lea-standalone"
        root.mkdir()
        typo = Path(d).resolve() / "lean-standalone" / "proofs" / "p.lean"
        with run_context(working_dir=str(root)):
            out = tools.write_file(str(typo), "should not be written")
        check("escaping absolute write is refused with an error", out.startswith("Error:"))
        check("the escaping file is NOT created", not typo.exists())
        check("no sibling tree is created by the rejected write", not typo.parent.exists())


def test_dotdot_traversal_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve() / "session"
        root.mkdir()
        with run_context(working_dir=str(root)):
            out = tools.write_file("../escape.lean", "nope")
        check("`..` traversal out of the workspace is refused", out.startswith("Error:"))
        check("the traversed file is NOT created", not (root.parent / "escape.lean").exists())


def test_edit_file_is_confined_too() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve() / "session"
        root.mkdir()
        # An out-of-workspace file the model might try to edit.
        outside = Path(d).resolve() / "outside.lean"
        outside.write_text("secret := 1")
        with run_context(working_dir=str(root)):
            out = tools.edit_file(str(outside), "secret := 1", "secret := 2")
        check("edit_file refuses a path outside the workspace", out.startswith("Error:"))
        check("the out-of-workspace file is untouched", outside.read_text() == "secret := 1")


def test_child_redirects_out_of_sandbox_absolute_path_into_scratch() -> None:
    # A subagent (depth > 0) works in an isolated scratch dir. When the coordinator's
    # task names an absolute canonical path OUTSIDE that dir, the write is REDIRECTED
    # into the scratch dir (by basename) rather than rejected — the coordinator collates
    # the candidate by re-reading it, so the child must be able to write *something*.
    with tempfile.TemporaryDirectory() as d:
        scratch = Path(d).resolve() / "session" / ".lea" / "tmp" / "run" / "agent"
        scratch.mkdir(parents=True)
        canonical = Path(d).resolve() / "session" / "Lea" / "Misc" / "BurnsideAux.lean"
        with run_context(working_dir=str(scratch), depth=1):
            out = tools.write_file(str(canonical), "theorem c : True := trivial")
        landed = scratch / "BurnsideAux.lean"
        check("child write to a canonical path lands in its scratch dir", landed.exists())
        check("child write is NOT written to the canonical path", not canonical.exists())
        check("child write reports success (not an error)", not out.startswith("Error:"))
        check("success message names the redirected scratch path", str(landed) in out)


def test_child_relative_path_still_lands_in_scratch() -> None:
    # The normal path: a child writing a relative filename lands in its working dir.
    with tempfile.TemporaryDirectory() as d:
        scratch = Path(d).resolve() / "agent"
        scratch.mkdir()
        with run_context(working_dir=str(scratch), depth=1):
            tools.write_file("candidate.lean", "ok")
        check("child relative write lands in scratch", (scratch / "candidate.lean").exists())


def test_no_context_preserves_todays_behavior() -> None:
    # Standalone CLI / unit calls set no run context → no confinement, path as-is.
    with tempfile.TemporaryDirectory() as d:
        target = Path(d).resolve() / "anywhere" / "p.lean"
        out = tools.write_file(str(target), "ok")  # no run_context
        check("write without a run context still works (unchanged)", target.exists())
        check("success message returned when unconfined", str(target) in out)


def main() -> None:
    print("Write-path sandbox tests (v2.3 hardening):")
    test_relative_path_resolves_against_working_dir_not_process_cwd()
    test_absolute_path_inside_working_dir_is_allowed()
    test_absolute_path_typo_escaping_working_dir_is_rejected()
    test_dotdot_traversal_is_rejected()
    test_edit_file_is_confined_too()
    test_child_redirects_out_of_sandbox_absolute_path_into_scratch()
    test_child_relative_path_still_lands_in_scratch()
    test_no_context_preserves_todays_behavior()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All write-sandbox tests passed.")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
