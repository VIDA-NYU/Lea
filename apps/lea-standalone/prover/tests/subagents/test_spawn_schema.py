"""v2.5 B1: the spawn schema is built per run, from the roles that actually exist.

The bug this prevents has no symptom: with a schema frozen at import and `subagent_type`
as free text, a role added later is invisible to the coordinator, so it is never
delegated to — and nothing raises. These tests assert the offer, not just the plumbing.
"""

import json

import lea.tools  # noqa: F401 — registers the built-ins
from lea import profiles
from lea.registry import build_toolset
from lea.subagents import GENERALIST, build_spawn_schema


def _subagent_type(schema):
    return schema["input_schema"]["properties"]["subagent_type"]


def test_every_defined_role_is_offered():
    prop = _subagent_type(build_spawn_schema())
    for name in profiles.available_profiles():
        assert name in prop["enum"], f"{name} exists on disk but is not offered"
    assert GENERALIST in prop["enum"]


def test_each_role_carries_its_description():
    """An enum of bare names tells the model nothing about WHEN to use one. The role's
    own description is what makes the choice possible — and is where a user-authored
    'when to use this' does its work."""
    prop = _subagent_type(build_spawn_schema())
    for name in profiles.available_profiles():
        desc = (profiles.load_profile(name).description or "").strip()
        if desc:
            assert desc in prop["description"], f"{name}'s description is not offered"


def test_a_new_role_appears_without_reimporting(tmp_path, monkeypatch):
    """THE regression. A role added after import must show up on the next build — the
    exact case a frozen schema gets wrong, silently."""
    monkeypatch.setattr(profiles, "_AGENTS_DIR", tmp_path)
    (tmp_path / "counterexample-hunter.yaml").write_text(
        "description: Look for a counterexample before trying to prove.\n"
        "system_prompt: |\n  You hunt counterexamples.\n"
    )
    prop = _subagent_type(build_spawn_schema())
    assert "counterexample-hunter" in prop["enum"]
    assert "Look for a counterexample" in prop["description"]


def test_a_malformed_role_is_skipped_not_fatal(tmp_path, monkeypatch):
    """One bad file must not take the whole toolset down — it is simply not offered."""
    monkeypatch.setattr(profiles, "_AGENTS_DIR", tmp_path)
    (tmp_path / "good.yaml").write_text("description: Fine.\nsystem_prompt: |\n  ok\n")
    (tmp_path / "broken.yaml").write_text("system_prompt: ''\n")   # empty prompt → invalid
    prop = _subagent_type(build_spawn_schema())
    assert "good" in prop["enum"]
    assert "broken" not in prop["enum"]


def test_the_loop_sees_the_dynamic_schema():
    """`build_toolset` must resolve the factory — otherwise all of the above is true of a
    schema nobody ever sends."""
    schemas, _ = build_toolset(["spawn_subagent"])
    prop = _subagent_type(schemas[0])
    assert "enum" in prop and GENERALIST in prop["enum"]


def test_the_template_is_never_mutated():
    """The factory deep-copies; two builds must not accumulate state."""
    first = json.dumps(build_spawn_schema(), sort_keys=True)
    second = json.dumps(build_spawn_schema(), sort_keys=True)
    assert first == second
