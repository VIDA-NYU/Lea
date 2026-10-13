"""Canonical immutable artifact/dependency references, shared with historical checks."""
from __future__ import annotations
import hashlib
import re
from . import formalizations, store
from .read_projections import read_projection
_IMPORT_LINE = re.compile(r"(?m)^\s*import\s+([^\n-]+)")

def _imported_modules(code: str) -> list[str]:
    modules: list[str] = []
    for match in _IMPORT_LINE.finditer(code or ""):
        for token in match.group(1).split():
            value = token.strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_'.]*(?:\.[A-Za-z_][A-Za-z0-9_']*)*", value):
                modules.append(value)
    return modules


def _transitive_project_dependencies(project_id: str | None, roots: list[dict]) -> list[dict]:
    if not project_id:
        return []
    artifacts = store.list_artifacts_for_scope(project_id)
    by_module = {item.get("module_name"): item for item in artifacts if item.get("module_name")}
    queued = [module for item in roots for module in _imported_modules(item.get("code") or "")]
    seen_modules: set[str] = set()
    seen_paths = {str(item.get("path") or "") for item in roots}
    dependencies: list[dict] = []
    while queued:
        module = queued.pop(0)
        if module in seen_modules:
            continue
        seen_modules.add(module)
        artifact = by_module.get(module)
        if not artifact:
            continue
        path = str(artifact.get("path") or "")
        if not path or path in seen_paths:
            continue
        step = None
        formalization_id = artifact.get("formalization_id")
        if formalization_id:
            step = next(
                (item for item in store.current_code_steps_for_formalization(formalization_id) if item.get("path") == path),
                None,
            )
        if step is None:
            candidates = store.code_steps_for_project_path(project_id, path)
            step = candidates[0] if candidates else None
        if not step:
            continue
        seen_paths.add(path)
        dependency = {**step, "role": "dependency", "module_name": module}
        dependencies.append(dependency)
        queued.extend(_imported_modules(dependency.get("code") or ""))
    return dependencies


@read_projection
def _snapshot(formalization_id: str, *, run_id: str | None = None) -> tuple[dict, list[dict]] | None:
    current = formalizations.current_snapshot(formalization_id)
    if not current or not current.get("revision_token") or not current.get("files"):
        return None
    roots = store.current_code_steps_for_formalization(formalization_id, run_id=run_id) if run_id else current["files"]
    if not roots:
        return None
    revision = formalizations._revision_token(roots)
    files = [*roots, *_transitive_project_dependencies(current.get("project_id"), roots)]
    refs = []
    for item in files:
        refs.append({
            "path": item.get("path") or "",
            "role": item.get("role") or "",
            "code_step_id": str(item.get("id") or ""),
            "blob_id": item.get("blob_id"),
            "sha256": item.get("blob_sha256") or "",
            "commit_sha": item.get("commit_sha") or "",
            "check_status": item.get("check_status"),
            "check_detail": item.get("check_detail"),
        })
    return {
        "revision_token": revision,
        "files": refs,
        "validity_status": current.get("validity_status") if revision == current["revision_token"] else None,
    }, files


def _dependency_hash(snapshot: dict) -> str:
    parts = [
        f"{item['path']}:{item.get('sha256') or item.get('blob_id')}"
        for item in snapshot["files"] if item.get("role") == "dependency"
    ]
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()


