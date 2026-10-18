"""Canonical shared obligations, one writer, durable failures, and publication.

Execution is through ordinary adapter runs. The Overleaf companion decides which
ready node to run; this registry is authoritative across companions and restarts.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

from lea.imports import direct_imports
from . import project_library as library, store
from .db import connect, write, utc_now

FAILURES = {"failed", "needs_review", "blocked"}
TRANSIENT_STOPS = {"user_stop", "transport_abandonment", "interrupted", "timeout", "stalled"}


def readiness(run_id: str, path: str | None = None) -> dict:
    """Resolve canonical dependencies from actual imports and durable waits only.

    This does not retry a dependency or dispatch a model. The existing companion
    coordinator observes the wait and owns dependency scheduling.
    """
    from hashlib import sha256
    run = store.get_run(run_id)
    session = store.get_session(run["session_id"]) if run else None
    project = store.get_project(session["project_id"]) if session and session.get("project_id") else None
    if not project:
        return {"dependency_ids": [], "evidence": {}}
    repo = library.project_repo(project).resolve()
    root = (library.lake_root(repo) or repo).resolve()
    with connect() as conn:
        registered = [dict(r) for r in conn.execute(
            "select id,module_name,path,formalization_id from project_dependencies where project_id=?", (project["id"],))]
    own = run.get("focus_formalization_id")
    by_module = {r["module_name"]: r for r in registered if r["formalization_id"] != own}
    awaited = run.get("waiting_dependencies")
    if awaited is None:
        awaited = json.loads(run.get("waiting_dependencies_json") or "[]")
    required = set(awaited)
    pending = [Path(path).resolve()] if path else [
        (repo / f["path"]).resolve() for f in store.list_formalization_files(own)] if own else []
    seen, imports = set(), {}
    while pending:
        file = pending.pop()
        if file in seen or not file.is_relative_to(root):
            continue
        seen.add(file)
        try:
            code = file.read_text()
        except OSError:
            continue
        for module in direct_imports(code):
            if module in by_module:
                imported = repo / by_module[module]["path"]
            elif root == repo and module.startswith(project["namespace"] + "."):
                imported = repo.joinpath(*module[len(project["namespace"]) + 1:].split(".")).with_suffix(".lean")
            else:
                imported = root.joinpath(*module.split(".")).with_suffix(".lean")
            try:
                imports[module] = sha256(imported.read_bytes()).hexdigest()
            except OSError:
                imports[module] = None
            if module in by_module:
                required.add(by_module[module]["id"])
            if imported.is_file():
                pending.append(imported)
    valid = {r["id"] for r in registered if r["formalization_id"] != own}
    required &= valid
    evidence, unavailable = {}, []
    for dep_id in sorted(required):
        dep = get(dep_id)
        evidence[dep_id] = {"ready": bool(dep.get("ready")), "state": dep["state"],
                            "source_hash": dep["source_hash"], "verification": {
                                k: (dep.get("result") or {}).get(k) for k in
                                ("content_hash", "check_status", "verification_status", "reusable")}}
        if not dep.get("ready"):
            unavailable.append(dep_id)
    with write() as conn:
        for dep_id in required:
            if own:
                row = next(r for r in registered if r["id"] == dep_id)
                if _cycle(conn, own, row["formalization_id"]):
                    raise ValueError("Canonical dependency import cycle requires review")
                conn.execute("insert or ignore into project_dependency_edges values (?,?,?,?)",
                             (project["id"], own, dep_id, utc_now()))
        conn.execute("update runs set waiting_dependencies_json=? where id=?",
                     (library.dumps(unavailable), run_id))
    environment = {}
    for name in ("lakefile.lean", "lakefile.toml", "lake-manifest.json", "lean-toolchain"):
        try:
            environment[name] = sha256((root / name).read_bytes()).hexdigest()
        except OSError:
            environment[name] = None
    return {"dependency_ids": unavailable, "evidence": {"dependencies": evidence, "imports": imports, "environment": environment}}


def _decode(row):
    if row is None:
        return None
    result = dict(row)
    for column, key in (("specification_json", "specification"), ("blocker_json", "blocker"), ("result_json", "result")):
        result[key] = json.loads(result.pop(column)) if result.get(column) else None
    return result


def get(dependency_id: str, *, reconcile: bool = True, validate: bool = True) -> dict | None:
    """Project readiness from current evidence; readers can defer compilation.

    Status projections use ``validate=False``: filesystem freshness remains
    authoritative, but rendering a status must not rebuild the project library.
    Explicit dependency coordination retains validation of changed modules.
    """
    with connect() as conn:
        row = conn.execute("select * from project_dependencies where id=?", (dependency_id,)).fetchone()
        if not row:
            return None
        attempts = [dict(r) for r in conn.execute("""select a.*,r.session_id from project_dependency_attempts a
            left join runs r on r.id=a.run_id where a.dependency_id=? order by generation""", (dependency_id,))]
        consumers = [r[0] for r in conn.execute("select requester_id from project_dependency_edges where dependency_id=? order by requester_id", (dependency_id,))]
    result = _decode(row)
    result.update(attempts=attempts, requesting_targets=consumers)
    if reconcile:
        project = store.get_project(result["project_id"])
        if project:
            records = library.refresh(project) if validate else library.entries(project)
            if validate and result["state"] == "ready" and not result["owner_run_id"]:
                # Recheck changed bytes/environment, using the module cache.
                # This refreshes evidence without creating another attempt.
                try:
                    library.validate_module(project, result["path"])
                except (ValueError, OSError):
                    # Missing/removed modules have no current certification.
                    pass
                records = library.entries(project)
            match = next((d for d in records if d["path"] == result["path"]
                          and d["user_name"] == result["declaration_name"]
                          and d.get("semantic_key") == result["semantic_key"] and d["reusable"]), None)
            result["ready"] = match is not None
            if match:
                result["state"] = "ready"
                result["result"] = match
            elif result["state"] == "ready":
                result["state"] = "stale"
        else:
            result["ready"] = False
    return result


def list_dependencies(project_id: str, requester_id: str | None = None, *, validate: bool = True) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("""select d.id from project_dependencies d where d.project_id=? and
            (? is null or exists(select 1 from project_dependency_edges e where e.dependency_id=d.id and e.requester_id=?))
            order by d.created_at,d.id""", (project_id, requester_id, requester_id)).fetchall()
    return [get(r[0], validate=validate) for r in rows]


def _slug(value, fallback="Result"):
    words = re.findall(r"[A-Za-z0-9]+", str(value))
    return ("".join(word[:1].upper() + word[1:] for word in words)[:60] or fallback)


def _cycle(conn, requester, dep_fid):
    if requester == dep_fid:
        return True
    edges = {}
    for row in conn.execute("""select e.requester_id,d.formalization_id from project_dependency_edges e
                               join project_dependencies d on d.id=e.dependency_id"""):
        edges.setdefault(row[0], []).append(row[1])
    # Source annotations are already durable in the ordinary run contexts.
    # Keep only each target's newest revision so removed annotations disappear.
    latest = {}
    forms = [dict(row) for row in conn.execute("select id,project_id,origin_key,declaration_name from formalizations")]
    for row in conn.execute("select formalization_id,source_bundle_json from lea_status_run_contexts order by created_at,run_generation"):
        latest[row[0]] = json.loads(row[1])
    by_id = {form["id"]: form for form in forms}
    for fid, source in latest.items():
        owner = by_id.get(fid)
        if not owner:
            continue
        for used in source.get("uses", []):
            label = used if isinstance(used, str) else used.get("targetKey", used.get("label"))
            for form in forms:
                if form["project_id"] == owner["project_id"] and label and (
                        (form["origin_key"] or "").endswith(":" + label) or form["declaration_name"] == label):
                    edges.setdefault(fid, []).append(form["id"])
    stack, seen = [dep_fid], set()
    while stack:
        current = stack.pop()
        if current == requester:
            return True
        if current not in seen:
            seen.add(current)
            stack.extend(edges.get(current, []))
    return False


def _check_import_cycle(project, requester, imports):
    if not requester:
        return
    repo = library.project_repo(project)
    root = library.lake_root(repo)
    target_paths = {str((repo / item["path"]).resolve()) for item in store.list_formalization_files(requester)}
    seen = set()
    def visit(module):
        if module in seen:
            return
        seen.add(module)
        if root:
            path = root / "proofs" / (module.replace(".", "/") + ".lean")
        elif module.startswith(project["namespace"] + "."):
            path = repo / (module[len(project["namespace"]) + 1:].replace(".", "/") + ".lean")
        else:
            return
        if str(path.resolve()) in target_paths:
            raise ValueError("A shared dependency cannot import the unfinished target it must unblock.")
        if path.is_file():
            for imported in direct_imports(path.read_text()):
                visit(imported)
    for imported in imports:
        visit(imported)


def _reject_primary_target(project, specification, targets):
    """A focused target is already tracked; it cannot reserve itself as a helper.

    Compare declaration identities, not types: distinct supporting results can
    legitimately have the same signature or cite the same source passage.
    """
    name = specification.get("name")
    if not isinstance(name, str) or not name.strip():
        return

    def qualified(value):
        value = value.strip().removeprefix("_root_.")
        return value if "." in value else project["namespace"] + "." + value

    requested = qualified(name)
    for target in targets:
        if not target:
            continue
        names = [target.get("declaration_name")]
        # A manually renamed Overleaf target retains its original document label.
        if target.get("origin") == "overleaf" and target.get("origin_key"):
            names.append(target["origin_key"].rsplit(":", 1)[-1])
        if not any(value and requested == qualified(value) for value in names):
            continue
        paths = [item["path"] for item in store.list_formalization_files(target["id"])]
        location = (" Use its existing associated file(s): " + ", ".join(paths) + ".") if paths else (
            " Write it directly in the ordinary target file in this project's workspace.")
        raise ValueError(
            "The requested dependency is the current formalization target, not a supporting result. "
            "It is already tracked and must not receive a second shared reservation. "
            "Keep the target declaration name " + (target.get("declaration_name") or name) + "." + location +
            " Use request_project_dependency only for distinct supporting mathematics.")


def ensure(project: dict, specification: dict, *, run_id: str | None = None,
           requester_id: str | None = None, mode: str = "deferred") -> dict:
    if mode not in {"inline", "deferred"}:
        raise ValueError("Execution mode must be inline or deferred.")
    run = store.get_run(run_id) if run_id else None
    if run and run.get("project_id") != project["id"]:
        raise ValueError("The requesting run belongs to another project.")
    if run_id and not run:
        raise ValueError("The requesting run does not exist.")
    focus = store.get_formalization(run["focus_formalization_id"]) if run and run.get("focus_formalization_id") else None
    requester_id = requester_id or (run.get("focus_formalization_id") if run else None)
    requester = None
    if requester_id:
        requester = store.get_formalization(requester_id)
        if not requester or requester.get("project_id") != project["id"]:
            raise ValueError("The requesting target belongs to another project.")
    _reject_primary_target(project, specification, [focus, requester])
    if mode == "inline" and (not run or run["status"] != "running"):
        raise ValueError("Inline ownership requires an active requesting run.")
    spec = {key: specification.get(key) for key in
            ("name", "kind", "statement", "signature", "body", "imports", "sources", "citation", "notes")}
    spec["kind"] = spec["kind"] or "theorem"
    spec["imports"] = spec["imports"] or []
    spec["sources"] = spec["sources"] or []
    if not isinstance(spec["imports"], list) or not isinstance(spec["sources"], list):
        raise ValueError("Imports and sources must be lists.")
    if any(not isinstance(item, str) for item in spec["imports"]):
        raise ValueError("Imports must be Lean module names.")
    if spec["kind"] not in {"theorem", "definition"} or any(
            spec[key] is not None and not isinstance(spec[key], str) for key in ("name", "statement", "signature", "body", "notes")):
        raise ValueError("Supply a theorem or definition with textual mathematical specifications.")
    if spec["citation"] is not None and (not isinstance(spec["citation"], dict)
            or not isinstance(spec["citation"].get("key"), str) or not spec["citation"]["key"].strip()):
        raise ValueError("A cited result requires a citation key and optional result locator.")
    if len(library.dumps(spec).encode()) > 65536:
        raise ValueError("Dependency specification exceeds 64 KiB.")
    _check_import_cycle(project, requester_id, spec["imports"])
    mathematics_hash = library.specification_environment(project, spec["imports"])
    source_hash = library.digest({"sources": spec["sources"], "citation": spec["citation"], "statement": spec["statement"],
        "mathematics": mathematics_hash})
    if not spec["signature"]:
        inspected = None
        key = "source-gap:" + source_hash
    else:
        inspected = library.inspect_specification(project, spec)
        key = library.semantic_key(inspected)
        spec["expected"] = inspected
    records = library.refresh(project)
    existing = next((d for d in sorted(records, key=lambda d: (d["path"], d["full_name"]))
                     if d.get("semantic_key") == key and d["reusable"]), None)
    adopted = None
    if not existing and inspected is not None and spec.get('name'):
        names = [d for d in records if d['user_name'] == spec['name'] or
                 ('.' not in spec['name'] and d['user_name'].rsplit('.', 1)[-1] == spec['name'])]
        if len(names) > 1:
            raise ValueError('An existing unfinished helper name is ambiguous; use its qualified declaration name.')
        if names:
            declarations = library.validate_module(project, names[0]['path'])
            candidate = next((d for d in declarations if d['user_name'] == names[0]['user_name']), None)
            if candidate and 'sorryAx' in (candidate.get('axioms') or []) and candidate.get('visibility') == 'public':
                if candidate.get('semantic_key') != key:
                    raise ValueError('The existing unfinished helper has a different pinned mathematical contract.')
                adopted = candidate
    now, dep_id, fid = utc_now(), str(uuid4()), str(uuid4())
    label = _slug(spec["name"] or "SharedResult")
    suffix = key[-12:]
    relative = f"Shared/{label}_{suffix}.lean"
    if spec["citation"]:
        citation_key = spec["citation"].get("key", "Citation") if isinstance(spec["citation"], dict) else spec["citation"]
        relative = f"External/{_slug(citation_key)}/{label}_{suffix}.lean"
    declaration = project["namespace"] + "." + label[:1].lower() + label[1:] + "_" + suffix
    if existing or adopted:
        relative, declaration = (existing or adopted)["path"], (existing or adopted)["user_name"]
    state = "ready" if existing else "blocked" if inspected is None else "inline" if mode == "inline" else "pending"
    blocker = {"category": "source_gap", "detail": "Available sources do not specify a precise Lean obligation. Supply its statement and hypotheses before retrying."} if inspected is None else None
    with write() as conn:
        old = conn.execute("select * from project_dependencies where project_id=? and semantic_key=?", (project["id"], key)).fetchone()
        if old:
            dep_id, fid = old["id"], old["formalization_id"]
            # Relevant evidence admits a new failed attempt, never interrupts a writer.
            if old["state"] in FAILURES and old["source_hash"] != source_hash and not old["owner_run_id"]:
                _next_attempt(conn, old, spec, source_hash, now)
            current = conn.execute("select * from project_dependencies where id=?", (dep_id,)).fetchone()
            if mode == "inline" and current["state"] == "pending" and not current["owner_run_id"]:
                conn.execute("update project_dependencies set state='inline',owner_run_id=?,updated_at=? where id=?", (run_id, now, dep_id))
                conn.execute("update project_dependency_attempts set state='inline',run_id=?,initiating_run_id=coalesce(initiating_run_id,?),updated_at=? where dependency_id=? and generation=?", (run_id, run_id, now, dep_id, current["generation"]))
            # An inline attempt can hand its drafts to a deferred worker.
            if mode == "deferred" and old["owner_run_id"] == run_id and old["state"] == "inline":
                previous = json.loads(old["specification_json"])
                notes = [note for note in (previous.get("notes"), spec.get("notes")) if note]
                previous["notes"] = "\n\n".join(dict.fromkeys(notes)) or None
                if spec["sources"]:
                    previous["sources"] = spec["sources"]
                revised_hash = library.digest({"sources": previous["sources"], "citation": previous["citation"],
                    "statement": previous["statement"], "mathematics": mathematics_hash})
                conn.execute("update project_dependencies set state='pending',owner_run_id=null,specification_json=?,source_hash=?,updated_at=? where id=?",
                             (library.dumps(previous), revised_hash, now, dep_id))
                conn.execute("update formalizations set source_hash=?,updated_at=? where id=?", (revised_hash, now, fid))
                conn.execute("update project_dependency_attempts set run_id=null,state='pending',updated_at=? where dependency_id=? and generation=?", (now, dep_id, old["generation"]))
        else:
            historical = conn.execute("select id from formalizations where project_id=? and declaration_name=?",
                                      (project["id"], declaration)).fetchone() if existing or adopted else None
            if historical:
                fid = historical["id"]
            else:
                conn.execute("""insert into formalizations
                    (id,project_id,display_title,declaration_name,kind,statement,origin,origin_key,source_hash,created_at,updated_at)
                    values (?,?,?,?,?,?,'project_dependency',?,?,?,?)""",
                    (fid, project["id"], str(spec["name"] or label)[:160], declaration, spec["kind"],
                     spec["statement"] or spec["signature"], key, source_hash, now, now))
            conn.execute("""insert into project_dependencies
                (id,project_id,semantic_key,formalization_id,specification_json,source_hash,path,module_name,
                 declaration_name,state,owner_run_id,blocker_json,result_json,created_at,updated_at)
                values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (dep_id, project["id"], key, fid, library.dumps(spec), source_hash, relative,
                 library.module_name(project, relative), declaration, state,
                 run_id if state == "inline" else None, library.dumps(blocker) if blocker else None,
                 library.dumps(existing) if existing else None, now, now))
            conn.execute("""insert into project_dependency_attempts
                (dependency_id,generation,run_id,initiating_run_id,state,created_at,updated_at) values (?,1,?,?,?,?,?)""",
                (dep_id, run_id if state == "inline" else None, run_id if state == "inline" else None, state, now, now))
            conn.execute("""insert or ignore into formalization_files(formalization_id,path,role,created_at,updated_at)
                            values (?,?,'primary',?,?)""", (fid, relative, now, now))
        if requester_id:
            if _cycle(conn, requester_id, fid):
                raise ValueError("This dependency would create a cycle in the formalization graph.")
            conn.execute("insert or ignore into project_dependency_edges values (?,?,?,?)", (project["id"], requester_id, dep_id, now))
    if run:
        store.link_session_formalization(run["session_id"], fid)
    dep = get(dep_id)
    # Revalidate stale bytes before allocating another mathematical proof attempt.
    if dep["state"] == "stale":
        library.validate_module(project, dep["path"])
        dep = get(dep_id)
    return response(dep, run_id=run_id)


