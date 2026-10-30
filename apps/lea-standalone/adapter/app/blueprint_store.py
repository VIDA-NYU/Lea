"""The blueprint store: the only writer of `.lea/blueprint.md` and `.lea/blueprint.draft.md`.

PLAN-blueprint-tools §2.2. The agent never writes blueprint markdown: its blueprint
tools (host-mediated, :mod:`blueprint_host`), the review routes and reconciliation
call this module, which validates, assigns declaration names, renders the canonical
form (:func:`blueprint.render`) and commits. Humans keep "Edit markdown": the PUT
routes store :func:`normalize` output.

Every write is one unit: project mutation reservation + a per-project lock around
read → mutate → render → write → ``GitStore.commit_all(paths=[rel])`` → one
project-scoped ``domain_changes`` row (``store.note_blueprint_change``) so the SQL
views refresh (§2.9). A write that renders the same bytes is skipped entirely (no
commit, no change row).

Names: the key is the declaration's short name. A node without a ``lean``/``file``
gets ``assign_names(namespace, key)``: ``<namespace>.<key>`` and
``<PascalCase(key)>.lean`` (repo-relative).

Statuses are never stored. ``upsert`` reports them from :func:`graph.build_graph`
on the written live file; a draft has no checks, so its nodes are ``ready`` (no
dependencies) or ``planned``.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path

from . import blueprint
from . import project_coordination
from . import store
from .gitstore import GitStore, GitStoreError
from .projects import project_repo_dir

LIVE = ".lea/blueprint.md"
DRAFT = ".lea/blueprint.draft.md"
MAX_STATEMENT_LENGTH = 4000
STATUSES = ("proved", "ready", "stated", "failed", "planned")
_FIELDS = ("kind", "statement", "uses", "lean", "file")
_HEADING_LINE_RE = re.compile(r"^(##[ \t]+\S)")

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock(project_id: str) -> threading.RLock:
    """Serialize read-modify-write per project (the mutation reservation alone
    admits concurrent mutations)."""
    with _locks_guard:
        return _locks.setdefault(project_id, threading.RLock())


# ── reads ─────────────────────────────────────────────────────────────────────


def _rel(draft: bool) -> str:
    return DRAFT if draft else LIVE


def _read(project: dict, proofs_root: Path, rel: str) -> str:
    """The file's text, ``""`` when it does not exist. Other read errors raise: a
    write must never replace a blueprint it could not read."""
    try:
        return (project_repo_dir(project, proofs_root) / rel).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def load(project: dict, proofs_root: Path, *, draft: bool = False) -> dict:
    """:func:`blueprint.parse` of the live (or draft) file; ``parse("")`` when missing."""
    return blueprint.parse(_read(project, proofs_root, _rel(draft)))


def exists_draft(project: dict, proofs_root: Path) -> bool:
    return (project_repo_dir(project, proofs_root) / DRAFT).is_file()


def normalize(text: str) -> tuple[str, list[dict]]:
    """``(render(parse(text)), warnings)`` — the human "Edit markdown" save path."""
    parsed = blueprint.parse(text or "")
    return blueprint.render(parsed), parsed["warnings"]


# ── names ─────────────────────────────────────────────────────────────────────


def _pascal(key: str) -> str:
    """``sum_choose_pow`` / ``sumChoosePow`` → ``SumChoosePow``; ``T_three`` → ``TThree``.
    Split on ``_``, upper-case each part's first letter, keep the rest. A trailing
    ``'`` (Mathlib's variant marker) becomes ``Prime`` so ``foo'`` does not share
    ``foo``'s file; other characters that cannot appear in a module name drop."""
    parts = []
    for part in key.split("_"):
        part = re.sub(r"[^A-Za-z0-9]", "", part.replace("'", "Prime"))
        if part:
            parts.append(part[0].upper() + part[1:])
    return "".join(parts) or "Node"


def assign_names(namespace: str, key: str) -> tuple[str, str]:
    """The assigned ``(lean, file)`` for a node: ``("<namespace>.<key>", "<PascalKey>.lean")``."""
    return f"{namespace}.{key}", f"{_pascal(key)}.lean"


# ── validation + merge (pure) ─────────────────────────────────────────────────


def _reject(key: str, code: str, message: str) -> dict:
    return {"key": key, "code": code, "message": message}


def _clean_name(value) -> str | None:
    if not isinstance(value, str):
        return None
    return blueprint._extract_name(value)


