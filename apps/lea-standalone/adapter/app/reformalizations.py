"""Serialized, durable reformalization operations. Proof bytes use artifact_blobs."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from threading import Lock
from uuid import uuid4

from fastapi import HTTPException
from lea import execution
from . import db, store

_actor = ContextVar("reformalization_actor", default=None)
_starting_lock = Lock()
_starting_continuations = set()


def capability():
    enabled = os.environ.get('LEA_REFORMALIZATION_ENABLED', '1').lower() not in {'0', 'false', 'no'}
    supported = execution.capability()['available']
    return {'version': 1, 'staging': supported, 'serialization': 'project',
            'automatic_shared_files': bool(enabled and supported), 'admission_enabled': bool(enabled and supported)}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


@contextmanager
def acting(operation_id):
    token = _actor.set(operation_id)
    try:
        yield
    finally:
        _actor.reset(token)


def actor():
    return _actor.get()


def registered_dependency(initiating_run_id, dependency_id, project_id):
    """Only a recorded consumer edge can bypass its foreground owner."""
    if not initiating_run_id or not dependency_id:
        return False
    with db.connect() as conn:
        return bool(conn.execute("""select 1 from runs r join project_dependency_edges e
            on e.requester_id=r.focus_formalization_id and e.project_id=r.project_id
            where r.id=? and r.project_id=? and e.dependency_id=?""",
            (initiating_run_id, project_id, dependency_id)).fetchone())


def owner(project_id):
    if not project_id:
        return None
    with db.connect() as conn:
        try:
            row = conn.execute("select * from reformalization_operations where project_id=? and state in ('active','applying')",
                               (project_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None  # migrations/startup, not a preservation bypass
            raise
    return dict(row) if row else None


def assert_writer(project_id):
    unsettled = unfinished_application(project_id)
    if unsettled and unsettled['operation_id'] != actor():
        raise HTTPException(409, detail={'error': 'publication_recovery_required',
            'message': 'This project has an interrupted publication to recover before new writes.'})
    current = owner(project_id)
    if current and current["id"] != actor():
        child = get(actor()) if actor() else None
        if child and child['project_id'] == project_id and (child.get('request') or {}).get('_foreground_owner') == current['id']:
            run = store.get_run(child.get('root_run_id')) or {}
            parent = for_run(run.get('initiating_run_id')) if run.get('initiating_run_id') else None
            if (run.get('purpose') == 'project_dependency' and parent
                    and registered_dependency(run.get('initiating_run_id'), run.get('dependency_id'), project_id)
                    and (parent['id'] == current['id'] or parent['request'].get('_foreground_owner') == current['id'])):
                return
        raise HTTPException(409, detail={"error": "project_busy",
            "message": "A reformalization owns this project's mathematics. Wait for it to settle.",
            "reformalization_id": current["id"]})


def unfinished_application(project_id):
    if not project_id:
        return None
    with db.connect() as conn:
        try:
            row = conn.execute("""select a.operation_id from reformalization_applications a
                join reformalization_operations o on o.id=a.operation_id where o.project_id=?
                and a.state in ('prepared','applying','restoring') limit 1""", (project_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                return None
            raise
    return dict(row) if row else None


def assert_readable(project_id):
    unsettled = unfinished_application(project_id)
    if unsettled and unsettled['operation_id'] != actor():
        raise HTTPException(503, detail={'error': 'publication_recovery_required',
            'message': 'Current project views are waiting for publication recovery.'}, headers={'Retry-After': '1'})


def get(operation_id):
    with db.connect() as conn:
        row = conn.execute("select * from reformalization_operations where id=?", (operation_id,)).fetchone()
    if not row:
        return None
    value = dict(row)
    for column, key in (("request_json", "request"), ("manifest_json", "manifest"), ("result_json", "result")):
        value[key] = json.loads(value.pop(column)) if value.get(column) else None
    return value


def for_run(run_id):
    run = store.get_run(run_id)
    return get(run["reformalization_id"]) if run and run.get("reformalization_id") else None


def update(operation_id, state=None, *, manifest=None, result=None):
    fields, values = ["updated_at=?"], [db.utc_now()]
    for column, value in (("state", state), ("manifest_json", dumps(manifest) if manifest is not None else None),
                          ("result_json", dumps(result) if result is not None else None)):
        if value is not None:
            fields.append(column + "=?")
            values.append(value)
    if state == 'queued':
        fields.append('queued_at=?')
        values.append(db.utc_now())
    with db.write() as conn:
        conn.execute("update reformalization_operations set " + ",".join(fields) + " where id=?", (*values, operation_id))


def enqueue(request, project_id):
    if not capability()['admission_enabled']:
        raise HTTPException(503, detail={'error': 'reformalization_unavailable',
            'message': execution.capability().get('reason') or 'New reformalizations are disabled; retained work is available.'})
    if not execution.capability()["available"]:
        raise HTTPException(503, detail={"error": "reformalization_executor_unavailable",
            "message": execution.capability().get("reason") or "The filesystem executor is unavailable."})
    form = store.get_formalization(request.get("focus_formalization_id")) if request.get("focus_formalization_id") else None
    if not form and project_id and (request.get("new_formalization") or {}).get("origin_key"):
        with db.connect() as conn:
            row = conn.execute("select * from formalizations where project_id=? and origin=? and origin_key=?",
                (project_id, request["new_formalization"].get("origin", "overleaf"), request["new_formalization"]["origin_key"])).fetchone()
        form = dict(row) if row else None
    if not form or form.get("project_id") != project_id:
        raise HTTPException(422, "Reformalization requires an existing target in this project.")
    if not store.list_formalization_files(form["id"]):
        raise HTTPException(422, "This target has no current Lean work to reformalize.")
    normalized = {**request, 'focus_formalization_id': form['id'], 'new_formalization': None}
    request_hash = digest(dumps({k: v for k, v in normalized.items() if k not in {'idempotency_key', 'session_id'}}))
    with db.connect() as conn:
        prior = conn.execute('select * from reformalization_operations where request_key=?', (request.get('idempotency_key'),)).fetchone() if request.get('idempotency_key') else None
        duplicate = conn.execute("select * from reformalization_operations where formalization_id=? and request_hash=? and state in ('queued','active','applying') order by rowid desc limit 1", (form['id'], request_hash)).fetchone()
    if prior:
        if prior['request_hash'] != request_hash or (request.get('session_id') and request['session_id'] != prior['session_id']):
            raise HTTPException(409, 'Idempotency key belongs to a different reformalization request')
        return get_after_transaction(dict(prior))
    if duplicate:
        return get_after_transaction(dict(duplicate))
    session_id = request.get("session_id")
    if not session_id:
        session_id = store.create_session("Re-formalize " + form["display_title"], project_id=project_id)["id"]
    session = store.get_session(session_id)
    if not session or session.get("project_id") != project_id:
        raise HTTPException(409, "Conversation belongs to another project.")
    store.link_session_formalization(session_id, form['id'])
    request = {**request, "session_id": session_id, "focus_formalization_id": form["id"],
               "new_formalization": None}
    request_hash = digest(dumps({k: v for k, v in request.items() if k not in {"idempotency_key", "session_id"}}))
    now, operation_id = db.utc_now(), str(uuid4())
    with db.write() as conn:
        key = request.get("idempotency_key")
        old = conn.execute("select * from reformalization_operations where request_key=?", (key,)).fetchone() if key else None
        if old:
            if old["request_hash"] != request_hash:
                raise HTTPException(409, "Idempotency key belongs to another reformalization request.")
            return get_after_transaction(dict(old))
        duplicate = conn.execute("select * from reformalization_operations where formalization_id=? and request_hash=? and state in ('queued','active','applying') order by rowid desc limit 1", (form["id"], request_hash)).fetchone()
        if duplicate:
            return get_after_transaction(dict(duplicate))
        conn.execute("""insert into reformalization_operations(id,project_id,formalization_id,session_id,
            request_key,request_hash,request_json,state,queued_at,created_at,updated_at) values (?,?,?,?,?,?,?,'queued',?,?,?)""",
            (operation_id, project_id, form["id"], session_id, key, request_hash, dumps(request), now, now, now))
    return get(operation_id)


def get_after_transaction(value):
    # Avoid opening a second reader while holding SQLite's write transaction.
    for column, key in (("request_json", "request"), ("manifest_json", "manifest"), ("result_json", "result")):
        value[key] = json.loads(value.pop(column)) if value.get(column) else None
    return value


def response(op):
    awaiting_segment = bool((op.get('result') or {}).get('continuation'))
    run = store.get_run(op.get('root_run_id')) if op.get('root_run_id') and not awaiting_segment else None
    bundle = store.get_run_submission(run['client_request_id']) if run and run.get('client_request_id') else None
    from .routes.runs import _creation_response
    details = _creation_response(bundle) if bundle else {}
    return {**details, "reformalization_id": op["id"], "run_id": None if awaiting_segment else op.get("root_run_id"),
        "session_id": op["session_id"], "focus_formalization_id": op["formalization_id"],
        "project_id": op["project_id"], "state": op["state"], "result": op.get("result"),
        "artifact_scope": "attempt" if op["state"] != "settled" else "canonical"}


def dispatch(project_id):
    """Claim one operation. No pending solver row exists while another owns it."""
    from . import runtime
    if not runtime.accepting_work():
        return
    if unfinished_application(project_id):
        return
    with db.write() as conn:
        if conn.execute("select 1 from reformalization_operations where project_id=? and state in ('active','applying')", (project_id,)).fetchone():
            return
        if conn.execute("select 1 from runs where project_id=? and status in ('pending','running')", (project_id,)).fetchone():
            return
        row = conn.execute("select * from reformalization_operations where project_id=? and state='queued' order by queued_at,rowid limit 1", (project_id,)).fetchone()
        if not row:
            return
        conn.execute("update reformalization_operations set state='active',updated_at=? where id=?", (db.utc_now(), row["id"]))
        operation_id = row["id"]
    op = get(operation_id)
    try:
        if not (op.get('result') or {}).get('approval_id'):
            from . import runregistry
            admitted = runregistry.registry.try_admit(operation_id, op['session_id'], project_id=project_id, reserve=True)
            if admitted.outcome != runregistry.ADMITTED:
                with db.write() as conn:
                    conn.execute("update reformalization_operations set state='queued' where id=?", (operation_id,))
                return
        if (op.get('result') or {}).get('continuation'):
            _dispatch_continuation(op)
            return
        if (op.get('result') or {}).get('approval_id'):
            from .reformalization_review import apply_approved
            from .bridge import Thread
            Thread(target=apply_approved, args=(op,), daemon=True, name='lea-reviewed-apply').start()
            return
        if store.get_formalization(op['formalization_id']).get('origin') == 'overleaf':
            from .bridge import Thread
            Thread(target=_start_run, args=(op,), daemon=True, name='lea-reformalization-source').start()
        else:
            _start_run(op)
    except Exception as exc:
        from . import runregistry
        runregistry.registry.release(operation_id)
        update(operation_id, "retained", result={"outcome": "failed", "detail": str(exc)})


def _start_run(op):
    from .routes.runs import create_run, RunRequest
    from . import overleaf_source, runregistry
    try:
        if get(op['id'])['state'] != 'active':
            runregistry.registry.release(op['id'])
            return
        request = dict(op['request'])
        form = store.get_formalization(op['formalization_id'])
        if form.get('origin') == 'overleaf':
            prepared = asyncio.run(overleaf_source.prepare(form['id']))
            request.update(source_context_ref=prepared['source_context_ref'], source_bundle=None)
        if get(op['id'])['state'] != 'active':
            runregistry.registry.release(op['id'])
            return
        request.update(artifact_operation=None, idempotency_key='reformalization:' + op['id'])
        with acting(op['id']):
            create_run(RunRequest(**request))
    except Exception as exc:
        runregistry.registry.release(op['id'])
        if get(op['id'])['state'] == 'active':
            update(op['id'], 'retained', result={'outcome': 'failed', 'detail': str(exc)})
    finally:
        if get(op['id'])['state'] == 'retained':
            dispatch_all()


def attach_run(operation_id, run_id):
    with db.write() as conn:
        op = conn.execute('select state from reformalization_operations where id=?', (operation_id,)).fetchone()
        if not op or op['state'] not in {'active', 'worker'}:
            return False
        conn.execute("update runs set reformalization_id=? where id=?", (operation_id, run_id))
        conn.execute("update reformalization_operations set root_run_id=?,updated_at=? where id=?",
                     (run_id, db.utc_now(), operation_id))
    from . import runregistry
    runregistry.registry.transfer_reservation(operation_id, run_id)
    return True


def attach_dependency_worker(parent_id, run):
    """A worker has its own stage/journal, under its parent's foreground slot."""
    op_id, now = str(uuid4()), db.utc_now()
    parent = get(parent_id)
    request = {'_foreground_owner': parent['request'].get('_foreground_owner') or parent_id,
               '_parent_operation': parent_id}
    with db.write() as conn:
        conn.execute("""insert into reformalization_operations(id,project_id,formalization_id,session_id,
            request_hash,request_json,state,root_run_id,created_at,updated_at) values (?,?,?,?,?,?,'worker',?,?,?)""",
            (op_id, run['project_id'], run['focus_formalization_id'], run['session_id'], digest(dumps(request)), dumps(request), run['id'], now, now))
        conn.execute('update runs set reformalization_id=? where id=?', (op_id, run['id']))
    return get(op_id)