def response(dep: dict, *, run_id: str | None = None) -> dict:
    state = dep["state"]
    outcome = "existing" if dep.get("ready") else "blocked" if state in FAILURES or state == "stale" else "inline" if state == "inline" and dep["owner_run_id"] == run_id else "deferred"
    spec = dep["specification"]
    namespace = dep["declaration_name"].rsplit(".", 1)[0]
    name = dep["declaration_name"].rsplit(".", 1)[1]
    imports = "\n".join(f"import {module}" for module in spec.get("imports", []))
    skeleton = None
    if spec.get("signature"):
        body = spec["body"] if spec["kind"] == "definition" else "by\n  sorry"
        skeleton = f"{imports}\n\nnamespace {namespace}\n\n{'noncomputable def' if spec['kind'] == 'definition' else 'theorem'} {name} {spec['signature']} := {body}\n\nend {namespace}\n"
    return {"outcome": outcome, "dependency": dep, "dependency_id": dep["id"],
        "path": dep["path"], "module": dep["module_name"], "namespace": namespace,
        "declaration_name": dep["declaration_name"], "import_statement": "import " + dep["module_name"],
        "skeleton": skeleton, "stop_reason": "dependency_wait" if outcome in {"deferred", "blocked"} else None}


def _next_attempt(conn, old, spec, source_hash, now):
    generation = old["generation"] + 1
    conn.execute("""update project_dependencies set generation=?,state='pending',owner_run_id=null,
        blocker_json=null,result_json=null,specification_json=?,source_hash=?,updated_at=? where id=?""",
        (generation, library.dumps(spec), source_hash, now, old["id"]))
    conn.execute("insert into project_dependency_attempts(dependency_id,generation,state,created_at,updated_at) values (?,?,'pending',?,?)", (old["id"], generation, now, now))
    # Explicit retry or a relevant source revision may also recover a checker
    # outage. Normal searches retain failed checks; a new attempt rechecks once.
    conn.execute("update project_library_modules set build_status='unchecked',verify_status=null,detail=null where project_id=? and path=?",
                 (old["project_id"], old["path"]))


