"""Blueprint → dependency graph with live-derived status (T2, D28/D29).

This is the *derive* side on top of :mod:`blueprint` (the *parse* side). It takes the
parsed nodes + `uses` edges and enriches each node with:

- **status** — derived from the live Lean state, so it can't lie (D28): resolve the
  node's `lean:` decl to a project ``.lean`` file, read that file's latest
  ``code_step`` verdict (already back-filled by lean_check, D6) + a ``sorry`` scan,
  and overlay ``ready`` from dependency readiness. Never recompiles — it reuses the
  stored verdict, keeping ``/graph`` cheap.
- **sessions / last_modified_by** — session attribution derived from ``code_steps``
  (D29): the node's file → every session that committed it, newest first. No
  node↔session table to keep in sync; it's reconstructed from git + the DB index.

Status values (→ node color in F7):
  ``planned`` (no decl yet) · ``stated`` (decl exists, has ``sorry`` / unchecked) ·
  ``ready`` (not proved, but every dependency is proved) · ``proved`` (checks ok,
  no ``sorry``) · ``failed`` (latest check errored).

Each node also carries a ``verified`` bool: True iff it is ``proved`` *and*
SafeVerify (kernel replay + axiom audit) passed for its current state — the
stronger, audit-grade verdict (a plain ``proved`` is only a Lean-check pass). It's
a flag, not a status, so the ``ready`` overlay (which keys off ``proved``) is
untouched. Surfaced as "Proved ✓" vs "check ✓ · audit pending" in the UI.

Known limitation: ``sorry`` detection is a decl-span *text* scan (sound for the
one-decl-per-file convention these projects use), not a Lean-aware parse.
"""

from __future__ import annotations

from pathlib import Path

from . import blueprint
from . import diagnostics
from . import store
from .artifacts import declaration_contains_sorry, scan_lean_declarations
from .projects import project_repo_dir

VALID_STATUSES = ("planned", "stated", "ready", "proved", "failed")


def _short(name: str) -> str:
    """Last dotted component of a (possibly qualified) decl name."""
    return name.rsplit(".", 1)[-1]


def _scan_lean_decls(
    repo: Path, unreadable: list[tuple[str, str]] | None = None
) -> tuple[dict[str, str | None], dict[str, str]]:
    """Walk a project repo's ``.lean`` files and return ``(fqn_to_file, file_to_text)``.

    ``fqn_to_file`` maps each declaration's fully-qualified name (namespace stack +
    decl name) to the file (repo-relative posix path) that declares it. A decl whose
    own name is already dotted is treated as pre-qualified (not prefixed).

    ``unreadable`` (F1) collects ``(relpath, reason)`` for files that could not be
    read, so the caller can report a partial scan rather than presenting it as a
    complete one."""
    fqn_to_file: dict[str, str | None] = {}
    file_to_text: dict[str, str] = {}
    unreadable = unreadable if unreadable is not None else []
    if not repo.is_dir():
        return fqn_to_file, file_to_text
    for path in sorted(repo.rglob("*.lean")):
        if ".lake" in path.parts or ".lea" in path.parts:
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeError) as exc:
            # F1: a file we can't read contributes no declarations, so every node it
            # declares resolves to "no file" and renders as *planned* — the graph
            # reports unwritten work that is actually written. Recorded so the
            # response can say the graph is partial instead of implying it is complete.
            unreadable.append((path.relative_to(repo).as_posix(), f"{type(exc).__name__}: {exc}"))
            continue
        rel = path.relative_to(repo).as_posix()
        file_to_text[rel] = text
        for declaration in scan_lean_declarations(text):
            if declaration.full_name in fqn_to_file and fqn_to_file[declaration.full_name] != rel:
                fqn_to_file[declaration.full_name] = None
            else:
                fqn_to_file[declaration.full_name] = rel
    return fqn_to_file, file_to_text


def _resolve_name(lean: str, fqn_to_file: dict[str, str | None]) -> str | None:
    """Qualified names are identities; legacy short names must be unambiguous."""
    if lean in fqn_to_file:
        return lean if fqn_to_file[lean] is not None else None
    if "." in lean:
        return None
    matches = [fqn for fqn in fqn_to_file if _short(fqn) == lean]
    return matches[0] if len(matches) == 1 and fqn_to_file[matches[0]] is not None else None


def _resolve_file(lean: str, fqn_to_file: dict[str, str | None]) -> str | None:
    resolved = _resolve_name(lean, fqn_to_file)
    return fqn_to_file.get(resolved) if resolved else None