def workspace_policy(workspace):
    workspace = Path(workspace).resolve()
    temporary = workspace / ".executor-tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    return {"writable_roots": [str(workspace)], "temporary_root": str(temporary)}


def canonical_policy(root, repo):
    root, repo = Path(root).resolve(), Path(repo).resolve()
    scratch = repo / ".lea/inspection"
    scratch.mkdir(parents=True, exist_ok=True)
    temporary = repo / ".lea/executor-tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    roots = [root / ".lake/build", root / ".lake/config", root / ".sv_scratch", scratch, temporary]
    return {"writable_roots": list(map(str, roots)), 'writable_files': [str(root / '.lake')],
            "temporary_root": str(temporary)}


def lean_files(repo):
    from .project_library import project_paths
    return {p.relative_to(repo).as_posix(): p for p in project_paths(Path(repo))}


def prepare(operation_id):
    from .project_library import project_repo, lake_root
    from lea.result_selection import inspect_file
    op = get(operation_id)
    if op["manifest"]:
        return op["manifest"]
    project = store.get_project(op["project_id"])
    canonical = project_repo(project)
    original_root = lake_root(canonical)
    if not original_root:
        raise ValueError("The project has no enclosing Lake workspace")
    base = db.DB_PATH.parent / "reformalizations" / operation_id
    staged_root = base / "workspace"
    staged = staged_root / canonical.relative_to(original_root)
    staged.mkdir(parents=True, exist_ok=True)
    for name in ("lakefile.lean", "lakefile.toml", "lake-manifest.json", "lean-toolchain"):
        if (original_root / name).is_file():
            shutil.copy2(original_root / name, staged_root / name)
    (staged_root / ".lake").mkdir(exist_ok=True)
    packages = original_root / ".lake/packages"
    if packages.exists():
        # The executor makes this dependency mount read-only, including build
        # outputs. Local project outputs are built in our own .lake/build.
        (staged_root / ".lake/packages").symlink_to(packages.resolve(), target_is_directory=True)
    source_hashes = {}
    for source in (original_root / "proofs").rglob("*.lean"):
        if source.is_symlink() or any(part.startswith(".") for part in source.relative_to(original_root / "proofs").parts):
            continue
        rel = source.relative_to(original_root)
        target = staged_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        source_hashes[str(rel)] = digest(source.read_bytes())
    if (canonical / ".lea").exists():
        shutil.copytree(canonical / ".lea", staged / ".lea", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("inspection", "executor-tmp", "tmp", "operations"))
    files = {}
    with db.write() as conn:
        for rel, path in lean_files(canonical).items():
            content = path.read_bytes().decode('utf-8')
            files[rel] = {"blob_id": store._put_blob(conn, content), "sha256": digest(content),
                          "mode": path.stat().st_mode & 0o777,
                          'provenance': {'kind': 'working_tree', 'captured_at': db.utc_now(),
                                         'mtime_ns': path.stat().st_mtime_ns}}
    project_sources = {str((canonical / p).relative_to(original_root)): info['sha256'] for p, info in files.items()}
    manifest = {"version": 1, "canonical_repo": str(canonical), "canonical_root": str(original_root),
        "workspace": str(staged_root), "repo": str(staged), "files": files, "source_hashes": project_sources,
        'dependency_files': {},
        "configuration": {n: digest((original_root / n).read_bytes()) for n in
            ("lakefile.lean", "lakefile.toml", "lake-manifest.json", "lean-toolchain") if (original_root / n).is_file()},
        "associations": store.list_formalization_files(op["formalization_id"]), "namespace": project["namespace"],
        "source_hash": store.get_formalization(op["formalization_id"]).get("source_hash"), "reports": {}, "checks": {}}
    form = store.get_formalization(op['formalization_id'])
    manifest['target_identity'] = {k: form.get(k) for k in ('declaration_name', 'kind', 'statement', 'source_hash', 'origin', 'origin_key')}
    from .gitstore import GitStore
    gs = GitStore(canonical.parent)
    index = []
    for entry in gs._git(canonical, 'ls-files', '--stage', '-z').split('\0'):
        if not entry:
            continue
        metadata, path = entry.split('\t', 1)
        if path not in files:
            continue
        mode, git_blob, stage_number = metadata.split()
        import subprocess
        content = subprocess.run(['git', 'cat-file', '-p', git_blob], cwd=canonical,
            capture_output=True, check=True).stdout.decode('utf-8')
        with db.write() as conn:
            blob = store._put_blob(conn, content)
        index.append({'path': path, 'mode': mode, 'stage': stage_number, 'blob_id': blob})
    manifest['index_before'] = index
    with db.connect() as conn:
        artifact = conn.execute("select source_hash,run_id from artifacts where formalization_id=? order by updated_at desc limit 1", (op["formalization_id"],)).fetchone()
        previous_context = conn.execute('select source_bundle_json from lea_status_run_contexts where run_id=?', (artifact['run_id'],)).fetchone() if artifact else None
        current_context = conn.execute('select source_bundle_json from lea_status_run_contexts where run_id=?', (op['root_run_id'],)).fetchone()
    manifest["previous_source_hash"] = artifact["source_hash"] if artifact else manifest["source_hash"]
    manifest['source_update'] = None
    if previous_context and current_context:
        previous_source, current_source = json.loads(previous_context[0]), json.loads(current_context[0])
        old_statement, new_statement = previous_source.get('statement'), current_source.get('statement')
        if old_statement and new_statement and old_statement != new_statement:
            manifest['source_update'] = {'previous_statement': old_statement, 'current_statement': new_statement,
                'previous_source_hash': previous_source['sourceIdentityHash'], 'current_source_hash': current_source['sourceIdentityHash']}
    explicit = op['request'].get('source_update_evidence')
    if explicit:
        form = store.get_formalization(op['formalization_id'])
        if (not explicit.get('previous_statement') or explicit.get('current_statement') != form.get('statement')
                or explicit.get('previous_statement') == explicit.get('current_statement')):
            raise ValueError('Source-update evidence must identify the changed current statement')
        manifest['source_update'] = {**explicit, 'current_source_hash': manifest['source_hash'],
                                     'previous_source_hash': manifest['previous_source_hash']}
    from .recovery import _dependency_fingerprints
    manifest['dependencies'] = _dependency_fingerprints(original_root)
    policy = {**workspace_policy(staged_root), "canonical_repo": str(canonical), "attempt_repo": str(staged)}
    with execution.scope(policy):
        from lea.interface import check
        for rel in files:
            checked = check(str(staged / rel), cold=True)
            manifest["checks"][rel] = {"status": checked.status, "detail": checked.detail}
            for evidence in ((checked.execution or {}).get('environment') or {}).get('files', {}).values():
                path = Path(evidence['path']).resolve()
                if path.is_relative_to(staged_root):
                    if evidence.get('kind') == 'source' and path.is_relative_to(staged_root / 'proofs'):
                        key = str(path.relative_to(staged_root))
                        manifest['source_hashes'][key] = evidence['sha256']
                else:
                    manifest['dependency_files'][str(path)] = evidence['sha256']
            try:
                manifest["reports"][rel] = inspect_file(staged / rel, allow_errors=True)
            except ValueError:
                manifest["reports"][rel] = None
    for rel, expected in manifest['source_hashes'].items():
        if digest((original_root / rel).read_bytes()) != expected or digest((staged_root / rel).read_bytes()) != expected:
            raise ValueError("Project source changed while capturing this retry")
    update(operation_id, manifest=manifest)
    return manifest