def retry(dependency_id: str) -> dict:
    dep = get(dependency_id)
    if not dep:
        raise LookupError("Dependency not found.")
    if dep.get("ready"):
        return response(dep)
    with write() as conn:
        old = conn.execute("select * from project_dependencies where id=?", (dependency_id,)).fetchone()
        if old["owner_run_id"] or old["state"] in {"pending", "running", "inline", "waiting"}:
            raise ValueError("This dependency already has an active attempt.")
        if not dep["specification"].get("signature"):
            raise ValueError("Supply a precise statement before retrying this source blocker.")
        _next_attempt(conn, old, dep["specification"], dep["source_hash"], utc_now())
    return response(get(dependency_id))


def admit_worker(conn, dependency_id: str, project_id: str, run_id: str, resumes_run_id: str | None = None,
                 initiating_run_id: str | None = None):
    if initiating_run_id:
        initiator = conn.execute("select project_id from runs where id=?", (initiating_run_id,)).fetchone()
        if not initiator or initiator[0] != project_id:
            raise ValueError("The initiating run must belong to this project.")
    dep = conn.execute("select * from project_dependencies where id=? and project_id=?", (dependency_id, project_id)).fetchone()
    continuation = bool(dep and resumes_run_id and dep["state"] == "waiting" and dep["owner_run_id"] == resumes_run_id)
    if dep and resumes_run_id and not continuation and dep['state'] in {'interrupted', 'failed'} and not dep['owner_run_id']:
        prior = conn.execute("""select r.status,r.recoverable from project_dependency_attempts a
            join runs r on r.id=a.run_id where a.dependency_id=? and a.generation=? and a.run_id=?""",
            (dependency_id, dep['generation'], resumes_run_id)).fetchone()
        continuation = bool(prior and prior['recoverable'] and prior['status'] in {'cancelled', 'max_turns'})
    if not dep or (not continuation and (dep["state"] not in {"pending", "interrupted"} or dep["owner_run_id"])):
        raise ValueError("Dependency is absent, blocked, ready, or already owned by another attempt.")
    conn.execute("update project_dependencies set state='running',owner_run_id=?,updated_at=? where id=?", (run_id, utc_now(), dependency_id))
    conn.execute("update project_dependency_attempts set state='running',run_id=?,initiating_run_id=coalesce(initiating_run_id,?),updated_at=? where dependency_id=? and generation=?", (run_id, initiating_run_id, utc_now(), dependency_id, dep["generation"]))


