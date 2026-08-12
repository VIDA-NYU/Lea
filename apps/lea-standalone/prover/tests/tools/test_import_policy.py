"""Targeted-import policy and `suggest_imports` tool tests.

Run: uv run python -m tests.tools.test_import_policy
"""

import os
import tempfile
from pathlib import Path

import lea.tools as tools

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def test_generated_write_rejects_mathlib_barrel() -> None:
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "Broad.lean"
        out = tools.write_file(
            str(target), "import Mathlib\n\ntheorem broad : True := by trivial\n"
        )
        check("write rejects bare Mathlib import", out.startswith("Error:"))
        check("rejected broad file is not created", not target.exists())


def test_targeted_write_is_allowed() -> None:
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "Targeted.lean"
        out = tools.write_file(
            str(target),
            "import Mathlib.Data.Nat.Prime.Basic\n\n"
            "theorem targeted (n : Nat) : n = n := by rfl\n",
        )
        check("targeted Mathlib module is allowed", not out.startswith("Error:"))
        check("targeted proof is written", target.exists())


def test_commented_barrel_text_is_not_an_import() -> None:
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "Commented.lean"
        out = tools.write_file(
            str(target),
            "/-\nimport Mathlib\n-/\n"
            "import Mathlib.Data.Nat.Prime.Basic\n\n"
            "theorem targeted (n : Nat) : n = n := by rfl\n",
        )
        check("commented Mathlib text does not trigger the gate",
              not out.startswith("Error:") and target.exists())


def test_edit_cannot_preserve_or_introduce_barrel() -> None:
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "Edit.lean"
        original = "import Mathlib\n\ntheorem t : True := by trivial\n"
        target.write_text(original)
        out = tools.edit_file(str(target), "trivial", "by exact True.intro")
        check("edit retaining broad import is rejected", out.startswith("Error:"))
        check("rejected edit leaves file unchanged", target.read_text() == original)


def test_agent_check_cannot_bypass_gate_with_existing_broad_file() -> None:
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "ShellWritten.lean"
        target.write_text("import Mathlib\n\ntheorem t : True := by trivial\n")
        out = tools.TOOL_HANDLERS["lean_check"]({"path": str(target)})
        check("agent-facing check rejects a shell-written broad import",
              out.startswith("Error:") and "umbrella" in out)


def test_operator_override_is_explicit() -> None:
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "Override.lean"
        prior = os.environ.get("LEA_ALLOW_BROAD_MATHLIB_IMPORT")
        os.environ["LEA_ALLOW_BROAD_MATHLIB_IMPORT"] = "1"
        try:
            out = tools.write_file(str(target), "import Mathlib\n")
        finally:
            if prior is None:
                os.environ.pop("LEA_ALLOW_BROAD_MATHLIB_IMPORT", None)
            else:
                os.environ["LEA_ALLOW_BROAD_MATHLIB_IMPORT"] = prior
        check("operator override permits exceptional broad import",
              not out.startswith("Error:") and target.exists())


def test_suggest_imports_parses_linter_and_cleans_scratch() -> None:
    with tempfile.TemporaryDirectory() as d:
        lake_root = Path(d)
        (lake_root / "lakefile.lean").write_text("package Test\n")
        proof = lake_root / "Proof.lean"
        proof.write_text(
            "import Mathlib\n\n"
            "theorem one_plus_one : (1 : Nat) + 1 = 2 := by norm_num\n"
        )
        captured: dict[str, object] = {}
        original_check = tools.lean_check

        def fake_check(path: str, **_kwargs) -> str:
            scratch = Path(path)
            captured["path"] = scratch
            captured["source"] = scratch.read_text()
            return (
                f"{scratch}:1:0: warning: unneeded import 'Mathlib'\n"
                f"{scratch}:1:0: warning: -- missing imports\n"
                "import Mathlib.Data.Nat.Notation\n"
                "import Mathlib.Tactic.NormNum.Basic\n"
            )

        tools.lean_check = fake_check
        try:
            out = tools.suggest_imports(str(proof))
        finally:
            tools.lean_check = original_check

        source = str(captured.get("source", ""))
        scratch = captured.get("path")
        check("analysis copy uses broad import internally", "import Mathlib\n" in source)
        check("analysis copy enables import-bump linter", "#import_bumps\n" in source)
        check("suggestion contains declaration import",
              "import Mathlib.Data.Nat.Notation" in out)
        check("suggestion contains tactic import",
              "import Mathlib.Tactic.NormNum.Basic" in out)
        check("suggestion excludes Mathlib barrel", "\nimport Mathlib\n" not in f"\n{out}")
        check("analysis scratch is removed",
              isinstance(scratch, Path) and not scratch.exists())
        check("original proof is untouched", proof.read_text().startswith("import Mathlib\n"))


def test_suggest_imports_keeps_needed_current_imports() -> None:
    with tempfile.TemporaryDirectory() as d:
        lake_root = Path(d)
        (lake_root / "lakefile.lean").write_text("package Test\n")
        proof = lake_root / "Proof.lean"
        proof.write_text(
            "import Mathlib.Data.Nat.Prime.Basic\n"
            "import Mathlib.Tactic.NormNum.Basic\n\n"
            "theorem t : True := by trivial\n"
        )
        original_check = tools.lean_check
        tools.lean_check = lambda *_args, **_kwargs: (
            "warning: unneeded import 'Mathlib'\n"
            "warning: unneeded import 'Mathlib.Tactic.NormNum.Basic'\n"
        )
        try:
            out = tools.suggest_imports(str(proof))
        finally:
            tools.lean_check = original_check
        check("needed targeted current import is retained",
              "import Mathlib.Data.Nat.Prime.Basic" in out)
        check("linter-reported redundant import is removed",
              "import Mathlib.Tactic.NormNum.Basic" not in out)


def main() -> None:
    print("Targeted import policy tests:")
    test_generated_write_rejects_mathlib_barrel()
    test_targeted_write_is_allowed()
    test_commented_barrel_text_is_not_an_import()
    test_edit_cannot_preserve_or_introduce_barrel()
    test_agent_check_cannot_bypass_gate_with_existing_broad_file()
    test_operator_override_is_explicit()
    test_suggest_imports_parses_linter_and_cleans_scratch()
    test_suggest_imports_keeps_needed_current_imports()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All targeted import policy tests passed.")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
