"""Derived declaration catalog. Lean owns types; current bytes own freshness.

The JSON index is disposable metadata, never another store of proof content.
Legacy files are inspected in place. Only explicit shared dependencies require
the stronger pinned-statement publication gate in project_dependencies.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from threading import RLock

from lea.imports import direct_imports
from lea.interface import rebuild, verify
from . import store
from .artifacts import scan_lean_declarations, scrub_lean_source
from .db import connect, write, utc_now
from .read_projections import read_projection, current_snapshot, decode_evidence

EXCLUDED = {".lea", ".git", ".lake", "node_modules", "build", "dist", "__pycache__"}
ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
_locks: dict[str, RLock] = {}
_lock_guard = RLock()
INSPECTOR = Path(__file__).resolve().parents[2] / "prover/lea/LibraryInspect.lean"


def dumps(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256((value if isinstance(value, bytes) else dumps(value).encode())).hexdigest()


def project_repo(project: dict) -> Path:
    from .config import load_config
    from .projects import project_repo_dir
    config = load_config()
    root = config.lea_root or Path(__file__).resolve().parents[2] / "prover"
    return project_repo_dir(project, root / "workspace/proofs").resolve()


def lock(project_id: str):
    with _lock_guard:
        return _locks.setdefault(project_id, RLock())


def lake_root(repo: Path) -> Path | None:
    return next((p for p in [repo, *repo.parents]
                 if (p / "lakefile.lean").exists() or (p / "lakefile.toml").exists()), None)


def module_name(project: dict, relative: str) -> str:
    return project["namespace"] + "." + ".".join(Path(relative).with_suffix("").parts)


def project_paths(repo: Path):
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED)
        for name in sorted(files):
            path = Path(root) / name
            if path.suffix == ".lean" and not path.is_symlink():
                # Candidate proofs are run scratch space, never import destinations.
                if "candidate" not in path.parts and not name.endswith(".candidate.lean"):
                    yield path


def environment_hash(project: dict, repo: Path, path: Path) -> str:
    from lea.check_evidence import manifest, environment_hash as fingerprint, CheckFailure
    try:
        environment = fingerprint(manifest(path, path.read_text(), lake_root(repo)))
    except (OSError, UnicodeError, CheckFailure) as exc:
        environment = digest({'unavailable': str(exc), 'source': digest(path.read_bytes()) if path.exists() else None})
    from lea import safeverify
    binary = safeverify.safe_verify_binary()
    return digest({'source': digest(path.read_bytes()) if path.exists() else None,
        'environment': environment, 'protocol': 1,
        'auditor': [str(binary), binary.stat().st_size, binary.stat().st_mtime_ns] if binary else None,
        'inspector': digest(INSPECTOR.read_bytes())})


def _lexical(code: str) -> list[dict]:
    result = []
    for decl in scan_lean_declarations(code):
        header = scrub_lean_source(decl.span).split(":=", 1)[0].strip()
        result.append(dict(full_name=decl.full_name, user_name=decl.full_name,
            short_name=decl.short_name, kind="theorem" if decl.kind in {"lemma", "theorem"} else "definition",
            visibility="private" if re.search(r"\bprivate\b", header) else "public",
            start_line=decl.start_line, end_line=decl.end_line, type=header,
            type_expr=None, body_expr=None, axioms=None))
    return result


def inspect_source(repo: Path, prefix: str, command: str):
    root = lake_root(repo)
    if not root:
        raise ValueError("No enclosing Lake project is available for statement inspection.")
    scratch = repo / ".lea/tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="library-", dir=scratch) as temp:
        path = Path(temp) / "Inspect.lean"
        path.write_text("import Lean\n" + prefix + "\n" + INSPECTOR.read_text().removeprefix("import Lean\n") + "\n" + command + "\n")
        result = subprocess.run(["lake", "env", "lean", str(path)], cwd=root,
                                capture_output=True, text=True, timeout=120)
        output = result.stdout + "\n" + result.stderr
        if result.returncode:
            raise ValueError("Lean could not inspect this statement/module: " + output[-4000:])
        marker = "LEA_LIBRARY_JSON:"
        if marker not in output:
            raise ValueError("Lean inspection produced no declaration evidence.")
        return json.JSONDecoder().raw_decode(output.rsplit(marker, 1)[1].lstrip())[0]


def inspect_module(project: dict, repo: Path, relative: str) -> list[dict]:
    module = module_name(project, relative)
    return inspect_source(repo, f"import {module}\n", f"#lea_inspect_module {json.dumps(module)}")


def inspect_specification(project: dict, specification: dict) -> dict:
    repo = project_repo(project)
    imports = specification.get("imports", [])
    if any(not re.fullmatch(r"[\w'.]+", x) for x in imports):
        raise ValueError("Imports must be Lean module names.")
    records = refresh(project)
    for imported in imports:
        files = {d["path"] for d in records if d["module"] == imported}
        for relative in files:
            inspected = validate_module(project, relative)
            if inspected and inspected[0]["build_status"] != "ok":
                raise ValueError(f"Required module {imported} does not currently build.")
    signature = str(specification.get("signature") or "").strip()
    if not signature or ":=" in signature:
        raise ValueError("Supply a declaration signature, excluding its name and proof body.")
    kind = specification.get("kind", "theorem")
    if kind not in {"theorem", "definition"}:
        raise ValueError("Dependency kind must be theorem or definition.")
    body = specification.get("body")
    if kind == "definition" and not isinstance(body, str):
        raise ValueError("Shared definitions require a pinned defining body.")
    namespace = project["namespace"]
    prefix = "\n".join(f"import {x}" for x in imports)
    prefix += f"\nnamespace {namespace}\n{'noncomputable def' if kind == 'definition' else 'theorem'} __lea_expected {signature} := "
    prefix += body if kind == "definition" else "by sorry"
    prefix += f"\nend {namespace}\n"
    inspected = inspect_source(repo, prefix, f'#lea_inspect_decl "{namespace}.__lea_expected"')
    if kind == "definition" and set(inspected["axioms"]) - ALLOWED_AXIOMS:
        raise ValueError("The pinned definition contains unproved or nonstandard axioms.")
    return inspected


def semantic_key(decl: dict) -> str:
    return digest({"kind": decl["kind"], "type": decl["type_expr"],
                   "body": decl.get("body_expr") if decl["kind"] != "theorem" else None})


def specification_environment(project: dict, imports: list[str]) -> str:
    repo = project_repo(project)
    root = lake_root(repo)
    values = {}
    for imported in imports:
        if root:
            path = root / "proofs" / (imported.replace(".", "/") + ".lean")
        elif imported.startswith(project["namespace"] + "."):
            path = repo / (imported[len(project["namespace"]) + 1:].replace(".", "/") + ".lean")
        else:
            continue
        if path.is_file():
            values[imported] = environment_hash(project, repo, path)
    if root:
        from .recovery import _dependency_fingerprints
        values["packages"] = {k: v for k, v in _dependency_fingerprints(root).items() if not k.startswith("proofs/")}
    return digest(values)


def audit_module(path: Path, declarations: list[dict]):
    if any(d["kind"] == "theorem" for d in declarations):
        return verify(str(path))
    # SafeVerify's public interface needs a theorem entry point. A harmless
    # witness lets it replay a definitions-only module, including all axioms.
    with tempfile.TemporaryDirectory(prefix="definition-audit-", dir=path.parent / ".lea/tmp" if
        (path.parent / ".lea/tmp").is_dir() else None) as temp:
        scratch = Path(temp) / "Definitions.lean"
        scratch.write_text(path.read_bytes().decode('utf-8') + "\ntheorem __lea_library_definition_audit : True := by trivial\n")
        result = verify(str(scratch))
        if result.execution:
            from lea.events import VerifyResult
            content = path.read_bytes().decode('utf-8')
            from lea.check_evidence import source_identity
            execution = {**result.execution, 'source_path': str(path.resolve()), 'source_identity': source_identity(path), 'content_sha256': digest(content.encode())}
            return VerifyResult(result.status, result.detail, execution, content)
        return result


def refresh(project: dict, *, repo: Path | None = None,
            resolve_environment: bool = True) -> list[dict]:
    """Refresh the source catalog; startup can defer compiler dependency resolution.

    An inventory-only refresh preserves unchanged records, including their
    historical outcomes and evidence. ``entries`` still checks that evidence
    against current dependencies before exposing a certified declaration.
    Changed/new source remains unchecked until an explicit validation.
    """
    repo = repo or project_repo(project)
    pid = project["id"]
    from lea.check_evidence import coordinator
    with coordinator(lake_root(repo) or repo).lease(), lock(pid):
        with connect() as conn:
            old = {r["path"]: dict(r) for r in conn.execute(
                "select * from project_library_modules where project_id = ?", (pid,))}
        found = set()
        updates = []
        for path in project_paths(repo):
            relative = path.relative_to(repo).as_posix()
            found.add(relative)
            code = path.read_text(errors="replace")
            source_hash = digest(path.read_bytes())
            previous = old.get(relative)
            if not resolve_environment and previous and previous["source_hash"] == source_hash:
                continue
            env_hash = (environment_hash(project, repo, path) if resolve_environment else
                        digest({"source": source_hash, "environment": "unresolved"}))
            if previous and previous["source_hash"] == source_hash and previous["environment_hash"] == env_hash:
                continue
            updates.append((pid, relative, source_hash, env_hash, dumps(_lexical(code)), utc_now()))
        with write() as conn:
            for values in updates:
                conn.execute("""insert into project_library_modules
                    (project_id,path,source_hash,environment_hash,declarations_json,updated_at)
                    values (?,?,?,?,?,?) on conflict(project_id,path) do update set
                    source_hash=excluded.source_hash, environment_hash=excluded.environment_hash,
                    declarations_json=excluded.declarations_json,build_status='unchecked',
                    verify_status=null,detail=null,updated_at=excluded.updated_at""", values)
            for relative in old.keys() - found:
                conn.execute("delete from project_library_modules where project_id=? and path=?", (pid, relative))
        return entries(project, repo=repo)


def validate_module(project: dict, relative: str, *, repo: Path | None = None) -> list[dict]:
    repo = repo or project_repo(project)
    from lea.check_evidence import mutation
    with mutation(repo), lock(project["id"]):
        refresh(project, repo=repo)
        with connect() as conn:
            row = conn.execute("select * from project_library_modules where project_id=? and path=?",
                               (project["id"], relative)).fetchone()
        if not row:
            raise ValueError("This file is not a canonical project module.")
        path = repo / relative
        from lea.check_evidence import freshness
        if row["build_status"] != "unchecked" and freshness(
                json.loads(row['check_evidence_json'] or '{}'), path)[0]:
            return entries(project, repo=repo, path=relative)
        status, audit_status, detail = "error", None, None
        execution = None
        declarations = json.loads(row["declarations_json"])
        try:
            built = rebuild(str(path))
            status, detail = built.status, built.detail
            if status == "ok":
                declarations = inspect_module(project, repo, relative)
                lexical = _lexical(path.read_text())
                positions = {d["user_name"]: d for d in lexical}
                for decl in declarations:
                    match = positions.get(decl["user_name"])
                    if match:
                        decl.update(start_line=match["start_line"], end_line=match["end_line"])
                    decl["semantic_key"] = semantic_key(decl)
                audit = audit_module(path, declarations)
                audit_status, detail = audit.status, audit.detail
                execution = getattr(audit, "execution", None)
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            detail = str(exc)
        from lea.check_evidence import freshness
        # A check never certifies bytes edited while the checker was running.
        if (not path.exists() or digest(path.read_bytes()) != row["source_hash"]
                or (execution and not freshness(execution, path)[0])):
            return refresh(project, repo=repo)
        with write() as conn:
            conn.execute("""update project_library_modules set declarations_json=?,build_status=?,
                verify_status=?,detail=?,updated_at=?,check_evidence_json=?,environment_hash=? where project_id=? and path=?
                and source_hash=? and environment_hash=?""",
                (dumps(declarations), status, audit_status, detail, utc_now(), dumps(execution) if execution else None,
                 environment_hash(project, repo, path), project["id"], relative,
                 row["source_hash"], row["environment_hash"]))
        return entries(project, repo=repo, path=relative)


@read_projection
def entries(project: dict, *, repo: Path | None = None, path: str | None = None) -> list[dict]:
    repo = repo or project_repo(project)
    with connect() as conn:
        rows = conn.execute("select * from project_library_modules where project_id=? and (? is null or path=?) order by path",
                            (project["id"], path, path)).fetchall()
        owners = {r["path"]: dict(r) for r in conn.execute(
            "select * from project_dependencies where project_id=?", (project["id"],))}
        historical = {r["path"]: dict(r) for r in conn.execute("""select ff.path,f.id,f.origin,
            f.origin_key,f.statement from formalization_files ff join formalizations f
            on f.id=ff.formalization_id where f.project_id=?""", (project["id"],))}
    result = []
    for row in rows:
        from lea.check_evidence import freshness
        try:
            evidence = decode_evidence(row['check_evidence_json'] or '{}')
        except (ValueError, TypeError):
            evidence = {}
        current = freshness(evidence, repo / row['path'], snapshot=current_snapshot())[0]
        for declaration in json.loads(row["declarations_json"]):
            decl = dict(declaration)
            axioms = decl.get("axioms")
            reusable = (current and row["build_status"] == "ok" and row["verify_status"] == "ok"
                and decl.get("visibility") == "public" and axioms is not None
                and not (set(axioms) - ALLOWED_AXIOMS) and decl["kind"] not in {"axiom", "recursor"})
            owner = owners.get(row["path"])
            prior = historical.get(row["path"])
            decl.update(path=row["path"], module=module_name(project, row["path"]),
                import_statement="import " + module_name(project, row["path"]),
                source_hash=row["source_hash"], environment_hash=row["environment_hash"],
                build_status=row["build_status"], verify_status=row["verify_status"],
                verification_detail=row["detail"], certified=reusable, reusable=reusable,
                dependency_id=owner["id"] if owner else None,
                formalization_id=owner["formalization_id"] if owner else prior["id"] if prior else None,
                source=json.loads(owner["specification_json"]).get("sources", []) if owner else
                    [{"kind": prior["origin"], "key": prior["origin_key"], "excerpt": prior["statement"]}] if prior else [],
                outstanding_obligations=["Private declaration: extraction is required before importing."]
                    if decl.get("visibility") == "private" else [])
            # Explicit claim parameters stay in the type, never get dropped by search.
            if re.search(r"\b\w*Claim\b", decl.get("type", "")) and decl["kind"] == "theorem":
                decl["outstanding_obligations"].append("This theorem's displayed type includes a claim parameter; it does not discharge that claim.")
            if owner:
                spec = json.loads(owner["specification_json"])
                decl["citation"] = spec.get("citation")
                # Certification is internal evidence. An owned module remains
                # unavailable to consumers until its writer publishes it.
                decl["reusable"] = reusable and owner["state"] == "ready" and not owner["owner_run_id"]
                if decl["user_name"] == owner["declaration_name"]:
                    decl["certified"] = reusable and decl.get("semantic_key") == owner["semantic_key"]
                    decl["reusable"] = decl["reusable"] and decl["certified"]
            result.append(decl)
    return result


def search(project: dict, query: str = "", *, limit: int = 20, offset: int = 0, validate: bool = True) -> dict:
    records = refresh(project)
    tokens = [s.casefold() for s in re.findall(r"[\w']+", query)]
    def score(d):
        text = dumps({k: d.get(k) for k in ("user_name", "type", "path", "citation", "source")}).casefold()
        return sum(1 for word in tokens if word in text)
    selected = sorted((d for d in records if not tokens or score(d)),
                      key=lambda d: (-score(d), not d["reusable"], d["path"], d["full_name"]))
    page = selected[offset:offset + max(1, min(limit, 100))]
    if validate:
        # Build only returned modules; do not compile the entire library at admission.
        for relative in dict.fromkeys(d["path"] for d in page if d["build_status"] == "unchecked"):
            validate_module(project, relative)
        return search(project, query, limit=limit, offset=offset, validate=False)
    return {"declarations": page, "total": len(selected),
            "next_offset": offset + len(page) if offset + len(page) < len(selected) else None}


def context(project: dict, repo: Path | None = None) -> str:
    records = refresh(project, repo=repo)
    ready = sum(d["reusable"] for d in records)
    return (f"\n## Project mathematics library\n{len(records)} declarations indexed; {ready} currently verified and importable. "
        "Use search_project_library to retrieve complete types and imports; temporary probes are excluded. "
        "Search this library, then Mathlib, before proving supporting mathematics. Reuse definitions as well as lemmas. "
        "A proposition definition or a theorem assuming an external claim is not a proof of that claim. "
        "Reserve new reusable results with request_project_dependency before writing them. The host supplies the path, "
        "module and namespace. Use inline for a short local argument and deferred for substantial mathematics. "
        "Target-specific tactical glue may remain private. Do not change an existing result's statement or move its file. "
        "Use available project sources; a citation alone never establishes a theorem.\n")
