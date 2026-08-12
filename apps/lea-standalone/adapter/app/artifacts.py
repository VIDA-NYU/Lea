"""Lean artifact shape classification for UI/result labels.

This is intentionally shallow: Lean itself owns correctness via ``lean_check``.
The classifier only decides whether a checked file should be displayed as a
theorem proof or as a definition-oriented artifact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_BLOCK_COMMENT_RE = re.compile(r"/-[\s\S]*?-/")
_LINE_COMMENT_RE = re.compile(r"--.*$", re.MULTILINE)
_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')

_PROOF_DECL_RE = re.compile(
    r"(?m)^\s*(?:private\s+|protected\s+|noncomputable\s+|unsafe\s+)*"
    r"(?:theorem|lemma|example)\b"
)
_DEFINITION_DECL_RE = re.compile(
    r"(?m)^\s*(?:private\s+|protected\s+|noncomputable\s+|unsafe\s+|partial\s+)*"
    r"(?:def|abbrev|structure|class|inductive|coinductive|instance|opaque)\b"
)


def classify_lean_artifact(code: str | None) -> str:
    """Return ``proof``, ``definition``, ``mixed``, or ``unknown`` for Lean code."""
    if not code or not code.strip():
        return "unknown"
    scrubbed = _scrub_comments_and_strings(code)
    has_proof = bool(_PROOF_DECL_RE.search(scrubbed))
    has_definition = bool(_DEFINITION_DECL_RE.search(scrubbed))
    if has_proof and has_definition:
        return "mixed"
    if has_proof:
        return "proof"
    if has_definition:
        return "definition"
    return "unknown"


def _scrub_comments_and_strings(code: str) -> str:
    without_comments = _LINE_COMMENT_RE.sub(" ", _BLOCK_COMMENT_RE.sub(" ", code))
    return _STRING_RE.sub('""', without_comments)


_NAMED_DECL_RE = re.compile(
    r"(?m)^\s*(?:private\s+|protected\s+|noncomputable\s+|unsafe\s+|partial\s+)*"
    r"(?:theorem|lemma|def|abbrev|structure|class|inductive|coinductive|instance|opaque)\s+"
    r"([A-Za-z_][A-Za-z0-9_']*)"
)

_DECLARATION_RE = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)?"
    r"(?:private\s+|protected\s+|noncomputable\s+|unsafe\s+|partial\s+|"
    r"scoped\s+|local\s+)*"
    r"(theorem|lemma|def|abbrev|structure|class|inductive|coinductive|instance|opaque)\s+"
    r"([A-Za-z_][A-Za-z0-9_'.]*)"
)
_NAMESPACE_RE = re.compile(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_'.]*)")
_SECTION_RE = re.compile(r"^\s*section(?:\s+([A-Za-z_][A-Za-z0-9_'.]*))?\s*$")
_MUTUAL_RE = re.compile(r"^\s*mutual\s*$")
_END_RE = re.compile(r"^\s*end\b\s*([A-Za-z_][A-Za-z0-9_'.]*)?")


@dataclass(frozen=True)
class LeanDeclaration:
    """A declaration and its source span in one Lean module.

    Lines are one-based and inclusive. ``span`` runs from the declaration header
    through the line before the next declaration (or EOF). This is intentionally a
    lexical index rather than a Lean parser; correctness still belongs to Lean.
    """

    short_name: str
    full_name: str
    keyword: str
    kind: str
    start_line: int
    end_line: int
    span: str


def scrub_lean_source(code: str) -> str:
    """Blank comments and strings while preserving line/column positions.

    Unlike the old regex helper this handles nested Lean block comments. Keeping
    newlines makes declaration spans line up with the original source.
    """

    out: list[str] = []
    index = 0
    block_depth = 0
    in_string = False
    while index < len(code):
        char = code[index]
        nxt = code[index + 1] if index + 1 < len(code) else ""
        if block_depth:
            if char == "/" and nxt == "-":
                block_depth += 1
                out.extend((" ", " "))
                index += 2
            elif char == "-" and nxt == "/":
                block_depth -= 1
                out.extend((" ", " "))
                index += 2
            else:
                out.append("\n" if char == "\n" else " ")
                index += 1
            continue
        if in_string:
            if char == "\\" and nxt:
                out.extend((" ", "\n" if nxt == "\n" else " "))
                index += 2
            elif char == '"':
                in_string = False
                out.append(" ")
                index += 1
            else:
                out.append("\n" if char == "\n" else " ")
                index += 1
            continue
        if char == "/" and nxt == "-":
            block_depth = 1
            out.extend((" ", " "))
            index += 2
        elif char == "-" and nxt == "-":
            out.extend((" ", " "))
            index += 2
            while index < len(code) and code[index] != "\n":
                out.append(" ")
                index += 1
        elif char == '"':
            in_string = True
            out.append(" ")
            index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _declaration_kind(keyword: str) -> str:
    if keyword in {"theorem", "lemma"}:
        return keyword
    return "definition"


def scan_lean_declarations(code: str) -> list[LeanDeclaration]:
    """Return all named top-level declarations with deterministic namespace FQNs."""

    if not code:
        return []
    original_lines = code.splitlines()
    scrubbed_lines = scrub_lean_source(code).splitlines()
    scope_stack: list[tuple[str, str | None]] = []
    found: list[tuple[int, str, str, str]] = []
    for index, line in enumerate(scrubbed_lines):
        namespace = _NAMESPACE_RE.match(line)
        if namespace:
            scope_stack.append(("namespace", namespace.group(1)))
            continue
        section = _SECTION_RE.match(line)
        if section:
            scope_stack.append(("section", section.group(1)))
            continue
        if _MUTUAL_RE.match(line):
            scope_stack.append(("mutual", None))
            continue
        if _END_RE.match(line):
            if scope_stack:
                scope_stack.pop()
            continue
        declaration = _DECLARATION_RE.match(line)
        if declaration:
            keyword, name = declaration.groups()
            namespaces = [value for kind, value in scope_stack if kind == "namespace" and value]
            full_name = name if "." in name else ".".join([*namespaces, name])
            found.append((index, keyword, name.rsplit(".", 1)[-1], full_name))

    result: list[LeanDeclaration] = []
    for position, (start, keyword, short_name, full_name) in enumerate(found):
        next_start = found[position + 1][0] if position + 1 < len(found) else len(original_lines)
        end = max(start, next_start - 1)
        result.append(
            LeanDeclaration(
                short_name=short_name,
                full_name=full_name,
                keyword=keyword,
                kind=_declaration_kind(keyword),
                start_line=start + 1,
                end_line=end + 1,
                span="\n".join(original_lines[start:next_start]),
            )
        )
    return result


def extract_declaration_name(code: str | None) -> str | None:
    """The first top-level declaration's name, or None.

    Recorded proof files are one-declaration-per-file by convention, so "first
    declaration" is "the declaration". Used by the run finalizer to write the
    structured artifact index (PLAN-system-hardening 4.1) — the durable answer
    to "which declaration lives in which file" that clients previously had to
    reverse-engineer from filesystem diffs."""
    if not code:
        return None
    declarations = scan_lean_declarations(code)
    return declarations[0].short_name if declarations else None


_SORRY_MARKER_RE = re.compile(r"\b(sorry|admit)\b")


def contains_sorry_marker(code: str | None) -> bool:
    """True when the code still leans on sorry/admit — comments and strings
    scrubbed first, so prose like `-- no sorry here` doesn't count. The
    ledger-side twin of the companion's containsSorryMarker (PLAN 4.4): status
    verdicts move adapter-side, so the scan lives here too."""
    if not code:
        return False
    return bool(_SORRY_MARKER_RE.search(scrub_lean_source(code)))


def declaration_present(code: str | None, name: str) -> bool:
    """Whether a top-level declaration with this exact name exists in the code
    (comments/strings scrubbed)."""
    if not code or not name:
        return False
    return any(
        declaration.full_name == name or declaration.short_name == name
        for declaration in scan_lean_declarations(code)
    )


def declaration_contains_sorry(code: str | None, name: str) -> bool:
    """Whether the named declaration's own lexical span uses ``sorry``/``admit``.

    Exact FQN matches win. A short name is accepted only when unambiguous. If a
    legacy row cannot be resolved, retain the conservative whole-file behavior.
    """

    if not code:
        return False
    declarations = scan_lean_declarations(code)
    exact = [declaration for declaration in declarations if declaration.full_name == name]
    candidates = exact or [
        declaration for declaration in declarations if declaration.short_name == name
    ]
    if len(candidates) == 1:
        return contains_sorry_marker(candidates[0].span)
    return contains_sorry_marker(code)
