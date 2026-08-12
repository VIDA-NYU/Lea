"""Domain-scoped tactic cascades (v2.3 item 26).

`_TACTIC_CASCADE` is one flat ladder for all of mathematics; this feature adds
domain-specific fragments that fire only when a file's imports/namespaces are in play,
appended to the `lean_check` tool result at tool-use time (never baked into the cached
system prompt).

These pin:
  * a hint fires only for domains PRESENT in the file text (import/open/qualified name);
  * no domain present → None (a plain proof gets no extra noise);
  * the per-activation `already` set dedups: each domain surfaces once, then is silent;
  * multiple present domains fire together, in table order (deterministic block);
  * the system prompt is untouched — the flat cascade stays, the domain table is separate;
  * the agent-loop helper reads the checked file relative to working_dir and dedups.

Run:  uv run python -m tests.prompt.test_domain_cascades
Exits 0 if every check passes, 1 otherwise.
"""

import sys
import tempfile
from pathlib import Path

from lea.agent import _domain_cascade_for_check
from lea.prompt import BASE_PROMPT, domain_cascade_hint

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


_MEASURE = "import Mathlib\nopen MeasureTheory\ntheorem t : True := by trivial\n"
_PLAIN = "import Mathlib\ntheorem t : 1 + 1 = 2 := by norm_num\n"
_MULTI = "import Mathlib.MeasureTheory.Integral.Basic\nimport Mathlib.Topology.Basic\ntheorem t : True := trivial\n"


# --- the pure hint function ----------------------------------------------------

def test_present_domain_fires():
    hint = domain_cascade_hint(_MEASURE)
    check("MeasureTheory file yields a hint", hint is not None)
    check("hint names the domain", "MeasureTheory" in hint)
    check("hint carries the domain tactic", "measurability" in hint)


def test_absent_domain_is_silent():
    check("a plain proof yields no domain hint", domain_cascade_hint(_PLAIN) is None)
    check("empty text yields no hint", domain_cascade_hint("") is None)


def test_dedup_via_already_set():
    already: set[str] = set()
    first = domain_cascade_hint(_MEASURE, already)
    check("first check surfaces the hint", first is not None)
    check("the domain is recorded in the set", "MeasureTheory" in already)
    second = domain_cascade_hint(_MEASURE, already)
    check("a second check on the same domain is silent", second is None)


def test_multiple_domains_fire_in_table_order():
    hint = domain_cascade_hint(_MULTI)
    check("multi-domain file yields a hint", hint is not None)
    check("both domains present", "MeasureTheory" in hint and "Topology" in hint)
    # Table order: MeasureTheory is defined before Topology in _DOMAIN_CASCADES.
    check("domains appear in table order",
          hint.index("MeasureTheory") < hint.index("Topology"))


def test_partial_dedup_still_surfaces_new_domains():
    already = {"MeasureTheory"}
    hint = domain_cascade_hint(_MULTI, already)
    check("already-seen domain is suppressed", hint is not None and "MeasureTheory" not in hint)
    check("the new domain still surfaces", "Topology" in hint)


# --- the system prompt is untouched -------------------------------------------

def test_domain_hints_are_not_in_the_system_prompt():
    # The whole point: domain fragments ride tool results, so the cached prompt prefix
    # is byte-identical run to run. The specific per-domain lines must NOT be baked in.
    check("polynomial degree tactic is not in the base prompt", "compute_degree" not in BASE_PROMPT)
    check("the flat cascade still ships in the prompt", "Automation ladder" in BASE_PROMPT)


# --- the agent-loop helper -----------------------------------------------------

def test_agent_helper_reads_relative_to_working_dir():
    with tempfile.TemporaryDirectory() as d:
        wd = Path(d).resolve()
        (wd / "P.lean").write_text(_MEASURE)
        surfaced: set[str] = set()
        hint = _domain_cascade_for_check({"path": "P.lean"}, str(wd), surfaced)
        check("helper resolves a relative path against working_dir", hint is not None)
        check("helper surfaces the domain", "MeasureTheory" in hint)
        # And it dedups through the same set on the next call.
        check("helper dedups on the second check",
              _domain_cascade_for_check({"path": "P.lean"}, str(wd), surfaced) is None)


def test_agent_helper_tolerates_missing_file():
    with tempfile.TemporaryDirectory() as d:
        check("a missing file yields no hint, no crash",
              _domain_cascade_for_check({"path": "nope.lean"}, d, set()) is None)


def test_agent_helper_handles_no_path():
    check("no path arg → no hint", _domain_cascade_for_check({}, None, set()) is None)


def main():
    print("domain-scoped tactic cascade tests (v2.3 item 26):")
    test_present_domain_fires()
    test_absent_domain_is_silent()
    test_dedup_via_already_set()
    test_multiple_domains_fire_in_table_order()
    test_partial_dedup_still_surfaces_new_domains()
    test_domain_hints_are_not_in_the_system_prompt()
    test_agent_helper_reads_relative_to_working_dir()
    test_agent_helper_tolerates_missing_file()
    test_agent_helper_handles_no_path()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        sys.exit(1)
    print("All domain-cascade item-26 tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