def retained_context(op):
    """Expose prior progress as advisory text, never as current project source."""
    with db.connect() as conn:
        previous = [r[0] for r in conn.execute("""select id from reformalization_operations
            where formalization_id=? and id<>? and state in ('retained','paused','review')
            order by rowid desc limit 3""", (op['formalization_id'], op['id']))]
    paths = []
    for operation_id in previous:
        for draft in drafts(operation_id):
            relative = Path(draft['path'])
            if relative.is_absolute() or '..' in relative.parts:
                continue
            target = Path(op['manifest']['repo']) / '.lea/retained-drafts' / operation_id / (str(relative) + '.txt')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(draft['code'].encode())
            paths.append(str(target))
    return paths


def fresh(op, *, applied=None):
    """Check the captured effective inputs, including new/deleted source paths."""
    from .project_library import lake_root, project_repo
    manifest = op["manifest"]
    project = store.get_project(op["project_id"])
    repo, root = Path(manifest["canonical_repo"]), Path(manifest["canonical_root"])
    if project["namespace"] != manifest["namespace"] or project_repo(project) != repo or lake_root(repo) != root:
        raise ValueError("Project identity changed during reformalization")
    if store.list_formalization_files(op["formalization_id"]) != manifest["associations"]:
        raise ValueError("Target file associations changed during reformalization")
    if store.get_formalization(op["formalization_id"]).get("source_hash") != manifest["source_hash"]:
        raise ValueError("The target source changed during reformalization")
    form = store.get_formalization(op['formalization_id'])
    if any(form.get(k) != value for k, value in manifest.get('target_identity', {}).items()):
        raise ValueError('The requested target identity or statement changed during reformalization')
    for name, expected in manifest["configuration"].items():
        if not (root / name).is_file() or digest((root / name).read_bytes()) != expected:
            raise ValueError("Lake configuration changed during reformalization")
    from .recovery import _dependency_fingerprints
    if _dependency_fingerprints(root) != manifest.get('dependencies', {}):
        raise ValueError('The installed compiler/dependency context changed during reformalization')
    actual = {str(p.relative_to(root)): digest(p.read_bytes()) for p in lean_files(repo).values()}
    for rel in manifest['source_hashes']:
        path = root / rel
        if not path.is_relative_to(repo):
            actual[rel] = digest(path.read_bytes()) if path.is_file() else None
    from lea.check_evidence import file_hash
    for path, expected_hash in manifest.get('dependency_files', {}).items():
        if file_hash(Path(path)) != expected_hash:
            raise ValueError('An installed compiler/dependency input changed during reformalization')
    expected = dict(manifest["source_hashes"])
    for rel, content in (applied or {}).items():
        key = str((repo / rel).relative_to(root))
        if content is None:
            expected.pop(key, None)
        else:
            expected[key] = digest(content)
    if actual != expected:
        raise ValueError("Current Lean sources changed; the candidate has been retained")