def publish_owned(run_id: str, relative: str, *, final: bool = False) -> dict | None:
    with connect() as conn:
        row = conn.execute("select id from project_dependencies where owner_run_id=? and path=?", (run_id, relative)).fetchone()
    if not row:
        return None
    dep = get(row[0], reconcile=False)
    run = store.get_run(run_id)
    if run.get("dependency_id") == dep["id"] and not final:
        return None
    if final and run["status"] != "running":
        return None
    project = store.get_project(dep["project_id"])
    try:
        records = library.validate_module(project, relative)
    except (ValueError, OSError):
        return None
    match = next((d for d in records if d["user_name"] == dep["declaration_name"] and d["certified"]), None)
    if not match or match.get("semantic_key") != dep["semantic_key"]:
        return None
    with write() as conn:
        from . import run_commands
        token=run_commands.current_token()
        if token and (not run_commands.owns(conn,run_id,token) or conn.execute('select stop_requested_reason from runs where id=?',(run_id,)).fetchone()[0]):
            return None
        changed = conn.execute("""update project_dependencies set state='ready',owner_run_id=null,
            result_json=?,blocker_json=null,updated_at=? where id=? and owner_run_id=? and generation=?""",
            (library.dumps(match), utc_now(), dep["id"], run_id, dep["generation"])).rowcount
        if not changed:
            return None
        conn.execute("update project_dependency_attempts set state='ready',updated_at=? where dependency_id=? and generation=?", (utc_now(), dep["id"], dep["generation"]))
    store.upsert_artifact(project_id=dep["project_id"], session_id=run["session_id"], run_id=run_id,
        declaration_name=dep["declaration_name"], kind=dep["specification"]["kind"], path=relative,
        module_name=dep["module_name"], formalization_id=dep["formalization_id"], source_hash=dep["source_hash"])
    return get(dep["id"])


