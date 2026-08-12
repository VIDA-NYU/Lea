"""Translate a third-party agent's tool names into Lea's (v2.5 H6).

Roles in the wild are written for whatever harness their author used. `lean4-skills`
ships four sub-agents whose `tools:` read:

    Read, Grep, Glob, Edit, Bash, mcp__lean-lsp__lean_goal, mcp__lean-lsp__lean_loogle, …

Lea's built-ins are `read_file` / `edit_file` / `bash`, and `lea.mcp` registers MCP tools
under their **bare** names (`lean_goal`), so an imported list names tools that do not
exist here. Before B4 that was fatal — `compose_child_tools` raised and the spawn died.
It now soft-drops, but a role that quietly loses two thirds of its toolset is not much
better than one that fails loudly. So names are translated at IMPORT, once, where the
result is inspectable, rather than being silently discarded on every spawn.

Anything unmapped is REPORTED, never invented: guessing that `Glob` means `bash` would
hand a read-only role a shell.
"""

from __future__ import annotations

import re

# Claude Code / OpenHands names → Lea's. Only where the meaning is genuinely the same.
ALIASES: dict[str, str] = {
    "read": "read_file",
    "read_file": "read_file",
    "view": "read_file",
    "edit": "edit_file",
    "edit_file": "edit_file",
    "str_replace_editor": "edit_file",
    "write": "write_file",
    "write_file": "write_file",
    "create": "write_file",
    "bash": "bash",
    "shell": "bash",
    "run_command": "bash",
}

# `mcp__<server>__<tool>` — Lea exposes MCP tools bare, prefixing only on a clash.
_MCP_PREFIX = re.compile(r"^mcp__[^_]+(?:_[^_]+)*?__(?P<tool>.+)$")

# Names with no Lea equivalent that are NOT worth reporting: the author meant "find
# files", which `search_mathlib` and `bash` already cover between them, and warning about
# every one of these would bury the names that actually matter.
_KNOWN_ABSENT = {"glob", "grep", "ls", "todowrite", "task", "webfetch", "websearch"}


def translate(names: list[str] | None) -> tuple[list[str], list[str]]:
    """`(lea_names, unmapped)` for a foreign tool list.

    Order and duplicates are normalized away; `unmapped` carries the names a human should
    look at, with the incidental ones filtered out.
    """
    out: list[str] = []
    unmapped: list[str] = []
    for raw in names or []:
        name = str(raw).strip()
        if not name:
            continue
        match = _MCP_PREFIX.match(name)
        if match:
            # An MCP tool: keep the bare name. Whether that server is configured is a
            # separate question, answered at spawn by B4's soft-drop.
            candidate = match.group("tool")
        else:
            candidate = ALIASES.get(name.lower(), "")
            if not candidate:
                if name.lower() not in _KNOWN_ABSENT:
                    unmapped.append(name)
                continue
        if candidate not in out:
            out.append(candidate)
    return out, unmapped
