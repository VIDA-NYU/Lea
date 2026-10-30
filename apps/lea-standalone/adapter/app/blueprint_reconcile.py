"""Blueprint reconciliation: keep the plan in step with the Lean the agent writes.

The blueprint (``.lea/blueprint.md``) names declarations; the project repo declares
them. Nothing guarantees the two agree, so after every passing ``lean_check`` in a
project run the bridge calls :func:`reconcile`, which

1. binds each node whose ``lean`` resolves to no declaration to the declaration
   that obviously is that node: the full name ``<namespace>.<key>`` first, else a
   declaration whose short name is the key and is unique in the repo (never an
   ambiguous one), written through ``blueprint_store.upsert`` as the ``system``
   actor in one commit;
2. diffs the derived graph before and after (:func:`diff_graph`), which the bridge
   streams as ``blueprint_node_changed`` and ``blueprint_unlocked`` events.

It also owns the two run-start pieces of the blueprint: naming the node a session is
bound to (:func:`bind_session_node`) and the ``## Project Blueprint`` section of the
composed project context (:func:`context_section`).

Status is never stored; it is always ``graph.build_graph``'s derivation. This module
only decides which declaration a node names. See docs/PLAN-blueprint-tools.md
§2.4, §2.6, §2.7 and §2.10.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import graph
from .projects import project_repo_dir

logger = logging.getLogger("lea-interface.blueprint_reconcile")

# Order of the per-status counts in the context message's counts line.
_COUNT_ORDER = ("proved", "ready", "stated", "failed", "planned")
# How many ready keys the counts line spells out before summarising the rest.
_READY_SHOWN = 8

TOOL_INSTRUCTION = (
    "Use blueprint_read for the full plan and blueprint_upsert to add or change nodes "
    "(one call for many nodes). The system renders .lea/blueprint.md; do not edit it."
)


# ── Graph helpers ────────────────────────────────────────────────────────────────


def _lean(node: dict) -> str | None:
    """The declaration a node names: the resolved name when it resolves, else the
    ``lean:`` value as written."""
    return node.get("resolved_lean") or node.get("lean")


def _file(node: dict) -> str | None:
    """The file a node lives in: the declaring file when its name resolves, else the
    planned ``file:`` value (``planned_file``, graph §2.8)."""
    return node.get("file") or node.get("planned_file")


def _declared(node: dict) -> bool:
    return bool(node.get("resolved_lean"))


def snapshot(project: dict, proofs_root: Path) -> list[dict] | None:
    """The derived graph's nodes right now, or None when the graph cannot be built.

    The bridge takes this before a check's verdict is recorded, so the diff after
    reconciliation reports what the check itself changed (a node bound earlier is
    already ``proved`` by the time reconciliation could look)."""
    try:
        return graph.build_graph(project, Path(proofs_root))["nodes"]
    except Exception:
        logger.exception("Could not derive the blueprint graph of project %s", project.get("id"))
        return None


# ── Diff ─────────────────────────────────────────────────────────────────────────


def diff_graph(before_nodes: list[dict], after_nodes: list[dict]) -> dict:
    """Compare two derived graphs (pure).

    ``unlocked`` lists, in blueprint order, the nodes whose status became ``ready``.
    ``changed`` lists every other status change as ``{"key", "from", "to", "lean",
    "file"}``; a node that became ready is reported once, as an unlock. Nodes that
    exist on one side only are skipped: a node appearing or disappearing is a
    blueprint edit (``blueprint_updated``), not a status change."""
    before = {}
    for node in before_nodes:
        before.setdefault(node["key"], node.get("status"))
    changed: list[dict] = []
    unlocked: list[str] = []
    seen: set[str] = set()
    for node in after_nodes:
        key = node["key"]
        if key in seen or key not in before:
            continue
        seen.add(key)
        old, new = before[key], node.get("status")
        if old == new:
            continue
        if new == "ready":
            unlocked.append(key)
        else:
            changed.append({"key": key, "from": old, "to": new,
                            "lean": _lean(node), "file": _file(node)})
    return {"changed": changed, "unlocked": unlocked}


# ── Binding ──────────────────────────────────────────────────────────────────────


def _bindings(nodes: list[dict], fqn_to_file: dict[str, str | None], namespace: str) -> list[dict]:
    """The ``{"key", "lean", "file"}`` bindings for nodes whose name resolves to no
    declaration. Exact full name ``<namespace>.<key>`` first; otherwise the one
    declaration whose short name is the key. A name declared in two files, a short
    name shared by two declarations, or a declaration another node already names is
    never bound."""
    claimed = {node["resolved_lean"] for node in nodes if node.get("resolved_lean")}
    by_short: dict[str, list[str]] = {}
    for fqn in fqn_to_file:
        by_short.setdefault(graph._short(fqn), []).append(fqn)
    bindings: list[dict] = []
    seen: set[str] = set()
    for node in nodes:
        key = node["key"]
        if key in seen:
            continue
        seen.add(key)
        if _declared(node):
            continue
        full = f"{namespace}.{key}" if namespace else key
        if full in fqn_to_file:
            target = full
        else:
            matches = by_short.get(key, [])
            target = matches[0] if len(matches) == 1 else None
        if target is None or fqn_to_file.get(target) is None or target in claimed:
            continue
        claimed.add(target)
        bindings.append({"key": key, "lean": target, "file": fqn_to_file[target]})
    return bindings


def reconcile(
    project: dict,
    proofs_root: Path,
    *,
    changed_path: str | None = None,
    before: list[dict] | None = None,
) -> dict:
    """Bind unresolved nodes to their declarations and report what changed.

    ``before`` is the node list to diff against (the bridge passes a
    :func:`snapshot` taken before the check's verdict was recorded); when omitted
    the graph is derived now. ``changed_path`` is the repo-relative file whose
    check triggered this, used only for logging.

    Returns ``{"bound": [{"key", "lean", "file"}], "changed": [{"key", "from",
    "to", "lean", "file"}], "unlocked": [key]}``."""
    proofs_root = Path(proofs_root)
    current = graph.build_graph(project, proofs_root)["nodes"]
    if before is None:
        before = current
    fqn_to_file, _ = graph._scan_lean_decls(project_repo_dir(project, proofs_root))
    bindings = _bindings(current, fqn_to_file, project.get("namespace") or "")
    bound: list[dict] = []
    if bindings:
        from . import blueprint_store
        result = blueprint_store.upsert(project, proofs_root, bindings, actor="system") or {}
        rejected = {item.get("key"): item for item in result.get("rejected") or []}
        for key, item in rejected.items():
            logger.warning("Blueprint node %s of project %s was not bound: %s %s", key,
                           project.get("id"), item.get("code"), item.get("message"))
        bound = [binding for binding in bindings if binding["key"] not in rejected]
        if bound:
            logger.info("Bound blueprint nodes of project %s after %s: %s", project.get("id"),
                        changed_path or "a check",
                        ", ".join(f"{b['key']} → {b['lean']}" for b in bound))
            current = graph.build_graph(project, proofs_root)["nodes"]
    return {"bound": bound, **diff_graph(before, current)}


# ── Bridge glue ──────────────────────────────────────────────────────────────────


def stream_events(
    project_id: str,
    result: dict,
    *,
    run_id: str | None,
    session_id: str | None,
    changed_path: str | None = None,
) -> list[tuple[str, dict]]:
    """The ``(event_type, payload)`` pairs a reconciliation result streams (§2.4):
    one ``blueprint_node_changed`` per change, then one ``blueprint_unlocked`` when
    anything became ready. Its ``cause`` is the node that became proved, preferring
    the one declared in ``changed_path``; None when nothing became proved."""
    events: list[tuple[str, dict]] = []
    for change in result.get("changed") or []:
        events.append(("blueprint_node_changed", {
            "project_id": project_id, "key": change["key"],
            "from": change["from"], "to": change["to"],
            "lean": change.get("lean"), "file": change.get("file"),
            "run_id": run_id, "session_id": session_id,
        }))
    unlocked = list(result.get("unlocked") or [])
    if unlocked:
        proved = [c for c in result.get("changed") or [] if c.get("to") == "proved"]
        own = [c for c in proved if changed_path and c.get("file") == changed_path]
        cause = (own or proved or [{}])[0].get("key")
        events.append(("blueprint_unlocked", {
            "project_id": project_id, "keys": unlocked, "cause": cause,
            "run_id": run_id, "session_id": session_id,
        }))
    return events


def after_check(
    project: dict,
    proofs_root: Path,
    changed_path: str,
    *,
    before: list[dict] | None,
    run_id: str | None,
    session_id: str | None,
) -> list[tuple[str, dict]]:
    """Reconcile after an accepted passing check and return the events to stream.

    Never raises: the check is already recorded, and a blueprint that cannot catch
    up must not fail the run. A failure is logged and streams nothing."""
    try:
        result = reconcile(project, proofs_root, changed_path=changed_path, before=before)
        return stream_events(project["id"], result, run_id=run_id, session_id=session_id,
                             changed_path=changed_path)
    except Exception:
        logger.exception("Could not reconcile the blueprint of project %s after checking %s (run %s)",
                         project.get("id"), changed_path, run_id)
        return []


def bind_session_node(project: dict, proofs_root: Path, key: str) -> dict | None:
    """Make sure the node a session proves names its declaration before the run starts.

    A node whose name does not resolve yet gets one through
    ``blueprint_store.upsert([{"key": key}], actor="system")``, which assigns
    ``<namespace>.<key>`` and its file when they are missing and leaves names the
    node already has. Returns the node's ``{"key", "lean", "file"}``, or None when
    the key is not a node of the blueprint (nothing is created)."""
    proofs_root = Path(proofs_root)
    nodes = {node["key"]: node for node in graph.build_graph(project, proofs_root)["nodes"]}
    node = nodes.get(key)
    if node is None:
        return None
    if _declared(node):
        return {"key": key, "lean": _lean(node), "file": _file(node)}
    from . import blueprint_store
    result = blueprint_store.upsert(project, proofs_root, [{"key": key}], actor="system") or {}
    for item in result.get("applied") or []:
        if item.get("key") == key:
            return {"key": key, "lean": item.get("lean"), "file": item.get("file")}
    for item in result.get("rejected") or []:
        logger.warning("Blueprint node %s of project %s could not be named: %s %s", key,
                       project.get("id"), item.get("code"), item.get("message"))
    return {"key": key, "lean": node.get("lean"), "file": _file(node)}


# ── Context message (§2.7) ───────────────────────────────────────────────────────


def counts_line(nodes: list[dict]) -> str:
    """``19 nodes · 1 proved · 2 ready (a, b) · 16 planned``; zero counts omitted."""
    total = len(nodes)
    parts = [f"{total} node{'' if total == 1 else 's'}"]
    for status in _COUNT_ORDER:
        keys = [node["key"] for node in nodes if node.get("status") == status]
        if not keys:
            continue
        part = f"{len(keys)} {status}"
        if status == "ready":
            shown = keys[:_READY_SHOWN]
            more = len(keys) - len(shown)
            part += " (" + ", ".join(shown) + (f", +{more} more" if more else "") + ")"
        parts.append(part)
    return " · ".join(parts)


def _dependency_line(key: str, node: dict | None) -> str:
    if node is None:
        return f"- `{key}` — not in the blueprint: state what you need as a hypothesis"
    status = node.get("status")
    file = _file(node)
    where = f" (`{file}`)" if file else ""
    if status == "proved":
        name = f" as `{_lean(node)}`" if _lean(node) else ""
        return f"- `{key}` — proved{name}{where}"
    if status == "failed":
        return f"- `{key}` — failing its check{where}: use as a hypothesis"
    if _declared(node):  # stated, or ready over a stated declaration
        return f"- `{key}` — stated with sorry{where}: use as a hypothesis"
    return f"- `{key}` — planned: state what you need as a hypothesis"


def _bound_block(key: str, nodes: list[dict], namespace: str) -> str:
    by_key: dict[str, dict] = {}
    for node in nodes:
        by_key.setdefault(node["key"], node)
    node = by_key.get(key)
    if node is None:
        return (f"This session is bound to the blueprint node `{key}`, which is not in the "
                "blueprint. Call blueprint_read to see the current plan.")
    kind = f" ({node['kind']})" if node.get("kind") else ""
    statement = (node.get("statement") or "").strip() or "(no statement yet)"
    lean = _lean(node) or (f"{namespace}.{key}" if namespace else key)
    file = _file(node)
    where = f" in `{file}`" if file else ""
    lines = [f"This session proves `{key}`{kind}: {statement}"]
    if node.get("status") == "proved":
        lines.append(f"It is already proved as `{lean}`{where}.")
    else:
        lines.append(f"Declare it as `{lean}`{where}.")
    uses = node.get("uses") or []
    if uses:
        lines.append("Dependencies:")
        lines.extend(_dependency_line(dep, by_key.get(dep)) for dep in uses)
    else:
        lines.append("Dependencies: none.")
    return "\n".join(lines)


def context_section(project: dict, proofs_root: Path, blueprint_node: str | None = None) -> str:
    """The body of the context message's ``## Project Blueprint`` section: the counts
    line, the bound node's block when the session proves one, and the tool
    instruction. The blueprint itself is not dumped; blueprint_read serves it."""
    nodes = graph.build_graph(project, Path(proofs_root))["nodes"]
    parts = [counts_line(nodes)]
    if blueprint_node:
        parts.append(_bound_block(blueprint_node, nodes, project.get("namespace") or ""))
    parts.append(TOOL_INSTRUCTION)
    return "\n\n".join(parts)
