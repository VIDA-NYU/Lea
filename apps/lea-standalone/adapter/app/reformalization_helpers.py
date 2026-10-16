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
        rows = conn.execute("select manifest_json,id from reformalization_applications where operation_id=? and state='published' order by created_at", (child_id,)).fetchall()
    parent_id = child['request'].get('_parent_operation') or child['request'].get('_foreground_owner')
    visited = {child_id}
    while parent_id and parent_id not in visited:
        visited.add(parent_id)
        parent = operations.get(parent_id)
        if not parent:
            break
        if parent['manifest'] and parent['state'] != 'settled':
            try:
                _refresh(parent, rows)
            except (ValueError, OSError) as exc:
                # The helper is already durably published. Retain an incompatible
                # parent draft without undoing that independently accepted work.
                operations.update(parent_id, 'retained', result={**(parent.get('result') or {}),
                    'outcome': 'conflict', 'detail': 'The helper was published; the parent draft needs refresh: ' + str(exc)})
        parent_id = parent['request'].get('_parent_operation') or parent['request'].get('_foreground_owner')


def _refresh(parent, rows):
    parent_id = parent['id']
    import json
    baseline = parent['manifest']
    applied = set(baseline.get('helper_applications', []))
    for row in rows:
        if row['id'] in applied:
            continue
        receipt = json.loads(row['manifest_json'])
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
        # A helper can introduce an import that the parent's original target did
        # not use. Refresh exactly the accepted compiler inputs, never another
        # project's whole tree or a solver-modified dependency copy.
        root, repo = Path(baseline['canonical_root']), Path(baseline['canonical_repo'])
        inputs = {e['path']: e for check in receipt['checks'].values()
                  for e in (check.get('execution', {}).get('environment') or {}).get('files', {}).values()}
        for raw, evidence in inputs.items():
            source = Path(raw).resolve()
            if evidence.get('kind') == 'source' and source.is_relative_to(root / 'proofs') and not source.is_relative_to(repo):
                content = source.read_bytes()
                if operations.digest(content) != evidence['sha256']:
                    raise ValueError('An imported helper source changed before parent refresh')
                target = Path(baseline['workspace']) / source.relative_to(root)
                if target.exists() and target.read_bytes() != content:
                    raise ValueError('A parent dependency copy differs from the published helper inputs')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                baseline['source_hashes'][str(source.relative_to(root))] = evidence['sha256']
            elif not source.is_relative_to(root) or source.is_relative_to(root / '.lake/packages'):
                baseline['dependency_files'][str(source)] = evidence['sha256']
        applied.add(row['id'])
        baseline['helper_applications'] = sorted(applied)
        operations.update(parent_id, manifest=baseline)
