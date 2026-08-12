"""Lean import-command parsing shared by tooling and SafeVerify target derivation."""

from __future__ import annotations

import re

IMPORT_COMMAND_RE = re.compile(
    r"(?m)^[ \t]*(?:(?:public|private|protected|meta)\s+)*import[ \t]+"
    r"(?P<modules>[^\n]+?)[ \t]*$"
)


def without_comments(code: str) -> str:
    """Blank Lean line/block comments while preserving text positions and newlines.

    Lean block comments nest. A line-oriented regex alone would mistake a commented
    `import Mathlib` for a real command, which is especially undesirable when
    deriving SafeVerify's trusted target prelude.
    """
    chars = list(code)
    i = 0
    block_depth = 0
    line_comment = False
    while i < len(chars):
        if line_comment:
            if chars[i] == "\n":
                line_comment = False
            else:
                chars[i] = " "
            i += 1
            continue
        if block_depth:
            if i + 1 < len(chars) and chars[i] == "/" and chars[i + 1] == "-":
                chars[i] = chars[i + 1] = " "
                block_depth += 1
                i += 2
            elif i + 1 < len(chars) and chars[i] == "-" and chars[i + 1] == "/":
                chars[i] = chars[i + 1] = " "
                block_depth -= 1
                i += 2
            else:
                if chars[i] != "\n":
                    chars[i] = " "
                i += 1
            continue
        if i + 1 < len(chars) and chars[i] == "-" and chars[i + 1] == "-":
            chars[i] = chars[i + 1] = " "
            line_comment = True
            i += 2
        elif i + 1 < len(chars) and chars[i] == "/" and chars[i + 1] == "-":
            chars[i] = chars[i + 1] = " "
            block_depth = 1
            i += 2
        else:
            i += 1
    return "".join(chars)


def direct_imports(code: str) -> list[str]:
    """Return direct module names from real Lean import commands, preserving order."""
    modules: list[str] = []
    for match in IMPORT_COMMAND_RE.finditer(without_comments(code)):
        for module in match.group("modules").split():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_'.]*", module):
                modules.append(module)
    return list(dict.fromkeys(modules))