def save_draft(operation_id, run_id, path, content, evidence=None):
    now = db.utc_now()
    with db.write() as conn:
        blob = store._put_blob(conn, content)
        cursor = conn.execute("insert into reformalization_drafts(operation_id,run_id,path,blob_id,evidence_json,created_at) values (?,?,?,?,?,?)",
            (operation_id, run_id, path, blob, dumps(evidence) if evidence else None, now))
    return {"id": f"draft:{cursor.lastrowid}", "operation_id": operation_id, "run_id": run_id,
            "path": path, "blob_id": blob, "code": content, "artifact_scope": "attempt",
            "created_at": now, "check_status": (evidence or {}).get("status"), "evidence": evidence}


def drafts(operation_id):
    with db.connect() as conn:
        rows = conn.execute("""select d.*,b.content as code from reformalization_drafts d join artifact_blobs b on b.id=d.blob_id
            where d.operation_id=? and d.id in (select max(id) from reformalization_drafts where operation_id=? group by path)
            order by d.path""", (operation_id, operation_id)).fetchall()
    op = get(operation_id)
    before = (op.get('manifest') or {}).get('files', {})
    return [{**dict(r), 'id': 'draft:' + str(r['id']), 'author': 'agent',
        'session_id': op['session_id'], 'formalization_id': op['formalization_id'],
        'baseline_blob_id': before.get(r['path'], {}).get('blob_id'),
        "artifact_scope": "attempt", "evidence": json.loads(r["evidence_json"]) if r["evidence_json"] else None} for r in rows]


