"""Conservative completion evidence for a named formalization target."""

from __future__ import annotations

from .artifacts import LeanDeclaration, declaration_contains_sorry, scan_lean_declarations


def target_declaration(code: str | None, name: str | None, kind: str) -> LeanDeclaration | None:
    """Prefer an exact qualified name; accept an unambiguous short name only."""
    if not code or not name:
        return None
    declarations = scan_lean_declarations(code)
    matches = ([item for item in declarations if item.full_name == name]
               if "." in name else
               [item for item in declarations if item.short_name == name])
    if len(matches) != 1:
        return None
    item = matches[0]
    if kind == "definition" and item.kind != "definition":
        return None
    if kind != "definition" and item.kind not in {"theorem", "lemma"}:
        return None
    return item


def checked_target(formalization: dict, steps: list[dict]) -> tuple[dict | None, str]:
    """Find a passing snapshot of the requested declaration, not a helper."""
    name = formalization.get("declaration_name")
    if not name:
        return None, "The requested declaration has no established Lean name."
    candidates = []
    for step in steps:
        code = step.get("code") or step.get("blob_content") or ""
        declaration = target_declaration(code, name, formalization.get("kind") or "theorem")
        if declaration is None:
            continue
        candidates.append((step, code, declaration))
    if len(candidates) > 1:
        return None, f"The requested declaration {name} is ambiguous across artifacts."
    if candidates:
        step, code, declaration = candidates[0]
        if step.get("check_status") != "ok":
            return None, "The requested declaration has no passing Lean check."
        if declaration_contains_sorry(code, declaration.full_name):
            return None, "The requested declaration still uses sorry or admit."
        return step, ""
    return None, f"The checked artifact does not declare the requested {name}."