def _clean_uses(value, known: set[str]) -> list[str]:
    if isinstance(value, str):
        tokens = blueprint._split_uses(value)
    else:
        tokens = [token for token in (str(v).strip().strip("`").strip() for v in (value or [])) if token]
    if len(tokens) == 1 and tokens[0].lower() in blueprint.USES_SENTINELS and tokens[0] not in known:
        return []
    return list(dict.fromkeys(tokens))


def _clean_statement(value: str) -> str:
    """Keep a statement from opening a new section when rendered: a ``## `` line
    outside a fence is escaped as ``\\##`` (markdown still shows ``##``)."""
    lines, in_fence = [], False
    for line in str(value).replace("\r\n", "\n").split("\n"):
        if blueprint._FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and _HEADING_LINE_RE.match(line):
            line = "\\" + line
        lines.append(line)
    return "\n".join(lines).strip("\n")


def _merge_batch(document: dict, inputs: list, *, namespace: str, other_keys: set[str]) -> tuple[list[str], list[dict], dict[str, dict]]:
    """Validate ``inputs`` against ``document`` (plus ``other_keys``, keys that
    satisfy ``uses`` without living in this document) with the whole batch visible,
    and merge the accepted ones into ``document["blocks"]`` in place.

    Returns ``(applied_keys, rejected, accepted_nodes)``."""
    existing: dict[str, dict] = {}
    for node in document["nodes"]:
        existing.setdefault(node["key"], node)
    known = set(existing) | set(other_keys)

    rejected: dict[str, dict] = {}
    rejected_list: list[dict] = []
    order: list[str] = []
    candidates: dict[str, dict] = {}
    uses_given: dict[str, bool] = {}

    def reject(key: str, code: str, message: str) -> None:
        if key not in rejected:
            rejected[key] = _reject(key, code, message)
            rejected_list.append(rejected[key])

    batch_keys = {item.get("key") for item in inputs if isinstance(item, dict) and isinstance(item.get("key"), str)}
    for item in inputs:
        key = item.get("key") if isinstance(item, dict) else None
        if not isinstance(key, str) or not key.strip():
            rejected_list.append(_reject(str(key or ""), "invalid_key", "Every node needs a string `key`."))
            continue
        key = key.strip()
        if key in rejected:
            continue
        base = candidates.get(key) or dict(existing.get(key) or {
            "type": "node", "key": key, "kind": None, "lean": None, "file": None, "uses": [], "statement": ""})
        candidate = {**base, "uses": list(base.get("uses") or [])}
        if key not in candidates:
            order.append(key)
        candidates[key] = candidate

        if not blueprint.is_valid_key(key):
            reject(key, "invalid_key",
                   f"`{key}` is not a valid key: use the declaration's short name (letters, digits, `_`, `'`; "
                   f"starting with a letter or `_`; at most {blueprint.MAX_KEY_LENGTH} characters).")
            continue
        if item.get("kind") is not None:
            kind = str(item["kind"]).strip().lower()
            if kind not in blueprint.VALID_KINDS:
                reject(key, "unknown_kind", f"node `{key}` has kind `{item['kind']}`; use theorem, lemma or definition.")
                continue
            candidate["kind"] = kind
        elif key not in existing and candidate.get("kind") is None:
            reject(key, "missing_kind", f"new node `{key}` needs a kind: theorem, lemma or definition.")
            continue
        if item.get("statement") is not None:
            statement = _clean_statement(item["statement"])
            if len(statement) > MAX_STATEMENT_LENGTH:
                reject(key, "statement_too_long",
                       f"node `{key}` has a {len(statement)}-character statement; keep it under "
                       f"{MAX_STATEMENT_LENGTH} (one or two sentences).")
                continue
            candidate["statement"] = statement
        if item.get("uses") is not None:
            candidate["uses"] = _clean_uses(item["uses"], known | batch_keys)
            uses_given[key] = True
        for field in ("lean", "file"):
            value = _clean_name(item.get(field))
            if value:
                candidate[field] = value

    cyclic_before = blueprint.find_cycles({k: list(n["uses"]) for k, n in existing.items()})
    while True:
        accepted = [k for k in order if k not in rejected]
        available = known | set(accepted)
        before = len(rejected)
        for key in accepted:
            if not uses_given.get(key):
                continue
            uses = candidates[key]["uses"]
            if key in uses:
                reject(key, "self_dependency", f"node `{key}` uses itself.")
                continue
            missing = [dep for dep in uses if dep not in available]
            if missing:
                reject(key, "dangling_dependency",
                       f"node `{key}` uses {', '.join(f'`{d}`' for d in missing)}, which "
                       f"{'is not a node' if len(missing) == 1 else 'are not nodes'}; add "
                       f"{'it' if len(missing) == 1 else 'them'} in the same call or drop the dependency.")
        accepted = [k for k in order if k not in rejected]
        adjacency = {k: list(n["uses"]) for k, n in existing.items()}
        adjacency.update({k: list(candidates[k]["uses"]) for k in accepted})
        cyclic = blueprint.find_cycles(adjacency)
        for key in accepted:
            changed = uses_given.get(key) and candidates[key]["uses"] != list((existing.get(key) or {}).get("uses") or [])
            if key in cyclic and (key not in cyclic_before or changed):
                reject(key, "cycle", f"node `{key}` would be on a dependency cycle.")
        if len(rejected) == before:
            break

    accepted = [k for k in order if k not in rejected]
    accepted_nodes: dict[str, dict] = {}
    for key in accepted:
        candidate = candidates[key]
        lean, file = assign_names(namespace, key)
        candidate["lean"] = candidate.get("lean") or lean
        candidate["file"] = candidate.get("file") or file
        target = existing.get(key)
        if target is not None:
            target.update({field: candidate[field] for field in ("kind", "lean", "file", "uses", "statement")})
            accepted_nodes[key] = target
        else:
            node = {"type": "node", **{f: candidate[f] for f in ("key", "kind", "lean", "file", "uses", "statement")}}
            document["blocks"].append(node)
            document["nodes"].append(node)
            existing[key] = node
            accepted_nodes[key] = node
    return accepted, rejected_list, accepted_nodes


