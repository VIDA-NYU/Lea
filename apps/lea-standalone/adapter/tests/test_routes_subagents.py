"""Sub-agents routes (D6): list built-in roles + save per-role overrides.

Route functions are called directly (as the other route tests do). The overrides live in
a JSON sidecar; only that path is patched — the roles come from the vendored prover YAML.
"""
import pytest
from fastapi import HTTPException

from app import subagent_overrides
from app.routes import subagents as sa
from app.routes.subagents import OverrideRequest


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(subagent_overrides, "_OVERRIDES_PATH", tmp_path / "subagent-overrides.json")


def _pc_default(profiles):
    return next(p for p in profiles if p["name"] == "proof-candidate")["default"]


def test_list_returns_builtins_with_defaults(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    profiles = sa.list_profiles()["profiles"]
    names = {p["name"] for p in profiles}
    assert {"proof-candidate", "premise-search"} <= names
    pc = next(p for p in profiles if p["name"] == "proof-candidate")
    assert pc["default"]["max_turns"] == 150           # from the vendored YAML
    assert pc["default"]["max_cost"] is None            # net-new field, no YAML default
    assert pc["override"] == {}                         # nothing stored yet
    assert pc["effective"] == pc["default"]             # so effective == default


def test_put_stores_only_the_diff_from_default(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    default = _pc_default(sa.list_profiles()["profiles"])
    # send the EFFECTIVE settings the user edited: changed max_turns + max_cost, prompt/tools unchanged
    updated = sa.update_profile("proof-candidate", OverrideRequest(
        model=None, max_turns=42, max_cost=0.5,
        system_prompt=default["system_prompt"], tools=default["tools"],
    ))
    assert updated["override"] == {"max_turns": 42, "max_cost": 0.5}   # only the changes
    assert updated["effective"]["max_turns"] == 42
    assert updated["effective"]["max_cost"] == 0.5
    assert updated["effective"]["system_prompt"] == default["system_prompt"]  # default flows through
    # persisted across a re-read
    assert subagent_overrides.get_override("proof-candidate") == {"max_turns": 42, "max_cost": 0.5}


def test_put_custom_prompt_and_model_are_stored(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    updated = sa.update_profile("premise-search", OverrideRequest(
        model="anthropic/claude", system_prompt="Be terse.",
    ))
    assert updated["override"]["model"] == "anthropic/claude"
    assert updated["override"]["system_prompt"] == "Be terse."
    assert updated["effective"]["model"] == "anthropic/claude"


def test_reset_to_defaults_clears_the_override(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    sa.update_profile("proof-candidate", OverrideRequest(max_turns=42))
    assert subagent_overrides.get_override("proof-candidate") == {"max_turns": 42}
    # sending the defaults back → diff is empty → override cleared
    default = _pc_default(sa.list_profiles()["profiles"])
    reset = sa.update_profile("proof-candidate", OverrideRequest(
        model=default["model"], max_turns=default["max_turns"], max_cost=default["max_cost"],
        system_prompt=default["system_prompt"], tools=default["tools"],
    ))
    assert reset["override"] == {}
    assert subagent_overrides.get_override("proof-candidate") == {}


def test_unknown_role_is_404(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        sa.update_profile("counterexample-finder", OverrideRequest(max_turns=5))
    assert exc.value.status_code == 404


def test_malformed_values_are_dropped(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    # max_turns=0 (invalid) and max_cost=-1 (invalid) are sanitized away; model kept
    updated = sa.update_profile("proof-candidate", OverrideRequest(
        model="x/y", max_turns=0, max_cost=-1.0,
    ))
    assert updated["override"] == {"model": "x/y"}
