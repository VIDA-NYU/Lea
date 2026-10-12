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


_SEGMENT = r"(?:[^\W\d][\w']*|«[^»\n]+»)"
_MODULE = re.compile(_SEGMENT + r"(?:\." + _SEGMENT + r")*")


def module_parts(name: str) -> list[str]:
    return [part[1:-1] if part.startswith('«') else part for part in re.findall(r"«[^»]+»|[^.]+", name)]


def direct_imports(code: str) -> list[str]:
    """Real imports, including Unicode and Lean's quoted identifier segments."""
    modules = []
    for match in IMPORT_COMMAND_RE.finditer(without_comments(code)):
        header = match.group('modules')
        cursor = 0
        for module in _MODULE.finditer(header):
            if header[cursor:module.start()].strip():
                break
            modules.append(module.group())
            cursor = module.end()
    return list(dict.fromkeys(modules))