# ── writes ────────────────────────────────────────────────────────────────────


def _default_preamble(project: dict, *, draft: bool) -> str:
    title = project.get("title") or project.get("slug") or ""
    if draft:
        return (f"# Blueprint draft — {title}\n\n"
                "Proposed nodes. Accept them to merge them into the blueprint, or discard the draft.")
    return blueprint.parse(blueprint.empty_document(title))["preamble"]


def _render(project: dict, document: dict, *, draft: bool) -> str:
    if not (document.get("preamble") or "").strip():
        document = {**document, "preamble": _default_preamble(project, draft=draft)}
    return blueprint.render(document)


def _install(path: Path, content: str | None) -> None:
    """Write (atomically) or delete one file. Inside a workspace command the write
    goes through the command's recovery snapshot (``SNAPSHOT_ACTIONS``)."""
    from . import workspace_commands
    if workspace_commands.current() is not None:
        from .workspace_effects import install_from_snapshot
        path.parent.mkdir(parents=True, exist_ok=True)
        install_from_snapshot(path, content)
        return
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".lea-blueprint-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _retire_stale_binding(project_id: str, rel: str) -> None:
    """After a write outside a workspace command (an agent tool call, reconciliation),
    drop the settled SQL binding of ``rel``: it names bytes that are no longer on
    disk. Without this, the next human save compares the old binding with the new
    file, reports an external conflict and holds the project for reconciliation. With
    the binding gone the file reads as untracked, and the save path checks the
    caller's content digest instead. In-flight (``pending``) and ``conflict`` rows
    are left to their owners."""
    from . import db
    with db.write() as conn:
        conn.execute(
            "delete from workspace_files where scope=? and path=? and materialization not in ('pending','conflict')",
            ("p:" + project_id, rel))


def _write(project: dict, proofs_root: Path, changes: dict[str, str | None], message: str) -> str:
    """Install every change, commit them once, record the domain change. Returns the SHA."""
    from . import workspace_commands
    repo = project_repo_dir(project, proofs_root)
    gs = GitStore(proofs_root)
    in_command = workspace_commands.current() is not None
    try:
        for rel, content in changes.items():
            _install(repo / rel, content)
            if not in_command:
                _retire_stale_binding(project["id"], rel)
        paths = []
        for rel in changes:
            if (repo / rel).exists():
                paths.append(rel)
            else:
                # Staging a deletion needs the path to be tracked; an untracked file
                # that was removed has nothing to commit.
                try:
                    if gs._git(repo, "ls-files", "--", rel).strip():
                        paths.append(rel)
                except GitStoreError:
                    pass
        return gs.commit_all(repo, message, paths=paths) if paths else gs._git(repo, "rev-parse", "HEAD").strip()
    finally:
        # Even a failed commit leaves new bytes on disk; the views must not keep
        # serving the old blueprint.
        store.note_blueprint_change(project["id"])


