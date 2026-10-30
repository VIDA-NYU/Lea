"""Host side of the blueprint tools (docs/PLAN-blueprint-tools.md §2.3).

The prover yields ``BlueprintRequested(tool, arguments, invocation_key)``; the bridge
calls :func:`tool_request` and answers with ``BlueprintAck``. Writes go through
``blueprint_store``, the only module that writes ``.lea/blueprint.md`` and
``.lea/blueprint.draft.md``. A drafting run (``purpose = blueprint_draft``) reads and
writes the draft; every other project run the live blueprint.

Each reply is recorded per ``(run_id, invocation_key)`` in ``blueprint_invocations``,
as ``project_dependencies.tool_request`` does for the project library: a replayed call
(crash recovery, a resumed turn) returns the recorded reply instead of writing again,
and a key reused with different arguments is refused.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from . import graph, store
from .config import load_config
from .db import connect, write

logger = logging.getLogger("lea-interface.blueprint_host")

TOOL_NAMES = ("blueprint_read", "blueprint_upsert", "blueprint_remove")
LIVE = ".lea/blueprint.md"
DRAFT = ".lea/blueprint.draft.md"
# Repo-relative paths write_file/edit_file refuse in a blueprint-enabled run.
PROTECTED_PATHS = (LIVE, DRAFT)
DRAFT_PURPOSE = "blueprint_draft"
MAX_UPSERT_NODES = 100
# The fields the agent may send (§2.3). `file` is assigned by the system; status is derived.
_AGENT_NODE_FIELDS = ("key", "kind", "statement", "uses", "lean")
_STATUSES = ("proved", "ready", "stated", "failed", "planned")


def _store():
    # Imported lazily: blueprint_store is written by a sibling package, and tests stub it.
    from . import blueprint_store
    return blueprint_store


def _dumps(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(value) -> str:
    return hashlib.sha256(_dumps(value).encode()).hexdigest()


def _proofs_root() -> Path:
    lea_root = load_config().lea_root or (Path(__file__).resolve().parents[2] / "prover")
    return Path(lea_root) / "workspace" / "proofs"


def _scope(run_id: str) -> tuple[dict, dict]:
    run = store.get_run(run_id)
    if not run:
        raise ValueError("Blueprint tools require a known run.")
    project_id = run.get("project_id")
    if not project_id:
        session = store.get_session(run["session_id"]) if run.get("session_id") else None
        project_id = session.get("project_id") if session else None
    project = store.get_project(project_id) if project_id else None
    if not project:
        raise ValueError("Blueprint tools require a project-scoped run.")
    return run, project


def is_draft(run: dict | None) -> bool:
    return bool(run) and run.get("purpose") == DRAFT_PURPOSE


def _string_list(value, what: str, *, required: bool) -> list[str] | None:
    if value is None and not required:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{what} must be a list of node keys.")
    if required and not value:
        raise ValueError(f"{what} must name at least one node key.")
    return value


def _read(project: dict, proofs_root: Path, draft: bool, keys: list[str] | None) -> dict:
    parsed = _store().load(project, proofs_root, draft=draft)
    nodes = [n for n in parsed.get("nodes") or [] if isinstance(n, dict) and n.get("key")]
    if draft:
        # A draft declares nothing yet: every node is planned, and a leaf is ready (§2.8).
        status = {n["key"]: "planned" if n.get("uses") else "ready" for n in nodes}
        verified: dict[str, bool] = {}
    else:
        # Status is derived in one place, graph.build_graph (§2.8); the blueprint file
        # itself supplies the node fields, including the assigned (planned) file.
        derived = graph.build_graph(project, proofs_root).get("nodes") or []
        status = {n["key"]: n.get("status") or "planned" for n in derived}
        verified = {n["key"]: bool(n.get("verified")) for n in derived}
    rows = [{
        "key": n["key"],
        "kind": n.get("kind"),
        "statement": n.get("statement"),
        "uses": list(n.get("uses") or []),
        "lean": n.get("lean"),
        "file": n.get("file"),
        "status": status.get(n["key"], "planned"),
        "verified": verified.get(n["key"], False),
    } for n in nodes]
    counts = {"total": len(rows), **{s: sum(1 for r in rows if r["status"] == s) for s in _STATUSES}}
    blocked = [{"key": r["key"], "waits_on": [d for d in r["uses"] if status.get(d) != "proved"]}
               for r in rows if r["status"] not in ("proved", "ready")]
    blocked = [b for b in blocked if b["waits_on"]]
    if keys is not None:
        wanted = set(keys)
        rows = [r for r in rows if r["key"] in wanted]
        blocked = [b for b in blocked if b["key"] in wanted]
    return {
        "nodes": rows,
        "ready": [r["key"] for r in rows if r["status"] == "ready"],
        "blocked": blocked,
        "counts": counts,
        "draft": draft,
    }


def _upsert_nodes(arguments: dict) -> list[dict]:
    nodes = arguments.get("nodes")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_UPSERT_NODES:
        raise ValueError(f"blueprint_upsert takes a list of 1 to {MAX_UPSERT_NODES} nodes.")
    clean = []
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("key"), str) or not node["key"]:
            raise ValueError("Every blueprint node needs a string key.")
        for field in ("kind", "statement", "lean"):
            if field in node and not isinstance(node[field], str):
                raise ValueError(f"Node {node['key']}: {field} must be a string.")
        if "uses" in node:
            _string_list(node["uses"], f"Node {node['key']}: uses", required=False)
        clean.append({field: node[field] for field in _AGENT_NODE_FIELDS if field in node})
    return clean


def _dispatch(run: dict, project: dict, tool: str, arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("Blueprint tool arguments must be an object.")
    proofs_root = _proofs_root()
    draft = is_draft(run)
    if tool == "blueprint_read":
        return _read(project, proofs_root, draft, _string_list(arguments.get("keys"), "keys", required=False))
    if tool == "blueprint_upsert":
        return _store().upsert(project, proofs_root, _upsert_nodes(arguments), draft=draft, actor="agent")
    if tool == "blueprint_remove":
        keys = _string_list(arguments.get("keys"), "keys", required=True)
        cascade = arguments.get("cascade", False)
        if not isinstance(cascade, bool):
            raise ValueError("cascade must be true or false.")
        return _store().remove(project, proofs_root, keys, cascade=cascade, draft=draft, actor="agent")
    raise ValueError("Unsupported blueprint tool.")


def tool_request(run_id: str, invocation_key: str, tool: str, arguments: dict) -> dict:
    """Answer one blueprint tool call, idempotently per ``(run_id, invocation_key)``."""
    request_hash = _digest({"tool": tool, "arguments": arguments})
    with connect() as conn:
        old = conn.execute("select request_hash,response_json from blueprint_invocations "
                           "where run_id=? and invocation_key=?", (run_id, invocation_key)).fetchone()
    if old:
        if old["request_hash"] != request_hash:
            raise ValueError("Invocation identity was reused for different arguments.")
        return json.loads(old["response_json"])
    run, project = _scope(run_id)
    result = _dispatch(run, project, tool, arguments)
    with write() as conn:
        conn.execute("insert or ignore into blueprint_invocations "
                     "(run_id, invocation_key, request_hash, response_json) values (?,?,?,?)",
                     (run_id, invocation_key, request_hash, _dumps(result)))
        stored = conn.execute("select request_hash,response_json from blueprint_invocations "
                              "where run_id=? and invocation_key=?", (run_id, invocation_key)).fetchone()
    if stored["request_hash"] != request_hash:
        raise ValueError("Invocation identity was reused for different arguments.")
    return json.loads(stored["response_json"])


def updated_event(run_id: str, session_id: str | None, tool: str, result: dict) -> dict | None:
    """The ``blueprint_updated`` payload (§2.4) for a write that changed something, else None.

    Never raises: the write already succeeded, and a missing refresh signal must not
    turn the agent's acknowledgement into an error."""
    if tool not in ("blueprint_upsert", "blueprint_remove") or not isinstance(result, dict):
        return None
    try:
        applied = [n["key"] for n in result.get("applied") or [] if isinstance(n, dict) and n.get("key")]
        removed = [k for k in result.get("removed") or [] if isinstance(k, str)]
        if not applied and not removed:
            return None
        run, project = _scope(run_id)
        return {"project_id": project["id"], "draft": is_draft(run), "applied": applied,
                "removed": removed, "run_id": run_id, "session_id": session_id}
    except Exception:  # noqa: BLE001 — a refresh signal is best effort
        logger.exception("blueprint_updated could not be composed for run %s", run_id)
        return None
