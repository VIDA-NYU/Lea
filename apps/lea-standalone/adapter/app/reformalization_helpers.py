"""Owner-local helper readiness and checked independent subpublications."""
from __future__ import annotations

from pathlib import Path
import shutil

from lea import execution, interface
from lea.result_selection import inspect_file, select
from . import db, store, project_dependencies, project_library
from . import reformalizations as operations
from .reformalization_scope import bounds


def readiness(operation_id, run_id, path):
    op = operations.get(operation_id)
    # Resolve imports in the staged tree, while the canonical registry remains
    # authoritative for other owners' declarations.
    repo = Path(op['manifest']['repo'])
    canonical = Path(op['manifest']['canonical_repo'])
    mapped = path
    if path:
        p = Path(path).resolve()
        if p.is_relative_to(repo):
            mapped = str(canonical / p.relative_to(repo))
    result = project_dependencies.readiness(run_id, mapped)
    with db.connect() as conn:
        owned = [dict(r) for r in conn.execute('select * from project_dependencies where owner_run_id=?', (run_id,))]
    for dep in owned:
        candidate = repo / dep['path']
        if not candidate.is_file():
            continue
        try:
            with execution.scope(operations.workspace_policy(op['manifest']['workspace'])):
                checked = interface.check(str(candidate), cold=True)
                chosen = select(inspect_file(candidate), dep['declaration_name'])
            if checked.status != 'ok' or project_library.semantic_key(chosen) != dep['semantic_key']:
                continue
            # This checked local obligation may satisfy its owner. It is not
            # globally ready until a canonical publication has been accepted.
            result['dependency_ids'] = [d for d in result['dependency_ids'] if d != dep['id']]
            result['evidence'][dep['id']] = {'ready': False, 'owner_local': True,
                'content_hash': chosen['content_sha256'], 'declaration': chosen['user_name']}
        except (ValueError, OSError):
            continue
    return result


def publish(operation_id, run_id, relative):
    """Try only a helper closure against the unchanged canonical target.

    A helper which cannot independently build remains local to the owner. The
    final application can settle the target and helper together.
    """
    with db.connect() as conn:
        dep = conn.execute('select * from project_dependencies where owner_run_id=? and path=?', (run_id, relative)).fetchone()
    if not dep:
        return None
    op = operations.get(operation_id)
    stage, canonical = Path(op['manifest']['repo']), Path(op['manifest']['canonical_repo'])
    try:
        with execution.scope(operations.workspace_policy(op['manifest']['workspace'])):
            report = inspect_file(stage / relative)
            selected = select(report, dep['declaration_name'])
            if project_library.semantic_key(selected) != dep['semantic_key']:
                return None
        # Publication works on an independently materialized helper-only copy.
        # Never temporarily replace the parent's active draft under tool threads.
        from .reformalization_apply import publish_helper
        return publish_helper(op, dict(dep), report, selected)
    except (ValueError, OSError):
        return {'outcome': 'owner_local', 'dependency_id': dep['id'], 'globally_ready': False}


def refresh_parent(child_id):
    child = operations.get(child_id)
    with db.connect() as conn:
        rows = conn.execute("select manifest_json from reformalization_applications where operation_id=? and state='published' order by created_at", (child_id,)).fetchall()
    parent_id = child['request'].get('_parent_operation') or child['request'].get('_foreground_owner')
    visited = {child_id}
    while parent_id and parent_id not in visited:
        visited.add(parent_id)
        parent = operations.get(parent_id)
        if not parent:
            break
        if parent['manifest']:
            _refresh(parent, rows)
        parent_id = parent['request'].get('_parent_operation') or parent['request'].get('_foreground_owner')


def _refresh(parent, rows):
    parent_id = parent['id']
    import json
    baseline = parent['manifest']
    for row in rows:
        receipt = json.loads(row[0])
        for rel, entry in receipt['files'].items():
            content = store.blob_content(entry['after_blob'])
            if content is None:
                raise ValueError('A dependency publication unexpectedly deleted its module')
            target = Path(baseline['repo']) / rel
            if target.exists():
                current = target.read_bytes().decode('utf-8')
                before = store.blob_content(entry['before_blob'])
                if current != before and current != content:
                    # Retain the draft first, then refresh only the checked helper's
                    # compiler command inside that draft, leaving target edits intact.
                    operations.save_draft(parent_id, parent['root_run_id'], rel, current)
                    name = receipt['selection']['declaration']
                    with execution.scope(operations.workspace_policy(baseline['workspace'])):
                        draft_report = inspect_file(target, allow_errors=True)
                    from .reformalization_scope import resolve
                    prior = resolve(draft_report, name)
                    published = resolve(receipt['reports'][rel], name)
                    lo, hi = bounds(current, prior)
                    nlo, nhi = bounds(content, published)
                    current = current[:lo] + content[nlo:nhi] + current[hi:]
                    target.write_text(current)
                elif current != content:
                    target.write_text(content)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            baseline['files'][rel] = {'blob_id': entry['after_blob'], 'sha256': operations.digest(content), 'mode': entry['mode']}
            key = str((Path(baseline['canonical_repo']) / rel).relative_to(Path(baseline['canonical_root'])))
            baseline['source_hashes'][key] = operations.digest(content)
            baseline['reports'][rel] = receipt['reports'][rel]
            baseline['checks'][rel] = {'status': receipt['checks'][rel]['status']}
    operations.update(parent_id, manifest=baseline)