def _decl_has_sorry(text: str, lean: str) -> bool:
    """Whether the decl named by ``lean`` contains ``sorry``/``admit``, scanning only
    that decl's span (its header line to the next decl header or EOF)."""
    if not text:
        return False
    return declaration_contains_sorry(text, lean)


def _base_status(lean: str | None, file: str | None, latest: dict | None, has_sorry: bool) -> str:
    """Per-node status before the dependency-aware ``ready`` overlay."""
    if not lean or not file:
        return "planned"
    if latest and latest.get("check_status") == "error" and latest.get("check_current"):
        return "failed"
    if has_sorry:
        return "stated"
    if latest and latest.get("check_status") == "ok" and latest.get("check_current"):
        return "proved"
    return "stated"  # decl exists but is unchecked / unknown — stated, not proved


def build_graph(project: dict, proofs_root: Path) -> dict:
    """Parse ``.lea/blueprint.md`` and enrich each node with derived status + session
    attribution (D28/D29). Returns ``{"nodes": [...], "edges": [...]}`` where each node
    is the parsed node plus ``status``, ``file``, ``sessions``, ``last_modified_by``."""
    repo = project_repo_dir(project, proofs_root)
    warnings: list[dict] = []
    try:
        text = (repo / ".lea" / "blueprint.md").read_text()
    except FileNotFoundError:
        # No blueprint yet is the normal state of a new project, not a failure.
        text = ""
    except (OSError, UnicodeError) as exc:
        # F1: a blueprint that EXISTS but can't be read produced an empty graph
        # indistinguishable from "this project has no blueprint" — so a permissions
        # or encoding problem read as "you haven't written one yet."
        text = ""
        warnings.append(diagnostics.resolve(
            "degraded", "asset.read_failed",
            f"The project blueprint could not be read ({type(exc).__name__}: {exc}); "
            "the graph is empty.",
            source="graph", context={"path": ".lea/blueprint.md", "project_id": project["id"]},
        ))
    parsed = blueprint.parse(text)
    unreadable: list[tuple[str, str]] = []
    fqn_to_file, file_to_text = _scan_lean_decls(repo, unreadable)
    for rel, reason in unreadable:
        warnings.append(diagnostics.resolve(
            "degraded", "asset.read_failed",
            f"{rel} could not be read ({reason}); any blueprint node it declares is "
            "shown as unwritten.",
            source="graph", context={"path": rel, "project_id": project["id"]},
        ))
    titles = {s["id"]: s.get("title") or "session" for s in store.list_project_sessions(project["id"])}
    enriched: list[dict] = []
    base: dict[str, str] = {}
    for node in parsed["nodes"]:
        lean = node["lean"]
        resolved = _resolve_name(lean, fqn_to_file) if lean else None
        file = fqn_to_file.get(resolved) if resolved else None
        # Bind the displayed source to the recorded checked bytes as well as
        # the compiler environment; file names and session ownership are insufficient.
        steps = (
            store.code_steps_for_project_path(project["id"], file)
            if file else []
        )
        latest = steps[0] if steps else None
        content = file_to_text.get(file, "")
        has_sorry = _decl_has_sorry(content, resolved) if file else False
        # A current check of different recorded bytes cannot certify this file.
        matching_check = latest if latest and latest.get("code") == content else None
        status = _base_status(resolved, file, matching_check, has_sorry)
        base[node["key"]] = status

        sessions: list[dict] = []
        seen: set[str] = set()
        for step in steps:  # already newest-first
            sid = step["session_id"]
            if sid not in seen:
                seen.add(sid)
                sessions.append({"session_id": sid, "title": titles.get(sid, "session"), "last_at": step["created_at"]})

        last_modified_by = sessions[0]["session_id"] if sessions else None
        # Audit-grade only: a Lean-check pass (`proved`) that SafeVerify also cleared
        # for the session holding the current file state. Never True for stated/ready/etc.
        verified = status == "proved" and store.safe_verify_current_step(project["id"], latest)

        enriched.append({
            **node,
            "file": file,
            "resolved_lean": resolved,
            "status": status,
            "verified": verified,
            "sessions": sessions,
            "last_modified_by": last_modified_by,
        })

    # ``ready`` overlay (D28): a not-yet-proved node whose every dependency is proved.
    for node in enriched:
        if node["status"] in ("planned", "stated") and node["uses"] and all(
            base.get(dep) == "proved" for dep in node["uses"]
        ):
            node["status"] = "ready"

    # `warnings` (F1) is what stops a PARTIAL graph from presenting itself as a
    # complete one. Empty on the happy path, so existing consumers are unaffected.
    return {"nodes": enriched, "edges": parsed["edges"], "warnings": warnings}