def settle_run(run_id: str):
    run = store.get_run(run_id)
    with connect() as conn:
        owned = [dict(r) for r in conn.execute("select * from project_dependencies where owner_run_id=?", (run_id,))]
    for old in owned:
        if run["stop_reason"] == "dependency_wait":
            state, blocker = "waiting", None
        elif run["stop_reason"] in TRANSIENT_STOPS or run.get("recovery_state") == "review":
            state, blocker = "interrupted", None
        else:
            state = "needs_review" if run["status"] == "needs_review" else "failed"
            blocker = {"category": run.get("stop_reason") or "verification",
                       "detail": run.get("result_detail") or "The canonical dependency did not pass its pinned statement and verification checks.", "run_id": run_id}
        with write() as conn:
            conn.execute("""update project_dependencies set state=?,owner_run_id=?,blocker_json=?,updated_at=?
                where id=? and owner_run_id=?""", (state, run_id if state == "waiting" else None,
                library.dumps(blocker) if blocker else None, utc_now(), old["id"], run_id))
            conn.execute("update project_dependency_attempts set state=?,detail=?,updated_at=? where dependency_id=? and generation=?",
                         (state, blocker["detail"] if blocker else None, utc_now(), old["id"], old["generation"]))


def recover():
    with connect() as conn:
        owners = [r[0] for r in conn.execute("select distinct owner_run_id from project_dependencies where owner_run_id is not null")]
    for run_id in owners:
        run = store.get_run(run_id)
        if run and run["status"] not in {"pending", "running"}:
            settle_run(run_id)


