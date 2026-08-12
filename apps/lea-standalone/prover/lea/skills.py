"""Skills — procedural-knowledge markdown fragments injected into the system prompt.

A skill is just a markdown file (a tactic recipe, a naming convention, a
project's house rules). `agent.skills` in config lists the files to inject, in
order; `load_skills` reads them and returns one block to append to the system
prompt. This is the explicit, config-driven generalization of the implicit
`lea.md` append (which still works — see prompt.load_system_prompt).
"""

from pathlib import Path

from .errors import SkillError

# Directories the AgentSkills standard puts beside an entry point. Their presence is what
# marks a skill as multi-file, and therefore advertised rather than injected.
_RESOURCE_DIRS = ("references", "scripts", "assets")


def _resource_listing(skill_dir: Path) -> list[str]:
    """Relative paths of a multi-file skill's resources, or [] if it has none."""
    found: list[str] = []
    for sub in _RESOURCE_DIRS:
        root = skill_dir / sub
        if not root.is_dir():
            continue
        found.extend(sorted(str(p.relative_to(skill_dir)) for p in root.rglob("*")
                            if p.is_file()))
    return found


def load_skills(paths: list[str]) -> str:
    """Read each skill and return a single block to append to the prompt.

    **Two modes, chosen by what is on disk (v2.5 H3).**

    A single-file skill is INJECTED whole, exactly as before — that is what Lea's own
    small always-on skills are, and nothing about them changes.

    A skill that ships `references/` (or `scripts/`/`assets/`) beside its entry point is
    ADVERTISED instead: the entry point plus a list of its resources and where they live,
    for the agent to `read_file` on demand. This is the AgentSkills progressive-disclosure
    contract, and it is not optional — a real skill's references run to hundreds of KB
    (`lean4-skills` ships ~690 KB across 41 files, some 170k tokens), so concatenating
    them is not a worse choice, it is an impossible one.

    Paths are resolved relative to the current working directory. Returns "" for an empty
    list. Raises SkillError if an entry point is missing or unreadable.
    """
    if not paths:
        return ""
    blocks: list[str] = []
    for p in paths:
        path = Path(p).expanduser()
        try:
            text = path.read_text()
        except OSError as e:
            raise SkillError(f"could not read skill {p!r}: {e}") from e
        name = path.parent.name if path.name.lower() == "skill.md" else path.stem
        resources = _resource_listing(path.parent)
        block = f"## Skill: {name}\n{text.strip()}"
        if resources:
            listing = "\n".join(f"- {path.parent / r}" for r in resources)
            block += (
                f"\n\nThis skill has reference material you can open with `read_file` "
                f"when you need it — do not assume its contents:\n{listing}"
            )
        blocks.append(block)
    return "\n\n" + "\n\n".join(blocks)
