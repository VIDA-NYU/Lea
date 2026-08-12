"""Guided authoring: four questions in, one string out (v2.5 C1/C2).

The model never sees fields — it sees `skills.body`, `agent_roles.system_prompt`, and a
tool's `description`. So the form's job is to make those strings *good*, not to introduce
a new shape. Everything here is that compile step, in one place, so a skill and a role
cannot drift in how their prose is assembled.

**Why these four questions.** "Write a skill" produces a blank page. "What is it? When
should Lea use it? When should it NOT? How is it done?" produces the four things a good
tool description actually contains — and the third is the one nobody writes unprompted,
which is why an agent with six overlapping tools uses the wrong one.

**`when_to_use` does double duty for a role.** It becomes the role's `description`, which
`build_spawn_schema` (B1) lists beside the role name in the coordinator's `enum`. Writing
"when to use this" and making the coordinator choose correctly are therefore the same act
— the connection Phase C exists to create.
"""

from __future__ import annotations

import json

# Order matters: this is the order the compiled text reads in, and the order the form
# asks. `key -> heading`; a heading of None means the text leads without one.
FIELDS: list[tuple[str, str | None]] = [
    ("summary", None),
    ("when_to_use", "When to use this"),
    ("when_not_to_use", "When NOT to use this"),
    ("how", "How to do it"),
]
FIELD_KEYS = [key for key, _ in FIELDS]

# The AgentSkills standard caps a description at 1024 characters, and OpenHands truncates
# with a pointer to the source rather than silently cutting. A role's `enum` line is read
# by the model on every turn it considers delegating, so brevity there is not cosmetic.
MAX_DESCRIPTION = 1024


def normalize(fields: dict | None) -> dict:
    """Keep only the known fields, as trimmed strings. Anything else is dropped rather
    than stored, so a stray key can never reach the compiled prose."""
    out = {}
    for key in FIELD_KEYS:
        value = (fields or {}).get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


def is_empty(fields: dict | None) -> bool:
    return not normalize(fields)


def compile_text(fields: dict | None) -> str:
    """The four answers as one markdown block.

    Deterministic: the same fields always produce the same text, so re-saving without
    edits is a no-op and a diff shows only what the user actually changed. An omitted
    field contributes nothing — no empty heading, which would read as a section the
    author forgot rather than one they didn't need.
    """
    clean = normalize(fields)
    parts: list[str] = []
    for key, heading in FIELDS:
        value = clean.get(key)
        if not value:
            continue
        parts.append(value if heading is None else f"**{heading}**\n{value}")
    return "\n\n".join(parts)


def short_description(fields: dict | None, fallback: str | None = None) -> str | None:
    """The one-line description: what the thing is, plus when to use it.

    For a role this becomes the `enum` line the coordinator reads while choosing, so it
    leads with `summary` (what it is) and appends `when_to_use` (why you'd pick it) —
    truncated rather than allowed to crowd out the other roles.
    """
    clean = normalize(fields)
    pieces = [clean.get("summary"), clean.get("when_to_use")]
    text = " ".join(p for p in pieces if p).strip()
    if not text:
        return fallback
    if len(text) > MAX_DESCRIPTION:
        text = text[: MAX_DESCRIPTION - 1].rstrip() + "…"
    return text


def dumps(fields: dict | None) -> str | None:
    """Serialize for the `authoring` column. Empty → NULL, so "hand-written" and
    "authored but blank" stay distinguishable."""
    clean = normalize(fields)
    return json.dumps(clean) if clean else None


def loads(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return normalize(json.loads(raw))
    except (TypeError, ValueError):
        return {}
