"""Keystone conformance test: `app.blueprint` against the shared parser fixtures.

The cases in `apps/lea-standalone/fixtures/blueprint` are the parser oracle for
both the adapter (this module's subject, the system of record) and the UI copy in
`src/app/lib/blueprintModel.js`. The format rules are docs/PLAN-blueprint-tools.md
§2.1; the case format is described in `fixtures/blueprint/README.md`.

Red by design until P1 lands the tolerant parser (`file`, `blocks`, coded
warnings) and `render`. Do not edit the fixtures to make this pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import blueprint

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "blueprint"
CASES = [path for path in sorted(FIXTURES.glob("*.md")) if path.name != "README.md"]
NODE_FIELDS = ("key", "kind", "lean", "file", "uses")
_MISSING = "<field missing>"


def _load(path: Path) -> tuple[str, dict]:
    text = path.read_text(encoding="utf-8")
    expected = json.loads(path.with_name(f"{path.stem}.expected.json").read_text(encoding="utf-8"))
    return text, expected


def _view(node: dict) -> dict:
    # A missing field fails the comparison instead of raising KeyError.
    return {field: node.get(field, _MISSING) for field in NODE_FIELDS}


def _full_view(node: dict) -> dict:
    return {**_view(node), "statement": (node.get("statement") or "").strip()}


def _warning_set(warnings: list[dict]) -> set[tuple[str, str]]:
    return {(w.get("code"), w.get("node")) for w in warnings}


def test_fixtures_are_present():
    assert len(CASES) >= 8, f"expected the keystone cases in {FIXTURES}"
    for path in CASES:
        assert path.with_name(f"{path.stem}.expected.json").is_file(), path.name


@pytest.mark.parametrize("path", CASES, ids=lambda path: path.stem)
def test_parse_matches_fixture(path):
    text, expected = _load(path)
    parsed = blueprint.parse(text)
    nodes = parsed["nodes"]

    assert [n.get("key") for n in nodes] == [n["key"] for n in expected["nodes"]]
    for got, want in zip(nodes, expected["nodes"]):
        assert _view(got) == {field: want[field] for field in NODE_FIELDS}, want["key"]
        statement = (got.get("statement") or "").strip()
        assert statement.startswith(want["statement_starts_with"]), (want["key"], statement[:80])

    prose = [b["heading"] for b in parsed["blocks"] if b["type"] == "prose"]
    assert prose == expected["prose_sections"]

    want_warnings = {(w["code"], w["node"]) for w in expected["warnings"]}
    assert _warning_set(parsed["warnings"]) == want_warnings
    assert _warning_set(blueprint.validate(text)) == want_warnings


@pytest.mark.parametrize("path", CASES, ids=lambda path: path.stem)
def test_render_is_canonical_and_idempotent(path):
    text, expected = _load(path)
    parsed = blueprint.parse(text)
    first = blueprint.render(parsed)

    assert blueprint.render(blueprint.parse(first)) == first
    assert [_full_view(n) for n in blueprint.parse(first)["nodes"]] == [_full_view(n) for n in parsed["nodes"]]
    assert first.endswith("\n") and not first.endswith("\n\n")
    if expected["canonical"]:
        assert first == text
