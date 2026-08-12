"""Shared targeted-import prompt policy tests.

Run: uv run python -m tests.prompt.test_import_policy
"""

from lea.prompt import load_system_prompt

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


def main() -> None:
    print("prompt targeted-import policy tests:")
    for variant in ("default", "interactive"):
        prompt = load_system_prompt(variant)
        check(f"{variant}: forbids Mathlib barrel",
              "must NOT use the umbrella `import Mathlib`" in prompt)
        check(f"{variant}: maps source paths to imports",
              "`Mathlib/Foo/Bar.lean` corresponds to" in prompt)
        check(f"{variant}: requires suggest_imports", "call `suggest_imports`" in prompt)
        check(f"{variant}: requires a clean re-check",
              "replace its import" in prompt and "run `lean_check` again" in prompt)
    default = load_system_prompt("default")
    check("old broad-import instruction removed",
          "Start files with `import Mathlib` when needed." not in default)
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All prompt targeted-import policy tests passed.")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