def tool_request(run_id: str, invocation_key: str, tool: str, arguments: dict) -> dict:
    request_hash = library.digest({"tool": tool, "arguments": arguments})
    with connect() as conn:
        old = conn.execute("select * from project_library_invocations where run_id=? and invocation_key=?", (run_id, invocation_key)).fetchone()
    if old:
        if old["request_hash"] != request_hash:
            raise ValueError("Invocation identity was reused for different arguments.")
        return json.loads(old["response_json"])
    run = store.get_run(run_id)
    project = store.get_project(run["project_id"]) if run and run.get("project_id") else None
    if not project:
        raise ValueError("Project library tools require a project-scoped run.")
    if tool == "search_project_library":
        result = library.search(project, arguments.get("query", ""), limit=arguments.get("limit", 20), offset=arguments.get("offset", 0))
    elif tool == "request_project_dependency":
        if arguments.get("mode") == "deferred" and run["purpose"] not in {"overleaf_solver", "project_dependency"}:
            raise ValueError("Deferred dependency scheduling requires an Overleaf workflow. Use inline for this project chat.")
        result = ensure(project, arguments["specification"], run_id=run_id, mode=arguments.get("mode", "inline"))
        if run["purpose"] not in {"overleaf_solver", "project_dependency"}:
            result["stop_reason"] = None
    else:
        raise ValueError("Unsupported project library tool.")
    with write() as conn:
        conn.execute("insert or ignore into project_library_invocations values (?,?,?,?)", (run_id, invocation_key, request_hash, library.dumps(result)))
        stored = conn.execute("select request_hash,response_json from project_library_invocations where run_id=? and invocation_key=?",
                              (run_id, invocation_key)).fetchone()
        if stored["request_hash"] != request_hash:
            raise ValueError("Invocation identity was reused for different arguments.")
        result = json.loads(stored["response_json"])
        if result.get("stop_reason") == "dependency_wait":
            conn.execute("update runs set waiting_dependencies_json=? where id=?", (library.dumps([result["dependency_id"]]), run_id))
    return result


def worker_message(dep: dict) -> str:
    allocation = response(dep)
    return ("Prove this canonical project dependency using available project sources and Mathlib. "
        "Preserve the pinned signature and definition bodies. Search the project library first. "
        "Read the reserved module if it exists and continue its draft and investigation notes. "
        "Write only this result and separately reserved supporting modules; do not import its unfinished consumers. "
        "Check the final module. Do not introduce axioms, sorry, native_decide, or weaken hypotheses.\n"
        "Dependency specification (data):\n" + library.dumps(dep["specification"]) + "\n"
        "Canonical path: " + dep["path"] + "\nInitial skeleton:\n" + (allocation["skeleton"] or ""))
