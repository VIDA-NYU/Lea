"""Sub-agent role resolution seam (v2.5 B2) — the counterpart of `skills_catalog`.

A user role is a DB row; the prover discovers roles as YAML files in the directories
named by `LeaConfig.agent_dirs`. This bridges the two: at run start it writes each row to
a per-run temp `<slug>.yaml` and hands `bridge.py` the directory to set on the config.

Rows in, files out — deliberately the same shape skills already use, and the reason the
prover stays ignorant of the database: it is handed directories, never a connection.

The temp dir lives in the system temp area, never inside a project repo, so materialized
roles cannot pollute the git-owned proof tree. The directory is cached across runs (see
below), so `cleanup` is a no-op the call site keeps for symmetry.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
from pathlib import Path

import yaml

from . import store


# Roles change rarely and every run needs the same files, so the materialized directory
# is CACHED on a hash of the role rows rather than rebuilt per run. Measured: rebuilding
# cost 22 ms of file I/O on every run — 50x the other resolution steps — to write role
# definitions that most runs never use, since a run only needs them if the coordinator
# actually delegates. Same reasoning as the MCP connection pool (A8): the expensive part
# is not per-run state, so it should not have per-run lifetime.
_cache_lock = threading.Lock()
_cached: tuple[str, str] | None = None   # (fingerprint, tempdir)


def _fingerprint(roles: list[dict]) -> str:
    payload = [
        [r["slug"], r["system_prompt"], r.get("description"), r.get("model"),
         r.get("tools"), r.get("max_turns")]
        for r in roles
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def reset_cache() -> None:
    """Drop the cached directory (tests, and any caller that wants a clean rebuild)."""
    global _cached
    with _cache_lock:
        if _cached:
            shutil.rmtree(_cached[1], ignore_errors=True)
        _cached = None


def materialize_roles() -> tuple[str | None, list[str]]:
    """Write every user role to `<tempdir>/<slug>.yaml`.

    Returns `(tempdir, skipped)` — `skipped` names roles that could not be written, so a
    caller can say so rather than let a role silently not exist (the absence-failure this
    whole phase is built to avoid). With no roles, returns `(None, [])` and creates
    nothing, so the common path allocates nothing.
    """
    global _cached
    roles = store.list_agent_roles()
    if not roles:
        return None, []
    fingerprint = _fingerprint(roles)
    with _cache_lock:
        if _cached and _cached[0] == fingerprint and Path(_cached[1]).is_dir():
            return _cached[1], []
    tempdir = tempfile.mkdtemp(prefix="lea-roles-")
    skipped: list[str] = []
    for role in roles:
        # Only the keys `profiles._ALLOWED_KEYS` permits — an unknown key makes the
        # prover refuse the whole file, which would turn a harmless extra column into a
        # role that vanishes.
        payload = {"name": role["slug"], "system_prompt": role["system_prompt"]}
        for key in ("description", "model", "tools", "max_turns"):
            if role.get(key) is not None:
                payload[key] = role[key]
        try:
            (Path(tempdir) / f"{role['slug']}.yaml").write_text(
                yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
            )
        except OSError:
            skipped.append(role["slug"])
    with _cache_lock:
        # Replace any older directory; a run still holding the previous path keeps its
        # own files until it exits, since the caller no longer deletes what it is given.
        if _cached and _cached[1] != tempdir:
            shutil.rmtree(_cached[1], ignore_errors=True)
        _cached = (fingerprint, tempdir)
    return tempdir, skipped


def cleanup(tempdir: str | None) -> None:
    """No-op: the directory is CACHED and shared across runs, so a run must not delete
    what the next one is about to reuse. Kept as the caller's `finally` contract, and so
    the call site still reads symmetrically with `skills_catalog.cleanup`."""
    return None