def _subject(keys: list[str], limit: int = 6) -> str:
    shown = ", ".join(keys[:limit])
    return shown + (f" (+{len(keys) - limit} more)" if len(keys) > limit else "")


def _upsert_message(inputs: list, applied: list[str], nodes: dict[str, dict], *, draft: bool, actor: str) -> str:
    target = "blueprint draft" if draft else "blueprint"
    by_key = {item.get("key"): item for item in inputs if isinstance(item, dict)}
    binding = actor == "system" and all(set(by_key.get(k) or {}) <= {"key", "lean", "file"} for k in applied)
    if binding:
        return f"{target}: bind " + _subject([f"{k} → {nodes[k]['lean']}" for k in applied])
    return f"{target}: {actor} upsert {_subject(applied)}"


def _statuses(project: dict, proofs_root: Path, document: dict, *, draft: bool) -> dict[str, str]:
    if draft:
        statuses: dict[str, str] = {}
        for node in document["nodes"]:
            statuses.setdefault(node["key"], "planned" if node["uses"] else "ready")
        return statuses
    from . import graph
    statuses = {}
    for node in graph.build_graph(project, proofs_root)["nodes"]:
        statuses.setdefault(node["key"], node["status"])
    return statuses


def _summary(statuses: dict[str, str], document: dict) -> tuple[list[str], dict]:
    keys = list(dict.fromkeys(node["key"] for node in document["nodes"]))
    counts = {"total": len(keys), **{status: 0 for status in STATUSES}}
    for key in keys:
        status = statuses.get(key, "planned")
        counts[status] = counts.get(status, 0) + 1
    ready = [key for key in keys if statuses.get(key) == "ready"]
    return ready, counts


def _applied_entries(applied: list[str], nodes: dict[str, dict], statuses: dict[str, str]) -> list[dict]:
    return [{"key": key, "kind": nodes[key]["kind"], "lean": nodes[key]["lean"], "file": nodes[key]["file"],
             "uses": list(nodes[key]["uses"]), "status": statuses.get(key, "planned")} for key in applied]


def upsert(project: dict, proofs_root: Path, nodes: list[dict], *, draft: bool = False, actor: str = "agent") -> dict:
    """Create or update nodes (merge by key; absent fields keep their value, ``uses``
    replaces; new nodes append). Every accepted node without ``lean``/``file`` gets
    :func:`assign_names`. Per-node rejection codes: ``invalid_key``,
    ``unknown_kind``, ``missing_kind`` (new node only), ``dangling_dependency``,
    ``self_dependency``, ``cycle``, ``statement_too_long``; rejected nodes are not
    written, the rest are.

    Returns ``{"applied": [{"key","kind","lean","file","uses","status"}],
    "rejected": [{"key","code","message"}], "ready": [key], "counts": {"total",
    "proved","ready","stated","failed","planned"}, "draft": bool, "commit_sha": str | None}``."""
    project_id = project["id"]
    rel = _rel(draft)
    with _lock(project_id), project_coordination.mutation(project_id):
        before = _read(project, proofs_root, rel)
        document = blueprint.parse(before)
        other_keys = {n["key"] for n in load(project, proofs_root)["nodes"]} if draft else set()
        applied, rejected, accepted = _merge_batch(
            document, list(nodes or []), namespace=project["namespace"], other_keys=other_keys)
        text = _render(project, document, draft=draft)
        sha = None
        if applied and text != before:
            sha = _write(project, proofs_root, {rel: text},
                         _upsert_message(list(nodes or []), applied, accepted, draft=draft, actor=actor))
        statuses = _statuses(project, proofs_root, document, draft=draft)
        ready, counts = _summary(statuses, document)
        return {"applied": _applied_entries(applied, accepted, statuses), "rejected": rejected,
                "ready": ready, "counts": counts, "draft": draft, "commit_sha": sha}


