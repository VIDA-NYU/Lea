"""Skill Factory CRUD + assignment endpoints (v2.1.1 W2, D50).

A skill is a DB row (markdown `body` in a column), not a git file (D45); the
store half is in `store` (W1). These routes are the thin REST layer over it:
list/create/read/update/delete plus the scope assignment (`is_global` ∪ the
per-project join, D47). No prover coupling — run-time resolution to
`cfg.skills` is W3, GitHub import is W4.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import ghimport, store
from ..config import github_token

router = APIRouter()


class SkillCreate(BaseModel):
    name: str
    body: str = ""            # supplied directly, or compiled from `authoring`
    authoring: dict | None = None
    is_global: bool = False
    project_ids: list[str] = []


class SkillUpdate(BaseModel):
    name: str | None = None
    body: str | None = None
    authoring: dict | None = None


class SkillAssignment(BaseModel):
    is_global: bool = False
    project_ids: list[str] = []


class SkillImport(BaseModel):
    url: str
    is_global: bool = False
    project_ids: list[str] = []


@router.get("/api/skills")
def list_skills() -> dict:
    return {"skills": store.list_skills()}


@router.post("/api/skills", status_code=201)
def create_skill(request: SkillCreate) -> dict:
    """Author a skill. Scope is applied in the same call (D58: "Add → choose
    scope"): when `is_global` or `project_ids` are given, the assignment is set
    right after create so the row comes back fully scoped."""
    try:
        skill = store.create_skill(request.name, request.body,
                                   authoring=request.authoring)
        if request.is_global or request.project_ids:
            skill = store.set_skill_assignment(
                skill["id"], is_global=request.is_global, project_ids=request.project_ids
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return skill


@router.post("/api/skills/import", status_code=201)
def import_skill(request: SkillImport) -> dict:
    """Add a skill from a GitHub link (D56): shallow-clone → snapshot the md into
    `body` → create + scope in one call. Uses the global GitHub token from Settings
    for private/rate-limited repos (public repos need none). A bad URL / clone
    failure / missing md is a 400."""
    try:
        imported = ghimport.fetch_skill(request.url, github_token())
    except ghimport.GitHubImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    try:
        skill = store.create_skill(
            imported.name, imported.body,
            source_url=imported.source_url, source_ref=imported.source_ref,
            # H4: the author's own `description:` — NOT as an authoring field. Passing it
            # as `authoring` would recompile `body` from that one line and discard the
            # imported SKILL.md entirely; and it would switch the editor to the guided
            # form, whose next save would do the same. An imported skill keeps its prose.
            description=imported.description,
        )
        if imported.files:
            store.set_skill_files(skill["id"], imported.files)
            skill = store.get_skill(skill["id"])
        if request.is_global or request.project_ids:
            skill = store.set_skill_assignment(
                skill["id"], is_global=request.is_global, project_ids=request.project_ids
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    # H5/H8: a real skill repo bundles more than the skill. Roles and servers are created
    # alongside it, and reported back so the import can say what it brought — an import
    # that silently adds a sub-agent would be worse than one that adds none.
    skill["imported_roles"] = _import_roles(imported.roles)
    skill["imported_servers"] = _import_servers(imported.mcp_servers)
    return skill


def _import_roles(roles: list[dict]) -> list[dict]:
    """Create each bundled role, skipping ones whose name is taken. A collision is
    reported, never silently overwritten — the existing role may be one the user wrote."""
    from lea import profiles as lea_profiles

    reserved = set(lea_profiles.available_profiles())
    created: list[dict] = []
    for role in roles:
        try:
            row = store.create_agent_role(
                name=role["name"], system_prompt=role["system_prompt"],
                description=role.get("description"), tools=role.get("tools"),
                reserved_names=reserved,
            )
            created.append({"name": row["slug"], "status": "added",
                            "unmapped_tools": role.get("unmapped_tools") or []})
        except ValueError as exc:
            created.append({"name": role["name"], "status": "skipped", "reason": str(exc)})
    return created


def _import_servers(servers: dict) -> list[dict]:
    """Create each declared MCP server **disabled**.

    The skill says which servers it wants; it does not get to start them. Running a
    third-party command on the user's machine stays an explicit act — they enable it in
    Library → MCP servers, where the Test button is.
    """
    created: list[dict] = []
    for name, spec in (servers or {}).items():
        if not isinstance(spec, dict):
            continue
        try:
            row = store.create_mcp_server(
                name=name,
                transport="stdio" if spec.get("command") else "http",
                command=spec.get("command"),
                args=[str(a) for a in spec.get("args") or []],
                env={k: str(v) for k, v in (spec.get("env") or {}).items()},
                url=spec.get("url"),
                enabled=False,
            )
            created.append({"name": row["slug"], "status": "added_disabled"})
        except ValueError as exc:
            created.append({"name": name, "status": "skipped", "reason": str(exc)})
    return created


@router.get("/api/skills/{skill_id}")
def get_skill(skill_id: str) -> dict:
    skill = store.get_skill(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@router.put("/api/skills/{skill_id}")
def update_skill(skill_id: str, request: SkillUpdate) -> dict:
    try:
        updated = store.update_skill(skill_id, name=request.name, body=request.body,
                                     authoring=request.authoring)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return updated


@router.put("/api/skills/{skill_id}/assignment")
def set_skill_assignment(skill_id: str, request: SkillAssignment) -> dict:
    """Set a skill's scope (D47): `is_global` plus the explicit per-project join.
    Replaces the join wholesale; unknown project ids are a 400."""
    try:
        updated = store.set_skill_assignment(
            skill_id, is_global=request.is_global, project_ids=request.project_ids
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return updated


@router.delete("/api/skills/{skill_id}")
def delete_skill(skill_id: str) -> dict:
    if not store.delete_skill(skill_id):
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"deleted": True, "id": skill_id}