def versions(operation_id):
    op = get(operation_id)
    if not op:
        raise HTTPException(404, 'Reformalization not found')
    items = [{'path': p, 'kind': 'baseline', 'blob_id': info['blob_id']}
             for p, info in (op.get('manifest') or {}).get('files', {}).items()]
    items.extend({**d, 'kind': 'index'} for d in (op.get('manifest') or {}).get('index_before', []))
    with db.connect() as conn:
        journals = [json.loads(r[0]) for r in conn.execute('select manifest_json from reformalization_applications where operation_id=?', (operation_id,))]
        items.extend({'path': r['path'], 'kind': 'draft', 'blob_id': r['blob_id']} for r in conn.execute('select path,blob_id from reformalization_drafts where operation_id=?', (operation_id,)))
    for journal in journals:
        for path, info in journal['files'].items():
            for field in ('before_blob', 'after_blob', 'displaced_blob', 'conflicting_blob', 'rollback_blob'):
                if info.get(field):
                    items.append({'path': path, 'kind': field, 'blob_id': info[field]})
    return items


def cancel(operation_id):
    op = get(operation_id)
    if not op:
        raise HTTPException(404, "Reformalization not found")
    run = store.get_run(op['root_run_id']) if op['root_run_id'] else None
    if run and run['status'] in {'pending', 'running'} and op["state"] in {"active", "applying", 'worker'}:
        from .bridge import request_stop
        request_stop(op["root_run_id"], reason="user_stop", source="reformalization")
    else:
        update(operation_id, "retained", result={"outcome": "cancelled"})
        from . import runregistry
        runregistry.registry.release(operation_id)
        dispatch_all()
    return response(get(operation_id))


