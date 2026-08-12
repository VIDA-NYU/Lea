"""Sub-agents endpoints (D6): view/edit every built-in role's settings.

Read the vendored role profiles from the prover (`lea.profiles`) and merge the user's
stored per-role overrides over them. A role is never mutated on disk — edits persist as
overrides (see `app.subagent_overrides`), merged at spawn by the prover's `_child_config`.
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from lea import profiles as lea_profiles

from .. import store, subagent_overrides

router = APIRouter()
logger = logging.getLogger("lea-interface.subagents")


class OverrideRequest(BaseModel):
    model: str | None = None
    max_turns: int | None = None
    max_cost: float | None = None
    system_prompt: str | None = None
    tools: list[str] | None = None


def _defaults(prof) -> dict:
    # max_cost is net-new (no YAML field), so its default is always None (uncapped).
    return {
        "model": prof.model,            # None → inherit the coordinator's model
        "max_turns": prof.max_turns,
        "max_cost": None,
        "system_prompt": prof.system_prompt,
        "tools": list(prof.tools or []),
    }


def _profile_payload(name: str) -> dict:
    prof = lea_profiles.load_profile(name)
    default = _defaults(prof)
    override = subagent_overrides.get_override(name)
    effective = {**default, **override}
    return {
        "name": prof.name,
        "description": prof.description,
        "default": default,
        "override": override,
        "effective": effective,
    }


class RoleCreate(BaseModel):
    name: str
    system_prompt: str = ""      # supplied directly, or compiled from `authoring`
    authoring: dict | None = None
    description: str | None = None
    model: str | None = None
    tools: list[str] | None = None
    max_turns: int | None = None


class RoleUpdate(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    authoring: dict | None = None
    description: str | None = None
    model: str | None = None
    tools: list[str] | None = None
    max_turns: int | None = None


def _vendored_names() -> set[str]:
    """The roles shipped inside the prover. Read WITHOUT a run context, so it sees only
    the vendored directory — which is exactly what "reserved" means here."""
    return set(lea_profiles.available_profiles())


def _user_role_payload(role: dict) -> dict:
    """A user role in the same shape as a built-in, so the page renders one list.

    `origin` is the only difference the frontend needs: a built-in can be retuned but not
    deleted, a user role can be edited and deleted outright.
    """
    return {
        "name": role["slug"],
        "description": role["description"],
        "origin": "user",
        "id": role["id"],
        "authoring": role.get("authoring") or {},
        "default": {
            "model": role["model"], "max_turns": role["max_turns"], "max_cost": None,
            "system_prompt": role["system_prompt"], "tools": list(role["tools"] or []),
        },
        "override": {},
        "effective": {
            "model": role["model"], "max_turns": role["max_turns"], "max_cost": None,
            "system_prompt": role["system_prompt"], "tools": list(role["tools"] or []),
        },
    }


@router.get("/api/sub-agents/profiles")
def list_profiles() -> dict:
    """Every role the coordinator can be offered — built-in and user-authored.

    Both kinds appear in one list because they are one concept to the user; `origin`
    distinguishes what may be done to each.
    """
    builtin = [{**_profile_payload(n), "origin": "builtin", "id": n}
               for n in lea_profiles.available_profiles()]
    return {"profiles": builtin + [_user_role_payload(r) for r in store.list_agent_roles()]}


@router.post("/api/sub-agents/roles", status_code=201)
def create_role(request: RoleCreate) -> dict:
    """Author a new role (B2/B3). Refused if the name collides with a built-in — two
    roles answering to one name makes "which one ran?" unanswerable."""
    try:
        role = store.create_agent_role(
            name=request.name, system_prompt=request.system_prompt,
            description=request.description, model=request.model,
            tools=request.tools, max_turns=request.max_turns,
            reserved_names=_vendored_names(), authoring=request.authoring,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _user_role_payload(role)


@router.put("/api/sub-agents/roles/{role_id}")
def update_role(role_id: str, request: RoleUpdate) -> dict:
    try:
        updated = store.update_agent_role(role_id, **request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail="Role not found")
    return _user_role_payload(updated)


@router.delete("/api/sub-agents/roles/{role_id}")
def delete_role(role_id: str) -> dict:
    if not store.delete_agent_role(role_id):
        raise HTTPException(status_code=404, detail="Role not found")
    return {"deleted": True, "id": role_id}


@router.put("/api/sub-agents/profiles/{name}")
def update_profile(name: str, request: OverrideRequest) -> dict:
    """Save a role's override. The body carries the *effective* settings the user edited;
    we store only the fields that DIFFER from the role's default (so untouched vendored
    defaults keep flowing through, and a reset-to-default clears the override)."""
    try:
        prof = lea_profiles.load_profile(name)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Unknown sub-agent role {name!r}")
    default = _defaults(prof)
    incoming = subagent_overrides.sanitize(request.dict())
    override = {k: v for k, v in incoming.items() if v != default.get(k)}
    subagent_overrides.save_override(name, override)
    return _profile_payload(name)