def remove(project: dict, proofs_root: Path, keys: list[str], *, cascade: bool = False,
           draft: bool = False, actor: str = "agent") -> dict:
    """Remove nodes by key. A key other remaining nodes use is refused (``in_use``)
    unless ``cascade``, which drops the edge from its dependents; an absent key is
    ``unknown_key``. Returns ``{"removed": [key], "rejected": [{"key","code","message"}],
    "commit_sha": str | None}``."""
    project_id = project["id"]
    rel = _rel(draft)
    with _lock(project_id), project_coordination.mutation(project_id):
        before = _read(project, proofs_root, rel)
        document = blueprint.parse(before)
        present = {node["key"] for node in document["nodes"]}
        rejected: list[dict] = []
        removing: list[str] = []
        for key in dict.fromkeys(k for k in (keys or []) if isinstance(k, str)):
            if key in present:
                removing.append(key)
            else:
                rejected.append(_reject(key, "unknown_key", f"`{key}` is not a node in this blueprint."))
        if not cascade:
            while True:
                gone = set(removing)
                blocked = {}
                for key in removing:
                    users = [n["key"] for n in document["nodes"] if key in n["uses"] and n["key"] not in gone]
                    if users:
                        blocked[key] = users
                if not blocked:
                    break
                for key, users in blocked.items():
                    rejected.append(_reject(key, "in_use",
                        f"`{key}` is used by {', '.join(f'`{u}`' for u in dict.fromkeys(users))}; "
                        "remove those first or pass cascade."))
                removing = [k for k in removing if k not in blocked]
        gone = set(removing)
        document["blocks"] = [b for b in document["blocks"] if not (b["type"] == "node" and b["key"] in gone)]
        document["nodes"] = [n for n in document["nodes"] if n["key"] not in gone]
        for node in document["nodes"]:
            node["uses"] = [dep for dep in node["uses"] if dep not in gone]
        sha = None
        if removing:
            text = _render(project, document, draft=draft)
            if text != before:
                target = "blueprint draft" if draft else "blueprint"
                sha = _write(project, proofs_root, {rel: text},
                             f"{target}: {actor} remove {_subject(removing)}" + (" (cascade)" if cascade else ""))
        return {"removed": removing, "rejected": rejected, "commit_sha": sha}


def accept_draft(project: dict, proofs_root: Path, keys: list[str] | None = None) -> dict:
    """Merge the selected draft nodes (all when ``keys`` is None) into the live
    blueprint with :func:`upsert` semantics (names already assigned), delete the
    draft, commit once. Returns ``{"applied": [...], "rejected": [...], "commit_sha"}``."""
    project_id = project["id"]
    with _lock(project_id), project_coordination.mutation(project_id):
        if not exists_draft(project, proofs_root):
            return {"applied": [], "rejected": [_reject(k, "unknown_key", "There is no blueprint draft.")
                                                for k in (keys or [])], "commit_sha": None}
        draft_nodes = load(project, proofs_root, draft=True)["nodes"]
        by_key: dict[str, dict] = {}
        for node in draft_nodes:
            by_key.setdefault(node["key"], node)
        rejected: list[dict] = []
        if keys is None:
            selected = list(by_key)
        else:
            selected = []
            for key in dict.fromkeys(k for k in keys if isinstance(k, str)):
                if key in by_key:
                    selected.append(key)
                else:
                    rejected.append(_reject(key, "unknown_key", f"`{key}` is not a node in the draft."))
        inputs = [{"key": key, **{field: by_key[key][field] for field in _FIELDS}} for key in selected]
        before = _read(project, proofs_root, LIVE)
        document = blueprint.parse(before)
        applied, refused, accepted = _merge_batch(document, inputs, namespace=project["namespace"], other_keys=set())
        rejected.extend(refused)
        changes: dict[str, str | None] = {DRAFT: None}
        text = _render(project, document, draft=False)
        if applied and text != before:
            changes[LIVE] = text
        message = (f"blueprint: accept draft {_subject(applied)}" if applied else "blueprint: discard draft (nothing accepted)")
        sha = _write(project, proofs_root, changes, message)
        statuses = _statuses(project, proofs_root, document, draft=False)
        return {"applied": _applied_entries(applied, accepted, statuses), "rejected": rejected, "commit_sha": sha}


def discard_draft(project: dict, proofs_root: Path) -> dict:
    """Delete the draft and commit. ``{"discarded": bool}`` (False when there was none)."""
    project_id = project["id"]
    with _lock(project_id), project_coordination.mutation(project_id):
        if not exists_draft(project, proofs_root):
            return {"discarded": False}
        _write(project, proofs_root, {DRAFT: None}, "blueprint: discard draft")
        return {"discarded": True}