def recover_operations():
    """Retain non-dispatchable owners after journal/run recovery, without billing."""
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("select id,root_run_id from reformalization_operations where state in ('active','applying')")]
    for row in rows:
        run = store.get_run(row['root_run_id']) if row['root_run_id'] else None
        if not run or (run['status'] not in {'pending', 'running'} and run.get('stop_reason') != 'dependency_wait'):
            update(row['id'], 'retained', result={'outcome': 'interrupted', 'detail': 'The interrupted attempt is retained; no solver was restarted.'})


def dispatch_all():
    with db.connect() as conn:
        projects = [r[0] for r in conn.execute("select distinct project_id from reformalization_operations where state='queued'")]
    for project_id in projects:
        dispatch(project_id)
    with db.connect() as conn:
        resumable = [r[0] for r in conn.execute("select id from reformalization_operations where state in ('active','worker') and result_json like '%\"continuation\"%'")]
    for operation_id in resumable:
        wake(operation_id)


def wake(operation_id):
    op = get(operation_id)
    if op['state'] in {'active', 'worker'} and (op.get('result') or {}).get('continuation'):
        _dispatch_continuation(op)
    else:
        dispatch(op['project_id'])


def queue_continuation(op, old, request, *, recovery_review=False):
    """Reacquire FIFO ownership before creating another accounting segment."""
    request_hash = digest(dumps({'operation_id': op['id'], 'old_run_id': old['id'], 'request': request}))
    previous = store.get_run_submission(request.get('idempotency_key')) if request.get('idempotency_key') else None
    if previous:
        if previous['run'].get('client_request_hash') != request_hash:
            raise HTTPException(409, 'Continuation identity was reused with different inputs')
        from .routes.runs import _creation_response
        return {**response(op), **_creation_response(previous)}
    waiting = (op.get('result') or {}).get('continuation')
    if waiting and waiting['request'].get('idempotency_key') == request.get('idempotency_key'):
        if waiting['request'] != request or waiting['old_run_id'] != old['id']:
            raise HTTPException(409, 'Continuation identity was reused with different inputs')
        return response(op)
    if old['id'] != op['root_run_id']:
        raise HTTPException(409, 'A newer segment of this attempt already exists')
    if op['state'] not in {'paused', 'active', 'worker', 'retained'}:
        raise HTTPException(409, 'This attempt is not paused for continuation')
    if not old.get('recoverable') or old['status'] not in {'cancelled', 'max_turns'}:
        raise HTTPException(409, 'This run is not a settled recoverable pause')
    if request.get('best_effort') and old.get('stop_reason') != 'source_obstruction':
        raise HTTPException(409, 'Best-effort continuation requires a source-obstruction pause')
    fresh(op)
    from . import recovery
    state, _, checkpoint_id = recovery.continuation_checkpoint(old['id'])
    source = None
    if request.get('source_context_ref'):
        from . import overleaf_source
        with db.connect() as conn:
            source = overleaf_source.resolve(conn, request['source_context_ref'], op['formalization_id'], bind_source=False)
    elif request.get('source_bundle'):
        from .source_context import validate_source
        source = validate_source(request['source_bundle'])
    if source and source['sourceIdentityHash'] != op['manifest']['source_hash']:
        raise HTTPException(409, 'The source changed. The paused draft is retained; start a new reformalization against the current source.')
    if request.get('best_effort'):
        from . import lea_status_store
        prior_context = lea_status_store.context(old['id'])
        effective_source = source or (prior_context or {}).get('source_bundle') or {}
        if (str(effective_source.get('proof') or '').strip()
                or effective_source.get('proofAssociation', {}).get('status') not in {'missing', 'not_applicable'}):
            raise HTTPException(409, 'Best-effort continuation requires a proofless source-obstruction pause')
    payload = {'old_run_id': old['id'], 'checkpoint_id': checkpoint_id,
        'request': request, 'source': source, 'saved_turn': int(state.get('turn', 0))}
    result = {**(op.get('result') or {}), 'continuation': payload}
    dependent_worker = bool(op['request'].get('_foreground_owner')
        and (owner(op['project_id']) or {}).get('id') == op['request']['_foreground_owner']
        and registered_dependency(old.get('initiating_run_id'), old.get('dependency_id'), op['project_id']))
    if ((request.get('dependency_resume') and op['state'] in {'active', 'worker'} and old.get('stop_reason') == 'dependency_wait')
            or dependent_worker):
        from .project_dependencies import get as dependency
        if any(not (dependency(key) or {}).get('ready') for key in json.loads(old.get('waiting_dependencies_json') or '[]')):
            raise HTTPException(409, 'The shared dependencies are not verified yet')
        update(op['id'], 'worker' if dependent_worker else None, result=result)
        _dispatch_continuation(get(op['id']))
        return response(get(op['id']))
    update(op['id'], 'queued', result=result)
    dispatch(op['project_id'])
    return response(get(op['id']))


