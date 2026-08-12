"""MCP server CRUD + scope + connection test (v2.5 E0/E0b).

The thin REST layer over `store`'s `mcp_servers` table, deliberately shaped like
`routes/skills.py` — the two are the same kind of library item and a user selects
both the same way (global ∪ per-project, D47).

`/test` is the piece OpenHands has no equivalent of: adding a server there saves
fine and fails silently at the next run's startup. Since `MCPManager.start()` is
~0.6 s, the form can answer *now* — connected with N tools, or the child's real
stderr. That converts the whole silent-failure class into a question answered while
the user is still looking at the fields (A2's preflight, E0b's button).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import store

router = APIRouter()


class ServerPayload(BaseModel):
    name: str
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    env_from: list[str] = []
    url: str | None = None
    api_key_name: str | None = None
    enabled: bool = True
    is_global: bool = False
    project_ids: list[str] = []


class ServerUpdate(BaseModel):
    name: str | None = None
    transport: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    env_from: list[str] | None = None
    url: str | None = None
    api_key_name: str | None = None
    enabled: bool | None = None


class ServerAssignment(BaseModel):
    is_global: bool = False
    project_ids: list[str] = []


class ServerTest(BaseModel):
    """A candidate server to dry-run. Accepts an unsaved draft so the form can test
    BEFORE saving — the common case is "did I type this right?", which is worth
    answering before a row exists."""
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    env_from: list[str] = []
    url: str | None = None
    api_key_name: str | None = None


@router.get("/api/mcp-servers")
def list_servers() -> dict:
    return {"servers": store.list_mcp_servers()}


@router.post("/api/mcp-servers", status_code=201)
def create_server(request: ServerPayload) -> dict:
    """Create + scope in one call, matching the Skill Factory's "Add → choose scope"
    (D58). A shape or secret-in-env problem is a 400 the form renders inline."""
    try:
        server = store.create_mcp_server(
            name=request.name, transport=request.transport, command=request.command,
            args=request.args, env=request.env, env_from=request.env_from,
            url=request.url, api_key_name=request.api_key_name, enabled=request.enabled,
        )
        if request.is_global or request.project_ids:
            server = store.set_mcp_server_assignment(
                server["id"], is_global=request.is_global, project_ids=request.project_ids
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return server


@router.get("/api/mcp-servers/defaults")
def server_defaults() -> dict:
    """Machine-specific values the form can offer, so the user never types a path."""
    return {"lean_project_path": _lean_project_path()}


@router.get("/api/mcp-servers/key-requirements")
def key_requirements() -> dict:
    """Which declared credentials are missing, and what breaks without them (D1/D2/D3).

    A server whose key is unsaved starts fine and then fails on first use with a 401 —
    a delayed, confusing failure. Naming the gap here lets the UI raise it at the moment
    the user can act on it, and warn before a key that other servers need is cleared.
    """
    from ..config import configured_provider_keys

    saved = set(configured_provider_keys())
    needs = store.mcp_key_requirements()
    return {
        "requirements": [
            {"env": env, "servers": sorted(set(servers)), "configured": env in saved}
            for env, servers in sorted(needs.items())
        ]
    }


@router.get("/api/mcp-servers/catalog")
def list_catalog() -> dict:
    """The curated entries, each marked with whether it is already installed (E2)."""
    from .. import catalog

    installed = {s["slug"] for s in store.list_mcp_servers()}
    out = []
    for entry in catalog.entries():
        item = dict(entry)
        item["installed"] = store.slugify_skill(entry["server"]["name"]) in installed
        out.append(item)
    return {"entries": out}


@router.post("/api/mcp-servers/catalog/{entry_id}", status_code=201)
def install_catalog_entry(entry_id: str) -> dict:
    """Install one curated entry: the server, pinned, with its Lean path filled in, and
    its recommended tool subset recorded (E2).

    The SKILL is not installed here — importing it is a separate, visible act under
    Library → Skills, because it brings sub-agents too and an install that silently added
    four of those would be worse than one that added none.
    """
    from .. import catalog

    entry = catalog.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown catalog entry")
    spec = entry["server"]
    env = {}
    if spec.get("needs_lean_path"):
        path = _lean_project_path()
        if path:
            env["LEAN_PROJECT_PATH"] = path
    try:
        server = store.create_mcp_server(
            name=spec["name"], transport=spec.get("transport", "stdio"),
            command=spec.get("command"), args=list(spec.get("args") or []),
            env=env, enabled=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {**server, "skill_url": entry.get("skill_url"),
            "recommended_tools": entry.get("recommended_tools") or []}


# NOTE: every STATIC /api/mcp-servers/<name> route must be declared ABOVE this one.
# FastAPI matches in declaration order, so `{server_id}` otherwise swallows them and the
# endpoint 404s as "MCP server not found" — which is how `defaults` shipped broken, since
# the tests called the function directly and never exercised routing.
@router.get("/api/mcp-servers/{server_id}")
def get_server(server_id: str) -> dict:
    server = store.get_mcp_server(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return server


@router.put("/api/mcp-servers/{server_id}")
def update_server(server_id: str, request: ServerUpdate) -> dict:
    try:
        updated = store.update_mcp_server(server_id, **request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return updated


@router.put("/api/mcp-servers/{server_id}/assignment")
def set_assignment(server_id: str, request: ServerAssignment) -> dict:
    try:
        updated = store.set_mcp_server_assignment(
            server_id, is_global=request.is_global, project_ids=request.project_ids
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return updated


@router.delete("/api/mcp-servers/{server_id}")
def delete_server(server_id: str) -> dict:
    if not store.delete_mcp_server(server_id):
        raise HTTPException(status_code=404, detail="MCP server not found")
    return {"deleted": True, "id": server_id}


class SkillMcpToggle(BaseModel):
    kind: str            # 'skill' | 'mcp_server'
    item_id: str
    action: str | None   # 'add' | 'remove' | null to clear the override


@router.get("/api/sessions/{session_id}/skills-mcp")
def session_skills_mcp(session_id: str) -> dict:
    """What THIS session resolves to, item by item, with where each state came from (E0e).

    Returns the whole library — not just the active items — because the picker's job is
    "here is everything, tick what this session should use". `source` is what makes the
    two tiers legible: `global` and `project` items are on by inheritance, `session` ones
    were toggled here. Without it the user cannot tell why something is ticked, and
    un-ticking a global item would look like it silently failed.
    """
    session = store.session_detail(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    project_id = session.get("project_id")

    def describe(items_all, active_ids, overrides, kind):
        out = []
        for item in items_all:
            override = overrides.get(item["id"])
            if item.get("is_global"):
                source = "global"
            elif override == "add":
                source = "session"
            elif override == "remove":
                source = "session-off"
            elif item["id"] in active_ids:
                source = "project"
            else:
                source = None
            out.append({
                "id": item["id"], "name": item["name"], "slug": item["slug"],
                "kind": kind, "on": item["id"] in active_ids, "source": source,
                "locked": bool(item.get("is_global")),
                "enabled": bool(item.get("enabled", True)),
            })
        return out

    skills_all = store.list_skills()
    skills_on = {s["id"] for s in store.skills_for_run(project_id, session_id)}
    servers_all = store.list_mcp_servers()
    servers_on = {s["id"] for s in store.mcp_servers_for_run(project_id, session_id)}

    return {
        "session_id": session_id,
        "project_id": project_id,
        "skills": describe(skills_all, skills_on,
                           store.session_skill_mcp_overrides(session_id, "skill"), "skill"),
        "mcp_servers": describe(servers_all, servers_on,
                                store.session_skill_mcp_overrides(session_id, "mcp_server"),
                                "mcp_server"),
    }


@router.put("/api/sessions/{session_id}/skills-mcp")
def update_session_skills_mcp(session_id: str, request: SkillMcpToggle) -> dict:
    """Add/drop one item for this session, or clear the override to inherit the project
    again. Stores the DIFF, so a later project-level change still reaches this session."""
    try:
        store.set_session_skill_mcp(session_id, request.kind, request.item_id, request.action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return session_skills_mcp(session_id)


def _lean_project_path() -> str | None:
    """The vendored Lake root, when it looks like a real Lean project (A4).

    This is the single field a mathematician cannot possibly supply — the vendored
    config shipped `LEAN_PROJECT_PATH: /ABSOLUTE/PATH/TO/...` as a literal placeholder —
    and the adapter has always known it. Returning it lets the form offer it in one
    click instead of asking someone to find it.
    """
    from ..config import load_config

    root = load_config().lea_root
    if not root:
        return None
    workspace = Path(root) / "workspace"
    if (workspace / "lean-toolchain").is_file() and (
        (workspace / "lakefile.lean").is_file() or (workspace / "lakefile.toml").is_file()
    ):
        return str(workspace)
    return None


def _preflight(transport: str, command: str | None, env: dict) -> str | None:
    """The deterministic checks, run BEFORE spawning anything (A2).

    Both are mistakes a spawn would only reveal via a traceback several seconds later,
    and both have exact answers available right now. Returns a ready-to-show sentence,
    or None when there is nothing to object to.
    """
    if transport == "stdio":
        if not shutil.which(command or ""):
            return (
                f"Lea can't find “{command}” on this computer. Check the spelling, or "
                f"install it first — most Lean servers are run with “uvx”."
            )
    path = (env or {}).get("LEAN_PROJECT_PATH")
    if path:
        p = Path(path).expanduser()
        if not p.is_dir():
            return f"There's no folder at {path}."
        if not (p / "lean-toolchain").is_file() or not (
            (p / "lakefile.lean").is_file() or (p / "lakefile.toml").is_file()
        ):
            return (
                f"{path} doesn't look like a Lean project — it needs a lean-toolchain "
                f"file and a lakefile."
            )
    return None


@router.post("/api/mcp-servers/test")
def test_server(request: ServerTest) -> dict:
    """Dry-run a server spec: connect, list its tools, disconnect.

    Returns `{ok, tool_count, tools, error, detail}`. `detail` is the child's real
    stderr when there is one — the line that actually says what is wrong (a missing
    lakefile, a command not on PATH), which before A3 was written to a terminal
    nobody reads.

    Deliberately NOT pooled: a test must observe a cold start, and caching a spec the
    user is still editing would hand the next edit a stale connection.
    """
    from lea.mcp import MCPManager, summarize_stderr

    spec: dict = {}
    if request.transport == "stdio":
        if not (request.command or "").strip():
            raise HTTPException(status_code=400, detail="A stdio server needs a command.")
        spec = {"command": request.command.strip(), "args": list(request.args)}
        if request.env:
            spec["env"] = dict(request.env)
        if request.env_from:
            spec["env_from"] = list(request.env_from)
    else:
        if not (request.url or "").strip():
            raise HTTPException(status_code=400, detail=f"A {request.transport} server needs a URL.")
        spec = {"url": request.url.strip()}
        if request.transport == "sse":
            spec["transport"] = "sse"

    # A2: answer the two knowable mistakes instantly instead of spawning, waiting, and
    # parsing a traceback for the same information.
    objection = _preflight(request.transport, request.command, request.env)
    if objection:
        return {"ok": False, "tool_count": 0, "tools": [],
                "error": "That won't start.", "reason": objection, "detail": ""}

    manager = MCPManager({"probe": spec})
    try:
        manager.start(register=False)
        if manager.startup_errors:
            err = manager.startup_errors[0]
            full = err.get("stderr_tail") or err.get("message") or ""
            # `reason` is the one line that says what to fix; `detail` keeps the raw
            # tail behind it. A stdio crash arrives as a full traceback whose last
            # line is box-drawing, so showing the tail verbatim tells the user nothing.
            return {
                "ok": False,
                "tool_count": 0,
                "tools": [],
                "error": "The server did not start.",
                "reason": summarize_stderr(full) or err.get("message") or "",
                "detail": full,
            }
        tools = [t["tool"] for t in manager._discovered]
        if not tools:
            # G3: it connected and offered nothing. Nothing raised, so without this
            # assertion the user would see a green "connected" and later wonder why the
            # agent never used it.
            return {"ok": False, "tool_count": 0, "tools": [],
                    "error": "It started, but offers no tools.",
                    "reason": "Lea connected to the server and it listed no tools, so it "
                              "would add nothing to a proof.",
                    "detail": ""}
        return {"ok": True, "tool_count": len(tools), "tools": tools,
                "error": None, "reason": "", "detail": ""}
    except Exception as exc:  # noqa: BLE001 — a failed probe is a result, not a 500
        return {"ok": False, "tool_count": 0, "tools": [],
                "error": "The server could not be reached.",
                "reason": f"{type(exc).__name__}: {exc}", "detail": ""}
    finally:
        manager.stop()
