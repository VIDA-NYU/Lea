"""Per-role sub-agent overrides (D6) — the user's edits from the Sub-agents page.

The built-in roles (`proof-candidate`, `premise-search`) ship as vendored YAML in the
prover (`lea/agents/*.yaml`). This module stores the user's PER-ROLE overrides
SEPARATELY — a JSON sidecar in the config dir — so a role can be retuned (model,
max_turns, max_cost, system_prompt, tools) WITHOUT mutating the vendored profile. The
adapter threads the loaded dict onto the run's `LeaConfig.subagent_overrides`, and the
prover's `_child_config` merges the matching entry over the profile at spawn.

Only fields that DIFFER from the role's default are stored, so a vendored default the
user didn't touch keeps flowing through; an empty override means "reset to defaults".
"""
from __future__ import annotations

import json
from typing import Any

from .config import ROOT, write_private_text

_OVERRIDES_PATH = ROOT / "config" / "subagent-overrides.json"
_ALLOWED_FIELDS = ("model", "max_turns", "max_cost", "system_prompt", "tools")


def load_overrides_checked() -> tuple[dict[str, dict], str | None]:
    """`(overrides, why_they_didn't_load)` — the same result as `load_overrides`, plus
    the reason it fell back to defaults (F2).

    This exists because `load_overrides` handles its own failure and returns `{}`, which
    means a caller can NEVER detect one: wrapping it in a `try` catches nothing, because
    nothing is raised. So a corrupt overrides file silently reverted every sub-agent to
    its vendored defaults — after the user had deliberately configured them on the
    Sub-agents page — and there was no channel by which anything could say so.

    A file that simply doesn't exist is NOT a failure: no overrides is the default state.
    Only an unreadable or malformed one is reported.
    """
    try:
        raw = _OVERRIDES_PATH.read_text()
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"the file is not valid JSON ({exc})"
    if not isinstance(data, dict):
        return {}, "the file does not contain a JSON object"
    kept = {k: v for k, v in data.items() if isinstance(v, dict)}
    dropped = [k for k in data if k not in kept]
    if dropped:
        # Partially usable: the good entries still apply, but the user must be told
        # which roles they configured are not in effect.
        return kept, f"these entries were not readable and were ignored: {', '.join(sorted(dropped))}"
    return kept, None


def load_overrides() -> dict[str, dict]:
    """All stored overrides: {role_name: {field: value}}. Missing/corrupt file → {}.

    Lossy by design for callers that only need the values (`get_override`, the settings
    route). Anything that must REPORT a load failure wants `load_overrides_checked`."""
    return load_overrides_checked()[0]


def get_override(name: str) -> dict:
    return load_overrides().get(name) or {}


def sanitize(override: dict | None) -> dict:
    """Keep only well-typed, non-empty override fields — a malformed value is dropped,
    never persisted (a bad override must fail safe, not mis-tune a child)."""
    out: dict[str, Any] = {}
    for k in _ALLOWED_FIELDS:
        v = (override or {}).get(k)
        if v is None:
            continue
        if k in ("model", "system_prompt") and isinstance(v, str) and v.strip():
            out[k] = v.strip()
        elif k == "max_turns" and isinstance(v, bool) is False and isinstance(v, int) and v >= 1:
            out[k] = v
        elif k == "max_cost" and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            out[k] = float(v)
        elif k == "tools" and isinstance(v, list) and all(isinstance(t, str) for t in v):
            out[k] = v
    return out


def save_override(name: str, override: dict) -> dict:
    """Persist `override` (already reduced to the diff-from-default) for role `name`.
    An empty override REMOVES the entry (reset to defaults). Returns what was stored."""
    clean = sanitize(override)
    everything = load_overrides()
    if clean:
        everything[name] = clean
    else:
        everything.pop(name, None)
    # Atomic (S6): a truncate-then-write left every role reset to defaults if it
    # was interrupted, and this file sits beside the secrets one.
    write_private_text(_OVERRIDES_PATH, json.dumps(everything, indent=2))
    return clean
