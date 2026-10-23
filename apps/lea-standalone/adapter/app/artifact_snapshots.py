"""Canonical immutable artifact/dependency references, shared with historical checks."""
from __future__ import annotations
import hashlib
import re
from . import formalizations, store
from .read_projections import read_projection, current_artifact_snapshots
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
    queued = [module for item in roots for module in _imported_modules(item.get("code") or "")]
    seen_modules: set[str] = set()
    seen_paths = {str(item.get("path") or "") for item in roots}
    dependencies: list[dict] = []
    while queued:
        module = queued.pop(0)
        if module in seen_modules:
            continue
        seen_modules.add(module)
        # Record only actual import lookups, including missing modules. Reading
        # the entire registry would make unrelated artifact publication a
        # dependency of every target observation.
        from . import db
        with db.connect() as conn:
            row = conn.execute('select * from artifacts where scope=? and module_name=? order by updated_at desc,id desc limit 1',
                               (project_id, module)).fetchone()
        artifact = dict(row) if row else None
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
    cache = current_artifact_snapshots()
    # Live and historical assessments in one response must describe the same
    # canonical revision. Discard this reuse when the response ends or retries;
    # attempt drafts and publication never reuse a captured canonical snapshot.
    if run_id is not None or cache is None:
        return _capture_snapshot(formalization_id, run_id=run_id)
    if formalization_id not in cache:
        cache[formalization_id] = _capture_snapshot(formalization_id)
    return cache[formalization_id]


def _capture_snapshot(formalization_id: str, *, run_id: str | None = None) -> tuple[dict, list[dict]] | None:
    if run_id:
        from . import reformalizations
        op = reformalizations.for_run(run_id)
        if op and op['formalization_id'] == formalization_id:
            drafts = reformalizations.drafts(op['id'])
            roots = [{**d, 'id': str(d['id']), 'role': 'generated',
                      'blob_sha256': reformalizations.digest(d['code'])} for d in drafts]
            refs = [{'path': d['path'], 'blob_id': d['blob_id'], 'sha256': d['blob_sha256'],
                     'code_step_id': d['id'], 'role': 'generated', 'artifact_scope': 'attempt',
                     'check_status': (d.get('evidence') or {}).get('status')} for d in roots]
            return {'revision_token': reformalizations.digest(reformalizations.dumps(refs)),
                    'files': refs, 'artifact_scope': 'attempt'}, roots
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