def _dispatch_continuation(op):
    with _starting_lock:
        if op['id'] in _starting_continuations:
            return
        _starting_continuations.add(op['id'])
    if store.get_formalization(op['formalization_id']).get('origin') == 'overleaf':
        from .bridge import Thread
        Thread(target=_continue_reserved, args=(op,), daemon=True, name='lea-reformalization-continue').start()
    else:
        _continue_reserved(op)


def _continue_reserved(op):
    from . import runregistry
    try:
        with acting(op['id']):
            _start_continuation(op)
    except Exception as exc:
        runregistry.registry.release(op['id'])
        if get(op['id'])['state'] in {'active', 'worker'}:
            update(op['id'], 'retained', result={**op['result'], 'outcome': 'conflict', 'detail': str(exc)})
    finally:
        with _starting_lock:
            _starting_continuations.discard(op['id'])
        if get(op['id'])['state'] == 'retained':
            dispatch_all()


def _start_continuation(op):
    from . import recovery, bridge, lea_status_store
    from .config import load_config
    from datetime import timedelta
    from . import runregistry
    if get(op['id'])['state'] not in {'active', 'worker'}:
        runregistry.registry.release(op['id'])
        return
    fresh(op)
    if not runregistry.registry.is_active(op['id']):
        admitted = runregistry.registry.try_admit(op['id'], op['session_id'], project_id=op['project_id'], reserve=True)
        if admitted.outcome != runregistry.ADMITTED:
            return
    saved = op['result']['continuation']
    old = store.get_run(saved['old_run_id'])
    request = saved['request']
    config = load_config()
    from . import settings
    if settings.spend_limit_reached(config.max_spend_usd):
        raise ValueError('Max spend limit reached; the paused draft is retained')
    automatic = request.get('dependency_resume', False)
    if automatic:
        from .project_dependencies import get as dependency
        if any(not (dependency(key) or {}).get('ready') for key in json.loads(old.get('waiting_dependencies_json') or '[]')):
            raise ValueError('The requested dependencies are not ready')
    budget = json.loads(old.get('automation_budget_json') or '{}')
    context = lea_status_store.context(old['id'])
    source = saved['source'] or (context['source_bundle'] if context else None)
    source_ref = request.get('source_context_ref')
    if store.get_formalization(op['formalization_id']).get('origin') == 'overleaf':
        from . import overleaf_source
        prepared = asyncio.run(overleaf_source.prepare(op['formalization_id']))
        source_ref = prepared['source_context_ref']
        with db.connect() as conn:
            source = overleaf_source.resolve(conn, source_ref, op['formalization_id'], bind_source=False)
        if source['sourceIdentityHash'] != op['manifest']['source_hash']:
            raise ValueError('The source changed while this continuation waited; the draft is retained')
    if get(op['id'])['state'] not in {'active', 'worker'}:
        runregistry.registry.release(op['id'])
        return
    allowance = old.get('max_turns') if automatic else (saved['saved_turn'] + config.max_turns if config.max_turns else None)
    seconds = budget.get('remaining_seconds', 900) if automatic else request.get('timeout_seconds') or 900
    if seconds <= 0 or (allowance is not None and allowance <= saved['saved_turn']):
        raise ValueError('The original allowance is exhausted; no automatic solver was restarted')
    bundle = store.create_run_bundle(message='Continue the retained staged reformalization from its saved execution point.',
        session_id=op['session_id'], project_id=op['project_id'], session_origin='ui', session_origin_url=None,
        model=old['model'], provider=old.get('provider'), max_turns=allowance, autonomous=bool(old['autonomous']),
        focus_formalization_id=op['formalization_id'], focus_source_hash=old.get('focus_source_hash'),
        purpose=old['purpose'], source_bundle=source, source_context_ref=source_ref,
        allow_source_pause=old.get('allow_source_pause', False) if request.get('allow_source_pause') is None else request['allow_source_pause'],
        client_request_id=request['idempotency_key'],
        client_request_hash=digest(dumps({'operation_id': op['id'], 'old_run_id': old['id'], 'request': request})),
        resumes_run_id=old['id'], resume_checkpoint_id=saved['checkpoint_id'],
        continuation_best_effort=request.get('best_effort', False), dependency_id=old.get('dependency_id'),
        initiating_run_id=old.get('initiating_run_id'), automation_budget=budget if automatic else None,
        deadline_at=(datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat())
    if not attach_run(op['id'], bundle['run']['id']):
        store.update_run(bundle['run']['id'], 'cancelled', result_kind='interrupted', cost_usd=0, input_tokens=0, output_tokens=0, usage_status='final')
        raise ValueError('This continuation was cancelled before dispatch')
    # Transfer existing registered obligations to the resumed accounting segment.
    with db.write() as conn:
        conn.execute('update project_dependencies set owner_run_id=? where owner_run_id=?', (bundle['run']['id'], old['id']))
    update(op['id'], result={k: v for k, v in op['result'].items() if k != 'continuation'})
    bridge.enqueue_run(bundle['run']['id'])
