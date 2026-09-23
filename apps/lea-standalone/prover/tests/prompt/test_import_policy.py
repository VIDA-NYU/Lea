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
    for variant in ("default", "interactive", "overleaf_faithful"):
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
    overleaf = load_system_prompt("overleaf_faithful")
    normalized_overleaf = " ".join(overleaf.split())
    check("Overleaf prompt preserves the source method",
          "When supplied, the source proof controls the approach" in overleaf)
    check("Overleaf prompt rejects silent mathematical repairs",
          "Never silently repair a mathematical gap" in overleaf)
    check("Overleaf prompt treats a missing proof as non-blocking",
          "A missing proof alone is never a reason to pause" in overleaf)
    check("Overleaf prompt reserves blocking for semantic changes",
          "choice between materially different meanings" in overleaf
          and "explicitly supplied proof's essential" in overleaf)
    check("Overleaf prompt reassesses legacy missing-proof blockers",
          "update the same finding key to a" in overleaf
          and "omitted findings remain active" in overleaf)
    check("Overleaf prompt honors an author-authorized best-effort continuation",
          "Author-authorized best-effort continuation" in normalized_overleaf
          and "do not pause again merely because those choices were absent" in normalized_overleaf)
    check("Overleaf prompt prefers faithful failure",
          "informative partial result is better than a" in overleaf
          and "silently changed or unrelated successful proof" in overleaf)
    check("Overleaf source pauses are disabled by default",
          "A blocking Lea Status finding does not pause this run" in overleaf)
    opted_in = load_system_prompt("overleaf_faithful", allow_source_pause=True)
    check("Overleaf opt-in permits source pauses",
          "A blocking Lea Status finding pauses this run" in opted_in)
    continuation = load_system_prompt("overleaf_continuation")
    check("Overleaf continuation also defaults to no source pause",
          "A blocking Lea Status finding does not pause this run" in continuation)
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        raise SystemExit(1)
    print("All prompt targeted-import policy tests passed.")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
