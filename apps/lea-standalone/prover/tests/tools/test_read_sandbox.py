"""Unit tests for the read-path sandbox and the scrubbed shell environment
(AUDIT-2026-07-24 S4).

`write_file`/`edit_file` have been confined since F3, but `read_file` was not — the
model could open anything the adapter process could, including `~/.ssh/id_rsa`, the
monorepo `.env`, and `config/lea.local.toml`, which stores every provider key and the
GitHub token in plaintext. And even with reads confined, `bash` inherited those same
keys through the environment, because `load_config` exports them into this process for
LiteLLM to read.

That combination mattered most on the autonomous Overleaf path: the task text is
derived from a shared LaTeX document, and no approval gate stands between an
instruction embedded in that document and the tool call that obeys it.

The confinement has to admit the Lake root, not just the session workspace —
`search_mathlib` returns paths under `.lake/packages/mathlib/` and the model reads
them next, so a workspace-only rule would break the normal proving loop. These tests
pin both halves: what is refused, and what must keep working.

Run:  uv run python -m tests.tools.test_read_sandbox
Exits 0 if every check passes, 1 otherwise.
"""

import os
import tempfile
from pathlib import Path

import lea.tools as tools
from lea.runctx import run_context

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def _workspace(root: Path) -> Path:
    """A realistic layout: a Lake project with a session dir and a vendored Mathlib."""
    lake_root = root / "workspace"
    (lake_root / "proofs" / "s1").mkdir(parents=True)
    (lake_root / "lakefile.toml").write_text("name = \"lea\"\n")
    (lake_root / ".lake" / "packages" / "mathlib" / "Mathlib").mkdir(parents=True)
    return lake_root


def test_reads_inside_the_session_workspace_are_allowed() -> None:
    with tempfile.TemporaryDirectory() as d:
        lake_root = _workspace(Path(d).resolve())
        session = lake_root / "proofs" / "s1"
        (session / "p.lean").write_text("theorem t : True := trivial\n")
        with run_context(working_dir=str(session)):
            out = tools.read_file(str(session / "p.lean"))
        check("a proof in the session dir reads normally", "theorem t" in out)


def test_relative_paths_are_resolved_from_the_run_workspace() -> None:
    """The repo-relative paths in project context must work verbatim."""
    with tempfile.TemporaryDirectory() as d:
        lake_root = _workspace(Path(d).resolve())
        session = lake_root / "proofs" / "s1"
        source = session / ".lea" / "files" / "overleaf" / "main.tex"
        source.parent.mkdir(parents=True)
        source.write_text("\\section{Local context}\n")
        with run_context(working_dir=str(session)):
            out = tools.read_file(".lea/files/overleaf/main.tex")
        check(
            "a project-relative Overleaf source reads from the run workspace",
            "Local context" in out,
        )


def test_mathlib_under_the_lake_root_stays_readable() -> None:
    """The case a workspace-only rule would have broken: search_mathlib hands back
    paths under .lake/packages, and the model reads them on the next turn."""
    with tempfile.TemporaryDirectory() as d:
        lake_root = _workspace(Path(d).resolve())
        lemma = lake_root / ".lake" / "packages" / "mathlib" / "Mathlib" / "Order.lean"
        lemma.write_text("theorem le_refl : True := trivial\n")
        with run_context(working_dir=str(lake_root / "proofs" / "s1")):
            out = tools.read_file(str(lemma))
        check("Mathlib source under the Lake root reads normally", "le_refl" in out)


def test_a_sibling_session_in_the_same_project_is_readable() -> None:
    with tempfile.TemporaryDirectory() as d:
        lake_root = _workspace(Path(d).resolve())
        sibling = lake_root / "proofs" / "s2"
        sibling.mkdir(parents=True)
        (sibling / "q.lean").write_text("theorem q : True := trivial\n")
        with run_context(working_dir=str(lake_root / "proofs" / "s1")):
            out = tools.read_file(str(sibling / "q.lean"))
        check("a project-mate's proof is readable", "theorem q" in out)


def test_credential_files_outside_the_lake_root_are_refused() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        lake_root = _workspace(root)
        secrets = root / "config" / "lea.local.toml"
        secrets.parent.mkdir(parents=True)
        secrets.write_text('openai_api_key = "sk-super-secret"\n')
        dotenv = root / ".env"
        dotenv.write_text("ANTHROPIC_API_KEY=sk-ant-secret\n")

        with run_context(working_dir=str(lake_root / "proofs" / "s1")):
            toml_out = tools.read_file(str(secrets))
            env_out = tools.read_file(str(dotenv))
            traversal = tools.read_file("../../../config/lea.local.toml")

        check("config/lea.local.toml is refused", toml_out.startswith("Error:"))
        check("the provider key never appears in the reply", "sk-super-secret" not in toml_out)
        check(".env is refused", env_out.startswith("Error:"))
        check("the anthropic key never appears in the reply", "sk-ant-secret" not in env_out)
        check("a ../ traversal is refused", traversal.startswith("Error:"))


def test_an_absolute_system_path_is_refused() -> None:
    with tempfile.TemporaryDirectory() as d:
        lake_root = _workspace(Path(d).resolve())
        with run_context(working_dir=str(lake_root / "proofs" / "s1")):
            out = tools.read_file("/etc/hosts")
        check("/etc/hosts is refused", out.startswith("Error:"))


def test_no_run_context_preserves_todays_behavior() -> None:
    """A standalone CLI call or a test has no working_dir; reads stay unrestricted,
    matching `_sandboxed_write_path`."""
    with tempfile.TemporaryDirectory() as d:
        target = Path(d).resolve() / "anywhere.txt"
        target.write_text("readable\n")
        out = tools.read_file(str(target))  # no run_context
        check("read without a run context still works (unchanged)", "readable" in out)


def test_the_shell_environment_carries_no_credentials() -> None:
    keys = {
        "OPENAI_API_KEY": "sk-openai",
        "ANTHROPIC_API_KEY": "sk-ant",
        "GEMINI_API_KEY": "AIza-gemini",
        "GITHUB_TOKEN": "ghp_token",
        "ANTHROPIC_AUTH_TOKEN": "auth-token",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "SOME_SERVICE_PASSWORD": "hunter2",
    }
    previous = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    os.environ["PATH"] = os.environ.get("PATH", "")
    try:
        scrubbed = tools.scrubbed_env()
        for name in keys:
            check(f"{name} is withheld from the shell", name not in scrubbed)
        check("PATH survives (the shell must still work)", "PATH" in scrubbed)

        out = tools.bash("echo \"[$OPENAI_API_KEY][$GITHUB_TOKEN]\"")
        check("a command cannot echo the provider key", "sk-openai" not in out)
        check("a command cannot echo the GitHub token", "ghp_token" not in out)
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_bash_still_runs_normal_commands() -> None:
    """Scrubbing must not break the shell the agent legitimately needs."""
    out = tools.bash("echo lea-ok")
    check("a plain command still runs", "lea-ok" in out)


def main() -> None:
    print("Read-path sandbox + scrubbed shell env tests (AUDIT-2026-07-24 S4):")
    test_reads_inside_the_session_workspace_are_allowed()
    test_relative_paths_are_resolved_from_the_run_workspace()
    test_mathlib_under_the_lake_root_stays_readable()
    test_a_sibling_session_in_the_same_project_is_readable()
    test_credential_files_outside_the_lake_root_are_refused()
    test_an_absolute_system_path_is_refused()
    test_no_run_context_preserves_todays_behavior()
    test_the_shell_environment_carries_no_credentials()
    test_bash_still_runs_normal_commands()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All read-sandbox tests passed.")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
