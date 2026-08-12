"""Declarative HTTP tool CRUD (v2.5 F1/F3).

Shaped like `routes/mcp_servers.py` — the two are the same kind of library item, and a
user scopes both the same way. The URL is validated against the prover's own `check_url`
at save time, so a tool cannot be stored pointing somewhere it would be refused at call
time (G6: fail while the user is looking at the field, not mid-proof).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import store

router = APIRouter()


class ToolPayload(BaseModel):
    name: str
    url: str
    description: str = ""
    authoring: dict | None = None
    method: str = "GET"
    params: dict = {}
    headers: dict = {}
    auth_key_name: str | None = None
    auth_header: str | None = None
    timeout: int | None = None
    enabled: bool = True
    is_global: bool = False
    project_ids: list[str] = []


class ToolAssignment(BaseModel):
    is_global: bool = False
    project_ids: list[str] = []


@router.get("/api/custom-tools")
def list_tools() -> dict:
    return {"tools": store.list_custom_tools()}


@router.post("/api/custom-tools", status_code=201)
def create_tool(request: ToolPayload) -> dict:
    try:
        tool = store.create_custom_tool(
            name=request.name, url=request.url, description=request.description,
            method=request.method, params=request.params, headers=request.headers,
            auth_key_name=request.auth_key_name, auth_header=request.auth_header,
            timeout=request.timeout, enabled=request.enabled, authoring=request.authoring,
        )
        if request.is_global or request.project_ids:
            tool = store.set_custom_tool_assignment(
                tool["id"], is_global=request.is_global, project_ids=request.project_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return tool


@router.get("/api/custom-tools/catalog")
def list_tool_catalog() -> dict:
    """Suggested tools, each marked with whether it is already installed (E2)."""
    from .. import catalog

    installed = {t["slug"] for t in store.list_custom_tools()}
    return {"entries": [
        {**e, "installed": store.slugify_skill(e["tool"]["name"]) in installed}
        for e in catalog.tool_entries()
    ]}


@router.post("/api/custom-tools/catalog/{entry_id}", status_code=201)
def install_tool_entry(entry_id: str) -> dict:
    """Install a suggested tool, UNSCOPED.

    Deliberately not global: a suggested tool may duplicate something the user already has
    (Loogle vs the Lean LSP server's `lean_loogle`), and T1 measured that handing the agent
    two ways to do one thing makes it choose by familiarity rather than fit. Scoping stays
    an explicit act, and the tool's own "when NOT to use" text carries the tiebreaker.
    """
    from .. import catalog

    entry = catalog.get_tool_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown tool")
    spec = entry["tool"]
    names = list(spec.get("params") or [])
    params = {
        "type": "object",
        "properties": {n: {"type": "string", "description": f"The {n} to send."} for n in names},
        "required": names,
    } if names else {}
    try:
        tool = store.create_custom_tool(
            name=spec["name"], url=spec["url"], params=params,
            authoring=spec.get("authoring"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return tool


# NOTE: static routes must stay ABOVE `{tool_id}` — FastAPI matches in declaration order.
@router.get("/api/custom-tools/{tool_id}")
def get_tool(tool_id: str) -> dict:
    tool = store.get_custom_tool(tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    return tool


@router.put("/api/custom-tools/{tool_id}/assignment")
def set_assignment(tool_id: str, request: ToolAssignment) -> dict:
    try:
        updated = store.set_custom_tool_assignment(
            tool_id, is_global=request.is_global, project_ids=request.project_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    return updated


@router.delete("/api/custom-tools/{tool_id}")
def delete_tool(tool_id: str) -> dict:
    if not store.delete_custom_tool(tool_id):
        raise HTTPException(status_code=404, detail="Tool not found")
    return {"deleted": True, "id": tool_id}
