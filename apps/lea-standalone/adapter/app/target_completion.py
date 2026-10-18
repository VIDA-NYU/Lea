"""Conservative completion evidence for a named formalization target."""

from __future__ import annotations

from .artifacts import (LeanDeclaration, declaration_contains_sorry, scan_lean_declarations,
                        declaration_signature, declaration_type)


def compiler_result(step, name, kind=None):
    """None denotes legacy evidence; False denotes a scoped failed selection."""
    import json
    import hashlib
    from .db import connect
    from lea.result_selection import ALLOWED_AXIOMS
    execution = step.get('execution') or (step.get('evidence') or {}).get('execution')
    if execution is None and step.get('check_evidence_id'):
        with connect() as conn:
            row = conn.execute('select evidence_json from proof_check_evidence where id=?', (step['check_evidence_id'],)).fetchone()
        execution = json.loads(row[0]) if row else {}
    execution = execution or {}
    if 'selected_results' not in execution and 'selected_result' not in execution:
        return None
    code = step.get('code') or step.get('blob_content') or ''
    if execution.get('content_sha256') != hashlib.sha256(code.encode()).hexdigest():
        return False
    results = execution.get('selected_results', [execution.get('selected_result')])
    matches = [d for d in results if d and (d['user_name'] == name or d['full_name'] == name or
               ('.' not in name and d['user_name'].rsplit('.', 1)[-1] == name))]
    if len(matches) != 1:
        return False
    selected = matches[0]
    if set(selected.get('axioms', [])) - ALLOWED_AXIOMS:
        return False
    if kind == 'definition' and selected['kind'] == 'theorem':
        return False
    if kind and kind != 'definition' and selected['kind'] != 'theorem':
        return False
    return selected


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


def checked_target(formalization: dict, steps: list[dict], *, require_current=True) -> tuple[dict | None, str]:
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
        confirmed = declaration_type(formalization.get('statement') or '')
        actual = declaration_type(declaration_signature(declaration) or '')
        if confirmed and confirmed != actual:
            return None, 'The checked declaration differs from the confirmed Lean statement.'
        if step.get("check_status") != "ok" or (require_current and not step.get("check_current")):
            return None, "The requested declaration has no passing Lean check."
        compiler = compiler_result(step, name, formalization.get('kind') or 'theorem')
        if compiler is False or (confirmed and compiler is None):
            return None, 'The requested declaration has no passing compiler trust evidence.'
        if compiler is None and declaration_contains_sorry(code, declaration.full_name):
            return None, "The requested declaration still uses sorry or admit."
        return step, ""
    return None, f"The checked artifact does not declare the requested {name}."
